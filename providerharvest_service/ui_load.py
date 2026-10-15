"""The Load Box: where a source's transformed output goes. For now one destination -- a published API address
(``/data/{source name}``, see ``load_api``) that always returns the newest good output -- switched on per source
here. Behind the site-wide login (admin_session.py)."""

from __future__ import annotations

import datetime
import json
import logging
import os
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import admin_session, build_info, ckan_load, harvest_db, load_api, nav, provider_sources, publishing, request_template, transform_forms as tf, transform_runner
from providerharvest_service.db import get_session
from providerharvest_service.models import CkanTarget, ProviderSource, Transform

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/load", tags=["load-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now
templates.env.globals["pending_approvals"] = nav.pending_approvals
templates.env.filters["utc"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else ""
templates.env.filters["short_utc"] = lambda value: value.strftime("%m-%d %H:%M") if value else ""


def _go(flash: str | None = None, kind: str = "success") -> RedirectResponse:
    query = "?" + urlencode({"flash": flash, "flash_kind": kind}) if flash else ""
    return RedirectResponse("/load" + query, status_code=303)


def _api_of(source: ProviderSource, base: str, pipeline=None) -> dict | None:
    """What an API source's published address does, for the Load Box: how callers ask it (GET or POST, and for GET the parameters the
    request template names), how the provider is called, the per-caller limit. None for a source that stores its data. The settings
    live on the source (Extract Box), on the pipeline's request page and on its Transform page; this only shows them."""
    if source.transport_type != "http":
        return None
    try:
        config = json.loads(source.config or "{}")
    except ValueError:
        config = {}
    from providerharvest_service import proxy as proxy_module

    swapped = proxy_module.request_is_swapped(pipeline)
    method = proxy_module.consumer_method(source, pipeline)
    hint = ""
    if swapped and method == "GET":
        hint = "?" + "&".join("%s=..." % name for name in request_template.placeholders(pipeline.request_template))
    path = publishing.address_of(source, pipeline) if pipeline is not None else "/data/%s" % source.name
    return {
        "url": "%s%s%s" % (base, path, hint), "method": method, "turned": swapped, "provider_method": source.proxy_method,
        "per_caller": config.get("proxy_rate_limit_per_ip", 30), "only": source.proxy_only,
        "request_summary": tf.request_summary(source.proxy_method, pipeline),
    }


@router.get("", response_class=HTMLResponse)
def overview(request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    # the configured public address: behind the gateway the request itself says "http" (see ui_provider._push_upload_url)
    base = (os.environ.get("LOADER_PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")
    targets = {t.pipeline_id: t for t in session.scalars(select(CkanTarget)).all()}
    rows = []  # one row per pipeline (what is published is a pipeline); a source with none yet gets one row
    for source in provider_sources.list_all(session):
        for pipeline in transform_runner.list_pipelines(session, source.id) or [None]:
            path = publishing.address_of(source, pipeline) if pipeline is not None else "/data/%s" % source.name
            rows.append({
                "source": source, "pipeline": pipeline, "published": publishing.is_pipeline_published(pipeline),
                "latest": load_api.latest_output(hsession, source.id, pipeline.id) if pipeline is not None and source.transport_type != "http" else None,
                "path": path, "url": base + path, "api": _api_of(source, base, pipeline), "ckan": targets.get(pipeline.id) if pipeline is not None else None,
                "base": ("/load/sources/%s" % source.id) + ("" if pipeline is None or pipeline.name == transform_runner.DEFAULT_PIPELINE else "/pipelines/%s" % pipeline.name),
            })
    return templates.TemplateResponse(request, "load/overview.html", {
        "rows": rows, "ckan_ready": ckan_load.configured(), "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
    })


@router.post("/sources/{source_id}/api")
@router.post("/sources/{source_id}/pipelines/{pipeline}/api")
async def set_api(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session)):
    """Publish or unpublish one pipeline of a source (the ``default`` one at /load/sources/<id>/api)."""
    source = session.get(ProviderSource, source_id)
    if source is None:
        return _go("No such source.", "error")
    form = await request.form()
    enable = str(form.get("enabled") or "") == "1"
    if enable and source.status != "active":
        return _go("%s is not active yet, so it cannot be published." % source.title, "error")
    who = admin_session.verify_session_cookie(request.cookies.get(admin_session.COOKIE_NAME, "")) or ""
    if pipeline == transform_runner.DEFAULT_PIPELINE:
        # a source that stores data publishes its transformed output, so it needs a pipeline; an API source with none gets one without
        # steps, which passes the provider's answer through
        target = publishing.set_published(session, source, enable, who)
        if enable and target is None:
            return _go("%s has no transform yet -- build one in the Transform Box first." % source.title, "error")
    else:
        target = transform_runner.get_pipeline(session, source_id, pipeline)
        if target is None:
            return _go("%s has no pipeline called %r." % (source.title, pipeline), "error")
        publishing.set_pipeline_published(session, target, enable, who)
    session.commit()
    path = publishing.address_of(source, target) if target is not None else "/data/%s" % source.name
    logger.info("load box: %s %s %s", who or "?", "published" if enable else "unpublished", path)
    note = ""
    connection = ckan_load.target_of(session, target.id) if target is not None else None
    if connection is not None:  # its CKAN resource follows: out when unpublished (it would link to a dead address), back when published again
        had = bool(connection.resource_id)
        warning = ckan_load.release(session, connection) if not enable else ckan_load.restore(session, source, target, connection)
        session.commit()
        note = " " + warning if warning else (" Its CKAN resource was removed." if had and not enable else (" Its CKAN resource is back." if enable and connection.resource_id else ""))
    return _go("%s is %s at %s.%s" % (source.title if target is None or target.name == transform_runner.DEFAULT_PIPELINE else "%s / %s" % (source.title, target.name),
                                      "published" if enable else "no longer published", path, note))


def _api_source(session: Session, source_id: str):
    source = session.get(ProviderSource, source_id)
    return source if source is not None and source.transport_type == "http" else None


def _request_base(source_id: str, pipeline: str) -> str:
    return "/load/sources/%s%s" % (source_id, "" if pipeline == transform_runner.DEFAULT_PIPELINE else "/pipelines/%s" % pipeline)


@router.get("/sources/{source_id}/request", response_class=HTMLResponse)
@router.get("/sources/{source_id}/pipelines/{pipeline}/request", response_class=HTMLResponse)
def request_page(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session)):
    """How callers ask a pipeline's published address, and the template that turns their call into the provider's."""
    source = _api_source(session, source_id)
    if source is None:
        return _go("Only an API source is asked on demand, so only it has a request.", "error")
    found = transform_runner.get_pipeline(session, source_id, pipeline)
    if found is None and pipeline != transform_runner.DEFAULT_PIPELINE:
        return _go("%s has no pipeline called %r." % (source.title, pipeline), "error")
    return templates.TemplateResponse(request, "load/request.html", {
        "source": source, "pipeline_name": pipeline, "provider_method": source.proxy_method, "proxy_name": source.name,
        "request_action": _request_base(source_id, pipeline) + "/request",
        "address": publishing.address_of(source, found) if found is not None else "/data/%s" % source.name,
        "request_swap": bool(found and found.request_swap), "request_template": (found.request_template if found else "") or "",
        "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
    })


@router.post("/sources/{source_id}/request")
@router.post("/sources/{source_id}/pipelines/{pipeline}/request")
async def set_request(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session)):
    """Callers use the other method than the provider, and the template turns their call into the provider's (see request_template.py).
    The provider's own method is the source's setting (Extract Box); this only turns it around. The setting is kept on the pipeline,
    and the default pipeline is created (empty, switched off) when the source has none yet."""
    source = _api_source(session, source_id)
    if source is None:
        return _go("Only an API source is asked on demand, so only it has a request.", "error")
    back = _request_base(source_id, pipeline) + "/request"

    def refuse(message: str) -> RedirectResponse:
        return RedirectResponse(back + "?" + urlencode({"flash": message, "flash_kind": "error"}), status_code=303)

    form = await request.form()
    swap = str(form.get("mode") or "") == "swap"
    template = str(form.get("template") or "").strip()
    try:
        if swap:
            (request_template.validate_json_template if source.proxy_method == "POST" else request_template.validate_query_template)(template)
    except request_template.RequestTemplateError as exc:
        return refuse(str(exc))
    who = admin_session.verify_session_cookie(request.cookies.get(admin_session.COOKIE_NAME, "")) or ""
    found = transform_runner.get_pipeline(session, source_id, pipeline)
    if found is None:
        if pipeline != transform_runner.DEFAULT_PIPELINE:
            return _go("%s has no pipeline called %r." % (source.title, pipeline), "error")
        found = Transform(source_id=source_id, name=pipeline, steps="[]", enabled=False, output_single=True)
        session.add(found)
    found.request_swap = swap
    found.request_template = template if swap else (template or found.request_template)
    found.updated_at, found.updated_by = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), who
    session.commit()
    logger.info("load box: %s %s the request turn-around of %s / %s", who or "?", "set" if swap else "switched off", source.name, pipeline)
    return _go("Callers now use %s on %s." % ("GET" if source.proxy_method == "POST" else "POST", publishing.address_of(source, found)) if swap
               else "Callers use the same method as the provider (%s)." % source.proxy_method)
