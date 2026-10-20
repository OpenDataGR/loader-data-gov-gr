"""The Load Box's CKAN pages: connect a pipeline to a dataset, push, change, disconnect -- as the signed-in person, against a fake CKAN."""

import pytest
from sqlalchemy import select

from providerharvest_service import ckan_client, ckan_load, ui_ckan
from providerharvest_service.models import CkanTarget
from tests.test_ckan_load import BASE
from tests.test_ckan_service import ServiceCkan
from tests.test_pipeline_publishing import _publish
from tests.test_pipelines_ui import _add, _pipeline, _setup
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _go, _isolated_db, _isolated_harvest_db, _master_key, _upload_data, client, source,
)


@pytest.fixture
def ckan(monkeypatch):
    """CKAN is configured, the signed-in person is user_abc, and every CKAN call goes to one fake."""
    fake = ServiceCkan()  # (a fake CKAN that also knows data services; creating it empties the little cache of organizations)
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "sysadmin-token")
    monkeypatch.setenv("LOADER_PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(ckan_load, "user_client", lambda session, ckan_user: fake)
    monkeypatch.setattr(ui_ckan, "_identity", lambda request: "user_abc")
    return fake


def _page(source_id, pipeline=None, tail="ckan"):
    return "/load/sources/%s%s/%s" % (source_id, "" if pipeline in (None, "default") else "/pipelines/%s" % pipeline, tail)


def _connect(client, source_id, pipeline=None, **form):
    data = {"mode": "link", "organization_id": "org1", "dataset_id": "ds1", "resource_name": "Traffic", "description": ""}
    data.update(form)
    return client.post(_page(source_id, pipeline), data=data, follow_redirects=False)


def _targets(_isolated_db):
    with _isolated_db() as s:
        return s.scalars(select(CkanTarget)).all()


def _ready(client, source):
    """A push source with a switched-on, published default pipeline and one output."""
    _setup(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _upload_data(client, source)
    _publish(client, source["id"])


# -- the Load Box and the page ---------------------------------------------------------------------------------------------------------

def test_the_load_box_offers_the_connection_only_when_ckan_is_set_up(client, source, monkeypatch):
    _setup(client, source)
    monkeypatch.delenv("CKAN_BASE_URL", raising=False)
    monkeypatch.delenv("CKAN_SYSADMIN_API_TOKEN", raising=False)
    assert "Push to CKAN" not in client.get("/load").text
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "t")
    page = client.get("/load").text
    assert '<th>Outputs</th>' in page and 'href="/load/sources/%s/ckan?load=new"' % source["id"] in page and '+ Add an output' in page


def test_the_page_explains_when_ckan_is_not_set_up_or_the_session_has_no_ckan_identity(client, source, monkeypatch):
    _setup(client, source)
    monkeypatch.delenv("CKAN_BASE_URL", raising=False)
    monkeypatch.delenv("CKAN_SYSADMIN_API_TOKEN", raising=False)
    assert "CKAN is not set up on this deployment." in client.get(_page(source["id"])).text
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "t")
    page = client.get(_page(source["id"])).text  # the test login is the shared password: no CKAN identity
    assert "this session has no CKAN identity" in page and "disabled" in page
    assert "this session has no CKAN identity" in _flash(_connect(client, source["id"]))


def test_the_page_shows_what_will_be_created_and_unknown_pipelines_are_refused(client, source, ckan):
    _setup(client, source)
    _add(client, source, "table")
    page = client.get(_page(source["id"], "table")).text
    assert "pipeline <strong>table</strong>" in page and "<code>%s/data/acme-push/table</code>" % BASE in page and "Connect and push" in page
    assert 'name="organization_id"' in page and 'name="dataset_id"' in page and 'name="resource_name"' in page and "Acme push / table" in page
    assert page.index('name="organization_id"') < page.index('name="dataset_id"') < page.index('name="resource_name"')  # organization, dataset, resource
    assert "No such pipeline" in _flash(client.get(_page(source["id"], "ghost"), follow_redirects=False))


def test_the_dataset_search_returns_the_datasets_the_person_may_use(client, source, ckan):
    found = client.get("/load/ckan/datasets", params={"q": "traffic"})
    assert found.status_code == 200 and found.json()["datasets"][0] == {"id": "ds1", "name": "attica-traffic", "title": "Attica traffic", "organization": "Traffic", "owner_org": "org1",
                                                                       "private": True, "state": "draft", "num_resources": 3, "modified": "2026-10-12"}
    assert ckan.calls[-1][1]["q"] == "traffic" and ckan.calls[-1][1]["fq"] == "owner_org:(org2 OR org1)"  # (all the person's organizations, when none is chosen)
    ckan.fail["package_search"] = ckan_client.CKANActionError("package_search", "boom")
    failed = client.get("/load/ckan/datasets")
    assert failed.status_code == 502 and "could not be reached" in failed.json()["error"]


def test_the_search_is_refused_without_a_ckan_identity(client, source, monkeypatch):
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "t")
    refused = client.get("/load/ckan/datasets")
    assert refused.status_code == 403 and "no CKAN identity" in refused.json()["error"]


# -- connecting ---------------------------------------------------------------------------------------------------------------------------------

def test_connecting_a_published_pipeline_creates_the_resource_as_a_link(client, source, ckan, _isolated_db):
    _ready(client, source)
    resp = _connect(client, source["id"])
    assert "Traffic is now a resource of Attica traffic in CKAN." in _flash(resp)
    (target,) = _targets(_isolated_db)
    assert (target.mode, target.dataset_id, target.resource_id, target.ckan_user, target.last_status) == ("link", "ds1", "res1", "user_abc", "ok")
    assert ckan.resources["res1"]["url"] == BASE + "/data/acme-push" and ckan.resources["res1"]["package_id"] == "ds1"
    page = client.get(_page(source["id"])).text
    assert "Connected to" in page and "in CKAN" in page and "Push now" in page and "Disconnect" in page
    row = client.get("/load").text
    assert ">Manage</a>" in row and "Attica traffic" in row and "in CKAN" in row


def test_each_pipeline_has_its_own_connection_and_address(client, source, ckan, _isolated_db):
    _ready(client, source)
    _add(client, source, "table")
    _pipeline(client, source, "table", "enabled", data={"enabled": "1"})
    _publish(client, source["id"], "table")
    _connect(client, source["id"])
    _connect(client, source["id"], "table", resource_name="Table")
    assert len(_targets(_isolated_db)) == 2
    assert sorted(r["url"] for r in ckan.resources.values()) == [BASE + "/data/acme-push", BASE + "/data/acme-push/table"]


def test_a_link_adds_the_public_address_it_points_at(client, source, ckan, _isolated_db):
    _setup(client, source)  # not published
    resp = _connect(client, source["id"])
    assert "The public address /data/acme-push was added too." in _flash(resp)
    (target,) = _targets(_isolated_db)
    assert target.last_status == "ok" and ckan.resources["res1"]["url"] == BASE + "/data/acme-push"
    assert client.get("/data/acme-push").status_code in (200, 404)  # (published now: 404 only because the test has no output yet)


def test_removing_the_public_address_takes_the_link_out_of_ckan(client, source, ckan, _isolated_db):
    _setup(client, source)
    _connect(client, source["id"])
    assert "res1" in ckan.resources
    _publish(client, source["id"], on=False)
    assert ckan.resources == {}  # the link would point at an address that no longer answers


def test_what_is_missing_is_explained_before_anything_is_sent_to_ckan(client, source, ckan):
    _ready(client, source)
    assert "Choose the organization first" in _flash(_connect(client, source["id"], organization_id=""))
    assert "Choose the dataset first" in _flash(_connect(client, source["id"], dataset_id=""))
    assert "Give the resource a name" in _flash(_connect(client, source["id"], resource_name=" "))
    assert "Choose link, upload, DataStore or data service" in _flash(_connect(client, source["id"], mode="teleport"))
    assert [c[0] for c in ckan.calls] == []


def test_a_dataset_ckan_does_not_allow_is_refused_and_nothing_is_saved(client, source, ckan, _isolated_db):
    _ready(client, source)
    ckan.fail["package_show"] = ckan_client.CKANNotAuthorized("package_show", "no")
    assert "may not change that dataset" in _flash(_connect(client, source["id"]))
    assert _targets(_isolated_db) == []


def test_an_api_source_can_only_be_linked(client, source, ckan, _isolated_db):
    from tests.test_live_transform import _http_source

    registry_id = _http_source(_isolated_db, "registry", post=True)  # published, no steps: passes the answer through
    assert "no output to upload" in _flash(_connect(client, registry_id, mode="upload"))
    assert "is now a resource" in _flash(_connect(client, registry_id))
    assert ckan.resources["res1"]["url"] == BASE + "/data/registry" and ckan.resources["res1"]["format"] == "JSON"
    assert "disabled" in client.get(_page(registry_id)).text  # the upload choice is greyed out on the page


def test_upload_mode_sends_the_newest_output_of_a_source_that_stores_data(client, source, ckan, _isolated_db):
    _ready(client, source)
    assert "is now a resource" in _flash(_connect(client, source["id"], mode="upload"))
    action, data, files = [c for c in ckan.calls if c[0] == "resource_create"][0]
    assert data["url"] == "" and files["upload"][0] == "Detector.json" and files["upload"][1].startswith(b"[")
    (target,) = _targets(_isolated_db)
    assert target.mode == "upload" and target.last_output_id


def test_upload_mode_without_an_output_yet_says_so(client, source, ckan, _isolated_db):
    _setup(client, source)  # the pipeline exists, but no file has arrived
    assert "There is no output to upload yet" in _flash(_connect(client, source["id"], mode="upload"))
    assert _targets(_isolated_db)[0].last_status == "error"


# -- changing, pushing, disconnecting --------------------------------------------------------------------------------------------------

def test_moving_to_another_dataset_takes_the_resource_out_of_the_old_one_first(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"])
    assert "is now a resource" in _flash(_connect(client, source["id"], organization_id="org2", dataset_id="ds2"))
    actions = [c[0] for c in ckan.calls if c[0].startswith("resource_")]
    assert actions == ["resource_create", "resource_delete", "resource_create"]  # the old one goes, a new one is made in the other dataset
    assert [r["package_id"] for r in ckan.resources.values()] == ["ds2"]  # exactly one resource, in the new dataset


def test_changing_the_name_updates_the_same_resource(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"])
    _connect(client, source["id"], resource_name="Renamed", description="About")
    assert [c[0] for c in ckan.calls if c[0].startswith("resource_")] == ["resource_create", "resource_update"]
    assert ckan.resources["res1"]["name"] == "Renamed" and ckan.resources["res1"]["description"] == "About" and len(_targets(_isolated_db)) == 1


def test_push_now_and_disconnect(client, source, ckan, _isolated_db):
    assert "not connected" in _flash(client.post(_page(source["id"], tail="ckan/push"), follow_redirects=False))  # nothing to push yet
    _ready(client, source)
    _connect(client, source["id"])
    assert "Pushed to CKAN." in _flash(client.post(_page(source["id"], tail="ckan/push"), follow_redirects=False))
    resp = client.post(_page(source["id"], tail="ckan/disconnect"), follow_redirects=False)
    assert "Disconnected" in _flash(resp) and resp.headers["location"].startswith("/load?")
    assert ckan.resources == {} and _targets(_isolated_db) == []
    assert "not connected" in _flash(client.post(_page(source["id"], tail="ckan/disconnect"), follow_redirects=False))


def test_a_disconnect_ckan_refuses_keeps_the_connection(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"])
    ckan.fail["resource_delete"] = ckan_client.CKANNotAuthorized("resource_delete", "no")
    assert "still connected" in _flash(client.post(_page(source["id"], tail="ckan/disconnect"), follow_redirects=False))
    assert len(_targets(_isolated_db)) == 1 and "res1" in ckan.resources


def test_the_organizations_are_listed_for_the_first_step(client, source, ckan):
    found = client.get("/load/ckan/organizations")
    assert found.status_code == 200 and found.json()["organizations"] == [{"id": "org2", "name": "air", "title": "Air", "package_count": None},
                                                                     {"id": "org1", "name": "traffic", "title": "Traffic", "package_count": None}]
    assert client.get("/load/ckan/organizations").status_code == 200 and len(ckan.calls) == 1  # asked once, then kept for a while
    ckan_load._ORG_CACHE.clear()
    ckan.fail["organization_list_for_user"] = ckan_client.CKANActionError("organization_list_for_user", "boom")
    failed = client.get("/load/ckan/organizations")
    assert failed.status_code == 502 and "could not be reached" in failed.json()["error"]


def test_the_datasets_are_searched_within_the_chosen_organization(client, source, ckan):
    found = client.get("/load/ckan/datasets", params={"q": "air", "org": "org2"})
    assert found.status_code == 200 and ckan.calls[-1][1]["fq"] == "owner_org:(org2)"
    assert [c[0] for c in ckan.calls] == ["package_search"]  # (the person's organizations are not asked for again)


def test_the_organization_endpoints_are_refused_without_a_ckan_identity(client, source, monkeypatch):
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "t")
    assert client.get("/load/ckan/organizations").status_code == 403


def test_the_dataset_must_be_in_the_chosen_organization_and_the_organization_one_the_person_may_use(client, source, ckan, _isolated_db):
    _ready(client, source)
    assert "is not in Air" in _flash(_connect(client, source["id"], organization_id="org2", dataset_id="ds1"))  # ds1 is in Traffic
    assert "may not add datasets to that organization" in _flash(_connect(client, source["id"], organization_id="org-not-mine"))
    assert _targets(_isolated_db) == [] and [c for c in ckan.calls if c[0].startswith("resource_")] == []
    assert "is now a resource of Attica traffic" in _flash(_connect(client, source["id"], organization_id="org1", dataset_id="ds1"))
    (target,) = _targets(_isolated_db)
    assert (target.organization_id, target.organization_title) == ("org1", "Traffic")
    page = client.get(_page(source["id"])).text
    assert "Now: <strong>Traffic</strong>" in page and "Now: <strong>Attica traffic</strong>" in page


def test_the_page_script_loads_organizations_first_then_searches_the_datasets_of_the_one_chosen(client, source, ckan):
    _setup(client, source)
    page = client.get(_page(source["id"])).text
    assert "inFlight.abort()" in page and "setTimeout(searchDatasets, 700)" in page and "if (n === 1) return;" in page  # fewer, later, cancellable searches
    assert "b.textContent = 'Retry'" in page and "say(dsResults, data.error, searchDatasets)" in page  # an error comes with a Retry button
    assert "fetch('/load/ckan/organizations'" in page and "'/load/ckan/datasets?org=' + encodeURIComponent(orgField.value)" in page
    assert 'id="ds-q" placeholder="Search the datasets of the chosen organization' in page and "disabled" in page.split('id="ds-q"')[1].split(">")[0]  # off until an organization is chosen


def test_the_lists_are_short_scrolling_boxes_and_each_dataset_shows_what_it_is(client, source, ckan):
    _setup(client, source)
    page = client.get(_page(source["id"])).text
    assert ".pick-list { max-height: 15rem; overflow-y: auto;" in page and 'id="org-results" class="pick-list"' in page and 'id="ds-results" class="pick-list"' in page
    assert "pill('Private', 'private') : pill('Public', 'public')" in page and "' resource' : ' resources'" in page and "'updated ' + d.modified" in page
