"""Executes a real harvest run for a single provider source: connects
via the exact same ``_build_transport`` dispatch ``connection_check``
already proved, fetches records or whole files, and stores them in the
dedicated harvested-data PostgreSQL (``harvest_db.py`` /
``harvest_models.py``).

Two shapes, dispatched on ``transport_type``: ``"http"`` sources yield
per-record JSON (each entry's ``inline_data``, already fetched by the
listing call itself) stored as ``HarvestedRecord``; ``"sftp"``/
``"scp"``/``"ftp"`` sources are whole-file transports (each entry needs
its own ``open_entry`` stream, read while the transport connection is
still open) stored as ``HarvestedFile``. Any other ``transport_type``
raises ``UnsupportedTransportType`` rather than silently doing nothing
useful.

Manually triggered from the "Harvest now" button, and automatically by
``scheduler.py`` on each source's own ``frequency``.

Needs two separate database sessions, not one: the config DB (SQLite,
to look up the ``ProviderSource`` row and decrypt its credentials) and
the harvest DB (PostgreSQL, to store the run/records/files) are
genuinely different engines -- there is no single session that spans
both.
"""

from __future__ import annotations

import json
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import connection_check, provider_sources
from providerharvest_service.harvest_models import HarvestedFile, HarvestedRecord, HarvestRun
from providerharvest_service.models import _utcnow

FILE_TRANSPORT_TYPES = ("sftp", "scp", "ftp")
SUPPORTED_TRANSPORT_TYPES = ("http",) + FILE_TRANSPORT_TYPES

#: These sources are documented to feed several small files (each
#: fetch well under 30s) -- 20MB is generous headroom for that, while
#: still catching a misconfigured/unexpected source before it reads an
#: arbitrarily large file into memory and into a single Postgres row.
#: Configurable per source (``max_file_size_bytes`` in its own config
#: blob) for the rare source that genuinely needs more.
DEFAULT_MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024

_READ_CHUNK_BYTES = 65536

#: Explicit, not ``mimetypes.guess_type`` -- that module consults the
#: OS's own registered types (e.g. the Windows registry), which is
#: genuinely environment-dependent: confirmed the hard way that a dev
#: machine's Windows registry maps ``.csv`` to
#: ``application/vnd.ms-excel`` (an Excel file association), not the
#: standard ``text/csv`` a Linux production VM would give. A fixed
#: table behaves identically everywhere this process runs.
_CONTENT_TYPE_BY_EXTENSION = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".xml": "application/xml",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".zip": "application/zip",
    ".gz": "application/gzip",
}


class UnsupportedTransportType(ValueError):
    pass


class FileTooLargeError(RuntimeError):
    pass


def _read_capped(stream, max_bytes: int) -> bytes:
    """Reads ``stream`` in chunks rather than a single ``.read()`` --
    the file-based transports deliberately hand back streamed,
    unbuffered handles (see e.g. ``SFTPTransport.open_entry``'s own
    docstring: "these can be multi-GB provider exports") specifically
    so a caller isn't forced to load a whole file into memory just to
    find out it's too big. Raises before exceeding ``max_bytes``,
    rather than after -- the oversized content itself is never fully
    buffered."""
    chunks = []
    total = 0
    while True:
        chunk = stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise FileTooLargeError(
                "file exceeds max_file_size_bytes=%d (stopped after %d bytes read)" % (max_bytes, total)
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _guess_content_type(filename: str) -> str:
    extension = os.path.splitext(filename)[1].lower()
    return _CONTENT_TYPE_BY_EXTENSION.get(extension, "application/octet-stream")


def run_harvest(config_session: Session, harvest_session: Session, source_id: str) -> HarvestRun:
    """Raises ``provider_sources.NotFound`` if the source doesn't exist,
    ``UnsupportedTransportType`` if its ``transport_type`` isn't one of
    ``SUPPORTED_TRANSPORT_TYPES``, ``provider_sources.InvalidTransition``
    if it isn't ``active`` -- the UI only ever shows the "Harvest now"
    button for an active source, but that's not enough on its own: this
    route must refuse the same request too, not just hide the button,
    or a direct POST could bypass the approval workflow (harvesting a
    still-pending or rejected source's real endpoint). Otherwise always
    returns a persisted ``HarvestRun`` -- a failed fetch (including a
    file that trips ``FileTooLargeError``) is recorded as
    ``status="error"`` with ``error_message`` set, not raised past this
    function, same "a failed check is exactly as valid a result as a
    successful one" rule ``connection_check`` uses.
    """
    source = provider_sources.get_or_404(config_session, source_id)
    if source.transport_type not in SUPPORTED_TRANSPORT_TYPES:
        raise UnsupportedTransportType(
            "Manual harvest is only supported for transport_type in %s so far "
            "(got %r)." % (SUPPORTED_TRANSPORT_TYPES, source.transport_type)
        )
    if source.status != "active":
        raise provider_sources.InvalidTransition(
            "Only an active source can be harvested (current status: %r)" % source.status
        )

    run = HarvestRun(source_id=source.id, source_name=source.name, status="running")
    harvest_session.add(run)
    harvest_session.commit()

    try:
        secret = provider_sources.credentials(config_session, source.id)
        transport = connection_check._build_transport(source, secret)
        count = 0
        # The whole fetch (listing, and for file transports, each
        # individual file open+read) happens inside this one
        # connection -- unlike HTTP, where `list_entries` already
        # returns each record's full body, a file transport's entries
        # only become readable via `open_entry` on the *same*, still-
        # connected transport.
        with transport:
            page = transport.list_entries(cursor=None)

            if source.transport_type == "http":
                for entry in page.entries:
                    payload = json.loads(entry.inline_data.decode("utf-8")) if entry.inline_data else {}
                    harvest_session.add(HarvestedRecord(run_id=run.id, ref=entry.ref, payload=payload))
                    count += 1
            else:
                config = json.loads(source.config or "{}")
                max_file_size = config.get("max_file_size_bytes", DEFAULT_MAX_FILE_SIZE_BYTES)
                for entry in page.entries:
                    handle = transport.open_entry(entry)
                    try:
                        content = _read_capped(handle, max_file_size)
                    finally:
                        handle.close()
                    filename = entry.ref.rsplit("/", 1)[-1]
                    harvest_session.add(HarvestedFile(
                        run_id=run.id,
                        filename=filename,
                        content_type=_guess_content_type(filename),
                        size=len(content),
                        content=content,
                    ))
                    count += 1

        run.records_count = count
        run.status = "success"
    except Exception as exc:  # noqa: BLE001 -- recorded verbatim, this IS the diagnostic
        run.status = "error"
        run.error_message = str(exc)
    finally:
        run.finished_at = _utcnow()
        harvest_session.commit()

    return run


def list_runs(harvest_session: Session, source_id: str, limit: int = 20) -> list[HarvestRun]:
    return list(
        harvest_session.scalars(
            select(HarvestRun)
            .where(HarvestRun.source_id == source_id)
            .order_by(HarvestRun.started_at.desc())
            .limit(limit)
        ).all()
    )


def list_records(harvest_session: Session, run_id: str, limit: int = 50) -> list[HarvestedRecord]:
    return list(
        harvest_session.scalars(
            select(HarvestedRecord)
            .where(HarvestedRecord.run_id == run_id)
            .order_by(HarvestedRecord.fetched_at.asc())
            .limit(limit)
        ).all()
    )


def list_files(harvest_session: Session, run_id: str, limit: int = 50) -> list[HarvestedFile]:
    return list(
        harvest_session.scalars(
            select(HarvestedFile)
            .where(HarvestedFile.run_id == run_id)
            .order_by(HarvestedFile.fetched_at.asc())
            .limit(limit)
        ).all()
    )


def get_file(harvest_session: Session, file_id: str) -> HarvestedFile | None:
    return harvest_session.get(HarvestedFile, file_id)
