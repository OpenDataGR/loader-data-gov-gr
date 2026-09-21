"""MVP transport: direct HTTPS to an IP-allowlisted provider endpoint.

Security properties this transport is responsible for maintaining:
  * TLS verification is always on (a custom CA bundle is configurable for
    providers using a private CA; there is no "skip verification" option).
  * The network target is (re-)validated via ``logic.validators`` on every
    ``connect()`` -- i.e. on every scheduled run, not just at registration.
  * Client-side rate limiting before sending, exponential backoff on
    429/5xx.
  * Every attempt is reported via ``on_request`` for audit logging --
    never headers/body, only metadata.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import time
from typing import BinaryIO, Callable, Optional
from urllib.parse import urlsplit

import requests

from providerharvest_service.engine.audit import OutboundRequestEvent
from providerharvest_service.engine.auth_strategies.base import AuthStrategy
from providerharvest_service.engine.validators import assert_safe_http_url
from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.transport.base import Entry, Page, Transport
from providerharvest_service.engine.transport.rate_limit import TokenBucket

DEFAULT_TIMEOUT_S = 30
DEFAULT_MAX_PAGES = 1000  # hard safety cap regardless of provider config
DEFAULT_MAX_RETRIES = 5
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _get_path(data: dict, path: str):
    """Minimal dotted-path getter: '$' means the value itself, otherwise
    'a.b.c' walks nested dict keys. Deliberately not a full JSONPath engine
    -- pagination envelopes are simple ("results", "data.items", "next")."""
    if path in ("$", "", None):
        return data
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class DirectHTTPSTransport(Transport):
    def __init__(
        self,
        base_url: str,
        auth_strategy: AuthStrategy,
        secret: SecretBundle,
        pagination: dict,
        *,
        auth_opts: dict | None = None,
        ca_bundle_path: str | None = None,
        max_requests_per_minute: int = 60,
        request_timeout_s: int = DEFAULT_TIMEOUT_S,
        max_pages: int = DEFAULT_MAX_PAGES,
        allow_private_ranges: bool = False,
        on_request: Optional[Callable[[OutboundRequestEvent], None]] = None,
        harvest_source_id: str | None = None,
        harvest_job_id: str | None = None,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ):
        self.base_url = base_url
        self._auth_strategy = auth_strategy
        self._secret = secret
        self._auth_opts = auth_opts or {}
        self._pagination = pagination or {}
        self._ca_bundle_path = ca_bundle_path
        self._rate_limiter = TokenBucket(max_requests_per_minute)
        self._timeout_s = request_timeout_s
        self._max_pages = max_pages
        self._allow_private_ranges = allow_private_ranges
        self._on_request = on_request
        self._harvest_source_id = harvest_source_id
        self._harvest_job_id = harvest_job_id
        self._session_factory = session_factory
        self._session: requests.Session | None = None
        self._pages_fetched = 0

    def connect(self) -> None:
        # Re-validated here (not just at provider registration time) so a
        # DNS-rebind between registration and this scheduled run is caught.
        assert_safe_http_url(self.base_url, allow_private_ranges=self._allow_private_ranges)
        self._session = self._session_factory()
        self._session.verify = self._ca_bundle_path if self._ca_bundle_path else True

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        # A no-op for most strategies; MTLSAuth uses this to remove the
        # temp files it wrote client cert/key material to.
        self._auth_strategy.close()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        assert self._session is not None, "connect() must be called before making requests"

        request_kwargs = self._auth_strategy.apply(kwargs, self._secret, self._auth_opts)
        request_kwargs.setdefault("timeout", self._timeout_s)

        parsed = urlsplit(url)
        attempt = 0
        while True:
            self._rate_limiter.acquire()
            started = time.monotonic()
            status = None
            error = None
            try:
                response = self._session.request(method, url, **request_kwargs)
                status = response.status_code
            except requests.RequestException as exc:
                error = str(exc)
                response = None
            duration_ms = (time.monotonic() - started) * 1000

            if self._on_request:
                self._on_request(OutboundRequestEvent(
                    harvest_source_id=self._harvest_source_id,
                    harvest_job_id=self._harvest_job_id,
                    host=parsed.hostname or "",
                    path=parsed.path or "/",
                    method=method,
                    status=status,
                    duration_ms=duration_ms,
                    auth_type_used=self._auth_strategy.name,
                    error=error,
                ))

            if response is not None and status not in RETRYABLE_STATUSES:
                response.raise_for_status()
                return response

            attempt += 1
            if attempt > DEFAULT_MAX_RETRIES:
                if response is not None:
                    response.raise_for_status()
                raise RuntimeError(
                    "Request to %s failed after %d retries: %s" % (url, attempt, error)
                )
            backoff = min(60, (2 ** attempt)) + random.uniform(0, 1)
            time.sleep(backoff)

    def _default_guid(self, record: dict) -> str:
        guid_field = self._pagination.get("guid_field")
        if guid_field and guid_field in record:
            return str(record[guid_field])
        digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode("utf-8")).hexdigest()
        return digest

    def list_entries(self, cursor: Optional[str]) -> Page:
        if self._pages_fetched >= self._max_pages:
            return Page(entries=[], next_cursor=None)
        self._pages_fetched += 1

        style = self._pagination.get("style", "page_number")
        params = dict(self._pagination.get("query_params") or {})
        if page_size := self._pagination.get("page_size"):
            params[self._pagination.get("page_size_param", "page_size")] = page_size

        if style == "page_number":
            page_param = self._pagination.get("page_param", "page")
            params[page_param] = int(cursor) if cursor else 1
        elif style == "cursor":
            cursor_param = self._pagination.get("cursor_param", "cursor")
            if cursor:
                params[cursor_param] = cursor
        else:
            raise ValueError("Unsupported pagination style %r" % style)

        response = self._request("GET", self.base_url, params=params)
        body = response.json()

        items_path = self._pagination.get("items_path", "$")
        records = _get_path(body, items_path) or []
        if not isinstance(records, list):
            raise ValueError(
                "pagination.items_path %r did not resolve to a list" % items_path
            )

        entries = [
            Entry(
                ref=self._default_guid(record),
                inline_data=json.dumps(record).encode("utf-8"),
            )
            for record in records
        ]

        next_cursor: Optional[str]
        if style == "page_number":
            next_cursor = str(params[page_param] + 1) if records else None
        else:  # cursor
            next_cursor_path = self._pagination.get("next_cursor_path", "next")
            next_cursor = _get_path(body, next_cursor_path)

        return Page(entries=entries, next_cursor=next_cursor)

    def open_entry(self, entry: Entry) -> BinaryIO:
        if entry.inline_data is None:
            raise ValueError(
                "Entry %r has no inline_data -- direct HTTPS api_records entries "
                "are expected to be fully fetched during list_entries" % entry.ref
            )
        return io.BytesIO(entry.inline_data)
