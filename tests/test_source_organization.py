"""A source belongs to a CKAN organization: chosen on the source form from the organizations the person may give a source to (all of them for a super
admin, those they administer for anyone else), checked against CKAN, kept on the source and shown in the Extract Box."""

import pytest

from providerharvest_service import ckan_load, ui_ckan, ui_provider
from providerharvest_service.models import ProviderSource
from tests.test_ckan_service import ServiceCkan
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _isolated_db, _isolated_harvest_db, _master_key, client, source,
)

FORM = {"name": "orgsrc", "title": "Org source", "kind": "pull", "transport_type": "http", "endpoint_url": "https://registry.example.gr/api/", "frequency": "DAILY",
        "auth_type": "api_key", "api_key": "k"}


@pytest.fixture
def ckan(monkeypatch):
    """CKAN is configured and one fake answers everything; the signed-in person is a super admin (the test login) with no CKAN account."""
    fake = ServiceCkan()  # (creating it empties the little cache of organizations)
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "sysadmin-token")
    monkeypatch.setattr(ckan_load, "sysadmin_client", lambda: fake)
    monkeypatch.setattr(ckan_load, "user_client", lambda session, ckan_user: fake)
    return fake


@pytest.fixture
def org_admin(monkeypatch):
    """The signed-in person is an organization administrator, not a super admin, and has a CKAN account."""
    monkeypatch.setattr(ui_provider, "_signed_in", lambda request: (False, "user_abc"))
    monkeypatch.setattr(ui_ckan, "_identity", lambda request: "user_abc")


def _register(client, **extra):
    data = dict(FORM)
    data.update(extra)
    return client.post("/provider/sources/new", data=data, follow_redirects=False)


def _source(_isolated_db, name="orgsrc"):
    with _isolated_db() as s:
        row = s.query(ProviderSource).filter_by(name=name).one()
        return row.id, row.organization_id, row.organization_title


# -- which organizations may be chosen -----------------------------------------------------------------------------------------------------------------------------

def test_a_super_admin_may_give_a_source_to_any_organization(client, ckan):
    found = client.get("/provider/organizations").json()["organizations"]
    assert [o["title"] for o in found] == ["Air", "Traffic", "Zeta authority"]
    assert [c[0] for c in ckan.calls] == ["organization_list"] and ckan.calls[0][1] == {"all_fields": True}


def test_anyone_else_may_choose_among_the_organizations_they_administer(client, ckan, org_admin):
    found = client.get("/provider/organizations").json()["organizations"]
    assert [o["title"] for o in found] == ["Traffic"]
    assert ckan.calls[0][1] == {"id": "user_abc", "permission": "admin"}  # administers, not merely may add datasets to


def test_without_a_ckan_account_or_without_ckan_there_is_nothing_to_choose_from(client, monkeypatch):
    monkeypatch.setattr(ui_provider, "_signed_in", lambda request: (False, ""))
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "t")
    refused = client.get("/provider/organizations")
    assert refused.status_code == 403 and "Sign in with your data.gov.gr account" in refused.json()["error"]
    monkeypatch.delenv("CKAN_BASE_URL")
    assert "not set up" in client.get("/provider/organizations").json()["error"]


# -- registering ------------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_super_admin_registers_a_source_with_an_organization_the_title_comes_from_ckan(client, ckan, _isolated_db):
    resp = _register(client, organization_id="org3", organization_title="whatever the form said")
    assert resp.status_code in (200, 303)
    assert _source(_isolated_db)[1:] == ("org3", "Zeta authority")  # (the title is CKAN's, not the form's)


def test_a_super_admin_may_leave_the_organization_out(client, ckan, _isolated_db):
    _register(client)
    assert _source(_isolated_db)[1:] == (None, None)


def test_an_organization_ckan_does_not_have_is_refused_and_nothing_is_saved(client, ckan, _isolated_db):
    resp = _register(client, organization_id="org-nope")
    assert resp.status_code == 400 and "You may not give a source to that organization." in resp.text
    with _isolated_db() as s:
        assert s.query(ProviderSource).count() == 0


def test_an_organization_administrator_must_choose_one_of_theirs(client, ckan, org_admin, _isolated_db):
    missing = _register(client)
    assert missing.status_code == 400 and "Choose the organization the source belongs to." in missing.text
    other = _register(client, organization_id="org2")  # they may add datasets there but do not administer it
    assert other.status_code == 400 and "You may not give a source to that organization." in other.text
    assert _register(client, organization_id="org1").status_code in (200, 303)
    assert _source(_isolated_db)[1:] == ("org1", "Traffic")


def test_a_deployment_without_ckan_takes_the_organization_as_given(client, _isolated_db, monkeypatch):
    monkeypatch.delenv("CKAN_BASE_URL", raising=False)
    monkeypatch.delenv("CKAN_SYSADMIN_API_TOKEN", raising=False)
    _register(client, organization_id="org9", organization_title="Local org")
    assert _source(_isolated_db)[1:] == ("org9", "Local org")


def test_ckan_being_down_is_said_and_nothing_is_saved(client, ckan, _isolated_db):
    from providerharvest_service import ckan_client

    ckan.fail["organization_list"] = ckan_client.CKANActionError("organization_list", "down")
    resp = _register(client, organization_id="org1")
    assert resp.status_code == 400 and "could not be checked with CKAN" in resp.text


# -- editing -----------------------------------------------------------------------------------------------------------------------------------------------------------

def test_an_existing_source_is_given_an_organization_and_a_form_without_the_field_leaves_it_alone(client, ckan, _isolated_db):
    _register(client)
    source_id = _source(_isolated_db)[0]
    edit = dict(FORM, api_key="")
    client.post("/provider/sources/%s/edit" % source_id, data=dict(edit, organization_id="org1"), follow_redirects=False)
    assert _source(_isolated_db)[1:] == ("org1", "Traffic")
    client.post("/provider/sources/%s/edit" % source_id, data=dict(edit, title="Renamed"), follow_redirects=False)  # (a form that does not carry the field)
    assert _source(_isolated_db)[1:] == ("org1", "Traffic")
    client.post("/provider/sources/%s/edit" % source_id, data=dict(edit, organization_id=""), follow_redirects=False)  # (the field, emptied: a super admin may)
    assert _source(_isolated_db)[1:] == (None, None)


def test_an_organization_administrator_cannot_move_a_source_to_an_organization_they_do_not_administer(client, ckan, org_admin, _isolated_db):
    _register(client, organization_id="org1")
    source_id = _source(_isolated_db)[0]
    resp = client.post("/provider/sources/%s/edit" % source_id, data=dict(FORM, api_key="", organization_id="org2"), follow_redirects=False)
    assert resp.status_code == 400 and "You may not give a source to that organization." in resp.text
    assert _source(_isolated_db)[1] == "org1"


# -- the pages -----------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_form_has_the_picker_and_says_whether_it_is_required(client, ckan):
    page = client.get("/provider/sources/new").text
    assert 'name="organization_id"' in page and 'id="org-q"' in page and "fetch('/provider/sources/organizations'" not in page and "fetch('/provider/organizations'" in page
    assert "Organization (optional)" in page and "var ORG_REQUIRED = false;" in page


def test_an_organization_administrator_sees_it_as_required(client, ckan, org_admin):
    page = client.get("/provider/sources/new").text
    assert "var ORG_REQUIRED = true;" in page and "Organization <span" in page and "(optional)</label>" not in page.split('for="org-q"')[1].split("</label>")[0]


def test_the_extract_box_shows_each_sources_organization_and_flags_the_ones_without_to_super_admins(client, ckan, _isolated_db):
    _register(client, organization_id="org1")
    _register(client, name="plain", title="Plain")
    page = client.get("/provider/sources").text
    assert '<div class="muted" style="font-size:0.78rem;">Traffic</div>' in page
    assert "no organization" in page  # the one without, for a super admin
