import base64
import os

import pytest
from fastapi.testclient import TestClient

from providerharvest_service import db, provider_sources, proxy
from providerharvest_service.app import app


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "test.db"))
    session_factory = db.make_session_factory(engine)

    def _override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db.get_session] = _override
    yield session_factory
    app.dependency_overrides.pop(db.get_session, None)


@pytest.fixture(autouse=True)
def _clear_rate_limiters():
    # Module-level cache, keyed by source id -- a fresh isolated DB each
    # test means fresh source ids, but clear it anyway so no test can
    # ever depend on bucket state a previous test left behind.
    proxy._rate_limiters.clear()
    yield
    proxy._rate_limiters.clear()


@pytest.fixture
def client():
    return TestClient(app)


class FakeUpstreamResponse:
    def __init__(self, status_code=200, content=b"{}", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "application/json"}


def _register_active_http_source(session_factory, **overrides):
    form = {
        "name": "acme-open-data",
        "title": "Acme open data",
        "endpoint_url": "https://provider.example.gr/api/records",
        "transport_type": "http",
        "frequency": "DAILY",
        "auth_type": "api_key",
        "api_key": "the-real-secret-key",
        "api_key_location": "query",
        "api_key_name": "api_key",
        # Matches a real submission of the registration form, which
        # pre-checks this box -- an omitted key means "unchecked" (see
        # provider_sources.register's own reasoning), not "default on".
        "proxy_enabled": "on",
    }
    form.update(overrides)
    with session_factory() as session:
        source = provider_sources.register(session, form)
        provider_sources.activate(session, source.id)
        return source.id


def test_proxies_to_the_registered_endpoint_with_auth_injected(client, _isolated_db, monkeypatch):
    _register_active_http_source(_isolated_db)

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeUpstreamResponse(content=b'{"ok": true}')

    monkeypatch.setattr(proxy.requests, "get", fake_get)
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)

    resp = client.get("/proxy/acme-open-data", params={"category": "health"})

    assert resp.status_code == 200
    assert resp.content == b'{"ok": true}'
    assert resp.headers["content-type"] == "application/json"

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://provider.example.gr/api/records"
    assert kwargs["params"]["category"] == "health"
    assert kwargs["params"]["api_key"] == "the-real-secret-key"


def test_caller_cannot_override_the_injected_api_key(client, _isolated_db, monkeypatch):
    _register_active_http_source(_isolated_db)
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeUpstreamResponse()

    monkeypatch.setattr(proxy.requests, "get", fake_get)
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)

    client.get("/proxy/acme-open-data", params={"api_key": "attacker-supplied"})

    assert calls[0][1]["params"]["api_key"] == "the-real-secret-key"


def test_unknown_source_is_404(client):
    resp = client.get("/proxy/no-such-source")
    assert resp.status_code == 404


def test_pending_source_is_404_not_leaked(client, _isolated_db):
    with _isolated_db() as session:
        provider_sources.register(session, {
            "name": "still-pending", "title": "t",
            "endpoint_url": "https://provider.example.gr/api", "transport_type": "http",
            "auth_type": "api_key", "api_key": "k",
        })
    resp = client.get("/proxy/still-pending")
    assert resp.status_code == 404


def test_non_http_transport_source_is_404(client, _isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, {
            "name": "acme-sftp", "title": "t", "endpoint_url": "sftp.example.gr",
            "transport_type": "sftp", "sftp_username": "u", "sftp_password": "p",
            "host_key_fingerprint": "SHA256:abcdef",
        })
        provider_sources.activate(session, source.id)
    resp = client.get("/proxy/acme-sftp")
    assert resp.status_code == 404


def test_opted_out_active_http_source_is_404(client, _isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, {
            "name": "opted-out-source", "title": "t",
            "endpoint_url": "https://provider.example.gr/api", "transport_type": "http",
            "auth_type": "api_key", "api_key": "k",
            # No proxy_enabled key at all -- same as an unchecked box.
        })
        provider_sources.activate(session, source.id)
        assert source.proxy_enabled is False  # sanity check on the fixture itself
    resp = client.get("/proxy/opted-out-source")
    assert resp.status_code == 404


def test_legacy_row_with_null_proxy_enabled_is_still_proxied(client, _isolated_db, monkeypatch):
    """A source registered before the proxy_enabled column existed gets
    SQL NULL for it (db._sync_schema's ALTER TABLE ADD COLUMN has no
    way to backfill a Python-side default onto existing rows) -- that
    must still mean "enabled", matching the behavior every such source
    already had in production before this opt-out feature existed."""
    source_id = _register_active_http_source(_isolated_db, proxy_enabled="on")
    with _isolated_db() as session:
        row = provider_sources.get_or_404(session, source_id)
        row.proxy_enabled = None
        session.commit()

    def fake_get(url, **kwargs):
        return FakeUpstreamResponse(content=b'{"ok": true}')

    monkeypatch.setattr(proxy.requests, "get", fake_get)
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)

    resp = client.get("/proxy/acme-open-data")
    assert resp.status_code == 200


def test_upstream_connection_failure_is_502(client, _isolated_db, monkeypatch):
    import requests as requests_module

    _register_active_http_source(_isolated_db)

    def fake_get(url, **kwargs):
        raise requests_module.ConnectionError("connection refused")

    monkeypatch.setattr(proxy.requests, "get", fake_get)
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)

    resp = client.get("/proxy/acme-open-data")
    assert resp.status_code == 502


def test_unsafe_network_target_is_502_not_500(client, _isolated_db, monkeypatch):
    from providerharvest_service.engine.validators import UnsafeNetworkTargetError

    _register_active_http_source(_isolated_db)

    def fail(url):
        raise UnsafeNetworkTargetError("resolves to a blocked range")

    monkeypatch.setattr(proxy, "assert_safe_http_url", fail)

    resp = client.get("/proxy/acme-open-data")
    assert resp.status_code == 502


def test_upstream_response_headers_are_not_passed_through(client, _isolated_db, monkeypatch):
    """Only content/status/content-type are relayed -- an upstream
    Set-Cookie or WWW-Authenticate header must never reach the caller."""
    _register_active_http_source(_isolated_db)

    def fake_get(url, **kwargs):
        return FakeUpstreamResponse(headers={
            "content-type": "application/json",
            "set-cookie": "session=upstream-secret",
            "www-authenticate": "Bearer realm=upstream",
        })

    monkeypatch.setattr(proxy.requests, "get", fake_get)
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)

    resp = client.get("/proxy/acme-open-data")
    assert "set-cookie" not in resp.headers
    assert "www-authenticate" not in resp.headers
