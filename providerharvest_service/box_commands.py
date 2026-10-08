"""Actions an admin can ask an FTP box to run, from the FTP box page.

Loader can never connect to a box, so a request is only a row here. The box's next
heartbeat collects it (the reply's ``commands`` -- an action *name* and an id, never a
command line), runs it from its own fixed list (ftp-forwarder ``box_commands.py``), and
its answer comes back in a later heartbeat (``command_results``). So an action takes
effect within about a minute, and a box that is down simply never answers.

* each request is handed to the box **at most once** ("sent"): a restart must not repeat
  because a reply was lost. One that gets no answer is marked expired, not retried;
* a request nobody collected within 15 minutes is expired too -- it must not fire long
  after anyone remembers asking;
* one action of a kind per box at a time (a second click while one is waiting is refused);
* every request records who asked and when, and what the box answered, and is kept 30 days.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from providerharvest_service.models import FtpBoxCommand, FtpBoxStatus

PENDING_TTL = datetime.timedelta(minutes=15)
SENT_TTL = datetime.timedelta(minutes=10)
KEEP_FOR = datetime.timedelta(days=30)
MAX_OUTPUT = 12000
SHOWN = 5


@dataclasses.dataclass(frozen=True)
class Action:
    key: str
    label: str
    description: str
    confirm: str = ""  # a question to ask first, for what interrupts something; "" for a harmless action


ACTIONS: dict[str, Action] = {a.key: a for a in (
    Action("diagnostics", "Run diagnostics", "A read-only snapshot: uptime, memory, disk, service states and recent log lines."),
    Action("reload-fail2ban", "Reload fail2ban", "Re-reads fail2ban's configuration; bans stay."),
    Action("restart-relay", "Restart the file relay",
           "Restarts the service that forwards received files to loader; a file being forwarded is retried.",
           "Restart the file relay on this box?"),
    Action("restart-proftpd", "Restart ProFTPD",
           "Restarts the FTP/SFTP server; transfers in progress are cut and providers reconnect.",
           "Restart ProFTPD? Uploads in progress will be interrupted."),
)}


class CommandError(ValueError):
    """Carries a message fit to show the admin."""


# Log viewing: not free text. An action is named "logs-<service>-<lines>", from short fixed lists; the
# box maps the word to its own journal unit (it never sees a unit name or a command from here).
FTP_LOG_SERVICES = ("proftpd", "sync", "relay", "fail2ban", "sshd")
LOG_LINES = (50, 100, 200, 500)
LOG_OUTPUT = 40000  # a log view is meant to be read, so it gets more room than the other actions
_LOG_ACTION = re.compile(r"^logs-([a-z0-9]{2,12})-(\d{2,3})$")


def log_action(service: str, lines: object) -> str:
    """The action name for viewing ``service``'s last ``lines`` lines, or ``CommandError`` for a number
    that is not one of the offered ones. (Which services a given box offers is checked by its caller.)"""
    try:
        count = int(str(lines))
    except ValueError:
        raise CommandError("Choose how many lines.") from None
    if count not in LOG_LINES:
        raise CommandError("Choose one of the offered numbers of lines.")
    if not re.fullmatch(r"[a-z0-9]{2,12}", service or ""):
        raise CommandError("Choose a service.")
    return "logs-%s-%d" % (service, count)


def parse_log_action(action: str) -> tuple[str, int] | None:
    """(service, lines) for a well-formed log action name, else None."""
    match = _LOG_ACTION.match(action or "")
    if not match or int(match.group(2)) not in LOG_LINES:
        return None
    return match.group(1), int(match.group(2))


def label(action: str) -> str:
    """What the history calls an action."""
    if action in ACTIONS:
        return ACTIONS[action].label
    parsed = parse_log_action(action)
    return "Show logs: %s, %d lines" % parsed if parsed else action


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _expire(session: Session, hostname: str, now: datetime.datetime) -> None:
    for row in session.scalars(select(FtpBoxCommand).where(
        FtpBoxCommand.hostname == hostname, FtpBoxCommand.state.in_(("pending", "sent")),
    )).all():
        if row.state == "pending" and row.requested_at < now - PENDING_TTL:
            row.state, row.finished_at = "expired", now
            row.output = "The box did not collect this request in time (is it down?)."
        elif row.state == "sent" and (row.sent_at or row.requested_at) < now - SENT_TTL:
            row.state, row.finished_at = "expired", now
            row.output = "The box took the request but never reported back."
    session.flush()  # (sessions here do not autoflush: what follows must see the new states)


def request(session: Session, hostname: str, action: str, who: str) -> FtpBoxCommand:
    """Queue ``action`` for ``hostname``. Raises ``CommandError`` for an unknown box or
    action, or when the same action is already waiting."""
    parsed = parse_log_action(action)
    if action not in ACTIONS and not (parsed and parsed[0] in FTP_LOG_SERVICES):
        raise CommandError("Unknown action.")
    if session.get(FtpBoxStatus, hostname) is None:
        raise CommandError("No such box.")
    now = _now()
    _expire(session, hostname, now)
    waiting = session.scalar(select(FtpBoxCommand).where(
        FtpBoxCommand.hostname == hostname, FtpBoxCommand.action == action, FtpBoxCommand.state.in_(("pending", "sent")),
    ))
    if waiting is not None:
        session.commit()  # (keep any expiry just recorded)
        raise CommandError("%s is already waiting for this box." % label(action))
    row = FtpBoxCommand(hostname=hostname, action=action, requested_by=(who or "")[:100], requested_at=now)
    session.add(row)
    session.execute(delete(FtpBoxCommand).where(FtpBoxCommand.requested_at < now - KEEP_FOR))
    session.commit()
    return row


def collect_for(session: Session, hostname: str) -> list[dict]:
    """What the box should run now, as the reply to its heartbeat -- and mark it handed over."""
    now = _now()
    _expire(session, hostname, now)
    out = []
    for row in session.scalars(select(FtpBoxCommand).where(
        FtpBoxCommand.hostname == hostname, FtpBoxCommand.state == "pending",
    ).order_by(FtpBoxCommand.requested_at)).all():
        row.state, row.sent_at = "sent", now
        out.append({"id": row.id, "action": row.action})
    return out


def record_results(session: Session, hostname: str, results: Iterable[object]) -> None:
    """The box's answers. Only for a request that really was handed to *this* box and has
    no answer yet -- anything else (unknown id, another box's, already answered) is ignored."""
    now = _now()
    for item in results:
        command_id = getattr(item, "id", None)
        row = session.get(FtpBoxCommand, command_id) if isinstance(command_id, str) else None
        if row is None or row.hostname != hostname or row.state != "sent":
            continue
        row.state = "ok" if getattr(item, "ok", False) else "failed"
        row.finished_at = now
        text = str(getattr(item, "output", ""))[:LOG_OUTPUT if row.action.startswith("logs-") else MAX_OUTPUT]
        row.output = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())


def recent(session: Session, hostname: str) -> list[FtpBoxCommand]:
    _expire(session, hostname, _now())
    return list(session.scalars(
        select(FtpBoxCommand).where(FtpBoxCommand.hostname == hostname)
        .order_by(FtpBoxCommand.requested_at.desc()).limit(SHOWN)
    ).all())
