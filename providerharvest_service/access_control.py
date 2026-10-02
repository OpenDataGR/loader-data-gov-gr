"""The Security box's whitelist and blacklist.

* **Blacklist ("block")** -- an address or range that is refused outright:
  loader answers every HTTP request from it with 403 before anything else runs
  (``access_middleware.py``), and the FTP box drops its packets at the firewall
  (ftp-forwarder ``access_rules.py``).
* **Whitelist ("allow")** -- addresses that are trusted: never banned, on the FTP
  box (fail2ban's ``ignoreip``) and on loader, and **they win over the
  blacklist**, so one trusted address inside a blocked range stays in.

The box never needs a new way in: it learns the rules from the reply to its
regular heartbeat (``admin_ftp.report_ftp_box_status``) and says which version
it has applied, which the page shows as "applied" or "waiting".

IPv4 only (the box's rules and sender tracking are IPv4), and the guards are on
purpose: nothing narrower than a /8 can be listed (a typo must not blacklist
the internet or whitelist it), and a block may not cover the person making the
request or the FTP box's own address -- an admin locking themselves, or the
relay, out is the one mistake that cannot be undone from the page.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import json
import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from providerharvest_service import db
from providerharvest_service.models import AccessRule, FtpBoxStatus, ProviderSource
from providerharvest_service.sender_allowlist import normalize_cidr

logger = logging.getLogger(__name__)

ALLOW, BLOCK = "allow", "block"
KINDS = (BLOCK, ALLOW)
MIN_PREFIX = 8
MAX_RULES_PER_KIND = 500
NOTE_MAX = 200
CACHE_TTL_S = 5.0

# What "for how long" means in the form; None is forever.
DURATIONS = (
    ("forever", "Forever", None),
    ("1h", "1 hour", datetime.timedelta(hours=1)),
    ("24h", "24 hours", datetime.timedelta(hours=24)),
    ("7d", "7 days", datetime.timedelta(days=7)),
    ("30d", "30 days", datetime.timedelta(days=30)),
)
_DURATION = {key: delta for key, _, delta in DURATIONS}

_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")


class AccessError(ValueError):
    """A rule that cannot be added; the message is shown to the admin."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)  # the database holds naive UTC


def _active(session: Session) -> list[AccessRule]:
    now = _now()
    return list(
        session.scalars(
            select(AccessRule)
            .where(or_(AccessRule.expires_at.is_(None), AccessRule.expires_at > now))
            .order_by(AccessRule.created_at.desc(), AccessRule.id)
        ).all()
    )


# -- the rules as the enforcers see them ---------------------------------------------

@dataclass(frozen=True)
class Snapshot:
    allow: tuple[str, ...] = ()
    block: tuple[str, ...] = ()
    version: str = ""

    def __post_init__(self):
        object.__setattr__(self, "_allow_nets", tuple(ipaddress.ip_network(c) for c in self.allow))
        object.__setattr__(self, "_block_nets", tuple(ipaddress.ip_network(c) for c in self.block))

    def verdict(self, address: str | None) -> str | None:
        """"allow", "block", or None for an address no rule mentions. Allow wins."""
        if not address:
            return None
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None
        if ip.version != 4:
            return None
        if any(ip in net for net in self._allow_nets):
            return ALLOW
        if any(ip in net for net in self._block_nets):
            return BLOCK
        return None


def version_of(allow: list[str] | tuple[str, ...], block: list[str] | tuple[str, ...]) -> str:
    """A short fingerprint of the active rules, so the box can say which it has applied."""
    text = "\n".join(["allow " + c for c in sorted(allow)] + ["block " + c for c in sorted(block)])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def snapshot(session: Session) -> Snapshot:
    rules = _active(session)
    allow = tuple(sorted({r.cidr for r in rules if r.kind == ALLOW}))
    block = tuple(sorted({r.cidr for r in rules if r.kind == BLOCK}))
    return Snapshot(allow=allow, block=block, version=version_of(allow, block))


_cache_lock = threading.Lock()
_cache: tuple[float, Snapshot] | None = None


def cached_snapshot() -> Snapshot:
    """What the per-request check reads. Each worker process keeps the rules for a
    few seconds rather than querying on every request, so a change shows up on
    every worker within ``CACHE_TTL_S`` (at once on the one that made it). A
    database error serves the last good rules, or none: it must never turn into
    refusing -- or admitting -- everybody."""
    global _cache
    with _cache_lock:
        if _cache is not None and time.monotonic() - _cache[0] < CACHE_TTL_S:
            return _cache[1]
        try:
            with db.SessionLocal() as session:
                fresh = snapshot(session)
        except Exception:  # noqa: BLE001 -- see docstring
            logger.exception("could not read the access rules")
            fresh = _cache[1] if _cache is not None else Snapshot()
        _cache = (time.monotonic(), fresh)
        return fresh


def invalidate() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def box_payload(session: Session) -> dict:
    """What the FTP box is told in the heartbeat reply."""
    snap = snapshot(session)
    return {"version": snap.version, "allow": list(snap.allow), "block": list(snap.block)}


# -- changing them ------------------------------------------------------------------

@dataclass(frozen=True)
class AffectedSource:
    """A push source whose allowed senders a block would cut off."""

    name: str
    title: str
    status: str
    entries: tuple[str, ...]  # the source's allowed-sender entries the block overlaps


def affected_sources(session: Session, cidr: str) -> list[AffectedSource]:
    """The push sources a block on ``cidr`` would lock out: those with an allowed
    sender (see sender_allowlist.py) that overlaps it. The blacklist beats a
    source's own allowed list, so blocking a provider's address cuts that
    provider off -- the one thing worth stopping to confirm. An entry that lies
    wholly inside a whitelisted range is left out: the whitelist wins over the
    blacklist, so that address stays in and the block does not affect it -- except
    a whitelist entry for exactly ``cidr`` itself: blocking it means *moving* it
    off the whitelist, so it protects nothing once the block is in."""
    network = ipaddress.ip_network(normalize_cidr(cidr))
    trusted = [ipaddress.ip_network(c) for c in snapshot(session).allow if c != str(network)]
    found: list[AffectedSource] = []
    for source in session.scalars(select(ProviderSource).where(ProviderSource.transport_type == "push")).all():
        try:
            entries = json.loads(source.config or "{}").get("allowed_senders") or []
        except ValueError:
            continue
        hit = []
        for entry in entries:
            try:
                net = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            if net.version == 4 and net.overlaps(network) and not any(net.subnet_of(t) for t in trusted):
                hit.append(str(net))
        if hit:
            found.append(AffectedSource(name=source.name, title=source.title, status=source.status, entries=tuple(sorted(hit))))
    return sorted(found, key=lambda s: s.name)


def rules(session: Session) -> list[AccessRule]:
    """The active (not expired) rules, newest first, for the page."""
    return _active(session)


def _clean_note(note: str | None) -> str:
    return "".join(ch for ch in (note or "") if ch.isprintable())[:NOTE_MAX].strip()


def _box_addresses(session: Session) -> list[str]:
    return [a for a in session.scalars(select(FtpBoxStatus.address)).all() if a]


def add_rule(
    session: Session,
    kind: str,
    text: str,
    *,
    note: str = "",
    created_by: str = "",
    duration: str = "forever",
    requester: str | None = None,
) -> AccessRule:
    """Adds one rule or raises ``AccessError`` with something to show the admin.
    ``requester`` is the address of whoever is asking (the guard against blocking
    yourself)."""
    if kind not in KINDS:
        raise AccessError("that is not a list")
    try:
        cidr = normalize_cidr(text)
    except ValueError as exc:
        raise AccessError(str(exc)) from None
    network = ipaddress.ip_network(cidr)
    if network.prefixlen < MIN_PREFIX:
        raise AccessError("%s is too wide -- ranges narrower than a /%d only (a typo must not cover the internet)" % (cidr, MIN_PREFIX))
    if network.overlaps(_LOOPBACK):
        raise AccessError("%s overlaps the loopback range, which cannot be listed" % cidr)
    if duration not in _DURATION:
        raise AccessError("unknown duration")

    other = ALLOW if kind == BLOCK else BLOCK
    if session.scalar(select(AccessRule).where(AccessRule.kind == other, AccessRule.cidr == cidr)) is not None:
        raise AccessError("%s is already on the %s -- move it instead" % (cidr, "whitelist" if other == ALLOW else "blacklist"))
    existing = session.scalar(select(AccessRule).where(AccessRule.kind == kind, AccessRule.cidr == cidr))
    if existing is not None:
        raise AccessError("%s is already on the %s" % (cidr, "blacklist" if kind == BLOCK else "whitelist"))
    if (session.scalar(select(func.count()).select_from(AccessRule).where(AccessRule.kind == kind)) or 0) >= MAX_RULES_PER_KIND:
        raise AccessError("the %s is full (%d entries)" % ("blacklist" if kind == BLOCK else "whitelist", MAX_RULES_PER_KIND))

    if kind == BLOCK:
        allow_now = snapshot(session)  # a whitelisted address inside the range keeps working, so it cannot be locked out by it
        covered = []
        if requester:
            covered.append(("your own address", requester))
        covered.extend(("the FTP box's address", address) for address in _box_addresses(session))
        for what, address in covered:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if ip.version == 4 and ip in network and allow_now.verdict(address) != ALLOW:
                raise AccessError("%s covers %s (%s) -- that would lock it out. Whitelist it first, or narrow the range." % (cidr, what, address))

    expires = None if _DURATION[duration] is None else _now() + _DURATION[duration]
    rule = AccessRule(kind=kind, cidr=cidr, note=_clean_note(note), created_by=_clean_note(created_by)[:100], expires_at=expires)
    session.add(rule)
    session.commit()
    invalidate()
    return rule


def remove_rule(session: Session, rule_id: str) -> AccessRule | None:
    rule = session.get(AccessRule, rule_id)
    if rule is None:
        return None
    session.delete(rule)
    session.commit()
    invalidate()
    return rule


def remove_exact(session: Session, kind: str, cidr: str) -> AccessRule | None:
    rule = session.scalar(select(AccessRule).where(AccessRule.kind == kind, AccessRule.cidr == cidr))
    if rule is None:
        return None
    session.delete(rule)
    session.commit()
    invalidate()
    return rule


def move(
    session: Session,
    text: str,
    to: str,
    *,
    created_by: str = "",
    duration: str = "forever",
    requester: str | None = None,
) -> AccessRule | None:
    """Put an address on ``to`` ("block" or "allow"), taking it off the other list
    if it is there; ``to="none"`` just takes it off whichever list holds it. If the
    new list refuses it, the old entry is left exactly as it was."""
    try:
        cidr = normalize_cidr(text)
    except ValueError as exc:
        raise AccessError(str(exc)) from None
    if to == "none":
        removed = [remove_exact(session, k, cidr) for k in KINDS]
        if not any(removed):
            raise AccessError("%s is on neither list" % cidr)
        return None
    if to not in KINDS:
        raise AccessError("that is not a list")
    other = ALLOW if to == BLOCK else BLOCK
    previous = session.scalar(select(AccessRule).where(AccessRule.kind == other, AccessRule.cidr == cidr))
    kept = None
    if previous is not None:
        kept = (previous.kind, previous.cidr, previous.note, previous.created_by, previous.expires_at, previous.created_at)
        session.delete(previous)
        session.flush()
    try:
        return add_rule(
            session, to, cidr, note=kept[2] if kept else "", created_by=created_by, duration=duration, requester=requester,
        )
    except AccessError:
        session.rollback()  # the old entry comes back untouched
        raise
