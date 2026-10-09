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

from providerharvest_service import build_info, connection_check, harvest_db, nav, provider_sources, retention
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


def _size(count: int) -> str:
    value = float(count)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1000


def _harvest_session():
    """A session on the harvest database, or None when it is not reachable (the page then shows no
    figures and, when asked to shorten a period, cannot preview and so refuses to delete blindly)."""
    try:
        return harvest_db.get_session_factory()()
    except Exception:  # noqa: BLE001 -- not configured / not reachable
        return None


def _retention_context(source, **extra) -> dict:
    hsession = _harvest_session()
    stored = None
    if hsession is not None:
        try:
            files, size, oldest = retention.stored(hsession, source.id)
            stored = {"files": files, "size": _size(size), "oldest": oldest.strftime("%Y-%m-%d %H:%M") if oldest else None}
        except Exception:  # noqa: BLE001
            stored = None
        finally:
            hsession.close()
    return {
        "source": source, "stored": stored, "applies_to_box": retention.applies_to_box(source),
        "loader_options": [(d, retention.label(d)) for d in retention.LOADER_OPTIONS],
        "box_options": [(d, retention.label(d)) for d in retention.BOX_OPTIONS],
        "current_loader": retention.loader_days(source), "current_box": retention.box_days(source),
        "default_loader": retention.LOADER_DEFAULT, "default_box": retention.BOX_DEFAULT, "confirm": None, **extra,
    }


@router.get("/{source_id}/retention", response_class=HTMLResponse)
def retention_page(source_id: str, request: Request, session: Session = Depends(get_session)):
    try:
        source = provider_sources.get_or_404(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_came_from(request, "/admin/sources", "No such source.", kind="error"), status_code=303)
    return templates.TemplateResponse(request, "admin/source_retention.html", _retention_context(source))


@router.post("/{source_id}/retention")
async def save_retention(source_id: str, request: Request, session: Session = Depends(get_session)):
    """Save a source's two retention periods. Shortening one deletes data, so it is previewed and
    confirmed first; with the harvest database out of reach nothing can be previewed, so a
    shortening is refused rather than done blind."""
    try:
        source = provider_sources.get_or_404(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_came_from(request, "/admin/sources", "No such source.", kind="error"), status_code=303)
    form = await request.form()
    try:
        loader = retention.parse_choice(form.get("loader"), retention.LOADER_OPTIONS, "files on the loader")
        box = retention.parse_choice(form.get("box"), retention.BOX_OPTIONS, "files on the FTP box")
    except retention.RetentionError as exc:
        return RedirectResponse(_came_from(request, "/admin/sources", str(exc), kind="error"), status_code=303)

    old_loader, old_box = retention.loader_days(source), retention.box_days(source)
    loader_shorter = loader != 0 and (old_loader == 0 or loader < old_loader)
    box_shorter = retention.applies_to_box(source) and box != 0 and (old_box == 0 or box < old_box)
    if (loader_shorter or box_shorter) and not form.get("confirmed"):
        files = size = 0
        if loader_shorter:
            hsession = _harvest_session()
            if hsession is None:
                return RedirectResponse(_came_from(
                    request, "/admin/sources", "The stored data cannot be read just now, so what a shorter period would delete cannot be shown. Nothing was changed.",
                    kind="error"), status_code=303)
            try:
                files, size = retention.preview(hsession, source.id, loader)
            finally:
                hsession.close()
        if files or box_shorter:
            context = _retention_context(source, confirm={
                "loader": loader, "box": box, "loader_files": files, "loader_size": _size(size), "loader_label": retention.label(loader),
                "box_shorter": box_shorter, "box_label": retention.label(box),
            })
            return templates.TemplateResponse(request, "admin/source_retention.html", context)
    retention.set_retention(session, source, loader=loader, box=box if retention.applies_to_box(source) else None)
    note = "Retention saved for %s: loader %s%s." % (source.name, retention.label(loader), ", FTP box %s" % retention.label(box) if retention.applies_to_box(source) else "")
    return RedirectResponse(_came_from(request, "/admin/sources", note), status_code=303)


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
