"""Publishing: the one switch that makes a source available to the outside, at ``/data/<name>`` (the Load Box).

A *published* source answers at ``/data/<name>``. For a source that stores data (a push source with a transform) that is its newest
good output; for an API source (HTTP) it is the provider's answer, fetched at the moment someone asks, reshaped by the source's
transform and request settings -- nothing is stored. Either way the same switch, the same address, and the same rule: nothing is
published until an admin switches it on, and only an active source answers.

``LoadTarget.api_enabled`` is the switch. (The old ``ProviderSource.proxy_enabled`` checkbox -- "also expose this as a live proxy" -- is
gone; ``migrate_legacy_proxy`` carried what it exposed over once, so nothing that was reachable stopped being reachable.)
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service.models import LoadTarget, ProviderSource

logger = logging.getLogger(__name__)


def is_published(session: Session, source_id: str) -> bool:
    target = session.scalar(select(LoadTarget).where(LoadTarget.source_id == source_id))
    return bool(target is not None and target.api_enabled)


def set_published(session: Session, source: ProviderSource, enabled: bool, who: str = "") -> LoadTarget:
    """Switch publishing on or off (the caller commits)."""
    target = session.scalar(select(LoadTarget).where(LoadTarget.source_id == source.id))
    if target is None:
        target = LoadTarget(source_id=source.id)
        session.add(target)
    target.api_enabled = enabled
    target.updated_by = who
    target.updated_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return target


def migrate_legacy_proxy(session: Session) -> int:
    """Once: an HTTP source that had its live proxy ticked (it answered at ``/proxy/<name>``) is published, so it keeps answering. The old
    flag is cleared as it is consumed, so this never publishes a source registered later. Returns how many were carried over."""
    carried = 0
    for source in session.scalars(select(ProviderSource).where(ProviderSource.transport_type == "http")).all():
        if source.proxy_enabled is False:
            continue
        if session.scalar(select(LoadTarget.id).where(LoadTarget.source_id == source.id)) is None:
            set_published(session, source, True, "carried over from the live proxy")
            carried += 1
        source.proxy_enabled = False
    session.commit()
    if carried:
        logger.info("publishing: %d HTTP source(s) that had a live proxy are now published", carried)
    return carried
