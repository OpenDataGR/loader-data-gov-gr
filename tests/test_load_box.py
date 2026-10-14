"""The Load Box: a source's newest transformed output at a public address, once an admin publishes it."""

import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from providerharvest_service import load_api, publishing
from providerharvest_service.app import app
from providerharvest_service.harvest_models import TransformedFile
from providerharvest_service.models import Transform
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _all_outputs, _flash, _isolated_db, _isolated_harvest_db, _master_key, _outputs_ready, client, source,
)


def _publish(client, source, on=True):
    return client.post("/load/sources/%s/api" % source["id"], data={"enabled": "1" if on else "0"}, follow_redirects=False)


def _publish_id(client, source_id, on=True):
    return client.post("/load/sources/%s/api" % source_id, data={"enabled": "1" if on else "0"}, follow_redirects=False)


def _anonymous():
    return TestClient(app, base_url="https://testserver")


def test_the_tab_lists_sources_and_nothing_is_published_by_default(client, source):
    page = client.get("/load").text
    assert "Load Box</a>" in page and "acme-push" in page and "no pipeline" in page and "Publish</button>" not in page
    assert _anonymous().get("/data/acme-push").status_code == 404


def test_publishing_needs_a_transform_and_then_serves_the_newest_output_to_anyone(client, source, _isolated_db, _isolated_harvest_db):
    assert "no transform yet" in _flash(_publish(client, source))
    _outputs_ready(client, source, n=2)
    assert "published at /data/acme-push" in _flash(_publish(client, source))
    with _isolated_db() as s:
        assert s.scalar(select(Transform.published).where(Transform.source_id == source["id"], Transform.name == "default")) is True
    stranger = _anonymous()
    resp = stranger.get("/data/acme-push")
    newest = _all_outputs(_isolated_harvest_db)[-1]
    assert resp.status_code == 200 and resp.content == newest.content
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["etag"] == '"%s"' % newest.id and "filename*=UTF-8" in resp.headers["content-disposition"]
    assert json.loads(resp.content)
    assert stranger.get("/data/acme-push/anything.json").content == newest.content  # a file-name ending for tools that want one
    assert stranger.get("/data/acme-push", headers={"If-None-Match": resp.headers["etag"]}).status_code == 304
    assert "published" in client.get("/load").text and "/data/acme-push" in client.get("/load").text


def test_unpublishing_and_unknown_or_inactive_sources_all_look_the_same(client, source, _isolated_db, _isolated_harvest_db):
    _outputs_ready(client, source)
    _publish(client, source)
    assert _anonymous().get("/data/acme-push").status_code == 200
    assert "no longer published" in _flash(_publish(client, source, on=False))
    assert _anonymous().get("/data/acme-push").status_code == 404
    assert _anonymous().get("/data/no-such-source").status_code == 404
    _publish(client, source)
    from providerharvest_service.models import ProviderSource
    with _isolated_db() as s:
        s.get(ProviderSource, source["id"]).status = "paused"
        s.commit()
    assert _anonymous().get("/data/acme-push").status_code == 404


def test_failed_or_emptied_outputs_are_never_served(client, source, _isolated_db, _isolated_harvest_db):
    _outputs_ready(client, source, n=1)
    _publish(client, source)
    with _isolated_harvest_db() as h:
        for row in h.scalars(select(TransformedFile)).all():
            row.content = b""  # retention emptied them
        h.commit()
    assert _anonymous().get("/data/acme-push").status_code == 404
    with _isolated_harvest_db() as h:
        assert load_api.latest_output(h, source["id"]) is None


def test_the_admin_page_needs_the_login_but_the_data_address_does_not(client, source):
    anonymous = _anonymous()
    assert anonymous.get("/load", follow_redirects=False).headers.get("location", "/admin/login").startswith("/admin/login")
    assert anonymous.post("/load/sources/%s/api" % source["id"], follow_redirects=False).headers.get("location", "/admin/login").startswith("/admin/login")
    assert "location" not in anonymous.get("/data/acme-push", follow_redirects=False).headers


def test_the_data_address_is_rate_limited_per_caller(client, source, _isolated_db, _isolated_harvest_db, monkeypatch):
    _outputs_ready(client, source, n=1)
    _publish(client, source)
    monkeypatch.setattr(load_api, "PER_IP_PER_MINUTE", 2)
    from providerharvest_service import proxy
    proxy._per_ip_limiters.clear()
    codes = [_anonymous().get("/data/acme-push").status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:]


def test_the_published_address_uses_the_configured_public_base_url(client, source, monkeypatch):
    monkeypatch.setenv("LOADER_PUBLIC_BASE_URL", "https://loader.example.gr/")
    from tests.test_transform_ui import _outputs_ready
    _outputs_ready(client, source, n=1)
    _publish(client, source)
    assert "https://loader.example.gr/data/acme-push" in client.get("/load").text


# -- the live proxy is part of the pipeline picture ----------------------------------------------------------------------------------

def _http_source(session_factory, name, *, post=False, activate=True, published=True, **extra):
    from providerharvest_service import provider_sources

    form = {"name": name, "title": name.title(), "transport_type": "http", "endpoint_url": "https://registry.example.gr/api/", "auth_type": "api_key",
            "api_key": "k", "proxy_rate_limit_per_ip": "7", **extra}
    if post:
        form["proxy_method"] = "POST"
    with session_factory() as session:
        source = provider_sources.register(session, form)
        if activate:
            provider_sources.activate(session, source.id)
        if published:
            publishing.set_published(session, source, True)
            session.commit()
        return source.id


def _row(page, name):
    return re.search(r"<tr>(?:(?!</tr>).)*?<span class=\"name-slug\">\(%s\)</span>.*?</tr>" % re.escape(name), page, re.S).group(0)


def test_an_api_source_is_published_at_the_same_address_pattern_and_says_it_stores_nothing(client, source, _isolated_db):
    registry_id = _http_source(_isolated_db, "registry", post=True)
    row = _row(client.get("/load").text, "registry")
    assert "asked on demand" in row and "nothing is stored" in row
    assert "published" in row and "POST" in row and "/data/registry" in row and "7/min per caller" in row
    assert 'href="https://testserver/data/registry"' not in row  # a POST address is not offered as a link (opening it would be a GET)
    assert "/load/sources/%s/api" % registry_id in row and "Unpublish" in row  # the same switch as every source
    assert 'href="/provider/sources/%s/edit"' % registry_id in row  # the settings live on the source


def test_a_source_that_is_not_active_yet_is_shown_as_published_but_not_live(client, source, _isolated_db):
    _http_source(_isolated_db, "waiting", post=True, activate=False)
    assert "published, not live (pending)" in _row(client.get("/load").text, "waiting")


def test_a_get_api_source_gets_a_link_and_an_unpublished_one_says_where_it_would_answer(client, source, _isolated_db):
    source_id = _http_source(_isolated_db, "openfeed")
    row = _row(client.get("/load").text, "openfeed")
    assert 'href="https://testserver/data/openfeed"' in row and "/load/sources/%s/api" % source_id in row
    _http_source(_isolated_db, "private-feed", published=False)
    private = _row(client.get("/load").text, "private-feed")
    assert "not published" in private and "would answer at" in private and "/data/private-feed" in private and "Publish</button>" in private


def test_an_api_source_can_be_published_without_a_transform_but_a_source_that_stores_data_cannot(client, source, _isolated_db):
    api_id = _http_source(_isolated_db, "feed", published=False)
    assert "is published at /data/feed" in _flash(_publish_id(client, api_id))
    assert "has no transform yet" in _flash(_publish_id(client, source["id"]))  # a push source: nothing to publish until it has a transform


def test_the_transform_box_offers_a_transform_for_an_api_source_and_says_it_acts_on_live_answers(client, source, _isolated_db):
    registry_id = _http_source(_isolated_db, "registry", post=True)
    page = client.get("/transform").text
    assert "acts on live answers" in page and "/transform/sources/%s" % registry_id in page
