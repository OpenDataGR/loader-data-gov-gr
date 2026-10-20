"""Publishing: the one switch that makes a *pipeline* available to the outside (the Load Box).

A published pipeline answers at ``/data/<source>/<pipeline>``; the source's ``default`` pipeline also at ``/data/<source>``. For a source
that stores data (a push source) that is the pipeline's newest good output; for an API source (HTTP) it is the provider's answer, fetched
at the moment someone asks, reshaped by the pipeline and its request settings -- nothing is stored. A pipeline of an API source that is
switched off (or has no steps) passes the provider's answer through unchanged. Nothing is published until an admin switches it on, and
only an active source answers.

``Transform.published`` is the switch. (It used to be ``LoadTarget.api_enabled``, one per source, which ``migrate_to_pipelines`` carried over
once: the default pipeline of that source is published, so nothing that was reachable stopped being reachable.)
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import transform_runner
from providerharvest_service.models import LoadTarget, ProviderSource, Transform

logger = logging.getLogger(__name__)


def is_pipeline_published(pipeline: Transform | None) -> bool:
    return bool(pipeline is not None and pipeline.published)


def is_published(session: Session, source_id: str) -> bool:
    """Is the source's default pipeline (the one at /data/<source>) published?"""
    return is_pipeline_published(transform_runner.get_transform(session, source_id))


def published_pipeline(session: Session, source_id: str, name: str) -> Transform | None:
    pipeline = transform_runner.get_pipeline(session, source_id, name)
    return pipeline if is_pipeline_published(pipeline) else None


def set_pipeline_published(session: Session, pipeline: Transform, enabled: bool, who: str = "") -> Transform:
    """Switch one pipeline's publishing on or off (the caller commits)."""
    pipeline.published = enabled
    pipeline.updated_by = who or pipeline.updated_by
    pipeline.updated_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return pipeline


def set_published(session: Session, source: ProviderSource, enabled: bool, who: str = "") -> Transform | None:
    """Switch the source's *default* pipeline on or off. An API source that has no pipeline yet gets one with no steps, which passes
    the provider's answer through; a source that stores data cannot be published without one (None)."""
    pipeline = transform_runner.get_transform(session, source.id)
    if pipeline is None:
        if not enabled or source.transport_type != "http":
            return None
        pipeline = Transform(source_id=source.id, name=transform_runner.DEFAULT_PIPELINE, steps="[]", enabled=False, output_single=True, updated_by=who)
        session.add(pipeline)
    return set_pipeline_published(session, pipeline, enabled, who)


def ensure_published(session: Session, source: ProviderSource, pipeline: Transform, who: str = "") -> bool:
    """Make sure the pipeline has its public address (a link or a data service in CKAN needs it). True when it was added just now; raises
    ``ValueError`` with a sentence when the source cannot answer yet (the caller commits)."""
    if is_pipeline_published(pipeline):
        return False
    if source.status != "active":
        raise ValueError("%s is not active yet, so its public address cannot be added." % source.title)
    set_pipeline_published(session, pipeline, True, who)
    return True


def address_of(source: ProviderSource, pipeline: Transform) -> str:
    """The path a pipeline is published at: the default one at /data/<source>, any other at /data/<source>/<pipeline>."""
    if pipeline.name == transform_runner.DEFAULT_PIPELINE:
        return "/data/%s" % source.name
    return "/data/%s/%s" % (source.name, pipeline.name)


def migrate_to_pipelines(session: Session) -> int:
    """Once: a source whose address was switched on (``LoadTarget.api_enabled``) now has its default pipeline published -- created, with no
    steps, for an API source that had none. The old flag is cleared as it is consumed, so this never publishes anything later. Returns how
    many sources were carried over."""
    carried = 0
    for target in session.scalars(select(LoadTarget).where(LoadTarget.api_enabled.is_(True))).all():
        source = session.get(ProviderSource, target.source_id)
        if source is not None and set_published(session, source, True, target.updated_by or "carried over") is not None:
            carried += 1
        target.api_enabled = False
    session.commit()
    if carried:
        logger.info("publishing: %d source(s) now have their default pipeline published", carried)
    return carried
