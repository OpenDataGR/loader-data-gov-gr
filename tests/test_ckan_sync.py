"""CKAN stays in step with the pipeline: a new output updates an upload resource, unpublishing removes the resource and publishing again restores
it, and removing a pipeline or a source takes its resources out of CKAN. All best effort: CKAN failing never blocks the loader."""

import pytest
from sqlalchemy import select

from providerharvest_service import ckan_client, ckan_load, harvest_db, ui_ckan
from providerharvest_service.models import CkanTarget
from tests.test_ckan_load import BASE, FakeCkan
from tests.test_ckan_ui import _connect, _page, _ready, _targets, ckan  # noqa: F401  (the ckan fixture)
from tests.test_pipeline_publishing import _publish
from tests.test_pipelines_ui import _add, _pipeline, _setup
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _go, _isolated_db, _isolated_harvest_db, _master_key, _upload_data, client, source,
)


@pytest.fixture(autouse=True)
def _harvest_factory(_isolated_harvest_db, monkeypatch):
    """The restore path opens its own harvest session: point it at the test database."""
    monkeypatch.setattr(harvest_db, "get_session_factory", lambda: _isolated_harvest_db)


# -- a new output ------------------------------------------------------------------------------------------------------------------------------------------

def test_a_new_file_updates_an_upload_resource_and_a_link_resource_is_left_alone(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"], mode="upload")
    created = len([c for c in ckan.calls if c[0] == "resource_create"])
    first = ckan.resources["res1"]
    _upload_data(client, source, name="Next.xml")  # a second file arrives and is transformed
    updates = [c for c in ckan.calls if c[0] == "resource_update"]
    assert created == 1 and len(updates) == 1 and updates[0][2]["upload"][0] == "Next.json"  # the new output replaced the file in the same resource
    assert len(ckan.resources) == 1 and ckan.resources["res1"] is first
    (target,) = _targets(_isolated_db)
    assert target.last_status == "ok" and target.last_output_id

    ckan.calls.clear()
    _connect(client, source["id"], mode="link")
    ckan.calls.clear()
    _upload_data(client, source, name="Third.xml")
    assert [c for c in ckan.calls if c[0].startswith("resource_")] == []  # a link always points at the newest: nothing to do


def test_a_failing_ckan_never_stops_a_file_from_being_received(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"], mode="upload")
    ckan.fail["resource_update"] = ckan_client.CKANActionError("resource_update", "boom")
    resp = _upload_data(client, source, name="Next.xml")  # (_upload_data asserts the upload was accepted)
    assert resp
    (target,) = _targets(_isolated_db)
    assert target.last_status == "error" and "could not be reached or failed" in target.last_error


def test_a_failed_transform_is_not_sent_to_ckan(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"], mode="upload")
    ckan.calls.clear()
    client.post("/push/%s/upload" % source["name"], files={"file": ("Bad.xml", b"<not xml", "application/xml")}, headers={"X-Api-Key": source["key"]})
    assert [c for c in ckan.calls if c[0].startswith("resource_")] == []


# -- unpublishing and publishing again -----------------------------------------------------------------------------------------------------------------

def test_unpublishing_removes_the_resource_and_publishing_again_puts_it_back(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"])
    assert "res1" in ckan.resources
    flash = _flash(_publish(client, source["id"], on=False))
    assert "no longer published" in flash and "Its CKAN resource was removed." in flash
    assert ckan.resources == {} and _targets(_isolated_db)[0].resource_id is None  # the connection stays, the resource is gone
    page = client.get(_page(source["id"])).text
    assert "not in CKAN" in page and "it is unpublished, so it was taken out of CKAN" in page
    flash = _flash(_publish(client, source["id"]))
    assert "Its CKAN resource is back." in flash and ckan.resources["res1"]["url"] == BASE + "/data/acme-push"


def test_publishing_a_pipeline_with_no_connection_says_nothing_about_ckan(client, source, ckan):
    _setup(client, source)
    flash = _flash(_publish(client, source["id"]))
    assert "published at" in flash and "CKAN" not in flash and ckan.calls == []


def test_an_upload_resource_comes_back_with_the_newest_output(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"], mode="upload")
    _publish(client, source["id"], on=False)
    assert ckan.resources == {}
    flash = _flash(_publish(client, source["id"]))
    assert "Its CKAN resource is back." in flash and ckan.resources["res1"]["name"] == "Traffic"
    assert [c[2]["upload"][0] for c in ckan.calls if c[0] == "resource_create"][-1] == "Detector.json"


def test_when_ckan_refuses_the_removal_the_unpublish_still_happens_and_the_person_is_told(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"])
    ckan.fail["resource_delete"] = ckan_client.CKANNotAuthorized("resource_delete", "no")
    flash = _flash(_publish(client, source["id"], on=False))
    assert "no longer published" in flash and "could not be removed" in flash and "by hand" in flash
    assert "res1" in ckan.resources
    assert "Could not be removed from CKAN" in _targets(_isolated_db)[0].last_error


def test_ckan_not_being_set_up_never_blocks_unpublishing(client, source, ckan, monkeypatch):
    _ready(client, source)
    _connect(client, source["id"])
    monkeypatch.delenv("CKAN_BASE_URL")
    flash = _flash(_publish(client, source["id"], on=False))
    assert "no longer published" in flash and "CKAN is not set up" in flash


# -- removing a pipeline or a source --------------------------------------------------------------------------------------------------------------------

def test_removing_a_pipeline_takes_its_resource_out_of_ckan_and_forgets_the_connection(client, source, ckan, _isolated_db):
    _ready(client, source)
    _add(client, source, "table")
    _pipeline(client, source, "table", "enabled", data={"enabled": "1"})
    _publish(client, source["id"], "table")
    _connect(client, source["id"])
    _connect(client, source["id"], "table", resource_name="Table")
    assert len(ckan.resources) == 2
    flash = _flash(_pipeline(client, source, "table", "delete"))
    assert "removed" in flash and "CKAN" not in flash
    assert [r["name"] for r in ckan.resources.values()] == ["Traffic"] and len(_targets(_isolated_db)) == 1  # only the other pipeline's remains


def test_a_pipeline_is_removed_even_when_ckan_cannot_remove_its_resource(client, source, ckan, _isolated_db):
    _ready(client, source)
    _add(client, source, "table")
    _publish(client, source["id"], "table")
    _connect(client, source["id"], "table", resource_name="Table")
    ckan.fail["resource_delete"] = ckan_client.CKANActionError("resource_delete", "down")
    flash = _flash(_pipeline(client, source, "table", "delete"))
    assert "removed" in flash and "could not be removed" in flash
    assert _targets(_isolated_db) == []  # the loader forgot it; the resource in CKAN has to be removed by hand, as the message says


def test_deleting_a_source_takes_all_its_resources_out_of_ckan(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"])
    resp = client.post("/provider/sources/%s/delete" % source["id"], follow_redirects=False)
    assert "Source deleted." in _flash(resp)
    assert ckan.resources == {} and _targets(_isolated_db) == []


def test_a_source_is_deleted_even_when_ckan_is_down_and_the_message_says_what_is_left(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"])
    ckan.fail["resource_delete"] = ckan_client.CKANActionError("resource_delete", "down")
    flash = _flash(client.post("/provider/sources/%s/delete" % source["id"], follow_redirects=False))
    assert "Source deleted." in flash and "could not be removed" in flash and "by hand" in flash
    assert _targets(_isolated_db) == []
