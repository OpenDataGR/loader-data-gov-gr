"""Is a push source ready to receive? -- the "Test connection" of a source the provider sends to us.

A push source has nothing the loader can connect to (the FTP box only calls out to the loader), so this is a *readiness check*: it reads
what the loader already knows -- the source, its login account, and what the FTP box last reported about itself -- and lists, as plain ticks
and crosses, everything that has to be true for a provider's upload to arrive. It cannot prove that a login from the outside works; only
a file arriving proves the whole path, and the last item says whether one ever has.

Each check is ``ok`` (true), ``warn`` (works but worth a look, or not proven yet) or ``fail`` (an upload would not arrive).
"""

from __future__ import annotations

import dataclasses
import datetime
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from providerharvest_service.models import FtpAccount, FtpBoxStatus, ProviderSource

STALE_AFTER = datetime.timedelta(minutes=5)  # a box that has not reported in this long is treated as down (same limit the Data boxes page uses)


@dataclasses.dataclass(frozen=True)
class Check:
    level: str  # "ok" | "warn" | "fail"
    label: str
    detail: str = ""


def _utc(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def _ago(delta: datetime.timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return "%d seconds ago" % seconds
    if seconds < 90 * 60:
        return "%d minutes ago" % round(seconds / 60)
    if seconds < 36 * 3600:
        return "%d hours ago" % round(seconds / 3600)
    return "%d days ago" % round(seconds / 86400)


def _last_file(source: ProviderSource, now: datetime.datetime) -> Check:
    if source.last_received_at is None:
        return Check("warn", "No file has arrived yet", "This is the only proof that the whole path works: ask the provider to send a file.")
    when = _utc(source.last_received_at)
    sender = (" from %s" % source.last_sender) if source.last_sender else ""
    return Check("ok", "A file arrived %s" % _ago(now - when), "at %s UTC%s." % (when.strftime("%Y-%m-%d %H:%M"), sender))


def _allowed_senders(source: ProviderSource) -> Check:
    try:
        senders = json.loads(source.config or "{}").get("allowed_senders") or []
    except ValueError:
        senders = []
    if not senders:
        return Check("ok", "Accepts uploads from any address", "No sender list is set. A list can limit it to the provider's own addresses.")
    return Check("ok", "Accepts uploads only from %d address%s or range%s" % (len(senders), "" if len(senders) == 1 else "es", "" if len(senders) == 1 else "s"),
                 "If the provider's address changes, uploads are refused until the list is updated.")


def _box_checks(session: Session, account_count_expected: int, now: datetime.datetime) -> list[Check]:
    boxes = list(session.scalars(select(FtpBoxStatus).order_by(FtpBoxStatus.reported_at.desc())).all())
    if not boxes:
        return [Check("fail", "No FTP box has reported yet", "The FTP box has never contacted the loader, so uploads to it cannot be forwarded here.")]
    box = boxes[0]
    age = now - _utc(box.reported_at)
    checks: list[Check] = []
    if age > STALE_AFTER:
        checks.append(Check("fail", "The FTP box has gone quiet", "Last heard from %s. If it is down, uploads fail and nothing is forwarded." % _ago(age)))
        return checks
    checks.append(Check("ok", "The FTP box is reporting", "Last heard from %s." % _ago(age)))
    if not box.proftpd_active:
        checks.append(Check("fail", "The FTP server (ProFTPD) is not running", "Nobody can log in to upload."))
    elif box.ftp_port_listening and box.sftp_port_listening:
        checks.append(Check("ok", "The FTP server is running, on both the FTP (21) and SFTP (2222) ports"))
    elif box.ftp_port_listening or box.sftp_port_listening:
        missing = "SFTP (2222)" if box.ftp_port_listening else "FTP (21)"
        checks.append(Check("warn", "The FTP server is not listening on %s" % missing, "Uploads over that protocol fail; the other one works."))
    else:
        checks.append(Check("fail", "The FTP server is running but listening on neither port", "Nobody can connect."))
    checks.append(Check("ok", "The relay is running", "It forwards each uploaded file to the loader.") if box.relay_service_active
                  else Check("fail", "The relay service is not running", "Uploaded files would stay on the box and not reach the loader."))
    if not box.sync_ok:
        checks.append(Check("fail", "The box could not fetch the account list", box.sync_message or "Its last sync with the loader failed."))
    elif box.account_count < account_count_expected:
        checks.append(Check("warn", "The box does not have all the accounts yet", "It holds %d, the loader has %d enabled. It fetches the list regularly; check again in a minute."
                                                                                  % (box.account_count, account_count_expected)))
    else:
        checks.append(Check("ok", "The box has the account list", "%d account%s, all the loader's enabled ones." % (box.account_count, "" if box.account_count == 1 else "s")))
    return checks


def check(session: Session, source: ProviderSource, now: datetime.datetime | None = None) -> list[Check]:
    """Every check for a push source, in the order a person would follow the upload. Raises ``ValueError`` for a source that is not push."""
    if source.transport_type != "push":
        raise ValueError("Only a push source has a readiness check.")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    checks: list[Check] = []
    checks.append(Check("ok", "The source is active") if source.status == "active" else
                  Check("fail", "The source is %s" % source.status, "A source accepts files only while it is active (approved and not paused)."))

    if source.push_method == "ftp":
        accounts = list(session.scalars(select(FtpAccount).where(FtpAccount.managed_source_id == source.id)).all())
        if not accounts:
            checks.append(Check("fail", "There is no login account for this source", "The provider has nothing to log in with."))
        else:
            account = accounts[0]
            if account.enabled:
                checks.append(Check("ok", "The login account is enabled", "Login: %s." % account.username))
            else:
                checks.append(Check("fail", "The login account is disabled", "It is switched on when the source is approved; it is off while the source is pending, paused or rejected."))
            checks.append(Check("ok", "Uploads are forwarded to this source") if account.push_target_source_name == source.name else
                          Check("fail", "The account does not forward to this source", "Files would arrive on the box but not be stored here."))
        enabled_total = session.scalar(select(func.count()).select_from(FtpAccount).where(FtpAccount.enabled.is_(True))) or 0
        checks.extend(_box_checks(session, enabled_total, now))
    else:
        has_key = bool((json.loads(source.config or "{}") or {}).get("push_api_key_hash"))
        checks.append(Check("ok", "An API key is set", "The provider sends it as X-Api-Key.") if has_key else
                      Check("fail", "No API key is set", "Create one with 'Rotate API key'."))
    checks.append(_allowed_senders(source))
    checks.append(_last_file(source, now))
    return checks


def summary(checks: list[Check]) -> tuple[str, str]:
    """(level, one line): the worst problem if there is one, otherwise whether it is working or waiting for its first file."""
    failed = [c for c in checks if c.level == "fail"]
    if failed:
        return "fail", "Not ready: %s." % failed[0].label
    waiting = [c for c in checks if c.label.startswith("No file has arrived")]
    warned = [c for c in checks if c.level == "warn" and c not in waiting]
    if warned:
        return "warn", "Ready, with a warning: %s." % warned[0].label
    if waiting:
        return "ok", "Ready -- waiting for the first file."
    return "ok", "Ready and working."
