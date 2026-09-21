"""Loads a streamed provider file as a CKAN resource, for
``delivery_mode="bulk_file"`` sources (SFTP/SCP/FTP -- whole files, not
per-record JSON like ``DataStoreLoader`` handles).

This loader does not talk to ckanext-datastore/xloader directly: creating
or updating a resource with an uploaded file is enough to trigger
ckanext-xloader's own automatic ``IResourceController`` hook, which
queues the actual DataStore load -- confirmed already wired up and firing
in this replica during the api_records/HTTP integration testing, so
there is nothing further for this loader to trigger by hand.
"""

from __future__ import annotations

import os
from typing import BinaryIO, Optional

from werkzeug.datastructures import FileStorage

#: Extension -> CKAN resource format label. Anything not listed here is
#: left blank rather than guessed -- xloader and CKAN's own format
#: badges handle an empty format fine, a wrong one is actively misleading.
_FORMAT_BY_EXTENSION = {
    ".csv": "CSV",
    ".tsv": "TSV",
    ".json": "JSON",
    ".xml": "XML",
    ".xlsx": "XLSX",
    ".xls": "XLS",
}


def _guess_format(filename: str) -> str:
    return _FORMAT_BY_EXTENSION.get(os.path.splitext(filename)[1].lower(), "")


class FileResourceLoader:
    def __init__(self, get_action):
        """``get_action``: CKAN's ``toolkit.get_action`` (injected for
        testability, same pattern as ``DataStoreLoader``)."""
        self._get_action = get_action

    def _find_existing_resource(self, context: dict, package_id: str,
                                 resource_name: str) -> Optional[dict]:
        package = self._get_action("package_show")(dict(context), {"id": package_id})
        for resource in package.get("resources", []):
            if resource["name"] == resource_name:
                return resource
        return None

    def load_stream(self, context: dict, package_id: str, filename: str,
                     stream: BinaryIO, *, resource_name: Optional[str] = None) -> dict:
        """Creates a resource for ``filename`` in ``package_id``, or
        updates it in place if a resource with the same name already
        exists there -- so repeated runs over an unchanged remote
        filename replace its content rather than accumulating
        duplicates, matching the "one remote file = one resource"
        model bulk-file sources use."""
        resource_name = resource_name or filename
        upload = FileStorage(stream=stream, filename=filename)
        data_dict = {
            "package_id": package_id,
            "name": resource_name,
            "upload": upload,
            "format": _guess_format(filename),
        }

        existing = self._find_existing_resource(context, package_id, resource_name)
        if existing is not None:
            data_dict["id"] = existing["id"]
            return self._get_action("resource_update")(dict(context), data_dict)
        return self._get_action("resource_create")(dict(context), data_dict)
