"""The Load Box: a source's newest transformed output at a public address, once an admin publishes it."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from providerharvest_service import load_api
from providerharvest_service.app import app
from providerharvest_service.harvest_models import TransformedFile
from providerharvest_service.models import LoadTarget
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _all_outputs, _flash, _isolated_db, _isolated_harvest_db, _master_key, _outputs_ready, client, source,
)


def _publish(client, source, on=True):
    return client.post("/load/sources/%s/api" % source["id"], data={"enabled": "1" if on else "0"}, follow_redirects=False)


def _anonymous():
    return TestClient(app, base_url="https://testserver")


def test_the_tab_lists_sources_and_nothing_is_published_by_default(client, source):
    page = client.get("/load").text
    assert "Load Box</a>" in page and "acme-push" in page and "not published" in page
    assert _anonymous().get("/data/acme-push").status_code == 404


def test_publishing_needs_a_transform_and_then_serves_the_newest_output_to_anyone(client, source, _isolated_db, _isolated_harvest_db):
    assert "no transform yet" in _flash(_publish(client, source))
    _outputs_ready(client, source, n=2)
    assert "published at /data/acme-push" in _flash(_publish(client, source))
    with _isolated_db() as s:
        assert s.scalar(select(LoadTarget.api_enabled).where(LoadTarget.source_id == source["id"])) is True
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
