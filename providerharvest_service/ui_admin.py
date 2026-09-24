"""Admin-facing web UI: the approval queue (activate/reject a pending
source) and the "every source, any status" dashboard (pause/resume
alongside activate/reject).

No login wall yet -- see ui_provider.py's module docstring for why:
this whole service currently runs as a single stand-in admin user.
Once Keycloak is wired up, this module is exactly where a real
admin-role check belongs (matching provider_source_activate/_reject's
own access rule in the old plugin: a data.gov.gr admin, not merely the
owning org's admin).
"""

from __future__ import annotations

from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from providerharvest_service import provider_sources
from providerharvest_service.db import get_session

router = APIRouter(prefix="/admin/sources", tags=["admin-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")


@router.get("", response_class=HTMLResponse)
def all_sources(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    sources = provider_sources.list_all(session)
    return templates.TemplateResponse(
        request,
        "admin/all_sources.html",
        {
            "sources": sources,
            "counts": provider_sources.status_counts(session),
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )


@router.get("/pending", response_class=HTMLResponse)
def pending(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    sources = provider_sources.list_pending(session)
    return templates.TemplateResponse(
        request,
        "admin/pending.html",
        {
            "sources": sources,
            "counts": provider_sources.status_counts(session),
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )


@router.post("/{source_id}/activate")
def activate(source_id: str, request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        provider_sources.activate(session, source_id)
        return RedirectResponse(
            _came_from(request, "/admin/sources/pending", "Source activated."), status_code=303
        )
    except provider_sources.NotFound:
        flash = "No such source."
    except provider_sources.InvalidTransition as exc:
        flash = str(exc)
    return RedirectResponse(
        _came_from(request, "/admin/sources/pending", flash, kind="error"), status_code=303
    )


@router.post("/{source_id}/reject")
async def reject(source_id: str, request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    form = await request.form()
    try:
        provider_sources.reject(session, source_id, form.get("reason"))
        return RedirectResponse(
            _came_from(request, "/admin/sources/pending", "Source rejected."), status_code=303
        )
    except provider_sources.NotFound:
        flash = "No such source."
    except provider_sources.InvalidTransition as exc:
        flash = str(exc)
    return RedirectResponse(
        _came_from(request, "/admin/sources/pending", flash, kind="error"), status_code=303
    )


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


_ALLOWED_REDIRECT_PATHS = ("/admin/sources", "/admin/sources/pending")


def _came_from(request: Request, default: str, flash: str, kind: str = "success") -> str:
    # pending and all_sources both link Activate/Reject/Pause/Resume at
    # the same routes -- redirect back to whichever page the POST
    # actually came from rather than hardcoding one destination (same
    # pattern the old blueprint's _source_pause_or_resume used). Only
    # ever redirects to one of this module's own pages: Referer is
    # client-supplied, so an unvalidated redirect there would be an
    # open-redirect vector.
    referer_path = urlparse(request.headers.get("referer") or "").path
    target = referer_path if referer_path in _ALLOWED_REDIRECT_PATHS else default
    return "%s?%s" % (target, urlencode({"flash": flash, "flash_kind": kind}))
