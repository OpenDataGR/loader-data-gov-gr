"""Automatic bans for loader's own HTTP side: an address that keeps failing to
authenticate is refused for a while, like fail2ban does on the FTP box.

* **10 failures in 10 minutes** -> banned for an hour;
* **30 failures in 24 hours** -> banned for a day (the slow scanner that stays
  under the first rule, as on the FTP side).

A ban is simply a temporary entry on the blacklist (``AccessRule``, kind
"block", created by ``auto-ban``), so it is enforced by the same middleware,
shows in the Security box -- under "Banned now", where an admin can lift it,
blacklist it for good or whitelist it -- and reaches the FTP box's firewall too.

What it deliberately does *not* ban, because getting it wrong locks the wrong
people out:

* a **whitelisted** address (that is what the whitelist is for), or one already
  blocked;
* the **FTP box's own address** (a mis-set sync key must stay recoverable);
* any address that is not **public** (internal, loopback, private ranges):
  those are our own tools and health checks, not scanners;
* failures of a **push API key**: a provider's machine retrying with a stale key
  after a rotation would otherwise be banned -- and stay banned after fixing it.
  Counted instead: web logins and the admin, sync and bearer credentials.

``LOADER_HTTP_AUTOBAN=off`` turns it off (the switch for an emergency; an admin
locked out can also run the "Clear automatic bans" workflow).
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from providerharvest_service import access_control
from providerharvest_service.models import AccessRule, FtpLoginAttempt

logger = logging.getLogger(__name__)

CREATED_BY = access_control.AUTO_BAN_BY

FAST_WINDOW = datetime.timedelta(minutes=10)
FAST_THRESHOLD = 10
FAST_BAN = datetime.timedelta(hours=1)
SLOW_WINDOW = datetime.timedelta(hours=24)
SLOW_THRESHOLD = 30
SLOW_BAN = datetime.timedelta(hours=24)


def enabled() -> bool:
    return os.environ.get("LOADER_HTTP_AUTOBAN", "on").strip().lower() not in ("off", "0", "false", "no")


def _failures(session: Session, address: str, channels, since: datetime.datetime) -> int:
    return session.scalar(
        select(func.count()).select_from(FtpLoginAttempt).where(
            FtpLoginAttempt.hostname == "loader",
            FtpLoginAttempt.ip == address,
            FtpLoginAttempt.code == 401,
            FtpLoginAttempt.channel.in_(list(channels)),
            FtpLoginAttempt.at >= since,
        )
    ) or 0


def consider(session: Session, address: str, channels) -> AccessRule | None:
    """Ban ``address`` if its recent failed authentications (in ``channels``) warrant
    it. Returns the new rule, or None. Never raises into the request that triggered it
    beyond what the caller already guards."""
    if not enabled():
        return None
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    if not ip.is_global or (ip.version == 6 and ip.ipv4_mapped is not None):
        return None
    if access_control.snapshot(session).verdict(address) is not None:
        return None  # whitelisted (never banned), or already blocked
    if address in access_control.box_addresses(session):
        return None

    now = access_control._now()
    slow = _failures(session, address, channels, now - SLOW_WINDOW)
    fast = _failures(session, address, channels, now - FAST_WINDOW)
    if slow >= SLOW_THRESHOLD:
        duration, why = SLOW_BAN, "%d failed logins or keys in 24 hours" % slow
    elif fast >= FAST_THRESHOLD:
        duration, why = FAST_BAN, "%d failed logins or keys in 10 minutes" % fast
    else:
        return None

    # One IPv6 customer holds a whole /64, so banning a single address of it would change nothing.
    cidr = "%s/32" % address if ip.version == 4 else str(ipaddress.ip_network("%s/64" % address, strict=False))
    rule = AccessRule(
        kind=access_control.BLOCK, cidr=cidr, note="auto-ban: " + why,
        created_by=CREATED_BY, expires_at=now + duration,
    )
    session.add(rule)
    access_control._audit(session, CREATED_BY, "banned", access_control.BLOCK, rule.cidr, why)
    try:
        session.commit()
    except IntegrityError:  # another worker banned it a moment ago
        session.rollback()
        return None
    access_control.invalidate()
    logger.warning("banned %s for %s: %s", address, duration, why)
    return rule
