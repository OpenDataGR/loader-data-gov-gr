"""Provider-facing self-service web UI: register a data source, see its
own sources and their approval/run status, pause/resume an active one.

No login wall yet -- every visitor here acts as the single stand-in
"admin, me" user the whole service currently runs as (see CLAUDE.md /
README's roadmap: real per-user auth is Keycloak, not registered in
production yet). Once that lands, ``source_list``/``pause``/``resume``
below gain a real owner check; for now there's exactly one provider.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from providerharvest_service import provider_sources
from providerharvest_service.db import get_session

router = APIRouter(prefix="/provider", tags=["provider-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")

FIELD_TYPES = provider_sources.TRANSPORT_TYPES
FREQUENCIES = provider_sources.FREQUENCIES
HTTP_AUTH_TYPES = provider_sources.HTTP_AUTH_TYPES


@router.get("/sources", response_class=HTMLResponse)
def source_list(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    sources = provider_sources.list_all(session)
    return templates.TemplateResponse(
        request,
        "provider/source_list.html",
        {
            "sources": sources,
            "counts": provider_sources.status_counts(session),
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )


@router.get("/sources/new", response_class=HTMLResponse)
def new_source_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "provider/source_form.html",
        _form_template_vars({}),
    )


@router.post("/sources/new", response_class=HTMLResponse)
async def new_source_submit(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    try:
        source = provider_sources.register(session, form)
    except provider_sources.ValidationError as exc:
        return templates.TemplateResponse(
            request,
            "provider/source_form.html",
            _form_template_vars(form, errors=exc.errors),
            status_code=400,
        )
    return RedirectResponse(
        _redirect_with_flash("Source %r registered and awaiting admin approval." % source.name),
        status_code=303,
    )


@router.post("/sources/{source_id}/pause")
def source_pause(source_id: str, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        provider_sources.pause(session, source_id)
        return RedirectResponse(_redirect_with_flash("Source paused."), status_code=303)
    except provider_sources.NotFound:
        flash = "No such source."
    except provider_sources.InvalidTransition as exc:
        flash = str(exc)
    return RedirectResponse(_redirect_with_flash(flash, kind="error"), status_code=303)


@router.post("/sources/{source_id}/resume")
def source_resume(source_id: str, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        provider_sources.resume(session, source_id)
        return RedirectResponse(_redirect_with_flash("Source resumed."), status_code=303)
    except provider_sources.NotFound:
        flash = "No such source."
    except provider_sources.InvalidTransition as exc:
        flash = str(exc)
    return RedirectResponse(_redirect_with_flash(flash, kind="error"), status_code=303)


def _redirect_with_flash(flash: str, kind: str = "success") -> str:
    return "/provider/sources?%s" % urlencode({"flash": flash, "flash_kind": kind})


def _form_template_vars(data, errors=None) -> dict:
    if not data:
        # A genuinely fresh form (GET /sources/new): seed the one
        # checkbox default HTML can't express via an <input> `value` --
        # an unchecked checkbox isn't submitted at all, so this must not
        # be applied when re-rendering after a validation error (that
        # `data` already reflects whatever the provider actually
        # submitted, including a deliberately unchecked box).
        data = {"ftp_use_tls": "on"}
    return {
        "data": data,
        "errors": errors or {},
        "transport_types": FIELD_TYPES,
        "frequencies": FREQUENCIES,
        "http_auth_types": HTTP_AUTH_TYPES,
    }
