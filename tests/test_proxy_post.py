"""A source whose API is called with POST (a JSON body is passed through): proxy-only, one method per source."""

import pytest

from providerharvest_service import provider_sources, proxy, publishing
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
        publishing.set_published(session, source, True)
        session.commit()
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


def test_a_post_source_is_proxy_only_never_pulled(_isolated_db):
    from providerharvest_service import harvest_runner, scheduler

    source_id = _register_post_source(_isolated_db)
    with _isolated_db() as session:
        source = provider_sources.get_or_404(session, source_id)
        assert source.proxy_method == "POST" and source.proxy_only and source.frequency == "MANUAL"
        assert scheduler.due_sources(session, None) == []
        with pytest.raises(harvest_runner.UnsupportedTransportType, match="asked on demand"):
            harvest_runner.run_harvest(session, None, source_id)


@pytest.fixture
def checking(monkeypatch):
    """Records what a connection check sends: it must be one harmless GET, never a POST."""
    from providerharvest_service import connection_check

    state = {"get": [], "post": [], "response": FakeUpstreamResponse(status_code=405), "error": None}

    def fake_get(url, **kw):
        state["get"].append((url, kw))
        if state["error"]:
            raise state["error"]
        return state["response"]

    def never_post(*args, **kwargs):
        raise AssertionError("a connection check must never POST to a POST provider")

    monkeypatch.setattr(connection_check.requests, "get", fake_get)
    monkeypatch.setattr(connection_check.requests, "post", never_post)
    monkeypatch.setattr(connection_check, "assert_safe_http_url", lambda url: None)
    return state


def test_a_post_source_is_checked_with_one_harmless_get_with_the_credential_and_nothing_is_created(_isolated_db, checking):
    from providerharvest_service import connection_check

    source_id = _register_post_source(_isolated_db, api_key_prefix="Token", api_key="abc")
    with _isolated_db() as session:
        checked = connection_check.check_connection(session, source_id)
        assert checked.last_check_status == "ok" and "405" in checked.last_check_message and "nothing was created" in checked.last_check_message
        assert "Fetch a sample" in checked.last_check_message  # where the credential is really proven
    (url, kwargs), = checking["get"]
    assert url == "https://registry.example.gr/api/gates/USER/gate/documents/" and checking["post"] == []
    assert kwargs["headers"]["Authorization"] == "Token abc" and kwargs["allow_redirects"] is False and kwargs["timeout"] <= 15
    assert not kwargs.get("params") and "data" not in kwargs


@pytest.mark.parametrize("status_code,expected,fragment", [
    (200, "ok", "HTTP 200"), (204, "ok", "HTTP 204"), (302, "ok", "HTTP 302"), (405, "ok", "405"),
    (401, "error", "credential may be refused"), (403, "error", "HTTP 403"), (404, "error", "404 (not found)"), (500, "error", "server error"), (503, "error", "HTTP 503"),
])
def test_how_the_endpoint_answers_decides_the_result(_isolated_db, checking, status_code, expected, fragment):
    from providerharvest_service import connection_check

    source_id = _register_post_source(_isolated_db)
    checking["response"] = FakeUpstreamResponse(status_code=status_code)
    with _isolated_db() as session:
        checked = connection_check.check_connection(session, source_id)
        assert checked.last_check_status == expected and fragment in checked.last_check_message


def test_an_unreachable_provider_or_a_target_that_is_not_allowed_is_an_error_with_the_reason(_isolated_db, checking, monkeypatch):
    from providerharvest_service import connection_check

    source_id = _register_post_source(_isolated_db)
    checking["error"] = connection_check.requests.ConnectionError("name resolution failed")
    with _isolated_db() as session:
        checked = connection_check.check_connection(session, source_id)
        assert checked.last_check_status == "error" and "Not reachable" in checked.last_check_message and "name resolution" in checked.last_check_message

    def refuse(url):
        raise connection_check.UnsafeNetworkTargetError("private address")

    monkeypatch.setattr(connection_check, "assert_safe_http_url", refuse)
    with _isolated_db() as session:
        checked = connection_check.check_connection(session, source_id)
        assert checked.last_check_status == "error" and "not allowed" in checked.last_check_message and "private address" in checked.last_check_message


def test_an_ordinary_get_source_is_still_checked_the_usual_way(_isolated_db, monkeypatch):
    from providerharvest_service import connection_check

    source_id = _register_active_http_source(_isolated_db)

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def list_entries(self, cursor=None):
            class Page:
                entries = [1, 2]
            return Page()

    monkeypatch.setattr(connection_check, "_build_transport", lambda *a, **k: Fake())
    with _isolated_db() as session:
        checked = connection_check.check_connection(session, source_id)
        assert checked.last_check_status == "ok" and "found 2 records" in checked.last_check_message


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


def test_unpublishing_switches_a_post_source_off_and_publishing_brings_it_back(client, _isolated_db, upstream):
    source_id = _register_post_source(_isolated_db)
    assert client.post("/proxy/registry-vehicle", content="{}", headers=JSON).status_code == 200
    with _isolated_db() as session:
        publishing.set_published(session, provider_sources.get_or_404(session, source_id), False)
        session.commit()
        assert provider_sources.get_or_404(session, source_id).frequency == "MANUAL"  # still never pulled
    assert client.post("/proxy/registry-vehicle", content="{}", headers=JSON).status_code == 404  # nobody can call it
    with _isolated_db() as session:
        publishing.set_published(session, provider_sources.get_or_404(session, source_id), True)
        session.commit()
    assert client.post("/proxy/registry-vehicle", content="{}", headers=JSON).status_code == 200
