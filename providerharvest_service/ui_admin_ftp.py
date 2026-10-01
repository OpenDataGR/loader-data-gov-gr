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
import ipaddress
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import build_info
from providerharvest_service.admin_ftp import generate_password, hash_password
from providerharvest_service.db import get_session
from providerharvest_service.models import FtpAccount, FtpBoxStatus, FtpUnbanRequest

router = APIRouter(prefix="/admin/ftp", tags=["ftp-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now

# A box that hasn't reported in this long is shown as stale rather than
# trusted at face value -- the account-sync cycle this relies on runs
# every 60s (see ftp-forwarder/deploy/loader-ftp-forwarder-sync.service),
# so several missed cycles in a row is a real signal, not just jitter.
_STALE_AFTER = datetime.timedelta(minutes=5)

_ALLOWED_REDIRECT_PATHS = ("/admin/ftp",)


def _came_from(request: Request, flash: str, kind: str = "success") -> str:
    referer_path = urlparse(request.headers.get("referer") or "").path
    target = referer_path if referer_path in _ALLOWED_REDIRECT_PATHS else "/admin/ftp"
    return "%s?%s" % (target, urlencode({"flash": flash, "flash_kind": kind}))


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@router.get("", response_class=HTMLResponse)
def ftp_overview(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    accounts = list(session.scalars(select(FtpAccount)).all())
    boxes = list(session.scalars(select(FtpBoxStatus)).all())
    waiting = {(r.hostname, r.ip) for r in session.scalars(select(FtpUnbanRequest)).all()}
    now = _utcnow()
    for box in boxes:
        box.pending_unban = {ip for host, ip in waiting if host == box.hostname}  # type: ignore[attr-defined]
        reported_at = box.reported_at
        if reported_at.tzinfo is None:
            reported_at = reported_at.replace(tzinfo=datetime.timezone.utc)
        box.is_stale = (now - reported_at) > _STALE_AFTER  # type: ignore[attr-defined]
    return templates.TemplateResponse(
        request,
        "admin/ftp.html",
        {
            "accounts": accounts,
            "boxes": boxes,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )


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
        return RedirectResponse(_came_from(request, "No such box.", kind="error"), status_code=303)
    form = await request.form()
    try:
        address = str(ipaddress.ip_address((form.get("ip") or "").strip()))
    except ValueError:
        return RedirectResponse(_came_from(request, "That is not an IP address.", kind="error"), status_code=303)
    if address not in box.banned:
        return RedirectResponse(
            _came_from(request, "%s is not on the ban list (it may already have been released)." % address, kind="error"),
            status_code=303,
        )
    already = session.scalar(
        select(FtpUnbanRequest).where(FtpUnbanRequest.hostname == hostname, FtpUnbanRequest.ip == address)
    )
    if already is None:
        session.add(FtpUnbanRequest(hostname=hostname, ip=address))
        session.commit()
    return RedirectResponse(
        _came_from(request, "Unban requested for %s -- the box applies it within about a minute." % address),
        status_code=303,
    )


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
        {"username": account.username, "password": password, "action": "rotated"},
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
