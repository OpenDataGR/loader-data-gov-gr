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

IPv4 and IPv6 both (IPv6 is enforced on the box with ip6tables and fail2ban's
``ignoreip``). The per-source and per-account allowed-sender lists take IPv6 too:
they become ProFTPD ``Allow`` rules, which take IPv6 (proven; see ip_limits.py).

The guards are on purpose: nothing wider than a /8 (IPv4) or a /32 (IPv6) can be
listed (a typo must not blacklist the internet or whitelist it), and a block may
not cover the person making the request or the FTP box's own address -- an
admin locking themselves, or the relay, out is the one mistake that cannot be
undone from the page. An IPv4-mapped IPv6 address (``::ffff:1.2.3.4``) is the
IPv4 address it wraps, so a rule cannot be dodged by writing it that way.
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

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from providerharvest_service import db
from providerharvest_service.models import AccessLog, AccessRule, FtpAccount, FtpBoxStatus, ProviderSource

logger = logging.getLogger(__name__)

ALLOW, BLOCK = "allow", "block"
AUTO_BAN_BY = "auto-ban"  # ``created_by`` of a block that auto_ban.py made (a temporary, automatic ban)
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

MIN_PREFIX_V6 = 32

# Never listable: loopback, the unspecified address, and the IPv4-mapped range (those are IPv4 addresses
# in disguise -- list the IPv4 address instead).
_UNLISTABLE = tuple(ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "::/128", "::ffff:0:0/96"))


class AccessError(ValueError):
    """A rule that cannot be added; the message is shown to the admin."""


def normalize_network(text: str) -> str:
    """An IPv4 or IPv6 address or range in canonical form ("203.0.113.9" -> "203.0.113.9/32",
    "2001:DB8::1/64" -> "2001:db8::/64"). Raises ValueError with a message fit to show an admin."""
    try:
        return str(ipaddress.ip_network((text or "").strip(), strict=False))
    except ValueError as exc:
        raise ValueError("not a valid IP address or CIDR range: %r" % text) from exc


def host_form(cidr: str) -> str:
    """A rule's address as people write it: a single host without its "/32" (IPv4) or "/128" (IPv6), a
    range unchanged. Not a string strip -- "2001:db8::/32" is a real IPv6 network and must stay."""
    try:
        network = ipaddress.ip_network(cidr)
    except ValueError:
        return cidr
    return str(network.network_address) if network.prefixlen == network.max_prefixlen else cidr


def _as_ip(address: str | None):
    """The address as an ``ip_address``, with an IPv4-mapped IPv6 address reduced to the IPv4 one it
    wraps (so ``::ffff:1.2.3.4`` is judged as 1.2.3.4). None if it is not an address."""
    if not address:
        return None
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


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
        ip = _as_ip(address)
        if ip is None:
            return None
        # (an address is never "in" a network of the other IP version, so one list serves both)
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
    network, trusted = _block_context(session, cidr)
    found: list[AffectedSource] = []
    for source in session.scalars(select(ProviderSource).where(ProviderSource.transport_type == "push")).all():
        try:
            entries = json.loads(source.config or "{}").get("allowed_senders") or []
        except ValueError:
            continue
        hit = _overlapping(entries, network, trusted)
        if hit:
            found.append(AffectedSource(name=source.name, title=source.title, status=source.status, entries=hit))
    return sorted(found, key=lambda s: s.name)


@dataclass(frozen=True)
class AffectedAccount:
    """An FTP account an admin made by hand, whose own allowed-address list a block would cut off."""

    username: str
    enabled: bool
    entries: tuple[str, ...]


def affected_accounts(session: Session, cidr: str) -> list[AffectedAccount]:
    """The same question as ``affected_sources`` for the FTP accounts that have no source
    behind them (an admin made them, and set their "Allowed from" list by hand). A
    provider's own account is not listed: its list mirrors its source's allowed
    senders, which ``affected_sources`` already reports."""
    network, trusted = _block_context(session, cidr)
    found: list[AffectedAccount] = []
    for account in session.scalars(select(FtpAccount).where(FtpAccount.managed_source_id.is_(None))).all():
        hit = _overlapping([a.cidr for a in account.allowed_ips], network, trusted)
        if hit:
            found.append(AffectedAccount(username=account.username, enabled=account.enabled, entries=hit))
    return sorted(found, key=lambda a: a.username)


def _block_context(session: Session, cidr: str):
    network = ipaddress.ip_network(normalize_network(cidr))
    trusted = [ipaddress.ip_network(c) for c in snapshot(session).allow if c != str(network)]
    return network, trusted


def _overlapping(entries, network, trusted) -> tuple[str, ...]:
    """The entries a block on ``network`` covers, leaving out any that lie wholly inside a
    whitelisted range (those stay in)."""
    hit = []
    for entry in entries:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        # (the lists these entries come from are IPv4 only, so a v6 block never overlaps one)
        if net.version == network.version and net.overlaps(network) and not any(
            t.version == net.version and net.subnet_of(t) for t in trusted
        ):
            hit.append(str(net))
    return tuple(sorted(hit))


def rules(session: Session) -> list[AccessRule]:
    """The active (not expired) rules, newest first, for the page."""
    return _active(session)


def _clean_note(note: str | None) -> str:
    return "".join(ch for ch in (note or "") if ch.isprintable())[:NOTE_MAX].strip()


def box_addresses(session: Session) -> list[str]:
    """The public addresses the FTP boxes report from: never to be blocked or auto-banned."""
    return [a for a in session.scalars(select(FtpBoxStatus.address)).all() if a]


_box_addresses = box_addresses  # (older name)


def add_rule(
    session: Session,
    kind: str,
    text: str,
    *,
    note: str = "",
    created_by: str = "",
    duration: str = "forever",
    requester: str | None = None,
    audit: bool = True,
) -> AccessRule:
    """Adds one rule or raises ``AccessError`` with something to show the admin.
    ``requester`` is the address of whoever is asking (the guard against blocking
    yourself)."""
    if kind not in KINDS:
        raise AccessError("that is not a list")
    try:
        cidr = normalize_network(text)
    except ValueError as exc:
        raise AccessError(str(exc)) from None
    network = ipaddress.ip_network(cidr)
    limit = MIN_PREFIX if network.version == 4 else MIN_PREFIX_V6
    if network.prefixlen < limit:
        raise AccessError("%s is too wide -- ranges narrower than a /%d only (a typo must not cover the internet)" % (cidr, limit))
    if any(network.version == bad.version and network.overlaps(bad) for bad in _UNLISTABLE):
        raise AccessError("%s overlaps a range that cannot be listed (loopback, the unspecified address, or IPv4-mapped IPv6 -- list the IPv4 address instead)" % cidr)
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
            ip = _as_ip(address)
            if ip is None:
                continue
            if ip in network and allow_now.verdict(address) != ALLOW:
                raise AccessError("%s covers %s (%s) -- that would lock it out. Whitelist it first, or narrow the range." % (cidr, what, address))

    expires = None if _DURATION[duration] is None else _now() + _DURATION[duration]
    rule = AccessRule(kind=kind, cidr=cidr, note=_clean_note(note), created_by=_clean_note(created_by)[:100], expires_at=expires)
    session.add(rule)
    if audit:
        _audit(session, created_by, "added", kind, cidr, _describe(rule))
    session.commit()
    invalidate()
    return rule


# -- the history ---------------------------------------------------------------------

AUDIT_RETENTION = datetime.timedelta(days=365)


def _describe(rule: AccessRule) -> str:
    """A rule in a few words for the history: its note and when it ends."""
    parts = []
    if rule.note:
        parts.append(rule.note)
    parts.append("until %s UTC" % rule.expires_at.strftime("%Y-%m-%d %H:%M") if rule.expires_at else "no expiry")
    return " -- ".join(parts)


def _audit(session: Session, who: str, action: str, kind: str, cidr: str, detail: str = "") -> None:
    """Record a change, in the same transaction as the change itself (so neither exists without the other)."""
    session.add(AccessLog(who=_clean_note(who)[:100], action=action, kind=kind, cidr=cidr, detail=_clean_note(detail)))
    session.execute(delete(AccessLog).where(AccessLog.at < _now() - AUDIT_RETENTION))


def history(session: Session, limit: int = 15) -> list[AccessLog]:
    """The most recent changes, newest first."""
    return list(session.scalars(select(AccessLog).order_by(AccessLog.at.desc(), AccessLog.id).limit(limit)).all())


def update_rule(session: Session, rule_id: str, *, note: str, duration: str = "keep", who: str = "") -> AccessRule | None:
    """Change a rule's note and/or when it ends, keeping the entry (its address and list never
    change here -- that is a move). ``duration`` is "keep" (leave the expiry alone), "forever",
    or one of the preset lengths counted from now. Returns None for a rule that does not exist
    or has already expired (it is no longer on the page). Raises ``AccessError`` for an unknown
    duration."""
    if duration != "keep" and duration not in _DURATION:
        raise AccessError("unknown duration")
    rule = session.get(AccessRule, rule_id)
    if rule is None or (rule.expires_at is not None and rule.expires_at <= _now()):
        return None
    before = _describe(rule)
    rule.note = _clean_note(note)
    if duration != "keep":
        rule.expires_at = None if _DURATION[duration] is None else _now() + _DURATION[duration]
    _audit(session, who, "changed", rule.kind, rule.cidr, "now: %s (was: %s)" % (_describe(rule), before))
    session.commit()
    invalidate()
    return rule


def remove_rule(session: Session, rule_id: str, *, who: str = "") -> AccessRule | None:
    rule = session.get(AccessRule, rule_id)
    if rule is None:
        return None
    _audit(session, who, "lifted" if rule.created_by == AUTO_BAN_BY else "removed", rule.kind, rule.cidr, _describe(rule))
    session.delete(rule)
    session.commit()
    invalidate()
    return rule


def remove_exact(session: Session, kind: str, cidr: str, *, who: str = "", audit: bool = True) -> AccessRule | None:
    rule = session.scalar(select(AccessRule).where(AccessRule.kind == kind, AccessRule.cidr == cidr))
    if rule is None:
        return None
    if audit:
        _audit(session, who, "removed", kind, cidr, _describe(rule))
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
        cidr = normalize_network(text)
    except ValueError as exc:
        raise AccessError(str(exc)) from None
    if to == "none":
        removed = [remove_exact(session, k, cidr, who=created_by) for k in KINDS]
        if not any(removed):
            raise AccessError("%s is on neither list" % cidr)
        return None
    if to not in KINDS:
        raise AccessError("that is not a list")
    if to == BLOCK:
        existing = session.scalar(select(AccessRule).where(AccessRule.kind == BLOCK, AccessRule.cidr == cidr))
        if existing is not None and existing.created_by == AUTO_BAN_BY:
            # An automatic (temporary) ban being made a real blacklist entry: keep the row, change who owns it
            # and, per the form, when it ends -- not "already on the blacklist".
            if duration not in _DURATION:
                raise AccessError("unknown duration")
            existing.created_by = _clean_note(created_by)[:100]
            existing.expires_at = None if _DURATION[duration] is None else _now() + _DURATION[duration]
            existing.note = ("was an automatic ban: " + existing.note.removeprefix("auto-ban: "))[:NOTE_MAX]
            _audit(session, created_by, "converted", BLOCK, cidr, "automatic ban made a blacklist entry -- " + _describe(existing))
            session.commit()
            invalidate()
            return existing
    other = ALLOW if to == BLOCK else BLOCK
    previous = session.scalar(select(AccessRule).where(AccessRule.kind == other, AccessRule.cidr == cidr))
    kept = None
    if previous is not None:
        kept = (previous.kind, previous.cidr, previous.note, previous.created_by, previous.expires_at, previous.created_at)
        session.delete(previous)
        session.flush()
    try:
        rule = add_rule(
            session, to, cidr, note=kept[2] if kept else "", created_by=created_by, duration=duration,
            requester=requester, audit=False,  # one "moved" entry below, not an "added" as well
        )
    except AccessError:
        session.rollback()  # the old entry comes back untouched
        raise
    _audit(
        session, created_by, "moved" if kept else "added", to, cidr,
        ("from the %s -- " % ("blacklist" if other == BLOCK else "whitelist") if kept else "") + _describe(rule),
    )
    session.commit()
    return rule
