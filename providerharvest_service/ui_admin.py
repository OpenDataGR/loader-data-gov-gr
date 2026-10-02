"""Admin-facing web UI: the approval queue (activate/reject a pending
source) and the "every source, any status" dashboard (pause/resume
alongside activate/reject).

Behind the site-wide login (admin_session.py) -- see ui_provider.py's
module docstring: this whole service currently runs as a single
stand-in admin user.
Once Keycloak is wired up, this module is exactly where a real
admin-role check belongs (matching provider_source_activate/_reject's
own access rule in the old plugin: a data.gov.gr admin, not merely the
owning org's admin).
"""

from __future__ import annotations

import datetime
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from providerharvest_service import build_info, connection_check, nav, provider_sources
from providerharvest_service.db import get_session

router = APIRouter(prefix="/admin/sources", tags=["admin-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now
templates.env.globals["pending_approvals"] = nav.pending_approvals


@router.get("", response_class=HTMLResponse)
def all_sources(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """The admin dashboard: the approval queue first (only when there is
    something to approve), then every source regardless of status."""
    sources = provider_sources.list_all(session)
    return templates.TemplateResponse(
        request,
        "admin/all_sources.html",
        {
            "sources": sources,
            "pending_sources": provider_sources.list_pending(session),
            "counts": provider_sources.status_counts(session),
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )


@router.get("/pending")
def pending() -> RedirectResponse:
    """The approval queue used to be its own page and tab; it now sits at the top
    of the admin dashboard. Kept so old bookmarks and links still land somewhere."""
    return RedirectResponse("/admin/sources#pending", status_code=303)


@router.post("/{source_id}/activate")
def activate(source_id: str, request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        provider_sources.activate(session, source_id)
        return RedirectResponse(_came_from(request, "/admin/sources", "Source activated."), status_code=303)
    except provider_sources.NotFound:
        flash = "No such source."
    except provider_sources.InvalidTransition as exc:
        flash = str(exc)
    return RedirectResponse(_came_from(request, "/admin/sources", flash, kind="error"), status_code=303)


@router.post("/{source_id}/reject")
async def reject(source_id: str, request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    form = await request.form()
    try:
        provider_sources.reject(session, source_id, form.get("reason"))
        return RedirectResponse(_came_from(request, "/admin/sources", "Source rejected."), status_code=303)
    except provider_sources.NotFound:
        flash = "No such source."
    except provider_sources.InvalidTransition as exc:
        flash = str(exc)
    return RedirectResponse(_came_from(request, "/admin/sources", flash, kind="error"), status_code=303)


@router.post("/{source_id}/pause")
def pause(source_id: str, request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        provider_sources.pause(session, source_id)
        return RedirectResponse(_came_from(request, "/admin/sources", "Source paused."), status_code=303)
    except provider_sources.NotFound:
        flash = "No such source."
    except provider_sources.InvalidTransition as exc:
        flash = str(exc)
    return RedirectResponse(_came_from(request, "/admin/sources", flash, kind="error"), status_code=303)


@router.post("/{source_id}/resume")
def resume(source_id: str, request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        provider_sources.resume(session, source_id)
        return RedirectResponse(_came_from(request, "/admin/sources", "Source resumed."), status_code=303)
    except provider_sources.NotFound:
        flash = "No such source."
    except provider_sources.InvalidTransition as exc:
        flash = str(exc)
    return RedirectResponse(_came_from(request, "/admin/sources", flash, kind="error"), status_code=303)


@router.post("/{source_id}/check-connection")
def check_connection(source_id: str, request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        source = connection_check.check_connection(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(
            _came_from(request, "/admin/sources", "No such source.", kind="error"), status_code=303
        )
    kind = "success" if source.last_check_status == "ok" else "error"
    return RedirectResponse(
        _came_from(request, "/admin/sources", "Connection check: %s" % source.last_check_message, kind=kind),
        status_code=303,
    )


@router.post("/{source_id}/delete")
def delete_source(source_id: str, request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        provider_sources.delete(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(
            _came_from(request, "/admin/sources", "No such source.", kind="error"), status_code=303
        )
    return RedirectResponse(_came_from(request, "/admin/sources", "Source deleted."), status_code=303)


_ALLOWED_REDIRECT_PATHS = ("/admin/sources",)


def _came_from(request: Request, default: str, flash: str, kind: str = "success") -> str:
    # Redirect back to the page the POST came from rather than hardcoding
    # one destination (same pattern the old blueprint's
    # _source_pause_or_resume used). Only ever redirects to one of this
    # module's own pages: Referer is client-supplied, so an unvalidated
    # redirect there would be an open-redirect vector. (An old
    # /admin/sources/pending referer is not in the list, so it falls to the
    # default -- the dashboard, where that page's content now lives.)
    referer_path = urlparse(request.headers.get("referer") or "").path
    target = referer_path if referer_path in _ALLOWED_REDIRECT_PATHS else default
    return "%s?%s" % (target, urlencode({"flash": flash, "flash_kind": kind}))
