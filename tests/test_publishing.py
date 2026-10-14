"""Publishing is the one switch: an API source answers at /data/<name> by asking the provider on demand, a source that stores data answers with
its newest output, and what used to answer at /data/<name> was carried over once so nothing that was reachable stopped being reachable."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from providerharvest_service import proxy, provider_sources, publishing
from providerharvest_service.app import app
from providerharvest_service.models import LoadTarget, Transform
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


# -- the one-time carry-over onto pipelines ------------------------------------------------------------------------------------------------------

def _legacy(session_factory, name, enabled, *, with_pipeline=False):
    """An HTTP source whose address was switched on (or off) the old way: one LoadTarget per source."""
    with session_factory() as session:
        source = provider_sources.register(session, {"name": name, "title": name, "endpoint_url": "https://x.example.gr/api", "transport_type": "http",
                                                     "auth_type": "api_key", "api_key": "k"})
        provider_sources.activate(session, source.id)
        session.add(LoadTarget(source_id=source.id, api_enabled=enabled, updated_by="an admin"))
        if with_pipeline:
            session.add(Transform(source_id=source.id, steps="[]", enabled=True))
        session.commit()
        return source.id


def _pipelines(session_factory):
    with session_factory() as session:
        return {(t.source_id, t.name): (bool(t.published), t.enabled) for t in session.scalars(select(Transform)).all()}


def test_a_source_that_was_published_has_its_default_pipeline_published_once_and_the_old_flag_is_consumed(_isolated_db):
    bare, shaped, off = _legacy(_isolated_db, "bare", True), _legacy(_isolated_db, "shaped", True, with_pipeline=True), _legacy(_isolated_db, "off", False)
    with _isolated_db() as session:
        assert publishing.migrate_to_pipelines(session) == 2
    pipelines = _pipelines(_isolated_db)
    assert pipelines[(bare, "default")] == (True, False)  # an API source with none: a pipeline without steps, which passes the answer through
    assert pipelines[(shaped, "default")] == (True, True)  # an existing pipeline is the one that is published, as it was
    assert (off, "default") not in pipelines  # not published stays not published
    with _isolated_db() as session:
        assert not session.scalars(select(LoadTarget).where(LoadTarget.api_enabled.is_(True))).all()  # consumed


def test_the_carry_over_runs_once_and_never_publishes_anything_later(_isolated_db):
    first = _legacy(_isolated_db, "first", True)
    with _isolated_db() as session:
        assert publishing.migrate_to_pipelines(session) == 1
        publishing.set_published(session, provider_sources.get_or_404(session, first), False, "an admin")  # unpublished on purpose
        session.commit()
        assert publishing.migrate_to_pipelines(session) == 0  # the flag was consumed: the choice stays
    assert _pipelines(_isolated_db)[(first, "default")][0] is False


def test_a_source_that_stores_data_and_has_no_pipeline_cannot_be_carried_over(_isolated_db, source):
    with _isolated_db() as session:
        session.add(LoadTarget(source_id=source["id"], api_enabled=True))
        session.commit()
        assert publishing.migrate_to_pipelines(session) == 0
    assert _pipelines(_isolated_db) == {}


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
    assert _caller().get("/data/feed").json() == _caller().get("/data/feed").json() == ANSWER
    _publish_id(client, source_id, on=False)
    assert _caller().get("/data/feed").status_code == 404 and _caller().get("/data/feed").status_code == 404


def test_the_per_caller_limit_applies_at_the_data_address(client, source, _isolated_db, _isolated_harvest_db, provider):
    _http_source(_isolated_db, "limited", proxy_rate_limit_per_ip="2")
    codes = [_caller().get("/data/limited").status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:]


def test_the_form_has_no_live_proxy_checkbox_any_more_and_says_where_publishing_is(client):
    page = client.get("/provider/sources/new").text
    # (the page also carries the Changelog, which names old commits: look for the form's own markup)
    assert '<input type="checkbox" id="proxy_enabled"' not in page and "Also expose this as a live proxy" not in page
    assert "Publish</em> switch" in page and "On the published address" in page and "How the provider is called" in page
