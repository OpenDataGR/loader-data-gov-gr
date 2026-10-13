"""Publishing is the one switch: an API source answers at /data/<name> by asking the provider on demand, a source that stores data answers with
its newest output, and what used to answer at /proxy/<name> was carried over once so nothing that was reachable stopped being reachable."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from providerharvest_service import proxy, provider_sources, publishing
from providerharvest_service.app import app
from providerharvest_service.models import LoadTarget
from tests.test_live_transform import (  # noqa: F401  (fixtures and helpers shared with the live-transform tests)
    ANSWER, JSON_BODY, FakeUpstreamResponse, _build, _flash, _http_source, _isolated_db, _isolated_harvest_db, _master_key, _clear_rate_limiters, client, source,
)
from tests.test_load_box import _publish_id


def _caller():
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def provider(monkeypatch):
    state = {"get": [], "post": [], "resp": FakeUpstreamResponse(content=json.dumps(ANSWER).encode())}
    monkeypatch.setattr(proxy.requests, "get", lambda url, **kw: (state["get"].append((url, kw)), state["resp"])[-1])
    monkeypatch.setattr(proxy.requests, "post", lambda url, **kw: (state["post"].append((url, kw)), state["resp"])[-1])
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    return state


# -- the one-time carry-over ------------------------------------------------------------------------------------------------------------------

def _legacy(session_factory, name, proxy_enabled, transport="http"):
    with session_factory() as session:
        source = provider_sources.register(session, {"name": name, "title": name, "endpoint_url": "https://x.example.gr/api", "transport_type": "http",
                                                     "auth_type": "api_key", "api_key": "k"})
        provider_sources.activate(session, source.id)
        source.proxy_enabled = proxy_enabled
        session.commit()
        return source.id


def _targets(session_factory):
    with session_factory() as session:
        return {t.source_id: (t.api_enabled, t.updated_by) for t in session.scalars(select(LoadTarget)).all()}


def test_a_source_that_had_a_live_proxy_is_published_once_and_the_old_flag_is_consumed(_isolated_db):
    ticked, unset, unticked = _legacy(_isolated_db, "ticked", True), _legacy(_isolated_db, "unset", None), _legacy(_isolated_db, "unticked", False)
    with _isolated_db() as session:
        assert publishing.migrate_legacy_proxy(session) == 2
    targets = _targets(_isolated_db)
    assert targets[ticked][0] is True and targets[unset][0] is True and unticked not in targets  # NULL meant "enabled", exactly as before
    assert "carried over" in targets[ticked][1]
    with _isolated_db() as session:
        assert all(provider_sources.get_or_404(session, i).proxy_enabled is False for i in (ticked, unset, unticked))


def test_the_carry_over_never_publishes_anything_registered_later_and_respects_a_choice_already_made(_isolated_db):
    first = _legacy(_isolated_db, "first", True)
    with _isolated_db() as session:
        publishing.migrate_legacy_proxy(session)
    later = _legacy(_isolated_db, "later", None)  # something new, with a leftover default
    with _isolated_db() as session:
        source = provider_sources.get_or_404(session, first)
        publishing.set_published(session, source, False, "an admin")  # unpublished on purpose
        session.commit()
        source.proxy_enabled = True
        session.commit()
        carried = publishing.migrate_legacy_proxy(session)
    assert carried == 1  # only "later" (it had no choice recorded); "first" already has one
    assert _targets(_isolated_db)[first][0] is False


def test_other_transports_are_left_alone(_isolated_db, source):
    with _isolated_db() as session:
        assert publishing.migrate_legacy_proxy(session) == 0  # the push source in the fixture is not an HTTP source
    assert _targets(_isolated_db) == {}


# -- /data/<name> for an API source ---------------------------------------------------------------------------------------------------------------

def test_a_published_get_api_is_asked_on_demand_at_the_data_address(client, source, _isolated_db, _isolated_harvest_db, provider):
    _http_source(_isolated_db, "feed")  # published
    resp = _caller().get("/data/feed", params={"category": "health"})
    assert resp.status_code == 200 and resp.json() == ANSWER
    (url, kw), = provider["get"]
    assert kw["params"]["category"] == "health" and kw["headers"]["X-API-Key"] == "k" and provider["post"] == []


def test_a_published_post_api_passes_the_body_on_and_nothing_is_stored(client, source, _isolated_db, _isolated_harvest_db, provider):
    _http_source(_isolated_db, "registry", post=True)
    body = '{"document": {"statements": {"platenumber": "TEST123"}}}'
    resp = _caller().post("/data/registry", content=body, headers=JSON_BODY)
    assert resp.status_code == 200 and resp.json() == ANSWER
    (url, kw), = provider["post"]
    assert kw["data"] == body.encode() and kw["headers"]["Content-Type"] == "application/json"


def test_an_unpublished_or_unknown_source_is_a_404_whatever_it_is(client, source, _isolated_db, _isolated_harvest_db, provider):
    _http_source(_isolated_db, "hidden", published=False)
    _http_source(_isolated_db, "waiting", activate=False)  # published, but not active
    for name in ("hidden", "waiting", "no-such"):
        assert _caller().get("/data/%s" % name).status_code == 404
        assert _caller().post("/data/%s" % name, content="{}", headers=JSON_BODY).status_code == 404
    assert provider["get"] == [] and provider["post"] == []


def test_the_publish_switch_turns_an_api_source_on_and_off(client, source, _isolated_db, _isolated_harvest_db, provider):
    source_id = _http_source(_isolated_db, "feed", published=False)
    assert _caller().get("/data/feed").status_code == 404
    assert "is published at /data/feed" in _flash(_publish_id(client, source_id))
    assert _caller().get("/data/feed").status_code == 200
    assert "no longer published" in _flash(_publish_id(client, source_id, on=False))
    assert _caller().get("/data/feed").status_code == 404


def test_the_request_settings_and_the_transform_apply_at_the_data_address(client, source, _isolated_db, _isolated_harvest_db, provider):
    from tests.test_request_swap import REGISTRY_BODY, _swap

    registry_id = _http_source(_isolated_db, "registry", post=True)
    _build(client, registry_id)  # keeps five fields, returns one object
    _swap(client, registry_id, template=REGISTRY_BODY)  # callers use GET
    resp = _caller().get("/data/registry", params={"plate": "ZMM3707"})
    assert resp.status_code == 200 and set(resp.json()) == {"plate_number", "make", "emporiki_onomasia_ochimatos", "colour", "kylindrismos"}
    assert json.loads(provider["post"][0][1]["data"])["document"]["statements"]["platenumber"] == "ZMM3707"
    assert _caller().post("/data/registry", content="{}", headers=JSON_BODY).status_code == 405  # the method callers use is GET now
    provider["resp"] = FakeUpstreamResponse(content=b"not json")
    broken = _caller().get("/data/registry", params={"plate": "x"})
    assert broken.status_code == 502 and "not json" not in broken.text  # fails closed


def test_a_source_that_stores_data_still_answers_with_its_output_and_takes_no_post(client, source, _isolated_db, _isolated_harvest_db, provider):
    from tests.test_load_box import _outputs_ready

    _outputs_ready(client, source, n=1)
    _publish_id(client, source["id"])
    assert _caller().get("/data/acme-push").status_code == 200
    refused = _caller().post("/data/acme-push", content="{}", headers=JSON_BODY)
    assert refused.status_code == 405 and refused.headers["allow"] == "GET"


def test_the_old_proxy_address_still_answers_for_a_published_source_and_follows_the_same_switch(client, source, _isolated_db, _isolated_harvest_db, provider):
    source_id = _http_source(_isolated_db, "feed")
    assert _caller().get("/proxy/feed").json() == _caller().get("/data/feed").json() == ANSWER
    _publish_id(client, source_id, on=False)
    assert _caller().get("/proxy/feed").status_code == 404 and _caller().get("/data/feed").status_code == 404


def test_the_per_caller_limit_applies_at_the_data_address(client, source, _isolated_db, _isolated_harvest_db, provider):
    _http_source(_isolated_db, "limited", proxy_rate_limit_per_ip="2")
    codes = [_caller().get("/data/limited").status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:]


def test_the_form_has_no_live_proxy_checkbox_any_more_and_says_where_publishing_is(client):
    page = client.get("/provider/sources/new").text
    # (the page also carries the Changelog, which names old commits: look for the form's own markup)
    assert '<input type="checkbox" id="proxy_enabled"' not in page and "Also expose this as a live proxy" not in page
    assert "Publish</em> switch" in page and "On the published address" in page and "How the provider is called" in page
