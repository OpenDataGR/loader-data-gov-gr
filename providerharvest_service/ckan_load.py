"""The Load Box's CKAN destination: a published pipeline can be given a **resource** in an existing CKAN dataset (data.gov.gr).

Four ways, chosen per pipeline (``CkanTarget.mode``):
* ``link`` -- the resource's URL is the pipeline's published address (``/data/<source>/<pipeline>``). Nothing is copied: CKAN links to the
  loader, which always answers with the newest output (or asks the provider, for an API source).
* ``upload`` -- the newest output file is uploaded into CKAN's own storage, again each time a new one is made (a source that stores data).

It always acts *as a person*: the CKAN user ``user_<keycloak sub>`` (see ``ckan_access``), with that person's own API token (minted once through
the sysadmin credential, ``ckan_tokens``), so CKAN's own permissions decide what may be touched -- the loader never writes with more rights
than the person has. The person who connected a pipeline is remembered on it (``ckan_user``) and later changes (a new output, unpublishing)
are made as them too.

The functions here take the CKAN client as an argument, so they are tested against a fake and never need a real CKAN.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import re
import threading
import time

from sqlalchemy import update
from sqlalchemy.orm import Session

from providerharvest_service import ckan_access, ckan_client, ckan_datastore, ckan_tokens, ckan_vocab, publishing
from providerharvest_service.models import CkanServiceDefaults, CkanTarget, ProviderSource, Transform

logger = logging.getLogger(__name__)

MODES = ("link", "upload", "datastore", "service")
MAX_UPLOAD_BYTES = 100 * 1000 * 1000
_FORMAT_BY_EXTENSION = {".csv": "CSV", ".tsv": "TSV", ".json": "JSON", ".jsonl": "JSON", ".xml": "XML", ".xlsx": "XLSX", ".xls": "XLS"}


#: A person's organizations, kept for a short while: asking CKAN for them is the slow call (a sysadmin has hundreds), and the page asks twice.
_ORG_CACHE: dict[str, tuple[float, list]] = {}
_ORG_CACHE_LOCK = threading.Lock()
ORG_CACHE_SECONDS = 600.0

#: The loader is one client among many of a CKAN whose web workers are few: it never has more than one call to it in flight per process, and a retry
#: waits a moment. (CKAN's public site answered 500 while this was not so, when the organization list was asked for again and again.)
_THROTTLE = threading.BoundedSemaphore(1)
RETRY_DELAY_S = 1.0


class CkanLoadError(Exception):
    """Something the person should be told, in words."""


@dataclasses.dataclass(frozen=True)
class OrganizationChoice:
    id: str
    name: str
    title: str
    package_count: int | None = None


@dataclasses.dataclass(frozen=True)
class DatasetChoice:
    id: str
    name: str
    title: str
    organization: str
    owner_org: str = ""
    private: bool = False
    state: str = "active"
    num_resources: int = 0
    modified: str = ""  # the date CKAN last changed it, as YYYY-MM-DD


def configured() -> bool:
    return ckan_access.configured()


def public_base_url() -> str:
    return (os.environ.get("LOADER_PUBLIC_BASE_URL") or "").rstrip("/")


def user_client(session: Session, ckan_user: str) -> ckan_client.CKANActionClient:  # replaced in tests
    """A CKAN client holding this CKAN user's own token (minted through the sysadmin credential the first time)."""
    try:
        return ckan_tokens.get_or_provision_ckan_client(session, ckan_user)
    except ckan_tokens.CkanTokenProvisioningError as exc:
        raise CkanLoadError("CKAN is not set up on this deployment: %s" % exc) from exc
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError("CKAN would not give %s a token (%s)." % (ckan_user, exc)) from exc


def _explain(exc: ckan_client.CKANActionError) -> str:
    if isinstance(exc, ckan_client.CKANNotAuthorized):
        return "CKAN says you may not change that dataset."
    if isinstance(exc, ckan_client.CKANNotFound):
        return "CKAN does not know that dataset or resource (it may have been deleted)."
    if isinstance(exc, ckan_client.CKANValidationError):
        return "CKAN refused it: %s" % exc
    return "CKAN could not be reached or failed (%s)." % exc


def _call(client: ckan_client.CKANActionClient, action: str, data: dict, files: dict | None = None):
    """Every call the loader makes to CKAN goes through here: one at a time per process, and its time and outcome are logged, so a slow or
    failing CKAN can be matched against what the loader sent."""
    with _THROTTLE:
        started = time.monotonic()
        try:
            result = client.call(action, data, files=files) if files else client.call(action, data)
        except ckan_client.CKANActionError as exc:
            logger.info("ckan call %s: %s after %d ms (HTTP %s)", action, type(exc).__name__, (time.monotonic() - started) * 1000, getattr(exc, "status_code", None))
            raise
        logger.info("ckan call %s: ok in %d ms", action, (time.monotonic() - started) * 1000)
        return result


def _read(client: ckan_client.CKANActionClient, action: str, data: dict):
    """A call that only reads. CKAN now and then answers one of its heavier actions with a server error (HTTP 5xx, or something that is not
    JSON); the second try nearly always works, so a read gets one."""
    try:
        return _call(client, action, data)
    except ckan_client.CKANActionError as exc:
        if isinstance(exc, (ckan_client.CKANNotAuthorized, ckan_client.CKANNotFound, ckan_client.CKANValidationError)) or not (getattr(exc, "status_code", None) or 0) >= 500:
            raise
        logger.info("ckan: %s answered %s, trying once more", action, exc.status_code)
        time.sleep(RETRY_DELAY_S)  # (not at once: a CKAN that just failed is best left a moment)
        return _call(client, action, data)


# -- finding the dataset -------------------------------------------------------------------------------------------------------------------------------

def list_organizations(client: ckan_client.CKANActionClient, ckan_user: str) -> list[OrganizationChoice]:
    """The organizations the person may add datasets to (an editor included; a sysadmin gets all of them), by title. Kept for two minutes."""
    now = time.monotonic()
    with _ORG_CACHE_LOCK:
        cached = _ORG_CACHE.get(ckan_user)
        if cached and now - cached[0] < ORG_CACHE_SECONDS:
            return list(cached[1])
    try:
        organizations = _read(client, "organization_list_for_user", {"id": ckan_user, "permission": "create_dataset"}) or []
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc
    found = sorted((OrganizationChoice(id=str(o["id"]), name=str(o.get("name") or ""), title=str(o.get("title") or o.get("display_name") or o.get("name") or ""),
                                       package_count=o.get("package_count") if isinstance(o.get("package_count"), int) else None)
                    for o in organizations if isinstance(o, dict) and o.get("id")), key=lambda o: o.title.lower())
    with _ORG_CACHE_LOCK:
        _ORG_CACHE[ckan_user] = (now, found)
    return list(found)


def list_admin_organizations(client: ckan_client.CKANActionClient, ckan_user: str) -> list[OrganizationChoice]:
    """The organizations the person *administers* (a source belongs to one of them), by title. Kept for ten minutes."""
    return _organizations(client, "%s:admin" % ckan_user, "organization_list_for_user", {"id": ckan_user, "permission": "admin"})


def sysadmin_client() -> ckan_client.CKANActionClient:  # replaced in tests
    return ckan_access._client()


def list_all_organizations(client: ckan_client.CKANActionClient) -> list[OrganizationChoice]:
    """Every organization in CKAN, by title (for a super admin). Kept for ten minutes."""
    return _organizations(client, "*", "organization_list", {"all_fields": True})


def _organizations(client, cache_key: str, action: str, data: dict) -> list[OrganizationChoice]:
    now = time.monotonic()
    with _ORG_CACHE_LOCK:
        cached = _ORG_CACHE.get(cache_key)
        if cached and now - cached[0] < ORG_CACHE_SECONDS:
            return list(cached[1])
    try:
        organizations = _read(client, action, data) or []
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc
    found = sorted((OrganizationChoice(id=str(o["id"]), name=str(o.get("name") or ""), title=str(o.get("title") or o.get("display_name") or o.get("name") or ""),
                                       package_count=o.get("package_count") if isinstance(o.get("package_count"), int) else None)
                    for o in organizations if isinstance(o, dict) and o.get("id")), key=lambda o: o.title.lower())
    with _ORG_CACHE_LOCK:
        _ORG_CACHE[cache_key] = (now, found)
    return list(found)


def search_datasets(client: ckan_client.CKANActionClient, ckan_user: str, query: str = "", limit: int = 20, organization_id: str | None = None) -> list[DatasetChoice]:
    """Datasets found by a text search: those of the organization given, or else of all the organizations the person can create datasets in.
    Which datasets CKAN lets the person *see* is CKAN's to decide (private ones only for those who may); what they may *add to* is checked
    when the resource is saved. Nothing here changes anything."""
    try:
        if organization_id:
            ids = [organization_id]  # (no look-up of the person's organizations here: it is the slow call, and the page already made it)
        else:
            ids = [o.id for o in list_organizations(client, ckan_user)]
        if not ids:
            return []
        found = _read(client, "package_search", {"q": query.strip() or "*:*", "fq": "owner_org:(%s)" % " OR ".join(ids), "rows": limit, "include_private": True,
                                                 "sort": "metadata_modified desc"}) or {}
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc
    return [_dataset_choice(d) for d in found.get("results", []) if isinstance(d, dict) and d.get("id")]


def _dataset_choice(d: dict) -> DatasetChoice:
    organization = d.get("organization") or {}
    return DatasetChoice(
        id=str(d["id"]), name=str(d.get("name") or ""), title=str(d.get("title") or d.get("name") or ""),
        organization=str(organization.get("title") or organization.get("name") or ""), owner_org=str(d.get("owner_org") or ""),
        private=bool(d.get("private")), state=str(d.get("state") or "active"),
        num_resources=int(d.get("num_resources") if isinstance(d.get("num_resources"), int) else len(d.get("resources") or [])),
        modified=str(d.get("metadata_modified") or "")[:10])


def get_dataset(client: ckan_client.CKANActionClient, dataset_id: str) -> DatasetChoice:
    try:
        d = _read(client, "package_show", {"id": dataset_id})
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc
    return _dataset_choice(d)


# -- the resource -----------------------------------------------------------------------------------------------------------------------------------------

def link_url(source: ProviderSource, pipeline: Transform, base_url: str | None = None) -> str:
    return (base_url if base_url is not None else public_base_url()) + publishing.address_of(source, pipeline)


def _format_of(filename: str) -> str:
    return _FORMAT_BY_EXTENSION.get(os.path.splitext(filename or "")[1].lower(), "")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def push(target: CkanTarget, source: ProviderSource, pipeline: Transform, client: ckan_client.CKANActionClient, *, output=None, base_url: str | None = None) -> None:
    """Create or update the pipeline's CKAN resource. ``output`` is the newest good output (a ``TransformedFile`` with its contents), needed
    for ``upload``. Sets the outcome on ``target`` (the caller commits); raises ``CkanLoadError`` after recording it."""
    try:
        _push(target, source, pipeline, client, output, base_url)
    except CkanLoadError as exc:
        target.last_status, target.last_error, target.updated_at = "error", str(exc), _now()
        raise
    target.last_status, target.last_error, target.last_pushed_at, target.updated_at = "ok", None, _now(), _now()


def _push(target: CkanTarget, source: ProviderSource, pipeline: Transform, client, output, base_url) -> None:
    if target.mode not in MODES:
        raise CkanLoadError("Unknown way of pushing: %r." % target.mode)
    if target.mode == "service":
        _push_service(target, source, pipeline, client, base_url)
        return
    if target.mode == "datastore":
        _push_datastore(target, pipeline, client, output)
        return
    fields = {"name": target.resource_name or pipeline.name, "description": target.description or ""}
    files = None
    if target.mode == "link":
        if not publishing.is_pipeline_published(pipeline):
            raise CkanLoadError("Add the pipeline's public address first: CKAN would link to an address that does not answer.")
        url = link_url(source, pipeline, base_url)
        if not url.startswith("http"):
            raise CkanLoadError("The public address of this loader is not configured (LOADER_PUBLIC_BASE_URL), so there is nothing to link to.")
        written = {"json": "JSON", "jsonl": "JSON", "xml": "XML", "csv": "CSV"}.get(pipeline.output_format or "json", "")
        fields.update({"url": url, "format": "JSON" if source.transport_type == "http" and not pipeline.enabled else written})
    else:
        if output is None or not getattr(output, "content", b""):
            raise CkanLoadError("There is no output to upload yet.")
        if len(output.content) > MAX_UPLOAD_BYTES:
            raise CkanLoadError("The newest output is larger than %d MB, too big to upload to CKAN." % (MAX_UPLOAD_BYTES // 1_000_000))
        fields.update({"url": "", "format": _format_of(output.filename)})
        files = {"upload": (output.filename or "output", output.content, output.content_type or "application/octet-stream")}
    try:
        if target.resource_id:
            try:
                _call(client, "resource_update", {"id": target.resource_id, **fields}, files)
                target.last_output_id = getattr(output, "id", None)
                return
            except ckan_client.CKANNotFound:
                target.resource_id = None  # it was deleted in CKAN: make it again
        created = _call(client, "resource_create", {"package_id": target.dataset_id, **fields}, files)
        target.resource_id = str(created["id"])
        target.last_output_id = getattr(output, "id", None)
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc


def _push_datastore(target: CkanTarget, pipeline: Transform, client, output) -> None:
    """Write the newest output's rows into the DataStore table of the pipeline's resource (made here, without a file, the first time)."""
    if output is None or not getattr(output, "content", b""):
        raise CkanLoadError("There is no output to load yet.")
    if len(output.content) > MAX_UPLOAD_BYTES:
        raise CkanLoadError("The newest output is larger than %d MB, too big to load into the DataStore." % (MAX_UPLOAD_BYTES // 1_000_000))
    cfg = ckan_datastore.config_of(target.datastore_config)
    if cfg["refresh"] == "append" and target.resource_id and target.last_output_id == getattr(output, "id", None):
        raise CkanLoadError("This output is already in the table (adding it again would repeat its rows). Wait for the next output.")
    try:
        rows = ckan_datastore.records_of(output.content, output.filename, output.content_type)
        known = {f["id"]: f["type"] for f in cfg.get("fields", [])} if cfg["refresh"] != "replace" else None
        fields, records = ckan_datastore.table_of(rows, known)
        if cfg["retention_days"]:
            fields, records = ckan_datastore.with_days(fields, records, cfg["time_column"])
            cutoff = (_now().date() - datetime.timedelta(days=cfg["retention_days"])).isoformat()
            records = [r for r in records if r[ckan_datastore.DAY_COLUMN] is None or r[ckan_datastore.DAY_COLUMN] >= cutoff]  # (older ones would only be pruned again)
        keys = cfg["keys"]
        if cfg["refresh"] == "upsert" and any(k not in {f["id"] for f in fields} for k in keys):
            raise ckan_datastore.DatastoreError("The key column(s) %s are not in the output any more." % ", ".join(repr(k) for k in keys))
    except ckan_datastore.DatastoreError as exc:
        raise CkanLoadError(str(exc)) from exc
    try:
        if not target.resource_id:
            created = _call(client, "resource_create", {"package_id": target.dataset_id, "name": target.resource_name or pipeline.name,
                                                       "description": target.description or "", "url": "_datastore_only_resource", "format": ""})
            target.resource_id = str(created["id"])
            cfg.pop("created", None)
        rid = target.resource_id
        try:
            _write_table(client, rid, cfg, fields, records, keys)
        except ckan_client.CKANNotFound:
            # the resource was deleted in CKAN: make it again, and start the table from nothing
            created = _call(client, "resource_create", {"package_id": target.dataset_id, "name": target.resource_name or pipeline.name,
                                                       "description": target.description or "", "url": "_datastore_only_resource", "format": ""})
            target.resource_id = rid = str(created["id"])
            cfg.pop("created", None)
            _write_table(client, rid, cfg, fields, records, keys)
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc
    cfg["fields"] = fields
    cfg["created"] = ckan_datastore.signature(fields, keys)
    fresh = ckan_datastore.config_of(target.datastore_config)  # (pruning keeps its own notes in it; do not overwrite them)
    for key in ("pruned_at", "prune_note", "archived"):
        if key in fresh:
            cfg[key] = fresh[key]
    target.datastore_config = json.dumps(cfg, ensure_ascii=False)
    target.last_output_id = getattr(output, "id", None)


def _write_table(client, rid: str, cfg: dict, fields: list[dict], records: list[dict], keys: list[str]) -> None:
    if cfg["refresh"] == "replace":
        try:
            _call(client, "datastore_delete", {"resource_id": rid, "force": True})  # (no filters: the whole table; there may be none yet)
        except ckan_client.CKANNotFound:
            pass
        cfg.pop("created", None)
    if cfg.get("created") != ckan_datastore.signature(fields, keys):
        _call(client, "datastore_create", {"resource_id": rid, "force": True, "fields": fields, **({"primary_key": keys} if keys else {})})
    method = "upsert" if cfg["refresh"] == "upsert" else "insert"
    for batch in ckan_datastore.batches(records):
        _call(client, "datastore_upsert", {"resource_id": rid, "force": True, "method": method, "records": batch})


def is_private(client: ckan_client.CKANActionClient, target: CkanTarget) -> bool | None:
    """Is the output's dataset (for a data service, the entry itself) private in CKAN right now? None when CKAN cannot say."""
    ident = target.resource_id if target.mode == "service" else target.dataset_id
    if not ident:
        return None
    try:
        return bool(_read(client, "package_show", {"id": ident}).get("private"))
    except (ckan_client.CKANActionError, AttributeError):
        return None


def set_private(client: ckan_client.CKANActionClient, target: CkanTarget, private: bool) -> None:
    """Make the output's dataset (or data service) private or public. CKAN's own permissions decide whether the person may; the whole dataset changes
    visibility, not just this output's resource."""
    ident = target.resource_id if target.mode == "service" else target.dataset_id
    if not ident:
        raise CkanLoadError("There is nothing in CKAN to change yet: push the output first.")
    try:
        _call(client, "package_patch", {"id": ident, "private": private})
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc


def remove(target: CkanTarget, client: ckan_client.CKANActionClient) -> bool:
    """Delete the pipeline's resource (or, for a data service, the entry) from CKAN. True when there was one (and it is gone now); one CKAN no
    longer has counts as gone."""
    if not target.resource_id:
        return False
    try:
        _call(client, "package_delete" if target.mode == "service" else "resource_delete", {"id": target.resource_id})
    except ckan_client.CKANNotFound:
        pass
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc
    target.resource_id = None
    target.last_output_id = None
    return True


# -- a data service: an entry of its own (a dataset of type ``data-service``) instead of a resource in a dataset ----------------------------------------------

#: What one organization's services share, remembered for the next one (the title, description and endpoint text belong to one service).
DEFAULT_KEYS = ("tags", "access_rights", "license", "themes", "legislation", "hvd_categories", "contact_name", "contact_email", "publisher_name",
                "publisher_email", "publisher_identifier", "publisher_type", "documentation", "landing_page", "rights")
META_DEFAULTS = {
    "title": "", "description": "", "endpoint_description": "", "tags": ["API"], "access_rights": "PUBLIC", "license": "CC_BY_4_0", "format": "JSON", "themes": [],
    "legislation": [ckan_vocab.LEGISLATION[0][0]], "hvd_categories": [], "contact_name": "", "contact_email": "", "publisher_name": "", "publisher_email": "",
    "publisher_identifier": "", "publisher_type": "NationalAuthority", "documentation": [], "landing_page": [], "rights": "",
}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL = re.compile(r"^https?://\S+$")


def _lines(value) -> list[str]:
    """A list the form sent as text (one per line or comma-separated), or already a list."""
    if isinstance(value, str):
        value = re.split(r"[\n,]+", value)
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def normalise_meta(raw: dict) -> dict:
    """The details of a data service, checked and cleaned. Raises ``CkanLoadError`` with a sentence for the person."""
    meta = dict(META_DEFAULTS)
    meta.update({k: v for k, v in (raw or {}).items() if k in META_DEFAULTS})
    meta["title"] = str(meta["title"] or "").strip()
    if not meta["title"]:
        raise CkanLoadError("Give the service a name.")
    if len(meta["title"]) > 200:
        raise CkanLoadError("The name of the service is longer than 200 characters.")
    for key, limit in (("description", 4000), ("endpoint_description", 4000), ("rights", 2000)):
        meta[key] = str(meta[key] or "").strip()
        if len(meta[key]) > limit:
            raise CkanLoadError("%s is longer than %d characters." % (key.replace("_", " ").capitalize(), limit))
    meta["tags"] = [t[:100] for t in _lines(meta["tags"])][:20]
    for key, kind, label in (("access_rights", "access_rights", "access rights"), ("format", "format", "format"), ("publisher_type", "publisher_type", "publisher type")):
        if not ckan_vocab.is_known(kind, str(meta[key])):
            raise CkanLoadError("Choose the %s from the list." % label)
    meta["license"] = str(meta["license"] or "").strip()
    if not (ckan_vocab.is_known("license", meta["license"]) or _URL.match(meta["license"])):
        raise CkanLoadError("Choose the licence from the list, or give its address (a link).")
    meta["themes"] = _lines(meta["themes"])
    if any(not ckan_vocab.is_known("theme", t) for t in meta["themes"]):
        raise CkanLoadError("A theme is not one of the listed ones.")
    meta["hvd_categories"] = _lines(meta["hvd_categories"])
    if any(not (ckan_vocab.is_known("hvd_category", c) or _URL.match(c)) for c in meta["hvd_categories"]):
        raise CkanLoadError("A high-value-dataset category is not one of the listed ones (or a link).")
    meta["legislation"] = [v[:500] for v in _lines(meta["legislation"])]
    for key in ("documentation", "landing_page"):
        meta[key] = _lines(meta[key])
        if any(not _URL.match(u) for u in meta[key]):
            raise CkanLoadError("%s must be links (starting with http:// or https://)." % key.replace("_", " ").capitalize())
    for key in ("contact_email", "publisher_email"):
        meta[key] = str(meta[key] or "").strip()
        if meta[key] and not _EMAIL.match(meta[key]):
            raise CkanLoadError("%r is not an e-mail address." % meta[key])
    for key in ("contact_name", "publisher_name", "publisher_identifier"):
        meta[key] = str(meta[key] or "").strip()[:200]
    return meta


def service_format(source: ProviderSource, pipeline: Transform) -> str:
    """The format of what the endpoint answers, as the vocabulary's code: what the pipeline writes while it is switched on; a pipeline that is off
    passes an API source's answer through as it is, which is JSON. It is never chosen by hand: it changes only when the pipeline's output does."""
    if not pipeline.enabled:
        return "JSON"
    return {"xml": "XML", "csv": "CSV"}.get(pipeline.output_format or "json", "JSON")


def describe_endpoint(source: ProviderSource, pipeline: Transform, url: str) -> str:
    """What the endpoint description of the service says, from what the loader knows: how to call it, with what, what comes back, how often."""
    from providerharvest_service import proxy, request_template

    method = proxy.consumer_method(source, pipeline)
    try:
        limit = json.loads(source.config or "{}").get("proxy_rate_limit_per_ip", 30)
    except ValueError:
        limit = 30
    written = {"json": "JSON", "jsonl": "JSON Lines", "xml": "XML", "csv": "CSV"}.get(pipeline.output_format or "json", "JSON")
    answer = written if pipeline.enabled else "the provider's own answer (JSON)"
    lines = []
    if proxy.request_is_swapped(pipeline) and method == "GET":
        names = request_template.placeholders(pipeline.request_template)
        lines.append("Call it with HTTP GET: %s?%s" % (url, "&".join("%s=<%s>" % (n, n) for n in names)))
        lines.append("Parameters (all required): %s." % ", ".join(names))
    elif method == "POST":
        lines.append("Call it with HTTP POST to %s, sending the request as a JSON body (Content-Type: application/json)." % url)
    else:
        lines.append("Call it with HTTP GET: %s (query parameters are passed on to the service)." % url)
    lines.append("It answers with %s, as it is produced at the moment of the call." % answer)
    lines.append("Free to call, without a key; at most %d requests per minute from one address." % limit)
    return "\n".join(lines)


def service_slug_candidates(source: ProviderSource, pipeline: Transform) -> list[str]:
    base = re.sub(r"[^a-z0-9_-]+", "-", (source.name or "service").lower()).strip("-") or "service"
    if pipeline.name != "default":
        base += "-" + pipeline.name
    return [base] + ["%s-%d" % (base, n) for n in range(2, 7)]


def service_payload(meta: dict, url: str, owner_org: str) -> dict:
    """The CKAN dataset of type ``data-service`` that stands for the endpoint (no name: that is chosen when it is created)."""
    payload = {
        "type": "data-service", "title": meta["title"], "notes": meta["description"], "owner_org": owner_org, "private": False,
        "tags": [{"name": t} for t in meta["tags"]], "endpoint_url": [url],
        "endpoint_description": [meta["endpoint_description"]] if meta["endpoint_description"] else [],
        "access_rights": ckan_vocab.uri("access_rights", meta["access_rights"]), "license": ckan_vocab.uri("license", meta["license"]),
        "format": ckan_vocab.uri("format", meta["format"]), "theme": [ckan_vocab.uri("theme", t) for t in meta["themes"]],
        "hvd_category": [ckan_vocab.uri("hvd_category", c) for c in meta["hvd_categories"]], "applicable_legislation": meta["legislation"],
        "documentation": meta["documentation"], "landing_page": meta["landing_page"], "rights": meta["rights"],
        "contact": [{"name": meta["contact_name"], "email": meta["contact_email"], "uri": "", "url": ""}] if meta["contact_name"] or meta["contact_email"] else [],
        "publisher": [{"name": meta["publisher_name"], "email": meta["publisher_email"], "identifier": meta["publisher_identifier"],
                       "type": ckan_vocab.uri("publisher_type", meta["publisher_type"])}] if meta["publisher_name"] else [],
    }
    return payload


#: CKAN's own licence ids for the licences the form offers (only "cc-by" is known from a script that creates datasets on the portal; the others are CKAN's
#: standard ids, and a licence with none is left out so CKAN's default applies).
LICENSE_IDS = {"CC_BY_4_0": "cc-by", "CC0": "cc-zero", "CC_BYSA_4_0": "cc-by-sa", "CC_BYNC_4_0": "cc-nc", "ODC_BY": "odc-by", "ODC_ODBL": "odc-odbl"}


def dataset_payload(meta: dict, owner_org: str, name: str, public: bool = False) -> dict:
    """A new CKAN dataset (data.gov.gr's DCAT-AP fields, in the shapes a working dataset-creating script of the portal uses): a single value for the
    legislation, the high-value category and the access rights, lists for the theme, contact and publisher."""
    payload = {
        "name": name, "title": meta["title"], "notes": meta["description"], "owner_org": owner_org, "private": not public,
        "tag_string": ",".join(meta["tags"]), "access_rights": ckan_vocab.uri("access_rights", meta["access_rights"]),
        "theme": [ckan_vocab.uri("theme", t) for t in meta["themes"]],
    }
    if meta["legislation"]:
        payload["applicable_legislation"] = meta["legislation"][0]
    if meta["hvd_categories"]:
        payload["hvd_category"] = ckan_vocab.uri("hvd_category", meta["hvd_categories"][0])
    if LICENSE_IDS.get(meta["license"]):
        payload["license_id"] = LICENSE_IDS[meta["license"]]
    if meta["contact_name"] or meta["contact_email"]:
        payload["contact"] = [{"name": meta["contact_name"], "email": meta["contact_email"], "uri": ""}]
    if meta["publisher_name"]:
        payload["publisher"] = [{"name": meta["publisher_name"], "uri": meta["publisher_identifier"] if _URL.match(meta["publisher_identifier"] or "") else ""}]
    return payload


def create_dataset(client, meta: dict, owner_org: str, source: ProviderSource, pipeline: Transform, public: bool = False) -> DatasetChoice:
    """Create the dataset a new load puts its resource in. Its address comes from the source and pipeline name; a taken one gets the next free number."""
    last: Exception | None = None
    for name in service_slug_candidates(source, pipeline):
        try:
            created = _call(client, "package_create", dataset_payload(meta, owner_org, name, public))
            return DatasetChoice(id=str(created["id"]), name=str(created.get("name") or name), title=meta["title"], organization="", owner_org=owner_org,
                                 private=not public, state="active")
        except ckan_client.CKANValidationError as exc:
            if "already in use" not in str(exc).lower() and "url" not in str(exc).lower():
                raise CkanLoadError(_explain(exc)) from exc
            last = exc
        except ckan_client.CKANActionError as exc:
            raise CkanLoadError(_explain(exc)) from exc
    raise CkanLoadError("CKAN says every address it could use for this dataset is taken (%s)." % last)


def _push_service(target: CkanTarget, source: ProviderSource, pipeline: Transform, client, base_url) -> None:
    if not publishing.is_pipeline_published(pipeline):
        raise CkanLoadError("Add the pipeline's public address first: the data service would point at an address that does not answer.")
    url = link_url(source, pipeline, base_url)
    if not url.startswith("http"):
        raise CkanLoadError("The public address of this loader is not configured (LOADER_PUBLIC_BASE_URL), so there is no endpoint to describe.")
    if not target.organization_id:
        raise CkanLoadError("Choose the organization the service belongs to.")
    try:
        meta = normalise_meta(json.loads(target.service_meta or "{}"))
    except ValueError as exc:
        raise CkanLoadError("The details of the service are unreadable: %s" % exc) from exc
    meta["format"] = service_format(source, pipeline)  # (whatever the form held: the pipeline decides)
    payload = service_payload(meta, url, target.organization_id)
    try:
        if target.resource_id:
            try:
                _call(client, "package_patch", {"id": target.resource_id, **{k: v for k, v in payload.items() if k != "type"}})
                target.dataset_title = meta["title"]
                return
            except ckan_client.CKANNotFound:
                target.resource_id = None  # it was deleted in CKAN: make it again
        created = None
        for name in service_slug_candidates(source, pipeline):
            try:
                created = _call(client, "package_create", {**payload, "name": name})
                break
            except ckan_client.CKANValidationError as exc:
                if "already in use" not in str(exc).lower() and "url" not in str(exc).lower():
                    raise
        if created is None:
            raise CkanLoadError("CKAN says every address it could use for this service is taken.")
        target.resource_id = target.dataset_id = str(created["id"])
        target.dataset_name, target.dataset_title = str(created.get("name") or ""), meta["title"]
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc


def get_defaults(session: Session, organization_id: str) -> dict:
    """The details this organization's services last used (what the form starts from), over the built-in defaults."""
    from sqlalchemy import select

    values = dict(META_DEFAULTS)
    row = session.scalar(select(CkanServiceDefaults).where(CkanServiceDefaults.organization_id == organization_id))
    if row is not None:
        try:
            values.update({k: v for k, v in json.loads(row.values or "{}").items() if k in DEFAULT_KEYS})
        except ValueError:
            pass
    return values


def save_defaults(session: Session, organization_id: str, meta: dict, who: str = "") -> None:
    """Remember what was chosen for this organization (the caller commits). Only what its services share: not a title or an endpoint text."""
    from sqlalchemy import select

    row = session.scalar(select(CkanServiceDefaults).where(CkanServiceDefaults.organization_id == organization_id))
    if row is None:
        row = CkanServiceDefaults(organization_id=organization_id)
        session.add(row)
    row.values = json.dumps({k: meta[k] for k in DEFAULT_KEYS if k in meta}, ensure_ascii=False)
    row.updated_by, row.updated_at = who, _now()


# -- keeping CKAN in step with the pipeline --------------------------------------------------------------------------------------------------------------
# Every function below is best effort and never raises: a CKAN that is down or says no must not stop an upload from being received, a pipeline from
# being unpublished or a source from being deleted. What went wrong is kept on the target (and returned as words where a person is looking).

def targets_of(session: Session, pipeline_id: str) -> list[CkanTarget]:
    """Every load of the pipeline into CKAN, oldest first (each is its own row in the Load Box)."""
    from sqlalchemy import select

    return list(session.scalars(select(CkanTarget).where(CkanTarget.pipeline_id == pipeline_id).order_by(CkanTarget.created_at, CkanTarget.id)).all())


def target_of(session: Session, pipeline_id: str) -> CkanTarget | None:
    """The first load of the pipeline (for callers that mean 'the' load)."""
    found = targets_of(session, pipeline_id)
    return found[0] if found else None


def _client_for(session: Session, target: CkanTarget):
    if not configured():
        raise CkanLoadError("CKAN is not set up on this deployment.")
    return user_client(session, target.ckan_user)


def sync_after_output(session: Session, source: ProviderSource, pipeline: Transform, output) -> None:
    """A new good output was stored: every ``upload`` and ``datastore`` load of the pipeline puts it into CKAN (as the person who connected it), each
    independently. A ``link`` or ``service`` one needs nothing."""
    if getattr(output, "status", "ok") != "ok":
        return
    try:
        targets = [t for t in targets_of(session, pipeline.id) if t.mode in ("upload", "datastore")]
    except Exception:  # noqa: BLE001
        logger.exception("ckan: looking up the loads of %s failed", getattr(pipeline, "name", "?"))
        session.rollback()
        return
    for target in targets:
        try:
            if target.last_output_id == getattr(output, "id", None) and target.resource_id:
                continue
            push(target, source, pipeline, _client_for(session, target), output=output)
            session.commit()
        except CkanLoadError:
            session.commit()  # (the reason was recorded on the target)
        except Exception:  # noqa: BLE001 -- receiving a file must not fail because of CKAN
            logger.exception("ckan: syncing a new output of %s / %s failed", getattr(source, "name", "?"), getattr(pipeline, "name", "?"))
            session.rollback()


def release(session: Session, target: CkanTarget) -> str:
    """Take the pipeline's resource out of CKAN (it was unpublished or removed). '' when all is well, else a sentence for the person."""
    if not target.resource_id:
        return ""
    try:
        remove(target, _client_for(session, target))
    except CkanLoadError as exc:
        target.last_status, target.last_error = "error", "Could not be removed from CKAN: %s" % exc
        return "Its CKAN resource could not be removed (%s) -- remove it in CKAN by hand." % exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("ckan: releasing a resource failed")
        return "Its CKAN resource could not be removed (%s) -- remove it in CKAN by hand." % exc
    target.last_status, target.last_error = None, None
    return ""


def restore(session: Session, source: ProviderSource, pipeline: Transform, target: CkanTarget) -> str:
    """The pipeline is published again: put its resource back. '' when all is well, else a sentence for the person."""
    if target.resource_id:
        return ""
    output = None
    if target.mode in ("upload", "datastore"):
        from providerharvest_service import harvest_db, load_api

        try:
            with harvest_db.get_session_factory()() as hsession:
                output = load_api.latest_output(hsession, source.id, pipeline.id)
                if output is not None:
                    hsession.expunge(output)
        except Exception:  # noqa: BLE001
            output = None
    try:
        push(target, source, pipeline, _client_for(session, target), output=output)
    except CkanLoadError as exc:
        return "It could not be put back in CKAN: %s" % exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("ckan: restoring a resource failed")
        return "It could not be put back in CKAN: %s" % exc
    return ""


def forget(session: Session, pipeline_id: str) -> str:
    """The pipeline is being removed: every one of its loads leaves CKAN and the connections are deleted (the caller commits). Returns the warnings, or ''."""
    warnings = []
    for target in targets_of(session, pipeline_id):
        warning = release(session, target)
        if warning:
            warnings.append(warning)
        session.delete(target)
    return " ".join(warnings)


def forget_source(session: Session, source_id: str) -> list[str]:
    """Same for every pipeline of a source that is being deleted. Returns the warnings."""
    from sqlalchemy import select

    warnings = []
    for target in session.scalars(select(CkanTarget).where(CkanTarget.source_id == source_id)).all():
        warning = release(session, target)
        if warning:
            warnings.append(warning)
        session.delete(target)
    return warnings


def refresh_after_change(session: Session, source: ProviderSource, pipeline: Transform) -> str:
    """The pipeline changed what an entry in CKAN says (its output format, or it was switched on or off): update its data services, as the person
    who connected them. Best effort, '' when there is nothing to do, else a sentence for the person."""
    notes = []
    for target in targets_of(session, pipeline.id):
        if target.mode != "service" or not target.resource_id:
            continue
        try:
            push(target, source, pipeline, _client_for(session, target))
            notes.append("The data service in CKAN was updated.")
        except CkanLoadError as exc:
            notes.append("The data service in CKAN could not be updated: %s" % exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ckan: refreshing a data service failed")
            notes.append("The data service in CKAN could not be updated: %s" % exc)
    return " ".join(notes)


# -- pruning a DataStore table that keeps history ------------------------------------------------------------------------------------------------------------
# Rows older than the chosen number of days leave the table a day at a time, each day first saved as a CSV file in a resource of the same dataset (when
# chosen). It runs from the loader's hourly cleanup, not from receiving a file, and at most every PRUNE_EVERY_S per table.

PRUNE_EVERY_S = 6 * 3600
PRUNE_DAYS_PER_PASS = 2
ARCHIVE_PAGE = 10_000
ARCHIVE_PART_ROWS = 100_000


def _csv_bytes(columns: list[str], rows: list[dict]) -> bytes:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
    return buffer.getvalue().encode("utf-8")


def _file_stem(target: CkanTarget) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", target.resource_name or "data").strip("-") or "data"


def archive_day(target: CkanTarget, client, day: str, ids: list[str]) -> list[str]:
    """Save one day's rows of the table as CSV resource(s) of the dataset (parts of at most ARCHIVE_PART_ROWS rows). Returns their resource ids; a part
    already made (``ids``) is updated, not made twice."""
    done = list(ids)
    state = {"part": 0, "rows": []}
    columns: list[str] = []

    def flush(last: bool) -> None:
        if not state["rows"]:
            return
        state["part"] += 1
        part = state["part"]
        single = last and part == 1
        label = "%s %s" % (target.resource_name or "Data", day) + ("" if single else " (part %d)" % part)
        name = "%s-%s%s.csv" % (_file_stem(target), day, "" if single else "-part%d" % part)
        data = {"name": label, "format": "CSV", "url": "", "description": "The rows of %s that the retention setting removed from the table." % day}
        files = {"upload": (name, _csv_bytes(columns, state["rows"]), "text/csv")}
        if part <= len(done):
            _call(client, "resource_update", {"id": done[part - 1], **data}, files)
        else:
            done.append(str(_call(client, "resource_create", {"package_id": target.dataset_id, **data}, files)["id"]))
        state["rows"] = []

    offset = 0
    while True:
        page = _read(client, "datastore_search", {"resource_id": target.resource_id, "filters": {ckan_datastore.DAY_COLUMN: day}, "limit": ARCHIVE_PAGE,
                                                  "offset": offset, "sort": "_id"})
        records = page.get("records") or []
        if not columns:
            columns = [f["id"] for f in page.get("fields") or [] if f["id"] not in ("_id", "_full_text")]
        state["rows"].extend(records)
        offset += len(records)
        if len(state["rows"]) >= ARCHIVE_PART_ROWS and len(records) == ARCHIVE_PAGE:
            flush(False)
        if len(records) < ARCHIVE_PAGE:
            break
    flush(True)
    return done


def _save_config(session: Session, target: CkanTarget, cfg: dict) -> None:
    target.datastore_config = json.dumps(cfg, ensure_ascii=False)
    session.commit()


def prune_target(session: Session, target: CkanTarget, *, now: datetime.datetime | None = None, client=None) -> str:
    """Prune one DataStore load if it is due. Returns a sentence about what was done ('' when nothing was due). Never raises: a failure is kept on the
    target as a note for the page."""
    cfg = ckan_datastore.config_of(target.datastore_config)
    days = cfg["retention_days"]
    now = now or _now()
    if target.mode != "datastore" or not days or not target.resource_id or cfg["refresh"] == "replace":
        return ""
    if cfg.get("pruned_at"):
        try:
            if (now - datetime.datetime.fromisoformat(cfg["pruned_at"])).total_seconds() < PRUNE_EVERY_S:
                return ""
        except ValueError:
            pass
    # claim the pass: of several workers, only the one whose update lands does the work
    before = target.datastore_config
    cfg["pruned_at"] = now.isoformat()
    won = session.execute(update(CkanTarget).where(CkanTarget.id == target.id, CkanTarget.datastore_config == before)
                          .values(datastore_config=json.dumps(cfg, ensure_ascii=False))).rowcount
    session.commit()
    if not won:
        return ""
    session.refresh(target)
    cutoff = (now.date() - datetime.timedelta(days=days)).isoformat()
    removed: list[str] = []
    try:
        client = client or _client_for(session, target)
        found = _read(client, "datastore_search", {"resource_id": target.resource_id, "fields": [ckan_datastore.DAY_COLUMN], "distinct": True, "limit": 5000}) or {}
        old = sorted({r[ckan_datastore.DAY_COLUMN] for r in found.get("records", []) if r.get(ckan_datastore.DAY_COLUMN) and r[ckan_datastore.DAY_COLUMN] < cutoff})
        for day in old[:PRUNE_DAYS_PER_PASS]:
            cfg = ckan_datastore.config_of(target.datastore_config)
            if cfg["archive"]:
                archived = cfg.setdefault("archived", {})
                archived[day] = archive_day(target, client, day, archived.get(day, []))
                _save_config(session, target, cfg)  # (the files exist now: a failed delete must not make them twice)
            _call(client, "datastore_delete", {"resource_id": target.resource_id, "force": True, "filters": {ckan_datastore.DAY_COLUMN: day}})
            removed.append(day)
            cfg = ckan_datastore.config_of(target.datastore_config)
            cfg.get("archived", {}).pop(day, None)
            cfg["prune_note"] = "Pruned %s on %s%s." % (", ".join(removed), now.strftime("%Y-%m-%d %H:%M"), " (saved as files first)" if cfg["archive"] else "")
            _save_config(session, target, cfg)
        return "Pruned %s." % ", ".join(removed) if removed else ""
    except (CkanLoadError, ckan_client.CKANActionError) as exc:
        reason = _explain(exc) if isinstance(exc, ckan_client.CKANActionError) else str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ckan: pruning a DataStore table failed")
        reason = str(exc)
    cfg = ckan_datastore.config_of(target.datastore_config)
    cfg["prune_note"] = "Pruning failed on %s: %s" % (now.strftime("%Y-%m-%d %H:%M"), reason)
    _save_config(session, target, cfg)
    return "Pruning failed: %s" % reason


def prune_all(session: Session, *, now: datetime.datetime | None = None) -> list[str]:
    """One pass over every DataStore load that has a retention. Returns the notes of those that did something."""
    from sqlalchemy import select

    notes = []
    for target in session.scalars(select(CkanTarget).where(CkanTarget.mode == "datastore")).all():
        note = prune_target(session, target, now=now)
        if note:
            notes.append("%s: %s" % (target.dataset_title or target.dataset_name or target.id, note))
    return notes
