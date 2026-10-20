"""The Load Box's CKAN pages: connect a published pipeline to a dataset in CKAN, push it, disconnect it. Behind the site-wide login
(admin_session.py). Everything is done *as the signed-in person* in CKAN (their ``user_<sub>`` account, see ckan_load), so CKAN's own
permissions decide; a session without a CKAN identity (the shared admin password) cannot use these pages."""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import build_info, ckan_access, ckan_datastore, ckan_load, ckan_vocab, harvest_db, load_api, nav, publishing, transform_runner
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


def _transform_base(source_id: str, pipeline: str) -> str:
    return "/transform/sources/%s%s" % (source_id, "" if pipeline == transform_runner.DEFAULT_PIPELINE else "/pipelines/%s" % pipeline)


def _go(path: str, flash: str, kind: str = "success") -> RedirectResponse:
    return RedirectResponse("%s%s%s" % (path, "&" if "?" in path else "?", urlencode({"flash": flash, "flash_kind": kind})), status_code=303)


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


def _loads(session: Session, pipeline: Transform | None) -> list[CkanTarget]:
    return ckan_load.targets_of(session, pipeline.id) if pipeline is not None else []


def _target(session: Session, pipeline: Transform | None, target_id: str = "") -> CkanTarget | None:
    """One of the pipeline's loads: the one asked for, else the first (a pipeline may have several, each its own line in the Load Box)."""
    loads = _loads(session, pipeline)
    if target_id:
        return next((t for t in loads if t.id == target_id), None)
    return loads[0] if loads else None


MODE_TEXT = {"link": "a link to the published address", "upload": "an uploaded copy of the newest output", "datastore": "rows in a DataStore table",
             "service": "as a data service"}


def _ckan_dataset_url(name: str, mode: str = "link") -> str:
    """Where the person can look at it on the portal: a dataset page, or (a data service) the data-service page."""
    return "%s/%s/%s" % (os.environ.get("CKAN_BASE_URL", "").rstrip("/"), "data-service" if mode == "service" else "dataset", name) if name else ""


def _lines(value: str) -> list[str]:
    return [v.strip() for v in str(value or "").replace("\r", "").split("\n") if v.strip()]


def _meta_from_form(form) -> dict:
    """The details of a data service as the form sent them (checked later by ckan_load.normalise_meta)."""
    many = lambda name: [str(v) for v in form.getlist(name)]  # noqa: E731
    licence = str(form.get("svc_license") or "")
    if licence == "__other__":
        licence = str(form.get("svc_license_other") or "").strip()
    return {
        "public": str(form.get("svc_public") or "") == "public", "title": form.get("svc_title"), "description": form.get("svc_description"), "endpoint_description": form.get("svc_endpoint_description"),
        "license": licence, "access_rights": form.get("svc_access_rights"), "themes": many("svc_themes"),
        "legislation": many("svc_legislation") + _lines(form.get("svc_legislation_other")), "hvd_categories": many("svc_hvd"),
        "contact_name": form.get("svc_contact_name"), "contact_email": form.get("svc_contact_email"), "publisher_name": form.get("svc_publisher_name"),
        "publisher_email": form.get("svc_publisher_email"), "publisher_identifier": form.get("svc_publisher_identifier"),
        "publisher_type": form.get("svc_publisher_type"), "documentation": _lines(form.get("svc_documentation")),
        "landing_page": _lines(form.get("svc_landing_page")), "rights": form.get("svc_rights"), "tags": form.get("svc_tags"),
    }


@router.get("/ckan/organizations")
def find_organizations(request: Request, session: Session = Depends(get_session)):
    """The organizations the signed-in person may add datasets to, for the first step of the connect page (JSON)."""
    problem = _problem_with_identity(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=403)
    ident = _identity(request)
    try:
        found = ckan_load.list_organizations(ckan_load.user_client(session, ident), ident)
    except ckan_load.CkanLoadError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"organizations": [dataclasses.asdict(o) for o in found]})


@router.get("/ckan/datasets")
def find_datasets(request: Request, q: str = "", org: str = "", session: Session = Depends(get_session)):
    """The datasets of one organization (or of all the person's) the signed-in person may add a resource to, for the second step (JSON)."""
    problem = _problem_with_identity(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=403)
    ident = _identity(request)
    try:
        found = ckan_load.search_datasets(ckan_load.user_client(session, ident), ident, q, organization_id=org or None)
    except ckan_load.CkanLoadError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"datasets": [dataclasses.asdict(d) for d in found]})


@router.get("/ckan/service-defaults")
def service_defaults(request: Request, org: str = "", session: Session = Depends(get_session)):
    """What this organization's data services last used (licence, themes, contact ...), for the form to start from (JSON)."""
    problem = _problem_with_identity(request)
    if problem:
        return JSONResponse({"error": problem}, status_code=403)
    values = ckan_load.get_defaults(session, org) if org else dict(ckan_load.META_DEFAULTS)
    return JSONResponse({"defaults": {k: values[k] for k in ckan_load.DEFAULT_KEYS}})


@router.get("/sources/{source_id}/ckan", response_class=HTMLResponse)
@router.get("/sources/{source_id}/pipelines/{pipeline}/ckan", response_class=HTMLResponse)
def ckan_page(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session),
              hsession: Session = Depends(harvest_db.get_session)):
    source, found = _found(session, source_id, pipeline)
    if source is None or found is None:
        return _go("/load", "No such pipeline.", "error")
    loads = _loads(session, found)
    wanted = request.query_params.get("load", "")
    target = None if wanted == "new" else _target(session, found, wanted)
    if wanted and wanted != "new" and target is None:
        return _go(_base(source_id, pipeline) + "/ckan", "No such load.", "error")
    base = ckan_load.public_base_url()
    newest = load_api.latest_output(hsession, source.id, found.id) if source.transport_type != "http" else None
    default_name = ("%s / %s" % (source.title, found.name)) if found.name != transform_runner.DEFAULT_PIPELINE else source.title
    # the details of a data service: those of the connection, else what the organization's services last used, over the built-in defaults
    try:
        meta = json.loads(target.service_meta) if target is not None and target.service_meta else None
    except ValueError:
        meta = None
    if meta is None:
        meta = ckan_load.get_defaults(session, target.organization_id) if target is not None and target.organization_id else dict(ckan_load.META_DEFAULTS)
        meta["title"] = default_name
        meta["endpoint_description"] = ckan_load.describe_endpoint(source, found, ckan_load.link_url(source, found, base))
    private = None
    if target is not None and not _problem_with_identity(request):
        try:
            private = ckan_load.is_private(ckan_load.user_client(session, _identity(request)), target)
        except ckan_load.CkanLoadError:
            private = None
    format_code = ckan_load.service_format(source, found)
    meta["format"] = format_code
    mode = target.mode if target is not None else ("service" if source.transport_type == "http" else "link")
    datastore_cfg = ckan_datastore.config_of(target.datastore_config if target is not None else None)
    known_license = ckan_vocab.is_known("license", str(meta.get("license") or ""))
    return templates.TemplateResponse(request, "load/ckan.html", {
        "source": source, "pipeline": found, "target": target, "base": _base(source_id, pipeline), "problem": _problem_with_identity(request),
        "link": ckan_load.link_url(source, found, base), "has_public_base": bool(base), "published": publishing.is_pipeline_published(found),
        "is_http": source.transport_type == "http", "newest": newest, "modes": ckan_load.MODES, "mode": mode, "loads": loads, "mode_text": MODE_TEXT,
        "private": private, "datastore_cfg": datastore_cfg, "columns": ckan_datastore.columns_of(newest), "creating": wanted == "new", "retention_choices": ckan_datastore.RETENTION_CHOICES,
        "default_name": default_name, "meta": meta, "known_license": known_license, "vocab": ckan_vocab,
        "format_label": dict(ckan_vocab.FORMATS)[format_code], "output_url": "%s/steps/output" % _transform_base(source_id, pipeline),
        "legislation_known": [u for u in meta.get("legislation", []) if any(u == k for k, _ in ckan_vocab.LEGISLATION)],
        "legislation_other": [u for u in meta.get("legislation", []) if not any(u == k for k, _ in ckan_vocab.LEGISLATION)],
        "fill_defaults": not (target is not None and target.service_meta),
        "dataset_url": _ckan_dataset_url(target.dataset_name, target.mode) if target else "",
        "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
    })


def _push_now(session: Session, hsession: Session, target: CkanTarget, source: ProviderSource, pipeline: Transform, ident: str):
    client = ckan_load.user_client(session, ident)
    output = None
    if target.mode in ("upload", "datastore"):
        output = load_api.latest_output(hsession, source.id, pipeline.id)
    ckan_load.push(target, source, pipeline, client, output=output)


@router.post("/sources/{source_id}/ckan")
@router.post("/sources/{source_id}/pipelines/{pipeline}/ckan")
async def ckan_save(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session),
                    hsession: Session = Depends(harvest_db.get_session)):
    """Connect the pipeline to CKAN -- as a resource in a dataset (a link, or an uploaded copy), or as a data service of an organization -- or
    change how or where, and push it."""
    source, found = _found(session, source_id, pipeline)
    back = _base(source_id, pipeline) + "/ckan"
    if source is None or found is None:
        return _go("/load", "No such pipeline.", "error")
    problem = _problem_with_identity(request)
    if problem:
        return _go(back, problem, "error")
    form = await request.form()
    mode = str(form.get("mode") or "link")
    adding = "target_id" in form and not str(form.get("target_id") or "")  # (a form that carries the field empty adds another load)
    organization_id = str(form.get("organization_id") or "").strip()
    dataset_id = str(form.get("dataset_id") or "").strip()
    name = str(form.get("resource_name") or "").strip()[:200]
    if mode not in ckan_load.MODES:
        return _go(back, "Choose link, upload, DataStore or data service.", "error")
    if mode in ("upload", "datastore") and source.transport_type == "http":
        return _go(back, "An API source stores nothing, so there is no output to %s: use a link or a data service." % ("upload" if mode == "upload" else "load"), "error")
    datastore_cfg = None
    if mode == "datastore":
        try:
            try:
                retention = int(str(form.get("ds_retention") or "0") or 0)
            except ValueError:
                retention = -1
            datastore_cfg = ckan_datastore.check_config(str(form.get("ds_refresh") or ""), [str(k) for k in form.getlist("ds_keys")], retention,
                                                        bool(form.get("ds_archive")), str(form.get("ds_time") or ""))
        except ckan_datastore.DatastoreError as exc:
            return _go(back, str(exc), "error")
    if not organization_id:
        return _go(back, "Choose the organization first.", "error")
    meta = None
    new_dataset = mode != "service" and str(form.get("dataset_mode") or "existing") == "new"
    if mode == "service" or new_dataset:
        try:
            meta = ckan_load.normalise_meta(_meta_from_form(form))
        except ckan_load.CkanLoadError as exc:
            return _go(back, str(exc).replace("the service", "the dataset") if new_dataset else str(exc), "error")
    if mode != "service":
        if not dataset_id and not new_dataset:
            return _go(back, "Choose the dataset first (it is listed once the organization is chosen; click it).", "error")
        if not name:
            return _go(back, "Give the resource a name.", "error")
    ident = _identity(request)
    try:
        client = ckan_load.user_client(session, ident)
        organization = next((o for o in ckan_load.list_organizations(client, ident) if o.id == organization_id), None)
        if organization is None:
            return _go(back, "You may not add datasets to that organization.", "error")
        dataset = ckan_load.get_dataset(client, dataset_id) if mode != "service" and not new_dataset else None
    except ckan_load.CkanLoadError as exc:
        return _go(back, str(exc), "error")
    if dataset is not None and dataset.owner_org != organization.id:
        return _go(back, "%s is not in %s." % (dataset.title, organization.title), "error")
    if new_dataset:
        try:
            dataset = ckan_load.create_dataset(client, meta, organization.id, source, found, public=meta["public"])
        except ckan_load.CkanLoadError as exc:
            return _go(back, "The dataset could not be created: %s" % exc, "error")
    chosen = str(form.get("target_id") or "")
    target = None if adding else _target(session, found, chosen)
    if chosen and target is None:
        return _go(back, "No such load.", "error")
    moving = target is not None and bool(target.resource_id) and (
        target.mode != mode or (mode == "service" and target.organization_id != organization.id) or (dataset is not None and target.dataset_id != dataset.id))
    if moving:
        # to another dataset, another organization or another way: what is in CKAN goes first, so the pipeline is never listed in two places
        try:
            ckan_load.remove(target, client)
        except ckan_load.CkanLoadError as exc:
            return _go(back, "What was in CKAN before could not be removed: %s" % exc, "error")
    if target is None:
        target = CkanTarget(source_id=source_id, pipeline_id=found.id, dataset_id="")
        session.add(target)
    target.mode = mode
    target.organization_id, target.organization_title = organization.id, organization.title
    if mode == "service":
        target.service_meta = json.dumps(meta, ensure_ascii=False)
        target.resource_name, target.description = meta["title"], ""
        target.dataset_title = meta["title"]
        label = "%s is now a data service in CKAN%s." % (meta["title"], "" if target.resource_id else " (%s)" % ("public" if meta["public"] else "private, until you make it public"))
    else:
        target.dataset_id, target.dataset_name, target.dataset_title = dataset.id, dataset.name, dataset.title
        target.resource_name, target.description = name, str(form.get("description") or "").strip()[:1000]
        if datastore_cfg is not None:
            old = ckan_datastore.config_of(target.datastore_config)
            same_way = old["refresh"] == datastore_cfg["refresh"] and old["keys"] == datastore_cfg["keys"]
            if not same_way:
                target.last_output_id = None  # (a changed way of loading starts from the newest output again)
            if same_way and old.get("fields"):
                datastore_cfg["fields"], datastore_cfg["created"] = old["fields"], old.get("created", "")
            for key in ("pruned_at", "prune_note", "archived"):  # (what pruning has done so far is kept)
                if key in old:
                    datastore_cfg[key] = old[key]
            target.datastore_config = json.dumps(datastore_cfg, ensure_ascii=False)
        label = "%s is now a resource of %s in CKAN." % (name, dataset.title) if not new_dataset else "%s was created in CKAN (%s) with %s as its resource." % (
            dataset.title, "public" if meta["public"] else "private, until you make it public", name)
    target.ckan_user, target.connected_by = ident, _who(request)
    address_note = ""
    if mode in ("link", "service"):  # (they point at the pipeline's public address: it is added with them)
        try:
            if publishing.ensure_published(session, source, found, _who(request)):
                address_note = " The public address %s was added too." % publishing.address_of(source, found)
        except ValueError as exc:
            session.commit()
            return _go("%s?load=%s" % (back, target.id), "Saved, but not pushed: %s" % exc, "error")
    try:
        _push_now(session, hsession, target, source, found, ident)
    except ckan_load.CkanLoadError as exc:
        session.commit()  # the connection is kept, with the reason, so it can be fixed and pushed again
        return _go("%s?load=%s" % (back, target.id), "Saved, but not pushed: %s" % exc, "error")
    if meta is not None:  # (a data service's details, or those of a dataset made now: the next one of this organization starts from them)
        ckan_load.save_defaults(session, organization.id, meta, _who(request))  # the next service of this organization starts from these
    session.commit()
    logger.info("ckan: %s connected %s / %s as %s (%s)", _who(request) or "?", source.name, found.name, mode, organization.title)
    return _go("%s?load=%s" % (back, target.id), label + address_note)


@router.post("/sources/{source_id}/ckan/push")
@router.post("/sources/{source_id}/pipelines/{pipeline}/ckan/push")
async def ckan_push(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session),
                    hsession: Session = Depends(harvest_db.get_session)):
    source, found = _found(session, source_id, pipeline)
    back = _base(source_id, pipeline) + "/ckan"
    form = await request.form()
    target = _target(session, found, str(form.get("target_id") or ""))
    if source is None or found is None or target is None:
        return _go(back, "This pipeline is not connected to CKAN.", "error")
    back = "%s?load=%s" % (back, target.id)
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


@router.post("/sources/{source_id}/ckan/visibility")
@router.post("/sources/{source_id}/pipelines/{pipeline}/ckan/visibility")
async def ckan_visibility(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session)):
    """Make the dataset (or data service) of one output public, or private again -- as the signed-in person, so CKAN's own permissions decide."""
    source, found = _found(session, source_id, pipeline)
    back = _base(source_id, pipeline) + "/ckan"
    form = await request.form()
    target = _target(session, found, str(form.get("target_id") or ""))
    if source is None or found is None or target is None:
        return _go(back, "This pipeline is not connected to CKAN.", "error")
    back = "%s?load=%s" % (back, target.id)
    problem = _problem_with_identity(request)
    if problem:
        return _go(back, problem, "error")
    private = str(form.get("private") or "") == "1"
    try:
        ckan_load.set_private(ckan_load.user_client(session, _identity(request)), target, private)
    except ckan_load.CkanLoadError as exc:
        return _go(back, "The visibility could not be changed: %s" % exc, "error")
    logger.info("ckan: %s made %s / %s %s", _who(request) or "?", source.name, found.name, "private" if private else "public")
    return _go(back, "%s is now %s in CKAN." % (target.dataset_title or target.dataset_name or "It", "private (only the organization's members can see it)" if private
                                                else "public (anyone can see it)"))


@router.post("/sources/{source_id}/ckan/disconnect")
@router.post("/sources/{source_id}/pipelines/{pipeline}/ckan/disconnect")
async def ckan_disconnect(source_id: str, request: Request, pipeline: str = transform_runner.DEFAULT_PIPELINE, session: Session = Depends(get_session)):
    """Take the resource out of CKAN and forget the connection."""
    source, found = _found(session, source_id, pipeline)
    back = _base(source_id, pipeline) + "/ckan"
    form = await request.form()
    target = _target(session, found, str(form.get("target_id") or ""))
    if source is None or found is None or target is None:
        return _go(back, "This pipeline is not connected to CKAN.", "error")
    back = "%s?load=%s" % (back, target.id)
    problem = _problem_with_identity(request)
    if problem:
        return _go(back, problem, "error")
    try:
        ckan_load.remove(target, ckan_load.user_client(session, _identity(request)))
    except ckan_load.CkanLoadError as exc:
        return _go(back, "The resource could not be removed from CKAN, so it is still connected: %s" % exc, "error")
    session.delete(target)
    session.commit()
    return _go("/load", "Disconnected: the load was removed from CKAN.")
