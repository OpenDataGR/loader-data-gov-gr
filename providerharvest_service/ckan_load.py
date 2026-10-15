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


# -- finding the dataset -------------------------------------------------------------------------------------------------------------------------------

def list_organizations(client: ckan_client.CKANActionClient, ckan_user: str) -> list[OrganizationChoice]:
    """The organizations the person may add datasets to (an editor included; a sysadmin gets all of them), by title."""
    try:
        organizations = client.call("organization_list_for_user", {"id": ckan_user, "permission": "create_dataset"}) or []
    except ckan_client.CKANActionError as exc:
        raise CkanLoadError(_explain(exc)) from exc
    found = [OrganizationChoice(id=str(o["id"]), name=str(o.get("name") or ""), title=str(o.get("title") or o.get("display_name") or o.get("name") or ""),
                                package_count=o.get("package_count") if isinstance(o.get("package_count"), int) else None)
             for o in organizations if isinstance(o, dict) and o.get("id")]
    return sorted(found, key=lambda o: o.title.lower())


def search_datasets(client: ckan_client.CKANActionClient, ckan_user: str, query: str = "", limit: int = 20, organization_id: str | None = None) -> list[DatasetChoice]:
    """Datasets the person may add a resource to, found by a text search: those of the organization given (which must be one they may add to),
    or else of all the organizations they can create datasets in. Nothing here changes anything."""
    try:
        organizations = client.call("organization_list_for_user", {"id": ckan_user, "permission": "create_dataset"}) or []
        ids = [str(o.get("id")) for o in organizations if isinstance(o, dict) and o.get("id")]
        if organization_id:
            if organization_id not in ids:
                raise CkanLoadError("You may not add datasets to that organization.")
            ids = [organization_id]
        if not ids:
            return []
        found = client.call("package_search", {"q": query.strip() or "*:*", "fq": "owner_org:(%s)" % " OR ".join(ids), "rows": limit, "include_private": True,
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
        d = client.call("package_show", {"id": dataset_id})
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


# -- keeping CKAN in step with the pipeline --------------------------------------------------------------------------------------------------------------
# Every function below is best effort and never raises: a CKAN that is down or says no must not stop an upload from being received, a pipeline from
# being unpublished or a source from being deleted. What went wrong is kept on the target (and returned as words where a person is looking).

def target_of(session: Session, pipeline_id: str) -> CkanTarget | None:
    from sqlalchemy import select

    return session.scalar(select(CkanTarget).where(CkanTarget.pipeline_id == pipeline_id))


def _client_for(session: Session, target: CkanTarget):
    if not configured():
        raise CkanLoadError("CKAN is not set up on this deployment.")
    return user_client(session, target.ckan_user)


def sync_after_output(session: Session, source: ProviderSource, pipeline: Transform, output) -> None:
    """A new good output was stored: an ``upload`` connection puts it into CKAN (as the person who connected it). A ``link`` one needs nothing."""
    try:
        target = target_of(session, pipeline.id)
        if target is None or target.mode != "upload" or getattr(output, "status", "ok") != "ok":
            return
        if target.last_output_id == getattr(output, "id", None) and target.resource_id:
            return
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
    if target.mode == "upload":
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
    """The pipeline is being removed: its resource leaves CKAN and the connection is deleted (the caller commits). Returns a warning, or ''."""
    target = target_of(session, pipeline_id)
    if target is None:
        return ""
    warning = release(session, target)
    session.delete(target)
    return warning


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
