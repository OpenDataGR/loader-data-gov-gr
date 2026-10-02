"""Provider-facing self-service web UI: register a data source, see its
own sources and their approval/run status, pause/resume an active one.

Behind the site-wide login (admin_session.py) -- one shared login
for now, so every signed-in visitor here acts as the single stand-in
"admin, me" user the whole service currently runs as (see CLAUDE.md /
README's roadmap: real per-user auth is Keycloak, not registered in
production yet). Once that lands, ``source_list``/``pause``/``resume``
below gain a real owner check; for now there's exactly one provider.
"""

from __future__ import annotations

import datetime
import os
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from providerharvest_service import build_info, connection_check, harvest_db, harvest_runner, nav, provider_sources
from providerharvest_service.db import get_session

router = APIRouter(prefix="/provider", tags=["provider-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now
templates.env.globals["pending_approvals"] = nav.pending_approvals

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
    generated_api_key = getattr(source, "generated_api_key", None)
    if generated_api_key:
        # The one and only moment this plaintext key is ever visible
        # anywhere -- only a bcrypt hash of it is persisted (see
        # provider_sources._push_fields), so this page can't be
        # revisited or reconstructed later.
        return templates.TemplateResponse(
            request,
            "provider/push_credentials.html",
            {
                "source": source,
                "api_key": generated_api_key,
                "upload_url": _push_upload_url(request, source.name),
                "allowed_senders": provider_sources.allowed_senders(source),
            },
        )
    generated_ftp = getattr(source, "generated_ftp", None)
    if generated_ftp:
        # Same one-time rule as the API key above: the plaintext password
        # (when we generated it) exists only in this response. A password
        # the provider chose themselves is None here and never echoed.
        return templates.TemplateResponse(
            request,
            "provider/push_ftp_credentials.html",
            {
                "source": source, "ftp": generated_ftp, "server": _ftp_public_endpoint(),
                "allowed_senders": provider_sources.allowed_senders(source),
            },
        )
    return RedirectResponse(
        _redirect_with_flash("Source %r registered and awaiting admin approval." % source.name),
        status_code=303,
    )


@router.get("/sources/{source_id}/edit", response_class=HTMLResponse)
def edit_source_form(source_id: str, request: Request, session: Session = Depends(get_session)):
    try:
        source = provider_sources.get_or_404(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    data = provider_sources.config_to_form_fields(source)
    return templates.TemplateResponse(
        request,
        "provider/source_form.html",
        _form_template_vars(data, editing=True, source_id=source_id),
    )


@router.post("/sources/{source_id}/edit", response_class=HTMLResponse)
async def edit_source_submit(source_id: str, request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    try:
        provider_sources.update(session, source_id, form)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    except provider_sources.ValidationError as exc:
        return templates.TemplateResponse(
            request,
            "provider/source_form.html",
            _form_template_vars(form, errors=exc.errors, editing=True, source_id=source_id),
            status_code=400,
        )
    return RedirectResponse(_redirect_with_flash("Source updated."), status_code=303)


@router.post("/sources/{source_id}/delete")
def delete_source(source_id: str, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        provider_sources.delete(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    return RedirectResponse(_redirect_with_flash("Source deleted."), status_code=303)


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


@router.post("/sources/{source_id}/check-connection")
def source_check_connection(source_id: str, session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        source = connection_check.check_connection(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    kind = "success" if source.last_check_status == "ok" else "error"
    return RedirectResponse(
        _redirect_with_flash("Connection check: %s" % source.last_check_message, kind=kind),
        status_code=303,
    )


@router.post("/sources/{source_id}/harvest-now")
def harvest_now(
    source_id: str,
    session: Session = Depends(get_session),
    hsession: Session = Depends(harvest_db.get_session),
) -> RedirectResponse:
    try:
        run = harvest_runner.run_harvest(session, hsession, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    except (harvest_runner.UnsupportedTransportType, provider_sources.InvalidTransition) as exc:
        return RedirectResponse(_redirect_with_flash(str(exc), kind="error"), status_code=303)
    if run.status == "success":
        source = provider_sources.get_or_404(session, source_id)
        noun = "record" if source.transport_type == "http" else "file"
        flash = "Harvest complete: %d %s%s." % (
            run.records_count, noun, "" if run.records_count == 1 else "s",
        )
        kind = "success"
    else:
        flash = "Harvest failed: %s" % run.error_message
        kind = "error"
    return RedirectResponse(_redirect_with_flash(flash, kind=kind), status_code=303)


@router.get("/sources/{source_id}/runs", response_class=HTMLResponse)
def source_runs(
    source_id: str,
    request: Request,
    session: Session = Depends(get_session),
    hsession: Session = Depends(harvest_db.get_session),
) -> HTMLResponse:
    try:
        source = provider_sources.get_or_404(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    runs = harvest_runner.list_runs(hsession, source_id)
    return templates.TemplateResponse(
        request,
        "provider/source_runs.html",
        {"source": source, "runs": runs},
    )


@router.get("/sources/{source_id}/runs/{run_id}", response_class=HTMLResponse)
def source_run_detail(
    source_id: str,
    run_id: str,
    request: Request,
    session: Session = Depends(get_session),
    hsession: Session = Depends(harvest_db.get_session),
) -> HTMLResponse:
    try:
        source = provider_sources.get_or_404(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    # Every transport except http stores whole files (HarvestedFile),
    # not per-record JSON (HarvestedRecord) -- sftp/scp/ftp via
    # run_harvest, push via push_ingest.record_pushed_file.
    is_file_transport = source.transport_type != "http"
    records = None if is_file_transport else harvest_runner.list_records(hsession, run_id)
    files = harvest_runner.list_files(hsession, run_id) if is_file_transport else None
    return templates.TemplateResponse(
        request,
        "provider/source_run_detail.html",
        {"source": source, "run_id": run_id, "records": records, "files": files},
    )


@router.get("/sources/{source_id}/runs/{run_id}/files/{file_id}")
def source_run_file_download(
    source_id: str,
    run_id: str,
    file_id: str,
    session: Session = Depends(get_session),
    hsession: Session = Depends(harvest_db.get_session),
) -> Response:
    try:
        provider_sources.get_or_404(session, source_id)
    except provider_sources.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such source.")
    harvested_file = harvest_runner.get_file(hsession, file_id)
    if harvested_file is None or harvested_file.run_id != run_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file.")
    return Response(
        content=harvested_file.content,
        media_type=harvested_file.content_type,
        headers={"Content-Disposition": "attachment; filename=\"%s\"" % harvested_file.filename},
    )


def _push_upload_url(request: Request, source_name: str) -> str:
    """``request.url_for`` alone gets the scheme wrong in production:
    Application Gateway terminates the real HTTPS and forwards to
    nginx over plain HTTP, and nginx's own ``X-Forwarded-Proto
    $scheme`` then reports *its own* connection's scheme (always
    "http", since nginx itself never terminates TLS here) rather than
    passing through the real one Application Gateway already set --
    confirmed the hard way: a real registration's credentials page
    showed ``http://loader.data.gov.gr/push/...`` instead of
    ``https://``. Fixing that would need nginx to stop overwriting the
    header *and* gunicorn/uvicorn configured to trust it from
    127.0.0.1 -- two more layers to get exactly right. An explicit,
    configured base URL sidesteps all of that; ``request.url_for`` is
    only the fallback for local dev/tests, where it's already correct.
    """
    base = os.environ.get("LOADER_PUBLIC_BASE_URL")
    if not base:
        return str(request.url_for("push_upload", source_name=source_name))
    return "%s/push/%s/upload" % (base.rstrip("/"), source_name)


def _ftp_public_endpoint() -> dict:
    """Where a provider connects to push files by FTP/SFTP -- the
    external FTP-receiving box (see ftp-forwarder/README.md), not this
    app's own host, so it can't be derived from the request; it's
    configured. ``host`` is None until LOADER_FTP_PUBLIC_HOST is set, and
    the credentials page says so rather than showing a wrong address."""
    return {
        "host": (os.environ.get("LOADER_FTP_PUBLIC_HOST") or "").strip() or None,
        "ftp_port": os.environ.get("LOADER_FTP_PUBLIC_PORT") or "21",
        "sftp_port": os.environ.get("LOADER_SFTP_PUBLIC_PORT") or "2222",
    }


def _redirect_with_flash(flash: str, kind: str = "success") -> str:
    return "/provider/sources?%s" % urlencode({"flash": flash, "flash_kind": kind})


def _human_size(n: int) -> str:
    """Decimal units, matching how the limit is quoted (100 MB) and how
    nginx/the WAF count it."""
    for unit, size in (("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if n >= size:
            return "%g %s" % (round(n / size, 1), unit)
    return "%d B" % n


def _form_template_vars(data, errors=None, editing=False, source_id=None) -> dict:
    if not data:
        # A genuinely fresh form (GET /sources/new): seed the checkbox
        # defaults HTML can't express via an <input> `value` -- an
        # unchecked checkbox isn't submitted at all, so this must not be
        # applied when re-rendering after a validation error (that
        # `data` already reflects whatever the provider actually
        # submitted, including a deliberately unchecked box).
        data = {"ftp_use_tls": "on", "proxy_enabled": "on"}
    return {
        "data": data,
        "errors": errors or {},
        "transport_types": FIELD_TYPES,
        # Pull sources are fetched by us, on a schedule: HTTP, SFTP/SCP
        # (one card -- identical settings; the SFTP-vs-SCP switch inside
        # it picks the stored transport_type) or FTP. Push sources have
        # a single "push" transport whose sub-choice (HTTPS key vs
        # FTP/SFTP login) is `push_method`.
        "pull_connectors": (("http", "HTTP", "http"), ("ssh", "SFTP / SCP", "sftp"), ("ftp", "FTP", "ftp")),
        "max_file_size_human": _human_size(harvest_runner.MAX_FILE_SIZE_BYTES),
        "frequencies": FREQUENCIES,
        "http_auth_types": HTTP_AUTH_TYPES,
        "editing": editing,
        "source_id": source_id,
    }
