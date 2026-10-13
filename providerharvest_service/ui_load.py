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

from providerharvest_service import admin_session, build_info, harvest_db, load_api, nav, provider_sources, publishing
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


def _api_of(source: ProviderSource, base: str, transform=None) -> dict | None:
    """What an API source's published address does, for the Load Box: how callers ask it (GET or POST, and for GET the parameters the
    request template names), how the provider is called, the per-caller limit. None for a source that stores its data. The settings
    themselves live on the source (Extract Box) and its Transform page; this only shows them."""
    if source.transport_type != "http":
        return None
    try:
        config = json.loads(source.config or "{}")
    except ValueError:
        config = {}
    from providerharvest_service import proxy as proxy_module, request_template

    swapped = proxy_module.request_is_swapped(transform)
    method = proxy_module.consumer_method(source, transform)
    hint = ""
    if swapped and method == "GET":
        hint = "?" + "&".join("%s=..." % name for name in request_template.placeholders(transform.request_template))
    return {
        "url": "%s/data/%s%s" % (base, source.name, hint), "method": method, "turned": swapped, "provider_method": source.proxy_method,
        "per_caller": config.get("proxy_rate_limit_per_ip", 30), "only": source.proxy_only,
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
            "api": _api_of(source, base, transforms.get(source.id)),
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
    # A source that stores data publishes its transformed output, so it needs a transform; an API source answers on demand and does not.
    if enable and source.transport_type != "http" and session.scalar(select(Transform).where(Transform.source_id == source.id)) is None:
        return _go("%s has no transform yet -- build one in the Transform Box first." % source.title, "error")
    who = admin_session.verify_session_cookie(request.cookies.get(admin_session.COOKIE_NAME, "")) or ""
    publishing.set_published(session, source, enable, who)
    session.commit()
    logger.info("load box: %s %s the API of source %s", who or "?", "published" if enable else "unpublished", source.name)
    return _go("%s is %s at /data/%s." % (source.title, "published" if enable else "no longer published", source.name))
