"""Admin-facing web UI for the FTP-push subsystem: the account list
(enable/disable/rotate password/delete) and the live status this
service's own database holds for every remote FTP/SFTP-receiving box
(see ``ftp_box_status`` -- populated by that box's own account-sync
job via ``PUT /admin/ftp-box-status``, since loader can never reach
that box directly to ask).

There is deliberately no "create account" form here: a provider gets
their account by registering a push source with FTP/SFTP
(``provider_sources.register``), which also wires the forwarding target
and ties the account's lifecycle to the source's approval. A second,
manual way to make one just invited a wrong home directory, a protocol
the box doesn't serve, and an account with no source behind it -- the
JSON API (``admin_ftp.py``) remains for scripted cases.

Deliberately a separate module/router from ``admin_ftp.py``, not just
another route added there: that module is the shared-key-gated JSON
API a remote box's own tooling and one-off ops workflows call; this is
the human-facing HTML view, matching ``ui_admin.py``'s own split from
its JSON-API counterpart and its "no login wall yet" placeholder model
(see that module's docstring for why -- the same real-auth gap applies
here).

A rotated password is rendered directly in the response
body of the POST that rotated it, never carried in a redirect's query
string or flash message -- consistent with this project's "shown
exactly once, never stored or re-displayed" rule for these credentials
(see admin_ftp.py's own ``FtpAccountCreated``/``PasswordRotated``):
a URL (unlike a response body) ends up in browser history and
Referer headers, so it's not an acceptable place for one to pass
through even briefly.
"""

from __future__ import annotations

import datetime
import re
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import admin_session, box_commands, box_history, build_info, http_box, nav
from providerharvest_service.admin_ftp import generate_password, hash_password, normalize_allowed_cidr
from providerharvest_service.ftp_credentials import password_problem
from providerharvest_service.db import get_session
from providerharvest_service.models import FtpAccount, FtpAllowedIp, FtpBoxStatus

router = APIRouter(prefix="/admin/ftp", tags=["ftp-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now
templates.env.globals["pending_approvals"] = nav.pending_approvals
templates.env.filters["action_label"] = box_commands.label
templates.env.filters["utc"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else ""
templates.env.filters["short_utc"] = lambda value: value.strftime("%m-%d %H:%M") if value else ""

# A box that hasn't reported in this long is shown as stale rather than
# trusted at face value -- the account-sync cycle this relies on runs
# every 60s (see ftp-forwarder/deploy/loader-ftp-forwarder-sync.service),
# so several missed cycles in a row is a real signal, not just jitter.
_STALE_AFTER = datetime.timedelta(minutes=5)

_ALLOWED_REDIRECT_PATHS = ("/admin/ftp",)

_MANAGED_NOTE = (
    "%r belongs to a push source -- its allowed addresses are set on the source "
    "(Provider portal, edit the source), not here."
)


# A push source's Credentials page: where a provider's own account is managed, and so where
# its actions return to (only ever this site's own path shape -- the Referer is client-supplied).
_CREDENTIALS_PATH = re.compile(r"^/provider/sources/[A-Za-z0-9-]{1,64}/credentials$")


def _came_from(request: Request, flash: str, kind: str = "success") -> str:
    referer_path = urlparse(request.headers.get("referer") or "").path
    allowed = referer_path in _ALLOWED_REDIRECT_PATHS or _CREDENTIALS_PATH.match(referer_path)
    target = referer_path if allowed else "/admin/ftp"
    return "%s?%s" % (target, urlencode({"flash": flash, "flash_kind": kind}))


def _account_home(account: FtpAccount) -> str:
    """Where this account is managed: its source's Credentials page for a provider's own
    account, the FTP box page for one an admin made by hand."""
    if account.managed_source_id:
        return "/provider/sources/%s/credentials" % account.managed_source_id
    return "/admin/ftp"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _gb(mb: int | None) -> str:
    return "?" if mb is None else ("%.1f GB" % (mb / 1024) if mb >= 1024 else "%d MB" % mb)


def _span(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return "%dd %dh" % (days, hours) if days else "%dh %dm" % (hours, rest // 60)


def _level(percent: float, warn: float = 80, bad: float = 92) -> str:
    return "bad" if percent >= bad else "warn" if percent >= warn else "ok"


def resource_view(box: FtpBoxStatus) -> list[dict]:
    return resource_rows(box.resource_figures, public=box.address)


def _size(count: int | None) -> str:
    if count is None:
        return "?"
    value = float(count)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1000


def _speed(bps: int | None) -> str:
    return "--" if bps is None else _size(bps) + "/s"


def resource_rows(r: dict, public: str | None = None) -> list[dict]:
    """A box's figures as rows for the page: a label, a text, and -- for what has a limit -- a
    percentage and how worrying it is ("ok" | "warn" | "bad"). Empty for a box that does not
    report them (older code), so the page just omits the section."""
    if not r:
        return []
    rows: list[dict] = []
    load, cpus = r.get("load"), r.get("cpus") or 1
    if isinstance(load, list) and len(load) == 3:
        busy = 100 * load[0] / cpus  # 100 means every CPU has work queued
        rows.append({"label": "Load", "text": "%.2f  %.2f  %.2f  (%d CPU%s)" % (*load, cpus, "" if cpus == 1 else "s"),
                     "percent": min(100.0, busy), "level": _level(busy, 100, 150)})
    total, avail = r.get("mem_total_mb"), r.get("mem_available_mb")
    if total and avail is not None:
        percent = 100 * (total - avail) / total
        swap = r.get("swap_used_mb")
        rows.append({"label": "Memory", "text": "%s of %s in use%s" % (_gb(total - avail), _gb(total),
                     "" if not swap else ", swap %s" % _gb(swap)), "percent": percent, "level": _level(percent)})
    for disk in r.get("disks") or []:
        if disk.get("total_mb"):
            percent = 100 * (disk.get("used_mb") or 0) / disk["total_mb"]
            rows.append({"label": "Disk " + str(disk.get("path")), "text": "%s of %s used" % (_gb(disk.get("used_mb")), _gb(disk["total_mb"])),
                         "percent": percent, "level": _level(percent)})
    own = [a for a in r.get("addresses") or [] if a != public]
    if public or own:
        text = ", ".join(([public + " (as loader sees it)"] if public else []) + own)
        rows.append({"label": "Address", "text": text, "percent": None, "level": "ok"})
    net = r.get("net")
    if isinstance(net, dict):
        rows.append({"label": "Network (%s)" % net.get("iface", "?"), "percent": None, "level": "ok",
                     "text": "in %s, out %s  --  since boot: %s in, %s out" % (
                         _speed(net.get("rx_bps")), _speed(net.get("tx_bps")), _size(net.get("rx_bytes")), _size(net.get("tx_bytes")))})
    if r.get("ftp_sessions") is not None or r.get("sftp_sessions") is not None:
        rows.append({"label": "Open sessions", "text": "%s FTP, %s SFTP" % (r.get("ftp_sessions", "?"), r.get("sftp_sessions", "?")), "percent": None, "level": "ok"})
    if r.get("http_connections") is not None:
        rows.append({"label": "Open connections", "text": "%d to nginx" % r["http_connections"], "percent": None, "level": "ok"})
    if r.get("workers") is not None:
        rows.append({"label": "Loader workers", "text": "%d running" % r["workers"], "percent": None, "level": "ok"})
    if r.get("service_up_s") is not None:
        rows.append({"label": "Loader up for", "text": _span(r.get("service_up_s")), "percent": None, "level": "ok"})
    if r.get("uptime_s") is not None:
        rows.append({"label": "Machine up for", "text": _span(r.get("uptime_s")), "percent": None, "level": "ok"})
    return rows


@router.get("", response_class=HTMLResponse)
def ftp_overview(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    accounts = list(session.scalars(select(FtpAccount)).all())
    boxes = list(session.scalars(select(FtpBoxStatus)).all())
    now = _utcnow()
    for box in boxes:
        reported_at = box.reported_at
        if reported_at.tzinfo is None:
            reported_at = reported_at.replace(tzinfo=datetime.timezone.utc)
        box.is_stale = (now - reported_at) > _STALE_AFTER  # type: ignore[attr-defined]
        box.resource_rows = resource_view(box)  # type: ignore[attr-defined]
        box.commands = box_commands.recent(session, box.hostname)  # type: ignore[attr-defined]
    session.commit()  # (recent() records requests that went unanswered as expired)
    window = request.query_params.get("window")
    window = window if window in box_history.WINDOWS else box_history.DEFAULT_WINDOW
    hours = box_history.WINDOWS[window]
    for box in boxes:
        readings = box_history.samples(session, box.hostname, hours)
        box.charts = box_history.charts(readings, hours)  # type: ignore[attr-defined]
        box.table = box_history.table(readings)  # type: ignore[attr-defined]
    own = box_history.samples(session, http_box.NAME, hours)
    local = {
        "charts": box_history.charts(own, hours),
        "table": box_history.table(own),
        "name": http_box.NAME,
        "rows": resource_rows(http_box.collect_resources(disks=("/",))),
        "commands": box_commands.recent(session, http_box.NAME),
        "actions": list(http_box.ACTIONS.values()),
        "log_services": list(http_box.log_units()),
    }
    return templates.TemplateResponse(
        request,
        "admin/ftp.html",
        {
            "accounts": accounts,
            "boxes": boxes,
            "http_box": local,
            "windows": list(box_history.WINDOWS),
            "window": window,
            "box_actions": list(box_commands.ACTIONS.values()),
            "box_log_services": list(box_commands.FTP_LOG_SERVICES),
            "log_lines": list(box_commands.LOG_LINES),
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )


@router.post("/http-box/logs")
async def show_http_box_logs(request: Request, session: Session = Depends(get_session)):
    """Read the last lines of one of loader's own logs, now (see http_box.read_logs)."""
    who = admin_session.verify_session_cookie(request.cookies.get(admin_session.COOKIE_NAME, "")) or ""
    form = await request.form()
    try:
        action = box_commands.log_action(str(form.get("service") or ""), form.get("lines"))
        http_box.perform(session, action, who)
    except box_commands.CommandError as exc:
        return RedirectResponse("/admin/ftp?%s" % urlencode({"flash": str(exc), "flash_kind": "error"}), status_code=303)
    return RedirectResponse("/admin/ftp?%s" % urlencode({"flash": "%s: see the output below." % box_commands.label(action)}), status_code=303)


@router.post("/boxes/{hostname}/logs")
async def ask_box_for_logs(request: Request, hostname: str, session: Session = Depends(get_session)):
    """Queue a log view for an FTP box; its text comes back with the box's next check-in."""
    who = admin_session.verify_session_cookie(request.cookies.get(admin_session.COOKIE_NAME, "")) or ""
    form = await request.form()
    try:
        action = box_commands.log_action(str(form.get("service") or ""), form.get("lines"))
        box_commands.request(session, hostname, action, who)
    except box_commands.CommandError as exc:
        return RedirectResponse("/admin/ftp?%s" % urlencode({"flash": str(exc), "flash_kind": "error"}), status_code=303)
    return RedirectResponse(
        "/admin/ftp?%s" % urlencode({"flash": "%s requested for %s. It arrives within about a minute." % (box_commands.label(action), hostname)}),
        status_code=303,
    )


@router.post("/http-box/actions/{action}")
def run_on_http_box(request: Request, action: str, background: BackgroundTasks, session: Session = Depends(get_session)):
    """An action on loader itself (see http_box.py). A diagnostics snapshot is taken now; the two
    that signal gunicorn do so after this response has gone out."""
    who = admin_session.verify_session_cookie(request.cookies.get(admin_session.COOKIE_NAME, "")) or ""
    try:
        _, send = http_box.perform(session, action, who)
    except box_commands.CommandError as exc:
        return RedirectResponse("/admin/ftp?%s" % urlencode({"flash": str(exc), "flash_kind": "error"}), status_code=303)
    label = http_box.ACTIONS[action].label
    note = {"diagnostics": "Done -- see the output below.",
            "reload-workers": "Reloading the workers; nothing is interrupted.",
            "restart-service": "Restarting loader. The pages are back in about 15 seconds."}[action]
    if send is not None:
        background.add_task(send)
    return RedirectResponse("/admin/ftp?%s" % urlencode({"flash": "%s: %s" % (label, note)}), status_code=303,
                            background=background)


@router.post("/boxes/{hostname}/actions/{action}")
def ask_box_to_run(request: Request, hostname: str, action: str, session: Session = Depends(get_session)):
    """Queue one of the box actions (see box_commands.py). It runs within about a minute, at the
    box's next heartbeat -- loader cannot reach the box itself."""
    who = admin_session.verify_session_cookie(request.cookies.get(admin_session.COOKIE_NAME, "")) or ""
    try:
        box_commands.request(session, hostname, action, who)
    except box_commands.CommandError as exc:
        return RedirectResponse("/admin/ftp?%s" % urlencode({"flash": str(exc), "flash_kind": "error"}), status_code=303)
    label = box_commands.ACTIONS[action].label
    return RedirectResponse(
        "/admin/ftp?%s" % urlencode({"flash": "%s requested for %s. The box picks it up within about a minute." % (label, hostname)}),
        status_code=303,
    )


@router.post("/{account_id}/allowed-ips")
async def add_allowed_address(request: Request, account_id: str, session: Session = Depends(get_session)):
    """Limit an account to log in from this address or range (and any others
    already listed). With none listed the account works from anywhere. The box
    applies it within about a minute, to FTP and SFTP alike."""
    account = session.get(FtpAccount, account_id)
    if account is None:
        return RedirectResponse(_came_from(request, "No such account.", kind="error"), status_code=303)
    if account.managed_source_id:
        return RedirectResponse(_came_from(request, _MANAGED_NOTE % account.username, kind="error"), status_code=303)
    form = await request.form()
    try:
        cidr = normalize_allowed_cidr(str(form.get("cidr") or ""))
    except ValueError as exc:
        return RedirectResponse(_came_from(request, str(exc), kind="error"), status_code=303)
    if any(a.cidr == cidr for a in account.allowed_ips):
        return RedirectResponse(_came_from(request, "%s is already allowed for %r." % (cidr, account.username)), status_code=303)
    session.add(FtpAllowedIp(ftp_account_id=account.id, cidr=cidr))
    session.commit()
    return RedirectResponse(
        _came_from(request, "%r can now only log in from the listed addresses (including %s)." % (account.username, cidr)),
        status_code=303,
    )


@router.post("/{account_id}/allowed-ips/{ip_id}/delete")
def remove_allowed_address(request: Request, account_id: str, ip_id: str, session: Session = Depends(get_session)):
    allowed = session.get(FtpAllowedIp, ip_id)
    if allowed is None or allowed.ftp_account_id != account_id:
        return RedirectResponse(_came_from(request, "No such address.", kind="error"), status_code=303)
    account = allowed.account
    if account.managed_source_id:
        return RedirectResponse(_came_from(request, _MANAGED_NOTE % account.username, kind="error"), status_code=303)
    session.delete(allowed)
    session.commit()
    session.refresh(account)
    note = "" if account.allowed_ips else " -- the account can now log in from anywhere"
    return RedirectResponse(_came_from(request, "Removed %s for %r%s." % (allowed.cidr, account.username, note)), status_code=303)


@router.get("/{account_id}/set-password", response_class=HTMLResponse)
def set_password_form(request: Request, account_id: str, session: Session = Depends(get_session)):
    account = session.get(FtpAccount, account_id)
    if account is None:
        return RedirectResponse(_came_from(request, "No such account.", kind="error"), status_code=303)
    return templates.TemplateResponse(
        request, "admin/ftp_set_password.html", {"account": account, "error": None, "home": _account_home(account)}
    )


@router.post("/{account_id}/set-password", response_class=HTMLResponse)
async def set_password(request: Request, account_id: str, session: Session = Depends(get_session)):
    """Set an account's password to one the admin chose -- typically what a
    provider told them -- instead of rotating to a generated one. The typed
    password is checked, hashed and forgotten: never echoed into the page, never
    put in a redirect or flash (a URL ends up in history and Referer headers),
    never stored in a form that could be shown again."""
    account = session.get(FtpAccount, account_id)
    if account is None:
        return RedirectResponse(_came_from(request, "No such account.", kind="error"), status_code=303)
    form = await request.form()
    password = str(form.get("password") or "")
    error = None
    if password != str(form.get("password_confirm") or ""):
        error = "The two passwords do not match."
    else:
        problem = password_problem(password)
        if problem:
            error = "Password: %s." % problem
    if error:
        return templates.TemplateResponse(
            request, "admin/ftp_set_password.html",
            {"account": account, "error": error, "home": _account_home(account)}, status_code=400,
        )
    account.password_hash = hash_password(password)
    session.commit()
    flash = "Password for %r set. It is stored only as a hash and cannot be shown again." % account.username
    return RedirectResponse("%s?%s" % (_account_home(account), urlencode({"flash": flash, "flash_kind": "success"})), status_code=303)


@router.post("/{account_id}/rotate-password", response_class=HTMLResponse)
def rotate_password(request: Request, account_id: str, session: Session = Depends(get_session)):
    account = session.get(FtpAccount, account_id)
    if account is None:
        return RedirectResponse(_came_from(request, "No such account.", kind="error"), status_code=303)

    password = generate_password()
    account.password_hash = hash_password(password)
    session.commit()

    return templates.TemplateResponse(
        request,
        "admin/ftp_password_reveal.html",
        {
            "username": account.username, "password": password, "action": "rotated",
            "back_url": _account_home(account),
            "back_label": "Back to the source's credentials" if account.managed_source_id else "Back to FTP box",
        },
    )


@router.post("/{account_id}/enable")
def enable_account(request: Request, account_id: str, session: Session = Depends(get_session)):
    account = session.get(FtpAccount, account_id)
    if account is None:
        return RedirectResponse(_came_from(request, "No such account.", kind="error"), status_code=303)
    account.enabled = True
    session.commit()
    return RedirectResponse(_came_from(request, "Account %r enabled." % account.username), status_code=303)


@router.post("/{account_id}/disable")
def disable_account(request: Request, account_id: str, session: Session = Depends(get_session)):
    account = session.get(FtpAccount, account_id)
    if account is None:
        return RedirectResponse(_came_from(request, "No such account.", kind="error"), status_code=303)
    account.enabled = False
    session.commit()
    return RedirectResponse(_came_from(request, "Account %r disabled." % account.username), status_code=303)


@router.post("/{account_id}/delete")
def delete_account(request: Request, account_id: str, session: Session = Depends(get_session)):
    account = session.get(FtpAccount, account_id)
    if account is None:
        return RedirectResponse(_came_from(request, "No such account.", kind="error"), status_code=303)
    username = account.username
    session.delete(account)
    session.commit()
    return RedirectResponse(_came_from(request, "Account %r deleted." % username), status_code=303)
