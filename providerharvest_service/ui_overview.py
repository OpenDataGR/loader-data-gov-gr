"""The Overview tab: every pipeline the signed-in person may see, drawn as a flow -- input, transform, output, where it goes (see ``overview.py``).
Behind the site-wide login (admin_session.py)."""

from __future__ import annotations

import datetime
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from providerharvest_service import build_info, harvest_db, nav, overview, ui_provider
from providerharvest_service.db import get_session

router = APIRouter(tags=["overview-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now
templates.env.globals["pending_approvals"] = nav.pending_approvals
templates.env.filters["utc"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else ""
templates.env.filters["short_utc"] = lambda value: value.strftime("%m-%d %H:%M") if value else ""


@router.get("/overview", response_class=HTMLResponse)
def overview_page(request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    is_super, ckan_user = ui_provider._signed_in(request)
    scope = overview.visible_sources(session, is_super=is_super, ckan_user=ckan_user)
    base = (os.environ.get("LOADER_PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")
    flows = overview.build(session, hsession, scope, base)
    groups = overview.group_by_organization(flows)
    return templates.TemplateResponse(request, "overview/flows.html", {
        "scope": scope, "groups": groups, "flows": flows, "is_super": is_super,
        "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
        "pipeline_count": sum(len(f["pipelines"]) for f in flows),
        "flags": {"running": sum(1 for f in flows if f["flag"] == "running"), "attention": sum(1 for f in flows if f["flag"] == "attention"),
                  "paused": sum(1 for f in flows if f["flag"] in ("paused", "pending", "rejected")), "idle": sum(1 for f in flows if f["flag"] == "idle")},
    })
