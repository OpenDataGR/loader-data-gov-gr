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

Two independent rate limits, both per-process, in-memory TokenBuckets --
correct on a single-process deployment, but this service runs under
gunicorn with multiple worker processes (see
deploy/loader-data-gov-gr.service, ``-w 4``), each with its own
independent buckets. The *effective* ceiling in production is therefore
up to ~4x either configured number, not exact -- a real cross-process
limiter (Redis, or this service's own DB) is a follow-up once that
imprecision actually matters:

  * ``max_requests_per_minute`` -- this service's own total outbound
    call rate to the provider, shared across every caller of this
    source's proxy (the same budget a scheduled harvest run will also
    draw from, once that exists). Enforced with the blocking
    ``acquire()`` -- smoothing a burst by making the caller wait a
    little is fine here, since only ONE well-behaved caller's request
    is ever waiting on it at a time in the common case.
  * ``proxy_rate_limit_per_ip`` -- how much of that shared budget any
    single calling IP may consume. Enforced with the non-blocking
    ``try_acquire()`` instead: rejecting an abusive caller outright
    with 429 is correct here, where *blocking* would tie up a worker
    thread sleeping on behalf of the exact caller being throttled --
    with only `-w 4` workers, a handful of hammering callers doing
    that could exhaust the whole pool.

The per-IP bucket cache is bounded (see ``_MAX_TRACKED_CALLERS``) and
evicts least-recently-used entries -- unlike the per-source cache
(naturally bounded by how many sources exist), a per-(source, caller
IP) cache would otherwise grow forever over the life of a long-running
process.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import provider_sources, request_template
from providerharvest_service.client_addr import sender_address
from providerharvest_service.db import get_session
from providerharvest_service.engine.auth_strategies import build_auth_strategy
from providerharvest_service.engine.transport.rate_limit import TokenBucket
from providerharvest_service.engine.validators import UnsafeNetworkTargetError, assert_safe_http_url
from providerharvest_service.models import ProviderSource

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proxy", tags=["proxy"])

DEFAULT_TIMEOUT_S = 30
_MAX_TRACKED_CALLERS = 4096

#: Per-process, per-source rate limiters -- see module docstring for
#: why this is an approximate, not exact, ceiling in production.
_rate_limiters: dict[str, TokenBucket] = {}

#: Per-process, per-(source, caller IP) rate limiters, bounded + LRU-evicted.
_per_ip_limiters: "OrderedDict[tuple[str, str], TokenBucket]" = OrderedDict()


def _rate_limiter_for(source_id: str, max_requests_per_minute: int) -> TokenBucket:
    bucket = _rate_limiters.get(source_id)
    if bucket is None:
        bucket = TokenBucket(max_requests_per_minute)
        _rate_limiters[source_id] = bucket
    return bucket


def _per_ip_rate_limiter_for(source_id: str, client_ip: str, max_requests_per_minute: int) -> TokenBucket:
    key = (source_id, client_ip)
    bucket = _per_ip_limiters.get(key)
    if bucket is not None:
        _per_ip_limiters.move_to_end(key)
        return bucket
    if len(_per_ip_limiters) >= _MAX_TRACKED_CALLERS:
        _per_ip_limiters.popitem(last=False)
    bucket = TokenBucket(max_requests_per_minute)
    _per_ip_limiters[key] = bucket
    return bucket


def _get_active_http_source_or_404(session: Session, source_name: str) -> ProviderSource:
    source = session.scalar(select(ProviderSource).where(ProviderSource.name == source_name))
    # Same 404 whether the name doesn't exist, isn't an HTTP source,
    # isn't active yet, or had its proxy explicitly opted out -- a
    # pending/rejected/paused/opted-out source's existence (and
    # definitely its real endpoint) must not be discoverable via this
    # public route. `is False`, not falsiness: NULL means "predates the
    # proxy_enabled column", which must mean enabled (see models.py's
    # own reasoning), not disabled.
    if (
        source is None
        or source.transport_type != "http"
        or source.status != "active"
        or source.proxy_enabled is False
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such proxied source")
    return source


def request_is_swapped(transform) -> bool:
    return bool(transform is not None and transform.request_swap and (transform.request_template or "").strip())


def consumer_method(source: ProviderSource, transform) -> str:
    """The method callers use on /proxy/<name>: the provider's own, or the other one when the request is turned around."""
    provider = source.proxy_method
    if not request_is_swapped(transform):
        return provider
    return "GET" if provider == "POST" else "POST"


MAX_BODY_BYTES = 64 * 1024  # a POST body passed through to the provider (a lookup, not an upload)


def call_provider(session: Session, source: ProviderSource, params: dict, body: bytes | None):
    """One real call to the provider's endpoint, with the source's own credential, method (``proxy_method``), timeout and rate limit.
    The caller has already built ``params`` / ``body``. Raises ``HTTPException`` (502) when the target is not allowed or the call fails.
    Used by the live proxy and by the Transform Box's "fetch a sample"."""
    config = json.loads(source.config or "{}")
    try:
        assert_safe_http_url(source.endpoint_url)
    except UnsafeNetworkTargetError as exc:
        # Re-validated on every call (DNS can rebind between registration and now, same requirement as a scheduled harvest run) -- a
        # target that fails this now is a server misconfiguration/attack, not a caller error.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "upstream target is not reachable: %s" % exc) from exc

    secret = provider_sources.credentials(session, source.id)
    auth_strategy = build_auth_strategy(source.auth_type)
    request_kwargs = auth_strategy.apply({"params": params}, secret, config.get("auth_opts") or {})
    request_kwargs.setdefault("timeout", config.get("request_timeout_s", DEFAULT_TIMEOUT_S))
    provider = source.proxy_method
    if provider == "POST":
        headers = dict(request_kwargs.get("headers") or {})
        headers["Content-Type"] = "application/json"
        request_kwargs["headers"] = headers
        request_kwargs["data"] = body

    _rate_limiter_for(source.id, config.get("max_requests_per_minute", 60)).acquire()

    try:
        return (requests.post if provider == "POST" else requests.get)(source.endpoint_url, **request_kwargs)
    except requests.RequestException as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "upstream request failed: %s" % exc) from exc


def _forward(session: Session, source_name: str, method: str, query_params: dict, client_ip: str, body: bytes | None) -> Response:
    source = _get_active_http_source_or_404(session, source_name)
    config = json.loads(source.config or "{}")

    # One method per source for callers: the provider's own (the one it was registered with), or -- when the source's Transform page
    # turns the request around -- the other one. Nothing can be sent to a provider in a shape that was never agreed.
    from providerharvest_service import transform_runner

    transform = transform_runner.get_transform(session, source.id)
    swap = request_is_swapped(transform)
    allowed = consumer_method(source, transform)
    if method != allowed:
        raise HTTPException(
            status.HTTP_405_METHOD_NOT_ALLOWED, "this source's proxy answers %s requests" % allowed, headers={"Allow": allowed}
        )

    per_ip_bucket = _per_ip_rate_limiter_for(source.id, client_ip, config.get("proxy_rate_limit_per_ip", 30))
    if not per_ip_bucket.try_acquire():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded for this source -- try again shortly",
            headers={"Retry-After": "60"},
        )

    provider = source.proxy_method
    params, provider_body = dict(query_params), body
    if swap:
        # Only what the template names goes on: the caller's own parameters and fields are dropped.
        try:
            if provider == "POST":
                provider_body, params = request_template.build_json_body(transform.request_template, dict(query_params)), {}
            else:
                data = json.loads(body or b"null")
                params = request_template.build_query(transform.request_template, data)
        except request_template.RequestTemplateError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    upstream = call_provider(session, source, params, provider_body)

    if 200 <= upstream.status_code < 300:
        reshaped = _reshape(session, source, upstream.content)
        if reshaped is not None:
            return reshaped
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
    )


def _reshape(session: Session, source: ProviderSource, content: bytes) -> Response | None:
    """The provider's successful answer through the source's transform (the Transform Box), when one is switched on. None when
    there is no transform to apply. It fails closed: a transform that cannot be applied gives an error -- the unreshaped answer is
    never passed on instead, since a transform that keeps only some fields exists to keep the others from leaving."""
    from providerharvest_service import transform_engine, transform_runner

    transform = transform_runner.get_transform(session, source.id)
    if transform is None or not transform.enabled:
        return None
    try:
        result = transform_runner.apply(session, transform, content, "answer.json")
    except transform_engine.TransformError as exc:
        logger.warning("proxy %s: the transform could not be applied to the provider's answer: %s", source.name, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "the provider's answer could not be reshaped") from exc
    return Response(content=result.content, status_code=status.HTTP_200_OK, media_type=result.content_type)


@router.get("/{source_name}")
async def proxy_get(source_name: str, request: Request, session: Session = Depends(get_session)) -> Response:
    # The real caller, not the socket peer: behind the gateway and nginx that
    # is the gateway's own address, which would put every outside consumer in
    # one shared bucket (see client_addr.py).
    return await run_in_threadpool(
        _forward, session, source_name, "GET", dict(request.query_params), sender_address(request) or "unknown", None
    )


@router.post("/{source_name}")
async def proxy_post(source_name: str, request: Request, session: Session = Depends(get_session)) -> Response:
    """For a source registered with the POST method: the caller's JSON body is passed through, the loader adds the
    provider's credential. The body is checked here -- JSON only, small -- before anything leaves the loader."""
    if not (request.headers.get("content-type") or "").lower().startswith("application/json"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "send the body as application/json")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "the body is limited to %d bytes" % MAX_BODY_BYTES)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "the body is limited to %d bytes" % MAX_BODY_BYTES)
    try:
        json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the body is not valid JSON") from exc
    return await run_in_threadpool(
        _forward, session, source_name, "POST", dict(request.query_params), sender_address(request) or "unknown", body
    )
