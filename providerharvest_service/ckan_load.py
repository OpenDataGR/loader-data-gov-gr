"""The Load Box's CKAN destination: a published pipeline can be given a **resource** in an existing CKAN dataset (data.gov.gr).

Two ways, chosen per pipeline (``CkanTarget.mode``):
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
import logging
import os

from sqlalchemy.orm import Session

from providerharvest_service import ckan_access, ckan_client, ckan_tokens, publishing
from providerharvest_service.models import CkanTarget, ProviderSource, Transform

logger = logging.getLogger(__name__)

MODES = ("link", "upload")
MAX_UPLOAD_BYTES = 100 * 1000 * 1000
_FORMAT_BY_EXTENSION = {".csv": "CSV", ".tsv": "TSV", ".json": "JSON", ".jsonl": "JSON", ".xml": "XML", ".xlsx": "XLSX", ".xls": "XLS"}


class CkanLoadError(Exception):
    """Something the person should be told, in words."""


@dataclasses.dataclass(frozen=True)
class DatasetChoice:
    id: str
    name: str
    title: str
    organization: str


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


# -- finding the dataset -------------------------------------------------------------------------------------------------------------------------------

def search_datasets(client: ckan_client.CKANActionClient, ckan_user: str, query: str = "", limit: int = 20) -> list[DatasetChoice]:
    """Datasets the person may add a resource to: those of the organizations they can create datasets in (an editor included), found by a text
    search. Nothing here changes anything."""
    try:
        organizations = client.call("organization_list_for_user", {"id": ckan_user, "permission": "create_dataset"}) or []
        ids = [str(o.get("id")) for o in organizations if isinstance(o, dict) and o.get("id")]
        if not ids:
            return []
        found = client.call("package_search", {"q": query.strip() or "*:*", "fq": "owner_org:(%s)" % " OR ".join(ids), "rows": limit, "include_private": True,
                                               "sort": "metadata_modified desc"}) or {}
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc
    return [DatasetChoice(id=str(d["id"]), name=str(d.get("name") or ""), title=str(d.get("title") or d.get("name") or ""),
                          organization=str((d.get("organization") or {}).get("title") or (d.get("organization") or {}).get("name") or ""))
            for d in found.get("results", []) if isinstance(d, dict) and d.get("id")]


def get_dataset(client: ckan_client.CKANActionClient, dataset_id: str) -> DatasetChoice:
    try:
        d = client.call("package_show", {"id": dataset_id})
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc
    return DatasetChoice(id=str(d["id"]), name=str(d.get("name") or ""), title=str(d.get("title") or d.get("name") or ""),
                         organization=str((d.get("organization") or {}).get("title") or ""))


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
    fields = {"name": target.resource_name or pipeline.name, "description": target.description or ""}
    files = None
    if target.mode == "link":
        if not publishing.is_pipeline_published(pipeline):
            raise CkanLoadError("Publish the pipeline first: CKAN would link to an address that does not answer.")
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
                client.call("resource_update", {"id": target.resource_id, **fields}, files=files)
                target.last_output_id = getattr(output, "id", None)
                return
            except ckan_client.CKANNotFound:
                target.resource_id = None  # it was deleted in CKAN: make it again
        created = client.call("resource_create", {"package_id": target.dataset_id, **fields}, files=files)
        target.resource_id = str(created["id"])
        target.last_output_id = getattr(output, "id", None)
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc


def remove(target: CkanTarget, client: ckan_client.CKANActionClient) -> bool:
    """Delete the pipeline's resource from CKAN. True when there was one (and it is gone now); a resource CKAN no longer has counts as gone."""
    if not target.resource_id:
        return False
    try:
        client.call("resource_delete", {"id": target.resource_id})
    except ckan_client.CKANNotFound:
        pass
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc
    target.resource_id = None
    target.last_output_id = None
    return True
