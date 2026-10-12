"""Connection health checks: a real dry run against a provider's actual
endpoint, using the already-built and already-tested engine transports
(engine/transport/*) and auth strategies -- confirming reachability and
authentication right now, independent of the registration/approval
`status` machine (a still-pending source can be checked; a checked
source can still be rejected).

This is step 1 of "external connection to the source": it answers
"can we reach and authenticate against this right now", and persists
that answer -- it does NOT fetch or store any actual harvested
records/files. That's the scheduled-harvest slice this sets up for
next (see README's roadmap), which will reuse the exact same
``_build_transport`` dispatch.
"""

from __future__ import annotations

import datetime
import json

import requests
from sqlalchemy.orm import Session

from providerharvest_service import provider_sources
from providerharvest_service.engine.auth_strategies import build_auth_strategy
from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.transport.base import Transport
from providerharvest_service.engine.transport.direct_https import DirectHTTPSTransport
from providerharvest_service.engine.transport.ftp import FTPTransport
from providerharvest_service.engine.transport.scp import ScpTransport
from providerharvest_service.engine.transport.sftp import SFTPTransport
from providerharvest_service.engine.validators import UnsafeNetworkTargetError, assert_safe_http_url
from providerharvest_service.models import ProviderSource


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _build_transport(source: ProviderSource, secret: SecretBundle) -> Transport:
    config = json.loads(source.config or "{}")

    if source.transport_type == "http":
        return DirectHTTPSTransport(
            base_url=source.endpoint_url,
            auth_strategy=build_auth_strategy(source.auth_type),
            secret=secret,
            pagination=config.get("pagination", {}),
            auth_opts=config.get("auth_opts"),
            max_requests_per_minute=config.get("max_requests_per_minute", 60),
            request_timeout_s=config.get("request_timeout_s", 30),
            max_pages=config.get("max_pages", 1000),
            max_retries=config.get("max_retries", 5),
        )

    if source.transport_type in ("sftp", "scp"):
        transport_cls = SFTPTransport if source.transport_type == "sftp" else ScpTransport
        return transport_cls(
            config["host"],
            secret=secret,
            port=config.get("port", 22),
            remote_path=config.get("remote_path", "/"),
            glob_pattern=config.get("glob_pattern", "*"),
            pinned_host_key_fingerprint=config.get("host_key_fingerprint"),
            connect_timeout_s=config.get("connect_timeout_s", 30),
        )

    if source.transport_type == "ftp":
        return FTPTransport(
            config["host"],
            secret=secret,
            port=config.get("port", 21),
            remote_path=config.get("remote_path", "/"),
            glob_pattern=config.get("glob_pattern", "*"),
            use_tls=config.get("use_tls", True),
            plain_ftp_acknowledged=config.get("plain_ftp_acknowledged", False),
            connect_timeout_s=config.get("connect_timeout_s", 30),
        )

    raise ValueError("Unsupported transport_type %r" % source.transport_type)


def _check_post_provider(source: ProviderSource, secret: SecretBundle) -> tuple[str, str]:
    """(status, message) for an API that is called with POST. A real POST can create something at the provider, so this sends one harmless
    GET (never a POST) with the credential and reads how the endpoint answers -- enough to show the address, the network path and the
    certificate work, and whether the credential is refused outright. It never follows a redirect and creates nothing."""
    config = json.loads(source.config or "{}")
    try:
        assert_safe_http_url(source.endpoint_url)
    except UnsafeNetworkTargetError as exc:
        return "error", "The target is not allowed: %s" % exc
    try:
        kwargs = build_auth_strategy(source.auth_type).apply({"params": {}}, secret, config.get("auth_opts") or {})
        kwargs.setdefault("timeout", min(int(config.get("request_timeout_s", 30)), 15))
        response = requests.get(source.endpoint_url, allow_redirects=False, **kwargs)
    except requests.RequestException as exc:
        return "error", "Not reachable: %s" % exc
    code = response.status_code
    full_test = "The credential and the request are only proven by a real call: use \"Fetch a sample\" on the source's Transform page."
    if code in (401, 403):
        return "error", "Reachable, but the provider answered HTTP %d to a harmless GET: the credential may be refused (some APIs also refuse a GET for other reasons). %s" % (code, full_test)
    if code == 404:
        return "error", "The provider answered HTTP 404 (not found): check the endpoint address."
    if code >= 500:
        return "error", "The provider answered HTTP %d (a server error)." % code
    if code == 405:
        return "ok", "Reachable -- the endpoint answered HTTP 405 to a harmless GET, as a POST-only API does; nothing was created. %s" % full_test
    return "ok", "Reachable -- the endpoint answered HTTP %d to a harmless GET; nothing was created. %s" % (code, full_test)


def _check_with_test_request(session: Session, source: ProviderSource) -> tuple[str, str]:
    """(status, message): the admin gave a test request for this API, so the check is one real call with it -- the strongest test there
    is (it proves the address, the credential and the request). For a POST API that is a real POST, which may create a record at the
    provider; that is why the request has to be given on purpose, and why "Test connection" asks first. Nothing the provider sent back is
    repeated in the message."""
    from urllib.parse import parse_qsl

    from fastapi import HTTPException

    from providerharvest_service import proxy

    provider = source.proxy_method
    try:
        if provider == "POST":
            params, body = {}, source.test_request.encode("utf-8")
        else:
            params, body = dict(parse_qsl(source.test_request.lstrip("?"), keep_blank_values=True)), None
        response = proxy.call_provider(session, source, params, body)
    except HTTPException as exc:
        return "error", "Not reachable: %s" % exc.detail
    code = response.status_code
    real = "a real POST: it may have created a record there" if provider == "POST" else "a real GET"
    if 200 <= code < 300:
        return "ok", "Reachable -- the provider answered the test request with HTTP %d (%s)." % (code, real)
    if code in (401, 403):
        return "error", "The provider refused the credential (HTTP %d)." % code
    if code == 404:
        return "error", "The provider answered HTTP 404 (not found): check the endpoint address."
    if code >= 500:
        return "error", "The provider answered HTTP %d (a server error)." % code
    return "error", "The provider refused the test request (HTTP %d): check the test request on the source." % code


def check_connection(
    session: Session,
    source_id: str,
    *,
    transport: Transport | None = None,
) -> ProviderSource:
    """Connects, lists one page of entries, and disconnects -- then
    records the result (ok/error, a human-readable message, and the
    timestamp) on the source row regardless of outcome: a failed check
    is exactly as valid a result as a successful one, both get
    persisted, neither raises past this function.

    ``transport`` is accepted purely so tests can inject a fake rather
    than needing a real network target; real callers omit it and
    ``_build_transport`` resolves the right one from the source's own
    config plus its decrypted credentials.
    """
    source = provider_sources.get_or_404(session, source_id)

    if source.transport_type == "http" and transport is None and (source.test_request or source.proxy_only):
        # With a test request given on the source: one real call with it. Without one, a POST API is checked with a harmless GET (a real
        # POST can create something at the provider), and any other API the usual way below.
        try:
            if source.test_request:
                status, message = _check_with_test_request(session, source)
            else:
                status, message = _check_post_provider(source, provider_sources.credentials(session, source_id))
        except Exception as exc:  # noqa: BLE001 -- shown verbatim, this IS the diagnostic
            status, message = "error", str(exc)
        source.last_check_at = _utcnow()
        source.last_check_status = status
        source.last_check_message = message
        session.commit()
        return source

    status = "ok"
    try:
        if transport is None:
            secret = provider_sources.credentials(session, source_id)
            transport = _build_transport(source, secret)
        with transport:
            page = transport.list_entries(cursor=None)
        count = len(page.entries)
        noun = "record" if source.transport_type == "http" else "file"
        message = "Reachable -- found %d %s%s." % (count, noun, "" if count == 1 else "s")
    except Exception as exc:  # noqa: BLE001 -- shown verbatim, this IS the diagnostic
        status = "error"
        message = str(exc)

    source.last_check_at = _utcnow()
    source.last_check_status = status
    source.last_check_message = message
    session.commit()
    return source
