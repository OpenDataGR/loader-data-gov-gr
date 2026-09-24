"""Live API proxy -- for providers who don't want their own API
publicly reachable: a consumer calls loader.data.gov.gr's public
endpoint, loader calls the provider's actual (possibly private/
firewalled) endpoint right then with the provider's own stored
credentials injected, and relays the response straight back. This is
NOT the scheduled/pull harvest model (nothing here runs on a
timer and nothing gets stored) -- one live call in, one live call out,
same "fixed endpoint, query params passed through" shape
``connection_check`` already validates connectivity for.

Deliberately narrow, matching the agreed first slice:
  * GET only, forwarding to exactly the source's own ``endpoint_url``
    -- never a caller-supplied path -- with the caller's query
    parameters passed through (auth injection may add/overwrite its
    own reserved ones, e.g. an api_key query param).
  * Only ``transport_type="http"`` sources -- SFTP/SCP/FTP are
    whole-file transports, "one API call" has no equivalent there.
  * Only ``status="active"`` sources -- a still-pending or rejected
    source's real endpoint/credentials must never become reachable
    through this public route just because someone knows its name.
  * Upstream response headers are never passed through verbatim (only
    content/status/content-type) -- this must not leak the provider's
    own auth challenge headers, cookies, or anything else upstream-
    specific to the caller.

No consumer-level access control yet (deliberately, matching this
whole service's current "no auth wall yet" posture) -- anyone who
knows a source's name can call its proxy. Revisit once Keycloak-backed
per-consumer access control exists.

Rate limiting is a per-process, in-memory TokenBucket keyed by source
id -- correct on a single-process deployment, but this service runs
under gunicorn with multiple worker processes (see
deploy/loader-data-gov-gr.service, ``-w 4``), each with its own
independent bucket. The *effective* ceiling in production is therefore
up to ~4x a source's configured max_requests_per_minute, not exact --
a real cross-process limiter (Redis, or this service's own DB) is a
follow-up once that imprecision actually matters.
"""

from __future__ import annotations

import json

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import provider_sources
from providerharvest_service.db import get_session
from providerharvest_service.engine.auth_strategies import build_auth_strategy
from providerharvest_service.engine.transport.rate_limit import TokenBucket
from providerharvest_service.engine.validators import UnsafeNetworkTargetError, assert_safe_http_url
from providerharvest_service.models import ProviderSource

router = APIRouter(prefix="/proxy", tags=["proxy"])

DEFAULT_TIMEOUT_S = 30

#: Per-process, per-source rate limiters -- see module docstring for
#: why this is an approximate, not exact, ceiling in production.
_rate_limiters: dict[str, TokenBucket] = {}


def _rate_limiter_for(source_id: str, max_requests_per_minute: int) -> TokenBucket:
    bucket = _rate_limiters.get(source_id)
    if bucket is None:
        bucket = TokenBucket(max_requests_per_minute)
        _rate_limiters[source_id] = bucket
    return bucket


def _get_active_http_source_or_404(session: Session, source_name: str) -> ProviderSource:
    source = session.scalar(select(ProviderSource).where(ProviderSource.name == source_name))
    # Same 404 whether the name doesn't exist, isn't an HTTP source, or
    # isn't active yet -- a pending/rejected/paused source's existence
    # (and definitely its real endpoint) must not be discoverable via
    # this public route.
    if source is None or source.transport_type != "http" or source.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such proxied source")
    return source


@router.get("/{source_name}")
def proxy_call(
    source_name: str,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    source = _get_active_http_source_or_404(session, source_name)
    config = json.loads(source.config or "{}")

    try:
        assert_safe_http_url(source.endpoint_url)
    except UnsafeNetworkTargetError as exc:
        # Re-validated on every call (DNS can rebind between
        # registration and now, same requirement as a scheduled
        # harvest run) -- a target that fails this now is a server
        # misconfiguration/attack, not a caller error.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "upstream target is not reachable: %s" % exc) from exc

    secret = provider_sources.credentials(session, source.id)
    auth_strategy = build_auth_strategy(source.auth_type)
    request_kwargs = auth_strategy.apply(
        {"params": dict(request.query_params)}, secret, config.get("auth_opts") or {}
    )
    request_kwargs.setdefault("timeout", DEFAULT_TIMEOUT_S)

    _rate_limiter_for(source.id, config.get("max_requests_per_minute", 60)).acquire()

    try:
        upstream = requests.get(source.endpoint_url, **request_kwargs)
    except requests.RequestException as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "upstream request failed: %s" % exc) from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
    )
