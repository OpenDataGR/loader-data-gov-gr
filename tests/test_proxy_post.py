"""A source whose API is called with POST (a JSON body is passed through): proxy-only, one method per source."""

import pytest

from providerharvest_service import provider_sources, proxy
from tests.test_proxy import (  # noqa: F401  (fixtures and helpers shared with the GET proxy tests)
    FakeUpstreamResponse, _clear_rate_limiters, _isolated_db, _master_key, _register_active_http_source, client,
)

JSON = {"Content-Type": "application/json"}


def _register_post_source(session_factory, **overrides):
    form = {
        "name": "registry-vehicle", "title": "Vehicle registry", "transport_type": "http", "proxy_method": "POST",
        "endpoint_url": "https://registry.example.gr/api/gates/USER/gate/documents/",
        "auth_type": "api_key", "api_key": "Token the-registry-token", "api_key_location": "header", "api_key_name": "Authorization",
        "request_timeout_s": "60",
    }
    form.update(overrides)
    with session_factory() as session:
        source = provider_sources.register(session, form)
        provider_sources.activate(session, source.id)
        return source.id


@pytest.fixture
def upstream(monkeypatch):
    calls = {"get": [], "post": []}
    monkeypatch.setattr(proxy.requests, "get", lambda url, **kw: (calls["get"].append((url, kw)), FakeUpstreamResponse())[-1])
    monkeypatch.setattr(proxy.requests, "post", lambda url, **kw: (calls["post"].append((url, kw)), FakeUpstreamResponse(
        content=b'{"data": {"document": {"statements": {"make": "SEAT"}}}}'))[-1])
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    return calls


def test_a_post_source_passes_the_json_body_on_with_the_providers_credential_added(client, _isolated_db, upstream):
    _register_post_source(_isolated_db)
    body = '{"document": {"statements": {"platenumber": "ZKY3395", "plate_number": "ZKY3395"}}}'

    resp = client.post("/proxy/registry-vehicle", content=body, headers=JSON)

    assert resp.status_code == 200 and b"SEAT" in resp.content
    url, kwargs = upstream["post"][0]
    assert url == "https://registry.example.gr/api/gates/USER/gate/documents/" and upstream["get"] == []
    assert kwargs["data"] == body.encode() and kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["Authorization"] == "Token the-registry-token"  # the loader's credential, never the caller's
    assert kwargs["timeout"] == 60


def test_a_caller_cannot_bring_their_own_credential_or_headers_to_the_provider(client, _isolated_db, upstream):
    _register_post_source(_isolated_db)
    client.post("/proxy/registry-vehicle", content="{}", headers={**JSON, "Authorization": "Token stolen", "X-Other": "x"})
    sent = upstream["post"][0][1]["headers"]
    assert sent["Authorization"] == "Token the-registry-token" and "X-Other" not in sent


def test_each_source_answers_only_the_method_it_was_registered_with(client, _isolated_db, upstream):
    _register_post_source(_isolated_db)
    _register_active_http_source(_isolated_db)  # an ordinary GET source
    assert client.get("/proxy/registry-vehicle").status_code == 405
    assert client.get("/proxy/registry-vehicle").headers["allow"] == "POST"
    resp = client.post("/proxy/acme-open-data", content="{}", headers=JSON)
    assert resp.status_code == 405 and resp.headers["allow"] == "GET"
    assert upstream["get"] == [] and upstream["post"] == []  # nothing reached any provider


@pytest.mark.parametrize("kwargs,status_code", [
    ({"content": "not json", "headers": JSON}, 400),
    ({"content": "{}", "headers": {"Content-Type": "text/plain"}}, 415),
    ({"content": "{}"}, 415),
    ({"content": '{"x": "%s"}' % ("a" * 70000), "headers": JSON}, 413),
])
def test_a_bad_body_is_refused_before_anything_is_sent(client, _isolated_db, upstream, kwargs, status_code):
    _register_post_source(_isolated_db)
    assert client.post("/proxy/registry-vehicle", **kwargs).status_code == status_code
    assert upstream["post"] == []


def test_the_post_proxy_is_still_hidden_and_limited_like_any_other(client, _isolated_db, upstream):
    assert client.post("/proxy/nobody", content="{}", headers=JSON).status_code == 404
    _register_post_source(_isolated_db, proxy_rate_limit_per_ip="2")
    codes = [client.post("/proxy/registry-vehicle", content="{}", headers=JSON).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:]


def test_upstream_trouble_with_a_post_is_a_502_and_a_providers_error_is_passed_on(client, _isolated_db, monkeypatch):
    _register_post_source(_isolated_db)
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    monkeypatch.setattr(proxy.requests, "post", lambda url, **kw: FakeUpstreamResponse(status_code=403, content=b'{"detail": "no"}'))
    assert client.post("/proxy/registry-vehicle", content="{}", headers=JSON).status_code == 403

    def boom(url, **kw):
        raise proxy.requests.ConnectionError("down")

    monkeypatch.setattr(proxy.requests, "post", boom)
    assert client.post("/proxy/registry-vehicle", content="{}", headers=JSON).status_code == 502


def test_a_post_source_is_proxy_only_never_pulled_never_checked_by_calling_it(_isolated_db, monkeypatch):
    from providerharvest_service import connection_check, harvest_runner, scheduler

    source_id = _register_post_source(_isolated_db)
    with _isolated_db() as session:
        source = provider_sources.get_or_404(session, source_id)
        assert source.proxy_method == "POST" and source.proxy_only and source.frequency == "MANUAL" and source.proxy_enabled is True
        assert scheduler.due_sources(session, None) == []
        with pytest.raises(harvest_runner.UnsupportedTransportType, match="live proxy"):
            harvest_runner.run_harvest(session, None, source_id)

        def must_not_run(*args, **kwargs):
            raise AssertionError("a check must not call a POST provider")

        monkeypatch.setattr(connection_check, "_build_transport", must_not_run)
        checked = connection_check.check_connection(session, source_id)
        assert checked.last_check_status is None and "live POST proxy" in checked.last_check_message


def test_the_method_survives_an_edit_and_a_wrong_one_is_refused(_isolated_db):
    source_id = _register_post_source(_isolated_db)
    form = {"name": "registry-vehicle", "title": "Vehicle registry (renamed)", "transport_type": "http", "proxy_method": "POST",
            "endpoint_url": "https://registry.example.gr/api/gates/USER/gate/documents/", "auth_type": "api_key",
            "api_key": "Token the-registry-token", "api_key_location": "header", "api_key_name": "Authorization", "frequency": "DAILY"}
    with _isolated_db() as session:
        provider_sources.update(session, source_id, form)
        source = provider_sources.get_or_404(session, source_id)
        assert source.proxy_method == "POST" and source.frequency == "MANUAL" and source.title.endswith("(renamed)")
        with pytest.raises(provider_sources.ValidationError) as excinfo:
            provider_sources.register(session, {**form, "name": "other", "proxy_method": "DELETE"})
        assert "proxy_method" in excinfo.value.errors


def test_an_ordinary_source_defaults_to_get_and_keeps_its_schedule(_isolated_db):
    source_id = _register_active_http_source(_isolated_db)
    with _isolated_db() as session:
        source = provider_sources.get_or_404(session, source_id)
        assert source.proxy_method == "GET" and not source.proxy_only and source.frequency == "DAILY"
