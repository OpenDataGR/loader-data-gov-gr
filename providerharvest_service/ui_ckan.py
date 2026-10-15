"""The Load Box's CKAN pages: connect a published pipeline to a dataset in CKAN, push it, disconnect it. Behind the site-wide login
(admin_session.py). Everything is done *as the signed-in person* in CKAN (their ``user_<sub>`` account, see ckan_load), so CKAN's own
permissions decide; a session without a CKAN identity (the shared admin password) cannot use these pages."""

from __future__ import annotations

import dataclasses
import datetime
import logging
import os
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import build_info, ckan_access, ckan_load, harvest_db, load_api, nav, publishing, transform_runner
from providerharvest_service.db import get_session
from providerharvest_service.models import CkanTarget, ProviderSource, Transform

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/load", tags=["load-ckan-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now
templates.env.globals["pending_approvals"] = nav.pending_approvals
templates.env.filters["utc"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else ""
templates.env.filters["short_utc"] = lambda value: value.strftime("%m-%d %H:%M") if value else ""


def _identity(request: Request) -> str:
    """The CKAN user the signed-in person is, or '' (the shared password has none)."""
    user = getattr(request.state, "user", None) or {}
    sub = str(user.get("sub") or "")
    return ckan_access.ckan_username(sub) if sub else ""


def _who(request: Request) -> str:
    user = getattr(request.state, "user", None) or {}
    return str(user.get("username") or "")


def _base(source_id: str, pipeline: str) -> str:
    return "/load/sources/%s%s" % (source_id, "" if pipeline == transform_runner.DEFAULT_PIPELINE else "/pipelines/%s" % pipeline)


def _go(path: str, flash: str, kind: str = "success") -> RedirectResponse:
    return RedirectResponse("%s?%s" % (path, urlencode({"flash": flash, "flash_kind": kind})), status_code=303)


def _problem_with_identity(request: Request) -> str:
    if not ckan_load.configured():
        return "CKAN is not set up on this deployment."
    if not _identity(request):
        return "Sign in with your data.gov.gr account to use CKAN: this session has no CKAN identity (the shared admin password has none)."
    return ""


def _found(session: Session, source_id: str, pipeline_name: str):
    source = session.get(ProviderSource, source_id)
    pipeline = transform_runner.get_pipeline(session, source_id, pipeline_name) if source is not None else None
    return source, pipeline


def _target(session: Session, pipeline: Transform | None) -> CkanTarget | None:
    return session.scalar(select(CkanTarget).where(CkanTarget.pipeline_id == pipeline.id)) if pipeline is not None else None


def _ckan_dataset_url(name: str) -> str:
    return "%s/dataset/%s" % (os.environ.get("CKAN_BASE_URL", "").rstrip("/"), name) if name else ""


@router.get("/ckan/datasets")
def find_datasets(request: Request, q: str = "", session: Session = Depends(get_session)):
    """The datasets the signed-in person may add a resource to, for the search box on the connect page (JSON)."""
    problem = _problem_with_identity(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=403)
    ident = _identity(request)
    try:
        found = ckan_load.search_datasets(ckan_load.user_client(session, ident), ident, q)
    except ckan_load.CkanLoadError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"datasets": [dataclasses.asdict(d) for d in found]})


@router.get("/sources/{source_id}/ckan", response_class=HTMLResponse)
@router.get("/sources/{source_id}/pipelines/{pipeline}/ckan", response_class=HTMLResponse)
def ckan_page(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session),
              hsession: Session = Depends(harvest_db.get_session)):
    source, found = _found(session, source_id, pipeline)
    if source is None or found is None:
        return _go("/load", "No such pipeline.", "error")
    target = _target(session, found)
    base = ckan_load.public_base_url()
    newest = load_api.latest_output(hsession, source.id, found.id) if source.transport_type != "http" else None
    return templates.TemplateResponse(request, "load/ckan.html", {
        "source": source, "pipeline": found, "target": target, "base": _base(source_id, pipeline), "problem": _problem_with_identity(request),
        "link": ckan_load.link_url(source, found, base), "has_public_base": bool(base), "published": publishing.is_pipeline_published(found),
        "is_http": source.transport_type == "http", "newest": newest, "modes": ckan_load.MODES,
        "default_name": ("%s / %s" % (source.title, found.name)) if found.name != transform_runner.DEFAULT_PIPELINE else source.title,
        "dataset_url": _ckan_dataset_url(target.dataset_name) if target else "",
        "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
    })


def _push_now(session: Session, hsession: Session, target: CkanTarget, source: ProviderSource, pipeline: Transform, ident: str):
    client = ckan_load.user_client(session, ident)
    output = None
    if target.mode == "upload":
        output = load_api.latest_output(hsession, source.id, pipeline.id)
    ckan_load.push(target, source, pipeline, client, output=output)


@router.post("/sources/{source_id}/ckan")
@router.post("/sources/{source_id}/pipelines/{pipeline}/ckan")
async def ckan_save(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session),
                    hsession: Session = Depends(harvest_db.get_session)):
    """Connect the pipeline to a dataset (or change how/where) and push it."""
    source, found = _found(session, source_id, pipeline)
    back = _base(source_id, pipeline) + "/ckan"
    if source is None or found is None:
        return _go("/load", "No such pipeline.", "error")
    problem = _problem_with_identity(request)
    if problem:
        return _go(back, problem, "error")
    form = await request.form()
    mode = str(form.get("mode") or "link")
    dataset_id = str(form.get("dataset_id") or "").strip()
    name = str(form.get("resource_name") or "").strip()[:200]
    if mode not in ckan_load.MODES:
        return _go(back, "Choose link or upload.", "error")
    if mode == "upload" and source.transport_type == "http":
        return _go(back, "An API source stores nothing, so there is no file to upload: use a link.", "error")
    if not dataset_id:
        return _go(back, "Choose the CKAN dataset first (search for it, then click it).", "error")
    if not name:
        return _go(back, "Give the resource a name.", "error")
    ident = _identity(request)
    try:
        dataset = ckan_load.get_dataset(ckan_load.user_client(session, ident), dataset_id)
    except ckan_load.CkanLoadError as exc:
        return _go(back, str(exc), "error")
    target = _target(session, found)
    if target is not None and target.resource_id and target.dataset_id != dataset.id:
        # moving to another dataset: the resource in the old one goes first, so the pipeline is never listed in two places
        try:
            ckan_load.remove(target, ckan_load.user_client(session, ident))
        except ckan_load.CkanLoadError as exc:
            return _go(back, "The resource in the old dataset could not be removed: %s" % exc, "error")
    if target is None:
        target = CkanTarget(source_id=source_id, pipeline_id=found.id)
        session.add(target)
    target.mode, target.dataset_id, target.dataset_name, target.dataset_title = mode, dataset.id, dataset.name, dataset.title
    target.resource_name, target.description = name, str(form.get("description") or "").strip()[:1000]
    target.ckan_user, target.connected_by = ident, _who(request)
    try:
        _push_now(session, hsession, target, source, found, ident)
    except ckan_load.CkanLoadError as exc:
        session.commit()  # the connection is kept, with the reason, so it can be fixed and pushed again
        return _go(back, "Saved, but not pushed: %s" % exc, "error")
    session.commit()
    logger.info("ckan: %s connected %s / %s to dataset %s (%s)", _who(request) or "?", source.name, found.name, dataset.name, mode)
    return _go(back, "%s is now a resource of %s in CKAN." % (name, dataset.title))


@router.post("/sources/{source_id}/ckan/push")
@router.post("/sources/{source_id}/pipelines/{pipeline}/ckan/push")
async def ckan_push(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session),
                    hsession: Session = Depends(harvest_db.get_session)):
    source, found = _found(session, source_id, pipeline)
    back = _base(source_id, pipeline) + "/ckan"
    target = _target(session, found)
    if source is None or found is None or target is None:
        return _go(back, "This pipeline is not connected to CKAN.", "error")
    problem = _problem_with_identity(request)
    if problem:
        return _go(back, problem, "error")
    try:
        _push_now(session, hsession, target, source, found, _identity(request))
    except ckan_load.CkanLoadError as exc:
        session.commit()
        return _go(back, str(exc), "error")
    session.commit()
    return _go(back, "Pushed to CKAN.")


@router.post("/sources/{source_id}/ckan/disconnect")
@router.post("/sources/{source_id}/pipelines/{pipeline}/ckan/disconnect")
async def ckan_disconnect(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session)):
    """Take the resource out of CKAN and forget the connection."""
    source, found = _found(session, source_id, pipeline)
    back = _base(source_id, pipeline) + "/ckan"
    target = _target(session, found)
    if source is None or found is None or target is None:
        return _go(back, "This pipeline is not connected to CKAN.", "error")
    problem = _problem_with_identity(request)
    if problem:
        return _go(back, problem, "error")
    try:
        ckan_load.remove(target, ckan_load.user_client(session, _identity(request)))
    except ckan_load.CkanLoadError as exc:
        return _go(back, "The resource could not be removed from CKAN, so it is still connected: %s" % exc, "error")
    session.delete(target)
    session.commit()
    return _go("/load", "Disconnected: the resource was removed from CKAN.")
