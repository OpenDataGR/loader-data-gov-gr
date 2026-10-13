"""Fetching a sample answer from the provider: one real call with the source's test request (set on the source), made with its own
credential, whose answer is kept as the sample -- instead of pasting one."""

import json

import pytest
from fastapi.testclient import TestClient

from providerharvest_service import proxy
from providerharvest_service.app import app
from tests.test_live_transform import (  # noqa: F401  (fixtures and helpers shared with the live-transform tests)
    ANSWER, FakeUpstreamResponse, _build, _flash, _http_source, _isolated_db, _isolated_harvest_db, _master_key, client, source,
    _clear_rate_limiters,
)

TEST_BODY = '{"document": {"statements": {"platenumber": "TEST123"}}}'


def _post_source(_isolated_db, name="registry", **extra):
    return _http_source(_isolated_db, name, post=True, test_request=TEST_BODY, **extra)


@pytest.fixture
def provider(monkeypatch):
    state = {"calls": [], "resp": FakeUpstreamResponse(content=json.dumps(ANSWER).encode()), "error": None}

    def call(kind):
        def fake(url, **kw):
            state["calls"].append((kind, url, kw))
            if state["error"]:
                raise state["error"]
            return state["resp"]
        return fake

    monkeypatch.setattr(proxy.requests, "post", call("post"))
    monkeypatch.setattr(proxy.requests, "get", call("get"))
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    return state


def _transform_for(client, source_id):
    client.post("/transform/sources/%s/create" % source_id, follow_redirects=False)


def _fetch(client, source_id):
    return client.post("/transform/sources/%s/fetch-sample" % source_id, follow_redirects=False)


def _sample_of(_isolated_db, source_id):
    from providerharvest_service import transform_runner

    with _isolated_db() as session:
        return transform_runner.get_transform(session, source_id).sample


def test_a_post_provider_is_asked_once_with_the_sources_test_request_and_its_answer_becomes_the_sample(client, source, _isolated_db, provider):
    registry_id = _post_source(_isolated_db)
    _transform_for(client, registry_id)

    resp = _fetch(client, registry_id)

    assert resp.status_code == 303 and resp.headers["location"].startswith("/transform/sources/%s/steps/new" % registry_id)  # straight to choosing fields
    assert "Sample fetched" in _flash(resp) and "choose the fields" in _flash(resp)
    assert json.loads(_sample_of(_isolated_db, registry_id)) == ANSWER
    (kind, url, kw), = provider["calls"]
    assert kind == "post" and kw["data"] == TEST_BODY.encode() and kw["headers"]["X-API-Key"] == "k" and kw["headers"]["Content-Type"] == "application/json"


def test_the_chooser_then_lists_the_fields_of_the_fetched_answer(client, source, _isolated_db, provider):
    registry_id = _post_source(_isolated_db)
    _transform_for(client, registry_id)
    location = _fetch(client, registry_id).headers["location"].split("?")[0] + "?type=read"
    page = client.get(location).text
    assert "data/document/statements/make" in page and "FIAT AUTO SPA PUNTO" in page and "pasted sample.json" in page


def test_with_steps_already_built_the_page_is_shown_again_and_the_sample_is_replaced(client, source, _isolated_db, provider):
    registry_id = _http_source(_isolated_db, "registry", post=True, test_request=TEST_BODY)
    _build(client, registry_id, enable=False)
    provider["resp"] = FakeUpstreamResponse(content=b'{"data": {"document": {"statements": {"make": "SEAT"}}}}')
    resp = _fetch(client, registry_id)
    assert resp.headers["location"].startswith("/transform/sources/%s/steps/input?" % registry_id) and "replaces the previous" in _flash(resp)
    assert "SEAT" in _sample_of(_isolated_db, registry_id)


def test_the_request_is_the_providers_own_whatever_callers_send(client, source, _isolated_db, provider):
    from tests.test_request_swap import REGISTRY_BODY, _swap

    registry_id = _post_source(_isolated_db)
    _transform_for(client, registry_id)
    _swap(client, registry_id, template=REGISTRY_BODY)  # callers use GET; the test request is still what the provider expects
    assert "Sample fetched" in _flash(_fetch(client, registry_id))
    assert provider["calls"][0][2]["data"] == TEST_BODY.encode()


def test_a_get_provider_is_asked_with_the_test_query_parameters(client, source, _isolated_db, provider):
    source_id = _http_source(_isolated_db, "lookup", test_request="?plate=AB1234&kind=car")
    _transform_for(client, source_id)
    assert "Sample fetched" in _flash(_fetch(client, source_id))
    (kind, url, kw), = provider["calls"]
    assert kind == "get" and kw["params"]["plate"] == "AB1234" and kw["params"]["kind"] == "car" and "data" not in kw


def test_without_a_test_request_there_is_nothing_to_send_and_the_page_says_where_to_set_it(client, source, _isolated_db, provider):
    registry_id = _http_source(_isolated_db, "registry", post=True)
    _transform_for(client, registry_id)
    assert "no test request" in _flash(_fetch(client, registry_id)) and provider["calls"] == []
    page = client.get("/transform/sources/%s/steps/input" % registry_id).text
    assert "Give this source a" in page and "/provider/sources/%s/edit" % registry_id in page and "Fetch a sample</button>" not in page


def test_the_page_shows_the_test_request_that_will_be_sent_and_asks_for_confirmation(client, source, _isolated_db):
    registry_id = _post_source(_isolated_db)
    _transform_for(client, registry_id)
    page = client.get("/transform/sources/%s/steps/input" % registry_id).text
    assert "TEST123" in page and "one real call" in page and "may create a record" in page and "return confirm(" in page
    assert "Fetch a sample</button>" in page


def test_what_the_provider_gives_back_must_be_usable_json_and_otherwise_nothing_changes(client, source, _isolated_db, provider):
    registry_id = _post_source(_isolated_db)
    _build(client, registry_id, enable=False)
    kept = _sample_of(_isolated_db, registry_id)
    for response, message in ((FakeUpstreamResponse(status_code=403, content=b'{"detail": "no"}'), "HTTP 403"),
                              (FakeUpstreamResponse(content=b"<html>oops</html>"), "not valid JSON"),
                              (FakeUpstreamResponse(content=json.dumps({"x": "a" * 210000}).encode()), "larger than")):
        provider["resp"] = response
        flash = _flash(_fetch(client, registry_id))
        assert message in flash and "oops" not in flash and "no\"" not in flash  # the provider's content is not echoed
        assert _sample_of(_isolated_db, registry_id) == kept


def test_a_provider_that_cannot_be_reached_or_a_target_that_is_not_allowed_is_explained(client, source, _isolated_db, provider, monkeypatch):
    registry_id = _post_source(_isolated_db)
    _transform_for(client, registry_id)
    provider["error"] = proxy.requests.ConnectionError("down")
    assert "The provider could not be asked" in _flash(_fetch(client, registry_id))
    provider["error"] = None

    def refuse(url):
        raise proxy.UnsafeNetworkTargetError("private address")

    monkeypatch.setattr(proxy, "assert_safe_http_url", refuse)
    assert "not reachable" in _flash(_fetch(client, registry_id))
    assert _sample_of(_isolated_db, registry_id) is None


def test_only_an_api_source_with_a_transform_has_this_and_only_a_signed_in_admin(client, source, _isolated_db, provider):
    assert "No such transform" in _flash(_fetch(client, source["id"]))  # a push source with no transform
    client.post("/transform/sources/%s/create" % source["id"], follow_redirects=False)
    assert "Only an API source" in _flash(_fetch(client, source["id"]))
    page = client.get("/transform/sources/%s/steps/input" % source["id"]).text  # a push source's Input page: no sample, no fetch -- the newest file
    assert "Or fetch one from the provider" not in page and "newest stored file" in page
    registry_id = _post_source(_isolated_db)
    _transform_for(client, registry_id)
    anonymous = TestClient(app, base_url="https://testserver")
    resp = anonymous.post("/transform/sources/%s/fetch-sample" % registry_id, follow_redirects=False)
    assert resp.headers["location"].startswith("/admin/login") and provider["calls"] == []


def test_the_shared_provider_call_still_serves_the_live_proxy(client, source, _isolated_db, provider):
    _post_source(_isolated_db)
    resp = TestClient(app, base_url="https://testserver").post("/proxy/registry", content="{}", headers={"Content-Type": "application/json"})
    assert resp.status_code == 200 and resp.json() == ANSWER and provider["calls"][0][0] == "post"
