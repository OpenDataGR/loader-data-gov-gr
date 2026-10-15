"""What the Overview draws: for each source the person may see, its pipelines as flows -- input (the source) -> transform (the steps) -> output ->
where it goes (the published address, CKAN). This module only gathers the facts, from what the other boxes already keep; the page draws them.

Who sees what: a super admin sees every source; anyone else sees the sources of the CKAN organizations they administer (a source with no organization
is seen by super admins only). Without a CKAN account, or with CKAN out of reach, a person who is not a super admin sees nothing, and is told why.
"""

from __future__ import annotations

import dataclasses
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import ckan_load, provider_sources, publishing, transform_engine as te, transform_forms as tf, transform_runner
from providerharvest_service.models import CkanTarget, ProviderSource, Transform

MAX_STEP_CHIPS = 5
MODE_LABELS = {"link": "link", "upload": "uploaded copy", "service": "data service"}


@dataclasses.dataclass
class Scope:
    """The sources a person may see, or why there are none."""
    sources: list[ProviderSource]
    everything: bool
    organizations: list[str]
    notice: str = ""


def visible_sources(session: Session, *, is_super: bool, ckan_user: str) -> Scope:
    if is_super:
        return Scope(provider_sources.list_all(session), True, [])
    if not ckan_user:
        return Scope([], False, [], "Sign in with your data.gov.gr account to see the pipelines of your organizations.")
    if not ckan_load.configured():
        return Scope([], False, [], "CKAN is not set up on this deployment, so there is no way to tell which organizations you administer.")
    try:
        organizations = ckan_load.list_admin_organizations(ckan_load.user_client(session, ckan_user), ckan_user)
    except ckan_load.CkanLoadError as exc:
        return Scope([], False, [], "CKAN could not be asked which organizations you administer (%s). Try again in a moment." % exc)
    if not organizations:
        return Scope([], False, [], "You do not administer any organization in CKAN.")
    ids = {o.id for o in organizations}
    mine = [s for s in provider_sources.list_all(session) if s.organization_id in ids]
    return Scope(mine, False, [o.title for o in organizations])


def _steps(pipeline: Transform) -> list[dict]:
    steps = transform_runner.steps_of(pipeline)
    chips = [{"label": tf.label_of(s), "what": tf.summarize(s)} for s in steps[:MAX_STEP_CHIPS]]
    if len(steps) > MAX_STEP_CHIPS:
        chips.append({"label": "+%d more" % (len(steps) - MAX_STEP_CHIPS), "what": ""})
    return chips


def _ckan(target: CkanTarget | None, published: bool) -> dict | None:
    if target is None:
        return None
    if target.last_status == "error":
        state, text = "error", target.last_error or "failed"
    elif target.resource_id:
        state, text = "ok", "in CKAN"
    else:
        state, text = "waiting", "not in CKAN"
    return {"mode": target.mode, "mode_label": MODE_LABELS.get(target.mode, target.mode), "title": target.dataset_title or target.dataset_name or "", "state": state,
            "text": text, "organization": target.organization_title or ""}


def _pipeline_flow(session: Session, hsession: Session, source: ProviderSource, pipeline: Transform, target: CkanTarget | None, base: str) -> dict:
    counts = transform_runner.output_counts(hsession, source.id, pipeline.id)
    published = publishing.is_pipeline_published(pipeline)
    path = publishing.address_of(source, pipeline)
    base_path = "/transform/sources/%s%s" % (source.id, "" if pipeline.name == transform_runner.DEFAULT_PIPELINE else "/pipelines/%s" % pipeline.name)
    load_path = "/load/sources/%s%s" % (source.id, "" if pipeline.name == transform_runner.DEFAULT_PIPELINE else "/pipelines/%s" % pipeline.name)
    api = source.transport_type == "http"
    live = bool(pipeline.enabled or (api and published))
    return {
        "name": pipeline.name, "enabled": bool(pipeline.enabled), "steps": _steps(pipeline), "step_count": len(transform_runner.steps_of(pipeline)),
        "output": te.OUTPUTS.get(pipeline.output_format or "json", (None, None, pipeline.output_format))[2].split(" (")[0],
        "request": tf.request_summary(source.proxy_method, pipeline) if api else "",
        "turned": bool(api and pipeline.request_swap and (pipeline.request_template or "").strip()),
        "ok": counts["ok"], "failed": counts["failed"], "latest": counts["latest"],
        "published": published, "path": path, "url": base + path, "ckan": _ckan(target, published), "ckan_link": load_path + "/ckan",
        "transform_url": base_path, "output_url": base_path + "#outputs", "load_url": "/load", "live": live,
    }


def build(session: Session, hsession: Session, scope: Scope, base_url: str = "") -> list[dict]:
    """The flows, grouped by organization (those with none last), each source with its pipelines."""
    base = (base_url or os.environ.get("LOADER_PUBLIC_BASE_URL") or "").rstrip("/")
    targets = {t.pipeline_id: t for t in session.scalars(select(CkanTarget)).all()}
    flows = []
    for source in scope.sources:
        pipelines = transform_runner.list_pipelines(session, source.id)
        items = [_pipeline_flow(session, hsession, source, p, targets.get(p.id), base) for p in pipelines]
        failing = any(i["failed"] for i in items) or any(i["ckan"] and i["ckan"]["state"] == "error" for i in items)
        unhealthy = source.last_check_status in ("error", "warn") if getattr(source, "last_check_status", None) else False
        if source.status != "active":
            flag = source.status  # pending / paused / rejected
        elif failing or unhealthy:
            flag = "attention"
        elif any(i["live"] for i in items):
            flag = "running"
        else:
            flag = "idle"
        flows.append({
            "source": source, "organization": source.organization_title or "", "pipelines": items, "flag": flag,
            "edit_url": "/provider/sources/%s/edit" % source.id, "runs_url": "/provider/sources/%s/runs" % source.id,
            "setup_url": "/transform/sources/%s" % source.id,
            "text": " ".join([source.title, source.name, source.organization_title or ""] + [i["name"] for i in items] + [i["ckan"]["title"] for i in items if i["ckan"]]).lower(),
        })
    flows.sort(key=lambda f: (f["organization"] == "", f["organization"].lower(), f["source"].title.lower()))
    return flows


def group_by_organization(flows: list[dict]) -> list[dict]:
    groups: list[dict] = []
    for flow in flows:
        title = flow["organization"] or "No organization"
        if not groups or groups[-1]["title"] != title:
            groups.append({"title": title, "assigned": bool(flow["organization"]), "flows": []})
        groups[-1]["flows"].append(flow)
    return groups
