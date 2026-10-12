"""Fetching a sample answer from the provider: one real call with an admin-given test request, using the source's own credential, whose
answer is kept as the sample -- instead of pasting one."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from providerharvest_service import proxy
from providerharvest_service.app import app
from tests.test_live_transform import (  # noqa: F401  (fixtures and helpers shared with the live-transform tests)
    ANSWER, FakeUpstreamResponse, _build, _flash, _http_source, _isolated_db, _isolated_harvest_db, _master_key, _registry, client, source,
    _clear_rate_limiters,
)
from tests.test_request_swap import REGISTRY_BODY, _swap

TEST_BODY = '{"document": {"statements": {"platenumber": "TEST123"}}}'


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


def _fetch(client, source_id, **data):
    return client.post("/transform/sources/%s/fetch-sample" % source_id, data=data, follow_redirects=False)


def _sample_of(_isolated_db, source_id):
    from providerharvest_service import transform_runner

    with _isolated_db() as session:
        return transform_runner.get_transform(session, source_id).sample


def test_a_post_provider_is_asked_once_with_the_given_request_and_its_answer_becomes_the_sample(client, source, _isolated_db, provider):
    registry_id = _registry(_isolated_db)
    _transform_for(client, registry_id)

    resp = _fetch(client, registry_id, body=TEST_BODY)

    assert resp.status_code == 303 and resp.headers["location"].startswith("/transform/sources/%s/steps/new" % registry_id)  # straight to choosing fields
    assert "Sample fetched" in _flash(resp) and "choose the fields" in _flash(resp)
    assert json.loads(_sample_of(_isolated_db, registry_id)) == ANSWER
    (kind, url, kw), = provider["calls"]
    assert kind == "post" and kw["data"] == TEST_BODY.encode() and kw["headers"]["X-API-Key"] == "k" and kw["headers"]["Content-Type"] == "application/json"


def test_the_chooser_then_lists_the_fields_of_the_fetched_answer(client, source, _isolated_db, provider):
    registry_id = _registry(_isolated_db)
    _transform_for(client, registry_id)
    location = _fetch(client, registry_id, body=TEST_BODY).headers["location"].split("?")[0] + "?type=read"
    page = client.get(location).text
    assert "data/document/statements/make" in page and "FIAT AUTO SPA PUNTO" in page and "pasted sample.json" in page


def test_with_steps_already_built_the_page_is_shown_again_and_the_sample_is_replaced(client, source, _isolated_db, provider):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id, enable=False)
    provider["resp"] = FakeUpstreamResponse(content=b'{"data": {"document": {"statements": {"make": "SEAT"}}}}')
    resp = _fetch(client, registry_id, body=TEST_BODY)
    assert resp.headers["location"].startswith("/transform/sources/%s?" % registry_id) and "replaces the previous" in _flash(resp)
    assert "SEAT" in _sample_of(_isolated_db, registry_id)


def test_when_the_request_is_turned_around_the_test_values_fill_the_template(client, source, _isolated_db, provider):
    registry_id = _registry(_isolated_db)
    _transform_for(client, registry_id)
    _swap(client, registry_id)
    page = client.get("/transform/sources/%s" % registry_id).text
    assert 'name="param_plate"' in page and 'name="body"' not in page.split("Or fetch one from the provider")[1]
    resp = _fetch(client, registry_id, param_plate='TEST"123')
    assert "Sample fetched" in _flash(resp)
    sent = json.loads(provider["calls"][0][2]["data"])
    assert sent["document"]["statements"]["platenumber"] == 'TEST"123'  # escaped like any caller's value
    assert "The parameter 'plate' is missing" in _flash(_fetch(client, registry_id))


def test_a_get_provider_is_asked_with_the_given_query_parameters(client, source, _isolated_db, provider):
    source_id = _http_source(_isolated_db, "lookup")
    _transform_for(client, source_id)
    page = client.get("/transform/sources/%s" % source_id).text
    assert 'name="query"' in page
    assert "Sample fetched" in _flash(_fetch(client, source_id, query="?plate=AB1234&kind=car"))
    (kind, url, kw), = provider["calls"]
    assert kind == "get" and kw["params"]["plate"] == "AB1234" and kw["params"]["kind"] == "car" and "data" not in kw


@pytest.mark.parametrize("data,message", [
    ({"body": "not json"}, "not valid JSON"), ({"body": ""}, "Give the request"), ({"body": '{"x": "%s"}' % ("a" * 70000)}, "larger than"),
])
def test_a_request_that_does_not_hold_up_is_refused_before_anything_is_sent(client, source, _isolated_db, provider, data, message):
    registry_id = _registry(_isolated_db)
    _transform_for(client, registry_id)
    assert message in _flash(_fetch(client, registry_id, **data))
    assert provider["calls"] == [] and _sample_of(_isolated_db, registry_id) is None


def test_what_the_provider_gives_back_must_be_usable_json_and_otherwise_nothing_changes(client, source, _isolated_db, provider):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id, enable=False)
    kept = _sample_of(_isolated_db, registry_id)
    for response, message in ((FakeUpstreamResponse(status_code=403, content=b'{"detail": "no"}'), "HTTP 403"),
                              (FakeUpstreamResponse(content=b"<html>oops</html>"), "not valid JSON"),
                              (FakeUpstreamResponse(content=json.dumps({"x": "a" * 210000}).encode()), "larger than")):
        provider["resp"] = response
        flash = _flash(_fetch(client, registry_id, body=TEST_BODY))
        assert message in flash and "oops" not in flash and "no\"" not in flash  # the provider's content is not echoed
        assert _sample_of(_isolated_db, registry_id) == kept


def test_a_provider_that_cannot_be_reached_or_a_target_that_is_not_allowed_is_explained(client, source, _isolated_db, provider, monkeypatch):
    registry_id = _registry(_isolated_db)
    _transform_for(client, registry_id)
    provider["error"] = proxy.requests.ConnectionError("down")
    assert "The provider could not be asked" in _flash(_fetch(client, registry_id, body=TEST_BODY))
    provider["error"] = None

    def refuse(url):
        raise proxy.UnsafeNetworkTargetError("private address")

    monkeypatch.setattr(proxy, "assert_safe_http_url", refuse)
    assert "not reachable" in _flash(_fetch(client, registry_id, body=TEST_BODY))
    assert _sample_of(_isolated_db, registry_id) is None


def test_only_an_api_source_with_a_transform_has_this_and_only_a_signed_in_admin(client, source, _isolated_db, provider):
    assert "No such transform" in _flash(_fetch(client, source["id"], body=TEST_BODY))  # a push source with no transform
    client.post("/transform/sources/%s/create" % source["id"], follow_redirects=False)
    assert "Only an API source" in _flash(_fetch(client, source["id"], body=TEST_BODY))
    assert "Or fetch one from the provider" not in client.get("/transform/sources/%s" % source["id"]).text
    registry_id = _registry(_isolated_db)
    _transform_for(client, registry_id)
    anonymous = TestClient(app, base_url="https://testserver")
    resp = anonymous.post("/transform/sources/%s/fetch-sample" % registry_id, data={"body": TEST_BODY}, follow_redirects=False)
    assert resp.headers["location"].startswith("/admin/login") and provider["calls"] == []


def test_the_button_asks_for_confirmation_and_says_it_may_create_a_record(client, source, _isolated_db):
    registry_id = _registry(_isolated_db)
    _transform_for(client, registry_id)
    page = client.get("/transform/sources/%s" % registry_id).text
    assert "one real call" in page and "may create a record" in page and "return confirm(" in page


def test_the_shared_provider_call_still_serves_the_live_proxy(client, source, _isolated_db, provider):
    registry_id = _registry(_isolated_db)
    resp = TestClient(app, base_url="https://testserver").post("/proxy/registry", content="{}", headers={"Content-Type": "application/json"})
    assert resp.status_code == 200 and resp.json() == ANSWER and provider["calls"][0][0] == "post"
