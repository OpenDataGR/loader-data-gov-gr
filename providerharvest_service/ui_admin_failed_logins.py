"""The report of refused logins on every way in -- FTP and SFTP (shipped in
batches by the FTP box, see ``admin_ftp.report_login_attempts``) and loader's
own HTTP side (the web login and the API/sync keys, recorded by
``failed_auth``): who has been trying the door, how often, when, by which
method and as which username. Two views of the same data -- one row per source
address with its number of attempts (the quick read: who, and how much),
and every individual attempt -- both filterable and both exportable as
CSV.

Everything in the table came off the wire from strangers, the username
above all, so it is shown escaped (Jinja's default) and the CSV export
defuses spreadsheet formulas: a "username" of ``=HYPERLINK(...)`` must
not become something that runs when an admin opens the file in Excel.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime
import io
import ipaddress
import math
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session

from providerharvest_service import build_info, failed_auth
from providerharvest_service.db import get_session
from providerharvest_service.models import FtpBoxStatus, FtpLoginAttempt, FtpUnbanRequest

router = APIRouter(prefix="/admin/security", tags=["security-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now

PAGE_SIZE = 100
ADDRESS_ROWS = 25
CSV_ROW_CAP = 50_000

# (key in the URL, label, hours back -- None = everything that is kept)
WINDOWS = (
    ("1", "Last hour", 1),
    ("24", "Last 24 hours", 24),
    ("168", "Last 7 days", 168),
    ("720", "Last 30 days", 720),
    ("all", "Everything kept", None),
)
_WINDOW_HOURS = {key: hours for key, _, hours in WINDOWS}
PORT_NAMES = {21: "FTP", 2222: "SFTP"}

# How a row came in. Rows that predate the column have no channel and are
# FTP/SFTP, told apart by the port they hit.
METHOD = func.coalesce(
    FtpLoginAttempt.channel,
    case((FtpLoginAttempt.port == 21, "FTP"), (FtpLoginAttempt.port == 2222, "SFTP"), else_="Other"),
)
_KNOWN_METHODS = ("FTP", "SFTP", failed_auth.WEB_LOGIN, failed_auth.PUSH_KEY, failed_auth.ADMIN_KEY,
                  failed_auth.BOX_KEY, failed_auth.BEARER, failed_auth.OTHER_HTTP)


def _method_of(attempt: FtpLoginAttempt) -> str:
    return attempt.channel or PORT_NAMES.get(attempt.port, "Other")


templates.env.filters["method"] = _method_of
templates.env.filters["utc"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


@dataclasses.dataclass(frozen=True)
class Filters:
    window: str = "24"
    address: str = ""
    method: str = ""
    username: str = ""

    def since(self) -> datetime.datetime | None:
        hours = _WINDOW_HOURS[self.window]
        if hours is None:
            return None
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        return now - datetime.timedelta(hours=hours)

    def query_string(self, **extra) -> str:
        params = {"window": self.window, "address": self.address, "username": self.username}
        if self.method:
            params["method"] = self.method
        params.update(extra)
        return urlencode({k: v for k, v in params.items() if v not in ("", None)})


def _read_filters(request: Request) -> Filters:
    query = request.query_params
    window = query.get("window", "24")
    method = (query.get("method") or "").strip()
    return Filters(
        window=window if window in _WINDOW_HOURS else "24",
        address=(query.get("address") or "").strip()[:45],
        method=method if method in _KNOWN_METHODS else "",
        username=(query.get("username") or "").strip()[:100],
    )


def _conditions(f: Filters) -> list:
    conditions = []
    since = f.since()
    if since is not None:
        conditions.append(FtpLoginAttempt.at >= since)
    if f.address:
        conditions.append(FtpLoginAttempt.ip.startswith(f.address, autoescape=True))
    if f.method:
        conditions.append(METHOD == f.method)
    if f.username:
        conditions.append(FtpLoginAttempt.username.contains(f.username, autoescape=True))
    return conditions


def _by_address(session: Session, conditions: list, limit: int | None):
    query = (
        select(
            FtpLoginAttempt.ip,
            func.count().label("attempts"),
            func.min(FtpLoginAttempt.at).label("first_at"),
            func.max(FtpLoginAttempt.at).label("last_at"),
            func.count(distinct(FtpLoginAttempt.username)).label("usernames"),
            func.group_concat(distinct(METHOD)).label("methods"),
        )
        .where(*conditions)
        .group_by(FtpLoginAttempt.ip)
        .order_by(func.count().desc(), FtpLoginAttempt.ip)
    )
    if limit:
        query = query.limit(limit)
    return session.execute(query).all()


def _banned_now(session: Session) -> set[str]:
    banned: set[str] = set()
    for box in session.scalars(select(FtpBoxStatus)).all():
        banned.update(box.banned)
    return banned


def _boxes_with_pending_unbans(session: Session) -> list[FtpBoxStatus]:
    boxes = list(session.scalars(select(FtpBoxStatus).order_by(FtpBoxStatus.hostname)).all())
    waiting = {(r.hostname, r.ip) for r in session.scalars(select(FtpUnbanRequest)).all()}
    for box in boxes:
        box.pending_unban = {ip for host, ip in waiting if host == box.hostname}  # type: ignore[attr-defined]
    return boxes


def _back(flash: str, kind: str = "success") -> RedirectResponse:
    return RedirectResponse("/admin/security/failed-logins?%s" % urlencode({"flash": flash, "flash_kind": kind}), status_code=303)


@router.post("/boxes/{hostname}/unban")
async def unban_address(request: Request, hostname: str, session: Session = Depends(get_session)):
    """Ask a box to let an address back in. Loader can't reach the box, so
    this only records the request; the box collects it from the reply to
    its next heartbeat (about a minute) and lifts the ban itself. Only an
    address currently on that box's reported ban list is accepted -- this
    is a way to release what fail2ban banned, not to send the box
    arbitrary addresses."""
    box = session.get(FtpBoxStatus, hostname)
    if box is None:
        return _back("No such box.", "error")
    form = await request.form()
    try:
        address = str(ipaddress.ip_address((form.get("ip") or "").strip()))
    except ValueError:
        return _back("That is not an IP address.", "error")
    if address not in box.banned:
        return _back("%s is not on the ban list (it may already have been released)." % address, "error")
    already = session.scalar(
        select(FtpUnbanRequest).where(FtpUnbanRequest.hostname == hostname, FtpUnbanRequest.ip == address)
    )
    if already is None:
        session.add(FtpUnbanRequest(hostname=hostname, ip=address))
        session.commit()
    return _back("Unban requested for %s -- the box applies it within about a minute." % address)


@router.get("/failed-logins", response_class=HTMLResponse)
def attempts_report(request: Request, page: int = 1, session: Session = Depends(get_session)) -> HTMLResponse:
    f = _read_filters(request)
    conditions = _conditions(f)

    total = session.scalar(select(func.count()).select_from(FtpLoginAttempt).where(*conditions)) or 0
    addresses = session.scalar(
        select(func.count(distinct(FtpLoginAttempt.ip))).where(*conditions)
    ) or 0
    by_method = session.execute(
        select(METHOD.label("method"), func.count().label("n"))
        .where(*conditions)
        .group_by(METHOD)
        .order_by(func.count().desc(), METHOD)
    ).all()
    top_usernames = session.execute(
        select(FtpLoginAttempt.username, func.count().label("n"))
        .where(*conditions)
        .group_by(FtpLoginAttempt.username)
        .order_by(func.count().desc(), FtpLoginAttempt.username)
        .limit(10)
    ).all()

    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 1), pages)
    events = session.scalars(
        select(FtpLoginAttempt)
        .where(*conditions)
        .order_by(FtpLoginAttempt.at.desc(), FtpLoginAttempt.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()

    return templates.TemplateResponse(
        request,
        "admin/failed_logins.html",
        {
            "f": f,
            "windows": WINDOWS,
            "total": total,
            "addresses": addresses,
            "by_method": by_method,
            "methods": _KNOWN_METHODS,
            "by_address": _by_address(session, conditions, ADDRESS_ROWS),
            "top_usernames": top_usernames,
            "events": events,
            "banned": _banned_now(session),
            "boxes": _boxes_with_pending_unbans(session),
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
            "export_query": f.query_string(),
            "page_query": f.query_string(),
            "retention_note": _retention_note(session),
            "oldest": session.scalar(select(func.min(FtpLoginAttempt.at))),
        },
    )


def _retention_note(session: Session) -> str:
    from providerharvest_service.admin_ftp import ATTEMPT_RETENTION

    return "%d days" % ATTEMPT_RETENTION.days


def _csv_cell(value):
    """Defuse spreadsheet formulas: a cell starting with = + - @ (or a tab
    or carriage return) is run as a formula by Excel and friends, and the
    username column is attacker-controlled. A leading apostrophe makes it
    plain text; harmless to anything that is not a spreadsheet."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _csv_response(header: list[str], rows, filename: str) -> Response:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_cell(cell) for cell in row])
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="%s"' % filename},
    )


@router.get("/failed-logins.csv")
def attempts_csv(request: Request, session: Session = Depends(get_session)) -> Response:
    f = _read_filters(request)
    attempts = session.scalars(
        select(FtpLoginAttempt)
        .where(*_conditions(f))
        .order_by(FtpLoginAttempt.at.desc(), FtpLoginAttempt.id.desc())
        .limit(CSV_ROW_CAP)
    ).all()
    return _csv_response(
        ["time_utc", "box", "source_address", "port", "method", "username_tried", "refusal_code"],
        ([a.at.strftime("%Y-%m-%d %H:%M:%S"), a.hostname, a.ip, a.port, _method_of(a), a.username, a.code] for a in attempts),
        "failed-logins.csv",
    )


@router.get("/failed-logins-by-address.csv")
def attempts_by_address_csv(request: Request, session: Session = Depends(get_session)) -> Response:
    f = _read_filters(request)
    banned = _banned_now(session)
    rows = _by_address(session, _conditions(f), CSV_ROW_CAP)
    return _csv_response(
        ["source_address", "attempts", "first_attempt_utc", "last_attempt_utc", "methods",
         "distinct_usernames_tried", "banned_now"],
        (
            [r.ip, r.attempts, r.first_at.strftime("%Y-%m-%d %H:%M:%S"), r.last_at.strftime("%Y-%m-%d %H:%M:%S"),
             "; ".join(sorted((r.methods or "").split(","))), r.usernames, "yes" if r.ip in banned else "no"]
            for r in rows
        ),
        "failed-logins-by-address.csv",
    )
