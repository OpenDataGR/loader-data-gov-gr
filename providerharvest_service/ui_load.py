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

from providerharvest_service import admin_session, build_info, harvest_db, load_api, nav, provider_sources
from providerharvest_service.db import get_session
from providerharvest_service.models import LoadTarget, ProviderSource, Transform

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


def _proxy_of(source: ProviderSource, base: str, transform=None) -> dict | None:
    """The live proxy as a destination: the source's address through the proxy, how it calls the provider, and whether it
    answers right now. None for a source that has no live proxy. The settings themselves live on the source (Extract Box)."""
    if source.transport_type != "http" or source.proxy_enabled is False:
        return None
    try:
        config = json.loads(source.config or "{}")
    except ValueError:
        config = {}
    from providerharvest_service import proxy as proxy_module, request_template

    swapped = proxy_module.request_is_swapped(transform)
    method = proxy_module.consumer_method(source, transform)
    # the call as a consumer makes it: for GET the parameters the template names, e.g. ?plate=...
    hint = ""
    if swapped and method == "GET":
        hint = "?" + "&".join("%s=..." % name for name in request_template.placeholders(transform.request_template))
    return {
        "url": "%s/proxy/%s%s" % (base, source.name, hint), "method": method, "live": source.status == "active", "turned": swapped,
        "provider_method": source.proxy_method,
        "status": source.status, "per_caller": config.get("proxy_rate_limit_per_ip", 30), "only": source.proxy_only,
    }


@router.get("", response_class=HTMLResponse)
def overview(request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    targets = {t.source_id: t for t in session.scalars(select(LoadTarget)).all()}
    transforms = {t.source_id: t for t in session.scalars(select(Transform)).all()}
    # the configured public address: behind the gateway the request itself says "http" (see ui_provider._push_upload_url)
    base = (os.environ.get("LOADER_PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")
    rows = []
    for source in provider_sources.list_all(session):
        target = targets.get(source.id)
        rows.append({
            "source": source, "transform": transforms.get(source.id), "published": bool(target and target.api_enabled),
            "latest": load_api.latest_output(hsession, source.id) if transforms.get(source.id) else None,
            "url": "%s/data/%s" % (base, source.name),
            "proxy": _proxy_of(source, base, transforms.get(source.id)),
        })
    return templates.TemplateResponse(request, "load/overview.html", {
        "rows": rows, "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
    })


@router.post("/sources/{source_id}/api")
async def set_api(source_id: str, request: Request, session: Session = Depends(get_session)):
    source = session.get(ProviderSource, source_id)
    if source is None:
        return _go("No such source.", "error")
    form = await request.form()
    enable = str(form.get("enabled") or "") == "1"
    if enable and source.status != "active":
        return _go("%s is not active yet, so it cannot be published." % source.title, "error")
    if enable and session.scalar(select(Transform).where(Transform.source_id == source.id)) is None:
        return _go("%s has no transform yet -- build one in the Transform Box first." % source.title, "error")
    who = admin_session.verify_session_cookie(request.cookies.get(admin_session.COOKIE_NAME, "")) or ""
    target = session.scalar(select(LoadTarget).where(LoadTarget.source_id == source.id))
    if target is None:
        target = LoadTarget(source_id=source.id)
        session.add(target)
    target.api_enabled = enable
    target.updated_by = who
    target.updated_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    session.commit()
    logger.info("load box: %s %s the API of source %s", who or "?", "published" if enable else "unpublished", source.name)
    return _go("%s is %s at /data/%s." % (source.title, "published" if enable else "no longer published", source.name))
