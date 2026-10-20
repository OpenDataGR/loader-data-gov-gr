"""The CKAN page's third choice, a data service: the form, saving it, what an organization's services remember, switching between the ways, and CKAN
following the pipeline (unpublish removes the entry, publishing again creates it)."""

import json
import re

import pytest

from providerharvest_service import ckan_load
from providerharvest_service.models import CkanServiceDefaults, CkanTarget
from tests.test_ckan_load import BASE
from tests.test_ckan_service import ServiceCkan  # noqa: F401
from tests.test_ckan_ui import _page, _ready, _targets, ckan  # noqa: F401  (the ckan fixture)
from tests.test_live_transform import _http_source
from tests.test_pipeline_publishing import _publish
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _isolated_db, _isolated_harvest_db, _master_key, client, source,
)


@pytest.fixture(autouse=True)
def _harvest_factory(_isolated_harvest_db, monkeypatch):
    from providerharvest_service import harvest_db

    monkeypatch.setattr(harvest_db, "get_session_factory", lambda: _isolated_harvest_db)


SERVICE = {"mode": "service", "organization_id": "org1", "svc_title": "Opencar vehicle lookup", "svc_description": "Look a vehicle up by plate.",
           "svc_endpoint_description": "Send the plate.", "svc_format": "JSON", "svc_license": "CC_BY_4_0", "svc_access_rights": "PUBLIC",
           "svc_themes": ["TRAN", "GOVE"], "svc_legislation": ["https://eur-lex.europa.eu/eli/dir/2019/1024/oj/eng"], "svc_legislation_other": "Law 1234",
           "svc_contact_name": "Ops", "svc_contact_email": "ops@example.gr", "svc_publisher_name": "Ministry", "svc_publisher_type": "NationalAuthority",
           "svc_documentation": "https://docs.example.gr/a\nhttps://docs.example.gr/b", "svc_tags": "API, vehicles"}


def _service(client, source_id, pipeline=None, **form):
    data = dict(SERVICE)
    data.update(form)
    return client.post(_page(source_id, pipeline), data=data, follow_redirects=False)


@pytest.fixture
def registry(client, source, _isolated_db):
    return _http_source(_isolated_db, "opencar-data", post=True)  # an API source with its default pipeline published (no steps)


# -- the page -------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_an_api_source_opens_on_the_data_service_choice_with_the_form_filled_in_from_the_pipeline(client, registry, ckan):
    page = client.get(_page(registry)).text
    assert re.search(r'id="mode-service" name="mode" value="service" checked', page) and 'id="mode-link" name="mode" value="link" checked' not in page
    assert 'name="svc_title" maxlength="200" value="Opencar-Data"' in page  # the name starts as the source's title
    assert "HTTP POST to %s/data/opencar-data" % BASE in page and "at most 7 requests per minute from one address" in page  # the endpoint description, written for you
    for needed in ('name="svc_themes"', 'name="svc_legislation"', 'name="svc_hvd"', 'name="svc_license"', 'name="svc_publisher_type"', 'name="svc_contact_email"'):
        assert needed in page
    assert "Geospatial" in page and "Government and public sector" in page and "CC BY 4.0" in page and "Open Data Directive" in page


def test_a_source_that_stores_data_opens_on_the_link_but_can_also_be_described_as_a_service(client, source, ckan):
    _ready(client, source)
    page = client.get(_page(source["id"])).text
    assert 'id="mode-link" name="mode" value="link" checked' in page and 'id="mode-service" name="mode" value="service"' in page
    assert "disabled" not in page.split('id="mode-service"')[1].split(">")[0]


def test_the_page_script_shows_the_part_that_belongs_to_the_choice_and_loads_the_organizations_defaults(client, registry, ckan):
    page = client.get(_page(registry)).text
    assert "document.getElementById('section-dataset').hidden = mode === 'service'" in page and "'/load/ckan/service-defaults?org='" in page
    assert "var FILL_DEFAULTS = true;" in page


# -- connecting as a service -----------------------------------------------------------------------------------------------------------------------------------------

def test_saving_as_a_service_creates_the_data_service_entry_in_the_organization(client, registry, ckan, _isolated_db):
    resp = _service(client, registry)
    assert "Opencar vehicle lookup is now a data service in CKAN (private, until you make it public)." in _flash(resp)
    (action, data, files), = [c for c in ckan.calls if c[0] == "package_create"]
    assert data["type"] == "data-service" and data["owner_org"] == "org1" and data["name"] == "opencar-data" and data["endpoint_url"] == [BASE + "/data/opencar-data"]
    assert data["theme"] == ["http://publications.europa.eu/resource/authority/data-theme/TRAN", "http://publications.europa.eu/resource/authority/data-theme/GOVE"]
    assert data["applicable_legislation"] == ["https://eur-lex.europa.eu/eli/dir/2019/1024/oj/eng", "Law 1234"]
    assert data["documentation"] == ["https://docs.example.gr/a", "https://docs.example.gr/b", BASE + "/api-docs/opencar-data"] and data["tags"] == [{"name": "API"}, {"name": "vehicles"}]  # (the loader adds its own API documentation)
    (target,) = _targets(_isolated_db)
    assert (target.mode, target.organization_id, target.resource_id, target.last_status, target.dataset_title) == ("service", "org1", "svc1", "ok", "Opencar vehicle lookup")
    assert json.loads(target.service_meta)["contact_email"] == "ops@example.gr"
    page = client.get(_page(registry)).text
    assert "Connected to" in page and "as a data service" in page and "https://ckan.example.gr/data-service/opencar-data" in page
    assert "Law 1234" in page and re.search(r'name="svc_themes" value="TRAN" checked', page)  # what was saved comes back


def test_a_service_needs_a_name_an_organization_and_valid_details(client, registry, ckan, _isolated_db):
    assert "Give the service a name" in _flash(_service(client, registry, svc_title=" "))
    assert "Choose the organization first" in _flash(_service(client, registry, organization_id=""))
    assert "not an e-mail" in _flash(_service(client, registry, svc_contact_email="nope"))
    assert "must be links" in _flash(_service(client, registry, svc_documentation="not a link"))
    assert "may not add datasets to that organization" in _flash(_service(client, registry, organization_id="org-not-mine"))
    assert _targets(_isolated_db) == [] and [c for c in ckan.calls if c[0].startswith("package_")] == []


def test_a_licence_given_as_a_link_is_accepted(client, registry, ckan):
    _service(client, registry, svc_license="__other__", svc_license_other="https://opendefinition.org/licenses/odc-by/")
    assert ckan.packages["svc1"]["license"] == "https://opendefinition.org/licenses/odc-by/"


def test_a_data_service_adds_the_public_address_it_describes(client, source, ckan, _isolated_db):
    _http = _http_source(_isolated_db, "feed", post=True, published=False)
    client.post("/transform/sources/%s/create" % _http, follow_redirects=False)  # it has a pipeline, but nothing is published
    resp = _service(client, _http)
    assert "is now a data service in CKAN (private, until you make it public). The public address /data/feed was added too." in _flash(resp)
    assert ckan.packages["svc1"]["endpoint_url"] == [BASE + "/data/feed"]
    assert "Remove</button>" in client.get("/load").text  # the address is now an output of its own


def test_a_source_that_cannot_answer_yet_cannot_get_a_service_and_keeps_the_reason(client, source, ckan, _isolated_db):
    _http = _http_source(_isolated_db, "waiting", post=True, published=False, activate=False)
    client.post("/transform/sources/%s/create" % _http, follow_redirects=False)
    assert "Saved, but not pushed: " in _flash(_service(client, _http)) and "is not active yet" in _flash(_service(client, _http))
    (target,) = _targets(_isolated_db)
    assert target.mode == "service" and ckan.packages == {}


# -- what an organization remembers -----------------------------------------------------------------------------------------------------------------------------------

def test_the_next_service_of_an_organization_starts_from_what_the_last_one_chose(client, registry, ckan, _isolated_db):
    _service(client, registry, svc_license="CC0", svc_themes=["ENER"], svc_contact_email="energy@example.gr")
    with _isolated_db() as s:
        stored = json.loads(s.query(CkanServiceDefaults).filter_by(organization_id="org1").one().values)
    assert stored["license"] == "CC0" and stored["themes"] == ["ENER"] and "title" not in stored and "endpoint_description" not in stored
    defaults = client.get("/load/ckan/service-defaults", params={"org": "org1"}).json()["defaults"]
    assert defaults["license"] == "CC0" and defaults["contact_email"] == "energy@example.gr" and defaults["themes"] == ["ENER"]
    other = client.get("/load/ckan/service-defaults", params={"org": "org2"}).json()["defaults"]
    assert other["license"] == "CC_BY_4_0" and other["themes"] == []  # another organization starts from the built-in ones
    second = _http_source(_isolated_db, "second-api", post=True)
    assert "id=\"svc_license\"" in client.get(_page(second)).text  # (the form of a pipeline with no connection starts from the built-in values; the page script fetches the organization's)


def test_the_defaults_endpoint_needs_a_ckan_identity(client, source, monkeypatch):
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "t")
    assert client.get("/load/ckan/service-defaults", params={"org": "org1"}).status_code == 403


# -- switching between the ways ---------------------------------------------------------------------------------------------------------------------------------------

def test_switching_from_a_resource_to_a_service_removes_the_resource_first(client, source, ckan, _isolated_db):
    _ready(client, source)
    client.post(_page(source["id"]), data={"mode": "link", "organization_id": "org1", "dataset_id": "ds1", "resource_name": "Traffic"}, follow_redirects=False)
    assert "res1" in ckan.resources
    assert "is now a data service" in _flash(_service(client, source["id"]))
    actions = [c[0] for c in ckan.calls if c[0] in ("resource_create", "resource_delete", "package_create")]
    assert actions == ["resource_create", "resource_delete", "package_create"]
    assert ckan.resources == {} and list(ckan.packages) == ["svc1"]
    assert _targets(_isolated_db)[0].mode == "service"


def test_switching_from_a_service_back_to_a_resource_removes_the_entry_first(client, source, ckan, _isolated_db):
    _ready(client, source)
    _service(client, source["id"])
    assert "svc1" in ckan.packages
    resp = client.post(_page(source["id"]), data={"mode": "link", "organization_id": "org1", "dataset_id": "ds1", "resource_name": "Traffic"}, follow_redirects=False)
    assert "is now a resource of Attica traffic" in _flash(resp)
    assert ckan.packages == {} and "res1" in ckan.resources and [c[0] for c in ckan.calls if c[0] == "package_delete"] == ["package_delete"]


def test_moving_a_service_to_another_organization_removes_it_from_the_old_one(client, source, ckan, _isolated_db):
    _ready(client, source)
    _service(client, source["id"])
    assert "is now a data service" in _flash(_service(client, source["id"], organization_id="org2"))
    # (CKAN's delete is soft and keeps the old address reserved, so the new entry is made under the next free one)
    assert [c[0] for c in ckan.calls if c[0].startswith("package_")] == ["package_create", "package_delete", "package_create", "package_create"]
    assert [(p["owner_org"], p["name"]) for p in ckan.packages.values()] == [("org2", "acme-push-2")]


def test_saving_again_updates_the_same_entry(client, source, ckan, _isolated_db):
    _ready(client, source)
    _service(client, source["id"])
    assert "is now a data service" in _flash(_service(client, source["id"], svc_title="Renamed", svc_themes=["TRAN"]))
    assert [c[0] for c in ckan.calls if c[0].startswith("package_")] == ["package_create", "package_patch"]
    assert ckan.packages["svc1"]["title"] == "Renamed" and len(_targets(_isolated_db)) == 1
    assert ckan.packages["svc1"]["name"] == "acme-push"


# -- CKAN follows the pipeline ---------------------------------------------------------------------------------------------------------------------------------------

def test_unpublishing_removes_the_service_and_publishing_again_creates_it(client, registry, ckan, _isolated_db):
    _service(client, registry)
    flash = _flash(_publish(client, registry, on=False))
    assert "no longer published" in flash and "Its CKAN resource was removed." in flash
    assert ckan.packages == {} and _targets(_isolated_db)[0].resource_id is None
    assert "its public address was removed, so it was taken out of CKAN" in client.get(_page(registry)).text
    flash = _flash(_publish(client, registry))
    assert "Its CKAN resource is back." in flash and len(ckan.packages) == 1
    (entry,) = ckan.packages.values()
    assert entry["title"] == "Opencar vehicle lookup" and entry["name"] == "opencar-data-2"  # the deleted one keeps its address reserved in CKAN: the new one takes the next


def test_disconnecting_deletes_the_service_entry(client, registry, ckan, _isolated_db):
    _service(client, registry)
    resp = client.post(_page(registry, tail="ckan/disconnect"), follow_redirects=False)
    assert "Disconnected" in _flash(resp) and ckan.packages == {} and _targets(_isolated_db) == []


def test_deleting_the_source_takes_its_service_out_of_ckan(client, registry, ckan, _isolated_db):
    _service(client, registry)
    flash = _flash(client.post("/provider/sources/%s/delete" % registry, follow_redirects=False))
    assert "Source deleted." in flash and ckan.packages == {} and _targets(_isolated_db) == []


# -- the format is the pipeline's ------------------------------------------------------------------------------------------------------------------------------------

def test_the_format_is_shown_read_only_with_a_way_to_the_pipelines_output(client, registry, ckan):
    page = client.get(_page(registry)).text
    assert 'name="svc_format"' not in page and 'id="svc_format_label">JSON<' in page
    assert 'href="/transform/sources/%s/steps/output">Change it on the pipeline' % registry in page


def test_a_format_sent_with_the_form_is_ignored_and_the_pipelines_is_used(client, source, ckan):
    _ready(client, source)
    _service(client, source["id"], svc_format="CSV")
    assert ckan.packages["svc1"]["format"].endswith("/file-type/JSON")  # the default pipeline writes JSON
    assert 'id="svc_format_label">JSON<' in client.get(_page(source["id"])).text


def test_changing_the_pipelines_output_updates_the_data_service_in_ckan(client, source, ckan, _isolated_db):
    from tests.test_transform_ui import _go

    _ready(client, source)
    _service(client, source["id"])
    assert ckan.packages["svc1"]["format"].endswith("/file-type/JSON")
    flash = _flash(_go(client, source, "output", data={"format": "xml", "root": "records", "record": "record"}))
    assert "Output set to" in flash and "The data service in CKAN was updated." in flash
    assert ckan.packages["svc1"]["format"].endswith("/file-type/XML") and 'id="svc_format_label">XML<' in client.get(_page(source["id"])).text
    flash = _flash(_go(client, source, "enabled", data={"enabled": "0"}))  # off: an API pipeline would pass the answer through, as JSON
    assert "Transform switched off." in flash and "The data service in CKAN was updated." in flash
    assert ckan.packages["svc1"]["format"].endswith("/file-type/JSON")


def test_a_ckan_that_cannot_be_updated_never_blocks_the_pipeline_change(client, source, ckan, _isolated_db):
    from providerharvest_service import ckan_client
    from tests.test_transform_ui import _go

    _ready(client, source)
    _service(client, source["id"])
    ckan.fail["package_patch"] = ckan_client.CKANActionError("package_patch", "down")
    flash = _flash(_go(client, source, "output", data={"format": "csv", "delimiter": ","}))
    assert "Output set to CSV" in flash and "could not be updated" in flash
    from providerharvest_service import transform_runner

    with _isolated_db() as s:
        assert transform_runner.get_transform(s, source["id"]).output_format == "csv"  # the change was made


def test_a_resource_connection_is_not_touched_by_a_change_of_the_output(client, source, ckan, _isolated_db):
    from tests.test_transform_ui import _go

    _ready(client, source)
    client.post(_page(source["id"]), data={"mode": "link", "organization_id": "org1", "dataset_id": "ds1", "resource_name": "Traffic"}, follow_redirects=False)
    ckan.calls.clear()
    flash = _flash(_go(client, source, "output", data={"format": "csv", "delimiter": ","}))
    assert "CKAN" not in flash and ckan.calls == []


def test_a_data_service_can_be_created_public_and_a_later_push_leaves_its_visibility_alone(client, registry, ckan):
    _service(client, registry, svc_public="public")
    assert ckan.packages["svc1"]["private"] is False
    ckan.packages["svc1"]["private"] = True  # (someone made it private in CKAN, or on its page)
    client.post(_page(registry, tail="ckan/push"), follow_redirects=False)
    assert ckan.packages["svc1"]["private"] is True and "private" not in [k for c in ckan.calls if c[0] == "package_patch" for k in c[1]]


def test_the_form_shows_the_documentation_link_that_will_be_added(client, registry, ckan):
    page = client.get(_page(registry)).text
    assert 'name="svc_docs"' in page and BASE + "/api-docs/opencar-data" in page and "checked" in page.split('name="svc_docs"')[1][:40]


def test_the_documentation_link_can_be_left_out_and_the_choice_is_kept(client, registry, ckan):
    _service(client, registry, svc_docs_sent="1")  # (the box is not ticked: a browser sends nothing for it)
    assert ckan.packages["svc1"]["documentation"] == ["https://docs.example.gr/a", "https://docs.example.gr/b"]
    page = client.get(_page(registry)).text
    assert "checked" not in page.split('name="svc_docs"')[1][:40]
    _service(client, registry, svc_docs_sent="1", svc_docs="1")
    assert BASE + "/api-docs/opencar-data" in ckan.packages["svc1"]["documentation"]
