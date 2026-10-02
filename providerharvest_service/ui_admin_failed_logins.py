"""The report of refused logins on every way in -- FTP and SFTP (shipped in
batches by the FTP box, see ``admin_ftp.report_login_attempts``) and loader's
own HTTP side (the web login and the API/sync keys, recorded by
``failed_auth``): who has been trying the door, how often, when, by which
method and as which username.

One page, three views of the same filtered data, switched by tabs so only the
one in use is drawn: **addresses** (one row per source address with its
number of attempts -- the quick read: who, and how much), **attempts** (every
individual try) and **usernames** (what they typed). Filters (period, address,
method, username, "banned now only") apply to all three; each table sorts by
its column headers and pages; clicking an address, username or method narrows
the filter. Sorting and paging are server-side so the CSV exports and the page
always agree. The ban list (with Unban) sits above the tabs.

Everything in the tables came off the wire from strangers, the username above
all, so it is shown escaped (Jinja's default) and the CSV export defuses
spreadsheet formulas: a "username" of ``=HYPERLINK(...)`` must not become
something that runs when an admin opens the file in Excel.
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
from sqlalchemy import and_, case, distinct, func, or_, select
from sqlalchemy.orm import Session

from providerharvest_service import build_info, failed_auth
from providerharvest_service.admin_ftp import SFTP_SCP
from providerharvest_service.db import get_session
from providerharvest_service.models import FtpBoxStatus, FtpLoginAttempt, FtpUnbanRequest

router = APIRouter(prefix="/admin/security", tags=["security-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now

PAGE = "/admin/security/failed-logins"
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

VIEWS = (("addresses", "Addresses"), ("attempts", "Attempts"), ("usernames", "Usernames"))
_VIEW_KEYS = tuple(key for key, _ in VIEWS)
PAGE_SIZE = {"addresses": 50, "attempts": 100, "usernames": 50}

# How a row came in. Rows that predate the column have no channel and are
# FTP or SFTP/SCP, told apart by the port they hit. Port 2222 is always shown
# as SFTP/SCP -- including rows stored earlier under the plain name "SFTP" --
# because both are one SSH login there: a refusal comes before the client says
# which of the two it wanted, so they cannot be told apart.
METHOD = case(
    (
        and_(FtpLoginAttempt.port == 2222, or_(FtpLoginAttempt.channel.is_(None), FtpLoginAttempt.channel == "SFTP")),
        SFTP_SCP,
    ),
    else_=func.coalesce(
        FtpLoginAttempt.channel,
        case((FtpLoginAttempt.port == 21, "FTP"), else_="Other"),
    ),
)
_KNOWN_METHODS = ("FTP", SFTP_SCP, "SSH", failed_auth.WEB_LOGIN, failed_auth.PUSH_KEY, failed_auth.ADMIN_KEY,
                  failed_auth.BOX_KEY, failed_auth.BEARER, failed_auth.PUSH_ADDRESS, failed_auth.OTHER_HTTP)

# What each view can be sorted by: URL key -> SQL expression. The first entry is the default.
_SORTS = {
    "addresses": {
        "attempts": func.count(),
        "first": func.min(FtpLoginAttempt.at),
        "last": func.max(FtpLoginAttempt.at),
        "ip": FtpLoginAttempt.ip,
        "usernames": func.count(distinct(FtpLoginAttempt.username)),
    },
    "attempts": {
        "time": FtpLoginAttempt.at,
        "ip": FtpLoginAttempt.ip,
        "port": FtpLoginAttempt.port,
        "method": METHOD,
        "username": FtpLoginAttempt.username,
    },
    "usernames": {
        "attempts": func.count(),
        "username": FtpLoginAttempt.username,
    },
}
_TIEBREAK = {
    "addresses": (FtpLoginAttempt.ip,),
    "attempts": (FtpLoginAttempt.id.desc(),),
    "usernames": (FtpLoginAttempt.username,),
}
# Counts and times are most useful biggest/newest first; names oldest-letter first.
_DESC_FIRST = {"attempts", "first", "last", "usernames", "time"}


def _default_direction(key: str) -> str:
    return "desc" if key in _DESC_FIRST else "asc"


def _method_of(attempt: FtpLoginAttempt) -> str:
    if attempt.port == 2222 and attempt.channel in (None, "SFTP"):
        return SFTP_SCP
    return attempt.channel or ("FTP" if attempt.port == 21 else "Other")


templates.env.filters["method"] = _method_of
templates.env.filters["utc"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else ""
templates.env.filters["short_utc"] = lambda value: value.strftime("%m-%d %H:%M") if value else ""


@dataclasses.dataclass(frozen=True)
class Filters:
    window: str = "24"
    address: str = ""
    method: str = ""
    username: str = ""
    banned_only: bool = False
    view: str = "addresses"
    sort: str = "attempts"
    direction: str = "desc"

    def since(self) -> datetime.datetime | None:
        hours = _WINDOW_HOURS[self.window]
        if hours is None:
            return None
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        return now - datetime.timedelta(hours=hours)

    def params(self, **override) -> dict:
        """The query parameters that reproduce this selection, with any ``override``
        applied (a value of None drops that parameter). Defaults are left out so
        links stay short."""
        view = (override["view"] if "view" in override else self.view) or "addresses"
        if "view" in override and "sort" not in override and "dir" not in override:
            sort = direction = ""  # a sort belongs to the view it was chosen in
        else:
            sort = override.get("sort", self.sort) or ""
            direction = override.get("dir", self.direction) or ""
        if sort == _first_sort(view) and direction == _default_direction(sort):
            sort = direction = ""  # that view's own default: leave it out, links stay short
        params = {
            "window": self.window, "address": self.address, "method": self.method, "username": self.username,
            "banned": "1" if self.banned_only else "",
            "view": view if view != "addresses" else "",
            "sort": sort, "dir": direction,
        }
        params.update({k: v for k, v in override.items() if k not in ("view", "sort", "dir")})
        return {k: v for k, v in params.items() if v not in ("", None)}

    def query_string(self, **override) -> str:
        return urlencode(self.params(**override))


def _first_sort(view: str) -> str:
    return next(iter(_SORTS[view]))


def _read_filters(request: Request) -> Filters:
    query = request.query_params
    window = query.get("window", "24")
    method = (query.get("method") or "").strip()
    view = query.get("view", "addresses")
    view = view if view in _VIEW_KEYS else "addresses"
    sort = query.get("sort", "")
    if sort not in _SORTS[view]:
        sort = _first_sort(view)
    direction = query.get("dir", "")
    if direction not in ("asc", "desc"):
        direction = _default_direction(sort)
    return Filters(
        window=window if window in _WINDOW_HOURS else "24",
        address=(query.get("address") or "").strip()[:45],
        method=method if method in _KNOWN_METHODS else "",
        username=(query.get("username") or "").strip()[:100],
        banned_only=query.get("banned") == "1",
        view=view,
        sort=sort,
        direction=direction,
    )


def _is_full_ipv4(text: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(text), ipaddress.IPv4Address)
    except ValueError:
        return False


def _conditions(f: Filters, banned: set[str] | None = None) -> list:
    conditions = []
    since = f.since()
    if since is not None:
        conditions.append(FtpLoginAttempt.at >= since)
    if f.address:
        if _is_full_ipv4(f.address):
            conditions.append(FtpLoginAttempt.ip == f.address)  # a whole address: that one, not 1.2.3.4x as well
        else:
            conditions.append(FtpLoginAttempt.ip.startswith(f.address, autoescape=True))
    if f.method:
        conditions.append(METHOD == f.method)
    if f.username:
        conditions.append(FtpLoginAttempt.username.contains(f.username, autoescape=True))
    if f.banned_only:
        conditions.append(FtpLoginAttempt.ip.in_(sorted(banned or {""})))  # nobody banned: matches nothing
    return conditions


def _order(view: str, sort: str, direction: str) -> list:
    primary = _SORTS[view][sort]
    return [primary.asc() if direction == "asc" else primary.desc(), *_TIEBREAK[view]]


def _by_address(session: Session, conditions: list, *, order=None, limit: int | None = None, offset: int = 0):
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
        .order_by(*(order or _order("addresses", "attempts", "desc")))
        .offset(offset)
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
    return RedirectResponse("%s?%s" % (PAGE, urlencode({"flash": flash, "flash_kind": kind})), status_code=303)


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
    banned = _banned_now(session)
    conditions = _conditions(f, banned)

    total = session.scalar(select(func.count()).select_from(FtpLoginAttempt).where(*conditions)) or 0
    counts = {
        "attempts": total,
        "addresses": session.scalar(select(func.count(distinct(FtpLoginAttempt.ip))).where(*conditions)) or 0,
        "usernames": session.scalar(select(func.count(distinct(FtpLoginAttempt.username))).where(*conditions)) or 0,
    }
    by_method = session.execute(
        select(METHOD.label("method"), func.count().label("n"))
        .where(*conditions)
        .group_by(METHOD)
        .order_by(func.count().desc(), METHOD)
    ).all()

    size = PAGE_SIZE[f.view]
    row_total = counts[f.view]
    pages = max(1, math.ceil(row_total / size))
    page = min(max(page, 1), pages)
    offset = (page - 1) * size
    order = _order(f.view, f.sort, f.direction)
    rows: list = []
    if f.view == "addresses":
        rows = _by_address(session, conditions, order=order, limit=size, offset=offset)
    elif f.view == "attempts":
        rows = session.scalars(
            select(FtpLoginAttempt).where(*conditions).order_by(*order).offset(offset).limit(size)
        ).all()
    else:
        rows = session.execute(
            select(FtpLoginAttempt.username, func.count().label("n"))
            .where(*conditions)
            .group_by(FtpLoginAttempt.username)
            .order_by(*order)
            .offset(offset)
            .limit(size)
        ).all()

    def link(**override) -> str:
        return "%s?%s" % (PAGE, f.query_string(**override))

    def sort_href(key: str) -> str:
        direction = ("asc" if f.direction == "desc" else "desc") if f.sort == key else _default_direction(key)
        return link(sort=key, dir=direction, page=None)

    def sort_mark(key: str) -> str:
        return ("▲" if f.direction == "asc" else "▼") if f.sort == key else ""

    def sort_aria(key: str) -> str:
        return ("ascending" if f.direction == "asc" else "descending") if f.sort == key else "none"

    boxes = _boxes_with_pending_unbans(session)
    return templates.TemplateResponse(
        request,
        "admin/failed_logins.html",
        {
            "f": f,
            "windows": WINDOWS,
            "methods": _KNOWN_METHODS,
            "views": VIEWS,
            "counts": counts,
            "by_method": by_method,
            "rows": rows,
            "banned": banned,
            "boxes": boxes,
            "banned_total": len(banned),
            "link": link,
            "sort_href": sort_href,
            "sort_mark": sort_mark,
            "sort_aria": sort_aria,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
            "page": page,
            "pages": pages,
            "row_total": row_total,
            "page_size": size,
            "export_query": f.query_string(view=None, sort=None, dir=None),
            "retention_note": _retention_note(),
            "oldest": session.scalar(select(func.min(FtpLoginAttempt.at))),
            "filtered": bool(f.address or f.method or f.username or f.banned_only),
        },
    )


def _retention_note() -> str:
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
        .where(*_conditions(f, _banned_now(session)))
        .order_by(*_order("attempts", "time", "desc"))
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
    rows = _by_address(session, _conditions(f, banned), limit=CSV_ROW_CAP)
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
