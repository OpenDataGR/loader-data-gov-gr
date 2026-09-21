"""Loads mapped provider rows into CKAN's DataStore.

Reuses ckanext-datastore (already installed on data.gov.gr) for storage
and the consumer-facing query API rather than building either from
scratch. One provider source maps to exactly one CKAN package + one
DataStore-backed resource; ``datastore_create`` (schema declaration) runs
once per resource, all further loads are ``datastore_upsert``.
"""

from __future__ import annotations

from typing import Iterable

from providerharvest_service.engine.mapping import FieldMappingProfile

DEFAULT_CHUNK_SIZE = 2000


class DataStoreLoader:
    def __init__(self, get_action):
        """
        get_action: CKAN's ``toolkit.get_action`` (injected for testability).

        Package/resource creation is a separate concern -- see
        ``logic.action.ensure_provider_resource`` -- handled once at
        approval time, not by this class, which only loads rows into an
        already-existing DataStore-backed resource.
        """
        self._get_action = get_action

    def ensure_datastore_schema(self, context: dict, resource_id: str,
                                profile: FieldMappingProfile) -> None:
        self._get_action("datastore_create")(context, {
            "resource_id": resource_id,
            "fields": profile.datastore_fields(),
            "primary_key": profile.primary_key_fields,
            # Existing rows/schema are preserved across repeated calls;
            # this only needs to run once, callers should still guard it
            # with ProviderSourceExtension.datastore_initialized.
            #
            # force=True: ckanext-datastore refuses to write to a
            # resource it considers "read-only" (one with a URL, as
            # ours has -- ensure_provider_resource sets it to the
            # provider's endpoint) unless told this is an intentional,
            # extension-managed write rather than a human's upload-form
            # edit. Confirmed by actually running a harvest job: without
            # this, every write failed with ckanext-datastore's own
            # "Cannot edit read-only resource" error.
            "force": True,
        })

    def upsert_rows(self, context: dict, resource_id: str, rows: list[dict]) -> None:
        if not rows:
            return
        self._get_action("datastore_upsert")(context, {
            "resource_id": resource_id,
            "records": rows,
            "method": "upsert",
            "force": True,
        })

    def load_all(self, context: dict, resource_id: str, rows: Iterable[dict],
                 *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> int:
        """Batches ``rows`` into ``datastore_upsert`` calls. Returns the
        total number of rows loaded. Callers processing a HarvestJob's
        objects one at a time should instead call ``upsert_rows`` directly
        per object/batch and track resumability themselves (see
        harvesters.base_generic) -- this helper is for bulk/file-based
        loads where all rows are already in hand.
        """
        total = 0
        batch: list[dict] = []
        for row in rows:
            batch.append(row)
            if len(batch) >= chunk_size:
                self.upsert_rows(context, resource_id, batch)
                total += len(batch)
                batch = []
        if batch:
            self.upsert_rows(context, resource_id, batch)
            total += len(batch)
        return total
