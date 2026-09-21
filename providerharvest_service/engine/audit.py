"""Shared audit-event shape for outbound provider connections.

Transports report every attempt through this dataclass via an ``on_request``
callback rather than importing CKAN's model directly, so they stay
unit-testable in isolation. ``model.audit_log`` provides the callback that
actually persists these as ``OutboundRequestLog`` rows.

Never include request/response headers or bodies here -- that is where
credentials live. Only metadata.
"""

from __future__ import annotations

import dataclasses
import datetime


@dataclasses.dataclass
class OutboundRequestEvent:
    harvest_source_id: str
    harvest_job_id: str | None
    host: str
    path: str  # path only, no querystring (querystring may carry API keys)
    method: str
    status: int | None  # None if the request never got a response (timeout, DNS failure, ...)
    duration_ms: float
    auth_type_used: str
    timestamp: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    error: str | None = None
