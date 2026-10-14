"""Each pipeline is published on its own: /data/<source>/<pipeline>, the default one also at /data/<source>; an API source's pipelines each have
their own request settings; and the old /proxy address is gone."""

import json

from fastapi.testclient import TestClient

from providerharvest_service.app import app
from tests.test_live_transform import (  # noqa: F401  (fixtures and helpers shared with the live-transform tests)
    ANSWER, JSON_BODY, _build, _caller, _flash, _http_source, _isolated_db, _isolated_harvest_db, _master_key, answer, client, source,
)
from tests.test_pipelines_ui import _add, _pipeline, _setup
from tests.test_request_swap import REGISTRY_BODY
from tests.test_transform_ui import _go, _upload_data


def _stranger():
    return TestClient(app, base_url="https://testserver")


def _publish(client, source_id, pipeline=None, on=True):
    path = "/load/sources/%s%s/api" % (source_id, "" if pipeline in (None, "default") else "/pipelines/%s" % pipeline)
    return client.post(path, data={"enabled": "1" if on else "0"}, follow_redirects=False)


# -- a source that stores data ---------------------------------------------------------------------------------------------------------

def test_each_pipeline_answers_at_its_own_address_and_only_when_published(client, source):
    _setup(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _add(client, source, "table")
    _pipeline(client, source, "table", "output", data={"format": "csv", "delimiter": ","})
    _pipeline(client, source, "table", "enabled", data={"enabled": "1"})
    _upload_data(client, source)  # one file: both pipelines store an output
    stranger = _stranger()
    assert stranger.get("/data/acme-push").status_code == 404 and stranger.get("/data/acme-push/table").status_code == 404  # nothing is published yet

    assert "published at /data/acme-push/table" in _flash(_publish(client, source["id"], "table"))
    csv = stranger.get("/data/acme-push/table")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv") and csv.text.lstrip("﻿").startswith("id,")
    assert stranger.get("/data/acme-push").status_code == 404  # the default pipeline is not published: the other one does not answer for it
    assert stranger.get("/data/acme-push/table/report.csv").content == csv.content  # a file name after the pipeline is ignored

    assert "published at /data/acme-push." in _flash(_publish(client, source["id"]))
    default = stranger.get("/data/acme-push")
    assert default.status_code == 200 and default.headers["content-type"].startswith("application/json") and json.loads(default.content)
    assert stranger.get("/data/acme-push/default").content == default.content  # the default one is also at its own name
    assert stranger.get("/data/acme-push/anything.json").content == default.content  # an unknown second segment is a file name for the default one

    assert "no longer published" in _flash(_publish(client, source["id"], "table", on=False))
    assert stranger.get("/data/acme-push/table").status_code == 404 and stranger.get("/data/acme-push").status_code == 200


def test_an_unpublished_pipeline_never_falls_back_to_the_default(client, source):
    _setup(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _add(client, source, "other")
    _upload_data(client, source)
    _publish(client, source["id"])
    stranger = _stranger()
    assert stranger.get("/data/acme-push").status_code == 200
    assert stranger.get("/data/acme-push/other").status_code == 404  # it exists, but is not published
    assert "no pipeline called" in _flash(_publish(client, source["id"], "ghost"))
    assert stranger.get("/data/acme-push/ghost.json").status_code == 200  # (not a pipeline: it is read as a file name of the default one)


def test_the_load_box_has_a_row_and_a_switch_for_every_pipeline(client, source):
    _setup(client, source)
    _add(client, source, "table")
    page = client.get("/load").text
    assert page.count('<div class="name-title">default</div>') == 1 and page.count('<div class="name-title">table</div>') == 1
    assert 'action="/load/sources/%s/api"' % source["id"] in page and 'action="/load/sources/%s/pipelines/table/api"' % source["id"] in page
    assert "would answer at <code>/data/acme-push</code>" in page and "would answer at <code>/data/acme-push/table</code>" in page
    _publish(client, source["id"], "table")
    row = client.get("/load").text
    assert "Unpublish" in row and "/data/acme-push/table" in row


def test_a_published_pipeline_that_is_removed_stops_answering(client, source):
    _setup(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _add(client, source, "table")
    _pipeline(client, source, "table", "enabled", data={"enabled": "1"})
    _upload_data(client, source)
    _publish(client, source["id"], "table")
    assert _stranger().get("/data/acme-push/table").status_code == 200
    flash = _flash(_pipeline(client, source, "table", "delete"))
    assert "removed" in flash and "/data/acme-push/table stopped answering" in flash
    assert _stranger().get("/data/acme-push/table").status_code == 404


# -- an API source ----------------------------------------------------------------------------------------------------------------------

def test_the_pipelines_of_an_api_source_each_have_their_own_request_settings(client, source, _isolated_db, answer):
    registry_id = _http_source(_isolated_db, "registry", post=True, published=False)
    _build(client, registry_id, enable=False)
    _add(client, {"id": registry_id}, "lookup")  # a copy of the default one
    client.post("/load/sources/%s/pipelines/lookup/request" % registry_id, data={"mode": "swap", "template": REGISTRY_BODY}, follow_redirects=False)
    _publish(client, registry_id)
    _publish(client, registry_id, "lookup")
    stranger = _stranger()
    # the default pipeline: callers POST, as the provider does
    assert stranger.get("/data/registry", params={"plate": "x"}).status_code == 405
    assert stranger.post("/data/registry", content="{}", headers=JSON_BODY).status_code == 200
    # the lookup pipeline: callers GET with a parameter, and the provider is still sent a POST
    resp = stranger.get("/data/registry/lookup", params={"plate": "ZMM3707"})
    assert resp.status_code == 200 and answer["calls"] == 2
    assert stranger.post("/data/registry/lookup", content="{}", headers=JSON_BODY).status_code == 405
    page = client.get("/load").text
    assert "/data/registry/lookup?plate=..." in page and "sent to the provider as POST" in page


def test_an_api_source_pipeline_request_page_names_its_pipeline_and_posts_to_its_own_address(client, source, _isolated_db):
    registry_id = _http_source(_isolated_db, "registry", post=True, published=False)
    _build(client, registry_id, enable=False)
    _add(client, {"id": registry_id}, "lookup")
    page = client.get("/load/sources/%s/pipelines/lookup/request" % registry_id).text
    assert "<h1>Request -- lookup</h1>" in page and 'action="/load/sources/%s/pipelines/lookup/request"' % registry_id in page and "/data/registry/lookup" in page
    assert "has no pipeline called" in _flash(client.get("/load/sources/%s/pipelines/ghost/request" % registry_id, follow_redirects=False))
    assert "has no pipeline called" in _flash(client.post("/load/sources/%s/pipelines/ghost/request" % registry_id, data={"mode": "same"}, follow_redirects=False))


# -- /proxy is gone ---------------------------------------------------------------------------------------------------------------------

def test_the_old_proxy_address_does_not_exist_any_more(client, source, _isolated_db, answer):
    _http_source(_isolated_db, "registry", post=True)  # published
    assert client.get("/proxy/registry").status_code == 404 and client.post("/proxy/registry", content="{}", headers=JSON_BODY).status_code == 404
    anonymous = _stranger().get("/proxy/registry", follow_redirects=False)
    assert anonymous.status_code in (303, 307) and anonymous.headers["location"].startswith("/admin/login")  # not an open address either
    assert answer["calls"] == 0 and _stranger().post("/data/registry", content="{}", headers=JSON_BODY).status_code == 200
