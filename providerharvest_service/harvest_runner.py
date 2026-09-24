"""Executes a real harvest run for a single provider source: connects
via the exact same ``_build_transport`` dispatch ``connection_check``
already proved, fetches records, and stores them in the dedicated
harvested-data PostgreSQL (``harvest_db.py`` / ``harvest_models.py``).

Manually triggered for this first slice ("Harvest now", a button on
the source's own page) -- an automatic scheduler that runs this on
each source's own ``frequency`` is the next slice, once this storage
pipeline itself is proven for real. Only ``transport_type="http"``
sources are supported so far -- SFTP/SCP/FTP are whole-file transports
with nowhere yet decided for the files themselves to land (see
README's roadmap); calling this against one raises ``ValueError``
rather than silently doing nothing useful.

Needs two separate database sessions, not one: the config DB (SQLite,
to look up the ``ProviderSource`` row and decrypt its credentials) and
the harvest DB (PostgreSQL, to store the run/records) are genuinely
different engines -- there is no single session that spans both.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import connection_check, provider_sources
from providerharvest_service.harvest_models import HarvestedRecord, HarvestRun
from providerharvest_service.models import _utcnow


class UnsupportedTransportType(ValueError):
    pass


def run_harvest(config_session: Session, harvest_session: Session, source_id: str) -> HarvestRun:
    """Raises ``provider_sources.NotFound`` if the source doesn't exist,
    ``UnsupportedTransportType`` if it isn't an HTTP source,
    ``provider_sources.InvalidTransition`` if it isn't ``active`` -- the
    UI only ever shows the "Harvest now" button for an active source,
    but that's not enough on its own: this route must refuse the same
    request too, not just hide the button, or a direct POST could
    bypass the approval workflow (harvesting a still-pending or
    rejected source's real endpoint). Otherwise always returns a
    persisted ``HarvestRun`` -- a failed fetch is recorded as
    ``status="error"`` with ``error_message`` set, not raised past this
    function, same "a failed check is exactly as valid a result as a
    successful one" rule ``connection_check`` uses.
    """
    source = provider_sources.get_or_404(config_session, source_id)
    if source.transport_type != "http":
        raise UnsupportedTransportType(
            "Manual harvest is only supported for transport_type='http' sources so far "
            "(got %r) -- SFTP/SCP/FTP file storage isn't designed yet." % source.transport_type
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
        with transport:
            page = transport.list_entries(cursor=None)

        for entry in page.entries:
            payload = json.loads(entry.inline_data.decode("utf-8")) if entry.inline_data else {}
            harvest_session.add(HarvestedRecord(run_id=run.id, ref=entry.ref, payload=payload))

        run.records_count = len(page.entries)
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
