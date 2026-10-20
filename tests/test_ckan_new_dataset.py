"""A load can put its resource in a dataset it creates: the DCAT-AP details are asked on the page (the ones a data service asks), the dataset is created in
the chosen organization as the signed-in person, private unless made public, and its resource follows -- against a fake CKAN."""

import json

import pytest

from providerharvest_service import ckan_client, ckan_load
from tests.test_ckan_datastore import DatastoreCkan, ckan  # noqa: F401
from tests.test_ckan_load import _pipeline, _source
from tests.test_ckan_ui import _connect, _page, _ready, _targets
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _isolated_db, _isolated_harvest_db, _master_key, client, source,
)

DETAILS = {"dataset_mode": "new", "svc_title": "Attica traffic", "svc_description": "Detector readings", "svc_license": "CC_BY_4_0", "svc_access_rights": "PUBLIC",
           "svc_themes": ["TRAN"], "svc_legislation": ["https://eur-lex.europa.eu/eli/dir/2019/1024/oj/eng"], "svc_hvd": ["c_dd313021"], "svc_contact_name": "Data team",
           "svc_contact_email": "data@example.gr", "svc_publisher_name": "Region of Attica", "svc_publisher_identifier": "https://ror.org/xyz", "svc_publisher_type": "NationalAuthority", "svc_tags": "traffic, attica"}


def _created(ckan_fake):
    return [c[1] for c in ckan_fake.calls if c[0] == "package_create"]


def test_the_dataset_payload_uses_the_portals_shapes():
    meta = ckan_load.normalise_meta({"title": "T", "description": "D", "themes": ["TRAN"], "hvd_categories": ["c_dd313021"], "contact_name": "N", "contact_email": "a@b.gr",
                                     "publisher_name": "P", "publisher_identifier": "https://ror.org/x", "tags": "a, b", "license": "CC_BY_4_0"})
    payload = ckan_load.dataset_payload(meta, "org1", "attica", public=True)
    assert payload["name"] == "attica" and payload["private"] is False and payload["tag_string"] == "a,b" and payload["license_id"] == "cc-by"
    assert payload["applicable_legislation"] == "https://eur-lex.europa.eu/eli/dir/2019/1024/oj/eng" and payload["hvd_category"] == "http://data.europa.eu/bna/c_dd313021"
    assert payload["theme"] == ["http://publications.europa.eu/resource/authority/data-theme/TRAN"] and payload["access_rights"].endswith("access-right/PUBLIC")
    assert payload["contact"] == [{"name": "N", "email": "a@b.gr", "uri": ""}] and payload["publisher"] == [{"name": "P", "uri": "https://ror.org/x"}]
    assert ckan_load.dataset_payload(meta, "org1", "attica")["private"] is True
    bare = ckan_load.dataset_payload(ckan_load.normalise_meta({"title": "T", "license": "CC_BYND_4_0", "legislation": []}), "org1", "x")
    assert "license_id" not in bare and "applicable_legislation" not in bare and "contact" not in bare and "publisher" not in bare and "hvd_category" not in bare


def test_a_taken_address_gets_the_next_free_one_and_other_refusals_are_explained():
    fake = DatastoreCkan()
    fake.taken.add("attica-traffic-data")
    meta = ckan_load.normalise_meta({"title": "T"})
    made = ckan_load.create_dataset(fake, meta, "org1", _source(), _pipeline())
    assert made.name == "attica-traffic-data-2" and made.owner_org == "org1" and made.private
    fake.fail["package_create"] = ckan_client.CKANNotAuthorized("package_create", "no")
    with pytest.raises(ckan_load.CkanLoadError, match="may not change"):
        ckan_load.create_dataset(fake, meta, "org1", _source(), _pipeline())


def test_a_new_dataset_is_created_private_and_the_resource_goes_into_it(client, source, ckan, _isolated_db):
    _ready(client, source)
    resp = _connect(client, source["id"], mode="datastore", ds_refresh="append", **DETAILS)
    assert "was created in CKAN (private, until you make it public) with Traffic as its resource" in _flash(resp)
    (payload,) = _created(ckan)
    assert payload["title"] == "Attica traffic" and payload["owner_org"] == "org1" and payload["private"] is True and payload["name"].startswith("acme-push")
    (target,) = _targets(_isolated_db)
    assert target.dataset_id == "svc1" and target.dataset_title == "Attica traffic" and target.mode == "datastore" and target.resource_id
    assert ckan.resources[target.resource_id]["package_id"] == "svc1"


def test_it_can_be_made_public_straight_away_and_the_details_are_kept_for_the_organization(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"], mode="link", new_public="1", **DETAILS)
    assert _created(ckan)[0]["private"] is False
    defaults = client.get("/load/ckan/service-defaults?org=org1").json()["defaults"]
    assert defaults["contact_name"] == "Data team" and defaults["themes"] == ["TRAN"] and defaults["publisher_name"] == "Region of Attica"


def test_a_missing_title_or_a_refusal_by_ckan_saves_nothing(client, source, ckan, _isolated_db):
    _ready(client, source)
    assert "Give the dataset a name" in _flash(_connect(client, source["id"], mode="upload", **dict(DETAILS, svc_title=" ")))
    ckan.fail["package_create"] = ckan_client.CKANValidationError("package_create", "{'license_id': ['Unknown']}")
    assert "The dataset could not be created" in _flash(_connect(client, source["id"], mode="upload", **DETAILS))
    assert _targets(_isolated_db) == []


def test_the_page_offers_the_choice_between_an_existing_and_a_new_dataset(client, source, ckan):
    _ready(client, source)
    page = client.get(_page(source["id"])).text
    assert 'name="dataset_mode" value="existing"' in page and 'name="dataset_mode" value="new"' in page and 'name="new_public"' in page
    assert "Title of the dataset" in page and 'class="svc-only"' in page
