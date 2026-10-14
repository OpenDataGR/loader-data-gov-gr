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
import hashlib
import os
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import build_info, connection_check, harvest_db, harvest_runner, nav, provider_sources, push_check, retention
from providerharvest_service.db import get_session
from providerharvest_service.models import LookupTable

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
    index_tables: dict[str, list] = {}  # informational: each source's index tables (name, rows)
    for table in session.scalars(select(LookupTable).where(LookupTable.source_id.is_not(None)).order_by(LookupTable.name)).all():
        index_tables.setdefault(table.source_id, []).append(table)
    return templates.TemplateResponse(
        request,
        "provider/source_list.html",
        {
            "sources": sources,
            "index_tables": index_tables,
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
def delete_source(source_id: str, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)) -> RedirectResponse:
    """Delete a source and everything that belongs to it: its pipelines, index tables, publish setting, stored runs, files and outputs."""
    try:
        provider_sources.get_or_404(session, source_id)
        stored = retention.delete_source_data(hsession, source_id)
        removed = provider_sources.delete(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    return RedirectResponse(_redirect_with_flash(provider_sources.deletion_summary(removed, stored)), status_code=303)


@router.get("/sources/{source_id}/credentials", response_class=HTMLResponse)
def source_credentials(source_id: str, request: Request, session: Session = Depends(get_session)):
    """The one place for everything that lets a push source in: its FTP/SFTP login
    (set or rotate the password) or HTTPS upload URL and API key (rotate), who may
    send (the allowed senders), and when it last did. The FTP box page shows a
    provider's own account read-only and links here."""
    try:
        source = provider_sources.get_or_404(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    if source.transport_type != "push":
        return RedirectResponse(
            _redirect_with_flash("Only a push source has a login or API key to manage.", kind="error"), status_code=303
        )
    return templates.TemplateResponse(
        request,
        "provider/source_credentials.html",
        {
            "source": source,
            "account": provider_sources.managed_ftp_account(session, source) if source.push_method == "ftp" else None,
            "server": _ftp_public_endpoint(),
            "upload_url": _push_upload_url(request, source.name),
            "allowed_senders": provider_sources.allowed_senders(source),
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )


@router.post("/sources/{source_id}/rotate-key", response_class=HTMLResponse)
def source_rotate_key(source_id: str, request: Request, session: Session = Depends(get_session)):
    """A new API key for an HTTPS push source -- the way back from a lost or leaked
    one. The old key stops working at once. The new one is rendered in the response
    body of this POST, never in a redirect or flash (a URL ends up in history and
    Referer headers), and shown exactly once, like at registration."""
    try:
        source, api_key = provider_sources.rotate_api_key(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    except provider_sources.NotRotatable as exc:
        return RedirectResponse(_redirect_with_flash(str(exc).capitalize() + ".", kind="error"), status_code=303)
    return templates.TemplateResponse(
        request,
        "provider/push_credentials.html",
        {
            "source": source,
            "api_key": api_key,
            "upload_url": _push_upload_url(request, source.name),
            "allowed_senders": provider_sources.allowed_senders(source),
            "rotated": True,
        },
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


def _date(raw: str | None) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(raw) if raw else None
    except ValueError:
        return None  # a date that is not one is ignored, not an error page


def _runs_query(request: Request) -> dict:
    """The Runs page's filters from the query string. Anything not on the offered lists falls back to
    the default, and is never echoed back raw."""
    params = request.query_params
    try:
        page = max(1, int(params.get("page", "1")))
    except ValueError:
        page = 1
    try:
        per = int(params.get("per", ""))
    except ValueError:
        per = harvest_runner.DEFAULT_PAGE_SIZE
    since, until = _date(params.get("from")), _date(params.get("to"))
    if since and until and since > until:
        since, until = until, since
    status_filter = params.get("status")
    return {
        "page": min(page, 1_000_000), "per": per if per in harvest_runner.PAGE_SIZES else harvest_runner.DEFAULT_PAGE_SIZE,
        "status": status_filter if status_filter in harvest_runner.RUN_STATUSES else None, "since": since, "until": until,
    }


def _link(query: dict, page: int) -> str:
    """The Runs page's own address for ``page`` with the current filters."""
    pairs = [("page", page)] if page != 1 else []
    if query["per"] != harvest_runner.DEFAULT_PAGE_SIZE:
        pairs.append(("per", query["per"]))
    for key, value in (("status", query["status"]), ("from", query["since"]), ("to", query["until"])):
        if value:
            pairs.append((key, value.isoformat() if hasattr(value, "isoformat") else value))
    return "?" + urlencode(pairs) if pairs else "?"


def _page_links(query: dict, pages: int) -> list[tuple[str, str, bool]]:
    """[(label, href, current)] -- the first, last, and a few pages around the current one, with "..." gaps."""
    current = query["page"]
    shown = sorted({1, pages, *range(max(1, current - 2), min(pages, current + 2) + 1)})
    links, previous = [], 0
    for number in shown:
        if number - previous > 1:
            links.append(("...", "", False))
        links.append((str(number), _link(query, number), number == current))
        previous = number
    return links


@router.get("/sources/{source_id}/readiness", response_class=HTMLResponse)
def source_readiness(source_id: str, request: Request, session: Session = Depends(get_session)):
    """The "test connection" of a push source: whether an upload would arrive, from what the loader knows (see push_check.py)."""
    try:
        source = provider_sources.get_or_404(session, source_id)
    except provider_sources.NotFound:
        return RedirectResponse(_redirect_with_flash("No such source.", kind="error"), status_code=303)
    if source.transport_type != "push":
        return RedirectResponse(_redirect_with_flash("Only a push source has a readiness check; test this one with Test connection.", kind="error"), status_code=303)
    checks = push_check.check(session, source)
    level, line = push_check.summary(checks)
    # The result is the source's health, like Test connection's: all green is "reachable", a problem "unreachable", warnings only a warning.
    source.last_check_at = datetime.datetime.now(datetime.timezone.utc)
    if level == "fail":
        source.last_check_status = "error"
    elif all(c.level == "ok" for c in checks):
        source.last_check_status = "ok"  # every check is green, "waiting for the first file" included: it is not proven until a file arrives
    else:
        source.last_check_status = "warn"
    source.last_check_message = line
    session.commit()
    return templates.TemplateResponse(request, "provider/source_readiness.html", {"source": source, "checks": checks, "level": level, "summary": line})


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
    query = _runs_query(request)
    runs, total = harvest_runner.query_runs(
        hsession, source_id, status=query["status"], since=query["since"], until=query["until"],
        limit=query["per"], offset=(query["page"] - 1) * query["per"],
    )
    pages = max(1, -(-total // query["per"]))
    if query["page"] > pages:  # a page past the end (the list shrank, or a stale link): the last one
        query["page"] = pages
        runs, total = harvest_runner.query_runs(
            hsession, source_id, status=query["status"], since=query["since"], until=query["until"],
            limit=query["per"], offset=(pages - 1) * query["per"],
        )
    return templates.TemplateResponse(
        request,
        "provider/source_runs.html",
        {
            "source": source, "runs": runs, "files": harvest_runner.files_by_run(hsession, [r.id for r in runs]),
            "total": total, "pages": pages, "q": query, "page_sizes": harvest_runner.PAGE_SIZES,
            "statuses": harvest_runner.RUN_STATUSES, "page_links": _page_links(query, pages),
            "prev_href": _link(query, query["page"] - 1) if query["page"] > 1 else None,
            "next_href": _link(query, query["page"] + 1) if query["page"] < pages else None,
            "filtered": bool(query["status"] or query["since"] or query["until"]),
        },
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
    # What the loader holds, as a fingerprint a provider can compare with their own (``sha256sum file``); not
    # stored, worked out from the contents -- so a file whose contents were removed by retention has none.
    checksums = {f.id: hashlib.sha256(f.content).hexdigest() for f in files or [] if f.content}
    return templates.TemplateResponse(
        request,
        "provider/source_run_detail.html",
        {"source": source, "run_id": run_id, "records": records, "files": files, "checksums": checksums},
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
    if harvested_file.size and not harvested_file.content:
        raise HTTPException(status.HTTP_410_GONE, "This file's contents were removed when it passed its retention period.")
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
        data = {"ftp_use_tls": "on"}
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
