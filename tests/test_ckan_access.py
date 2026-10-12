"""Who may use the loader according to CKAN: a sysadmin, or an administrator of at least one organization.
The CKAN side is a fake that answers the way the real data.gov.gr CKAN was seen to answer (see ckan_access.py)."""

import pytest

from providerharvest_service import admin_session, ckan_access, ckan_client, oidc_login
from tests.test_oidc_login import (  # noqa: F401  (fixtures and helpers shared with the Keycloak login tests)
    _admin_session_env, _browser, _call_back, _flash, _isolated_db, _master_key, _start, _upload_root, keycloak,
)

SUB = "9b2288a6-f855-43ca-a5ba-7a3ef08de4de"


class FakeCkan:
    def __init__(self):
        self.users = {}      # CKAN user name -> {"sysadmin": bool, "state": "active", "orgs": [names administered]}
        self.calls = []
        self.down = False

    def call(self, action, data):
        self.calls.append((action, dict(data)))
        if self.down:
            raise ckan_client.CKANActionError(action, "request failed: connection refused")
        user = self.users.get(data.get("id"))
        if user is None:
            raise ckan_client.CKANNotFound(action, "Not found")
        if action == "user_show":
            return {"name": data["id"], "sysadmin": user.get("sysadmin", False), "state": user.get("state", "active")}
        if action == "organization_list_for_user":
            assert data["permission"] == "admin"  # the only permission that separates administrators from editors
            return [{"name": n} for n in user.get("orgs", [])]
        raise AssertionError("unexpected CKAN action %s" % action)


@pytest.fixture
def ckan(monkeypatch):
    fake = FakeCkan()
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "token")
    monkeypatch.setattr(ckan_access, "_client", lambda: fake)
    return fake


def test_a_ckan_sysadmin_is_allowed_without_needing_any_organization(ckan):
    ckan.users["user_" + SUB] = {"sysadmin": True}
    verdict = ckan_access.check(SUB)
    assert verdict.allowed and verdict.sysadmin and verdict.ckan_user == "user_" + SUB
    assert [a for a, _ in ckan.calls] == ["user_show"]


def test_an_administrator_of_an_organization_is_allowed_and_the_organizations_are_known(ckan):
    ckan.users["user_" + SUB] = {"orgs": ["attica-region", "other-org"]}
    verdict = ckan_access.check(SUB)
    assert verdict.allowed and not verdict.sysadmin and verdict.organizations == ("attica-region", "other-org")
    assert ckan.calls[1] == ("organization_list_for_user", {"id": "user_" + SUB, "permission": "admin"})


def test_an_editor_or_member_is_refused_because_they_administer_nothing(ckan):
    ckan.users["user_" + SUB] = {"orgs": []}
    verdict = ckan_access.check(SUB)
    assert not verdict.allowed and "not an administrator of any organization" in verdict.reason


def test_someone_without_a_ckan_account_is_refused_and_told_why(ckan):
    verdict = ckan_access.check("someone-new")
    assert not verdict.allowed and "no account in CKAN" in verdict.reason


def test_the_ckan_user_is_found_by_keycloak_id_never_by_name(ckan):
    ckan.users["ksfikas"] = {"sysadmin": True}  # a different CKAN user that merely has the same name
    assert not ckan_access.check(SUB).allowed
    assert all(data["id"] == "user_" + SUB for _, data in ckan.calls)


def test_a_deleted_ckan_account_is_refused(ckan):
    ckan.users["user_" + SUB] = {"sysadmin": True, "state": "deleted"}
    assert not ckan_access.check(SUB).allowed


def test_when_ckan_cannot_be_asked_nobody_is_let_in(ckan):
    ckan.users["user_" + SUB] = {"sysadmin": True}
    ckan.down = True
    with pytest.raises(ckan_access.CkanAccessError, match="could not be asked"):
        ckan_access.check(SUB)


def test_without_the_ckan_connection_set_up_the_rule_cannot_answer(monkeypatch):
    monkeypatch.delenv("CKAN_BASE_URL", raising=False)
    with pytest.raises(ckan_access.CkanAccessError, match="not set up"):
        ckan_access.check(SUB)
    with pytest.raises(ckan_access.CkanAccessError):
        ckan_access.check("")


# -- the rule applied at the Keycloak login ----------------------------------------------------------------------------------------

@pytest.fixture
def ckan_rule(monkeypatch, ckan, keycloak):
    monkeypatch.setenv("LOADER_OIDC_ACCESS", "ckan-org-admin")
    keycloak.roles = []  # the Keycloak role is not what decides any more
    keycloak.id_overrides = {"sub": SUB}
    keycloak.access_overrides = {"sub": SUB}
    return ckan


def test_with_the_ckan_rule_an_organization_admin_gets_in_without_any_keycloak_role(ckan_rule, keycloak):
    ckan_rule.users["user_" + SUB] = {"orgs": ["attica-region"]}
    browser = _browser()
    resp = _call_back(browser, _start(browser, keycloak))
    assert resp.status_code == 303 and resp.headers["location"] == "/transform"
    assert admin_session.verify_session_cookie(browser.cookies.get(admin_session.COOKIE_NAME)) == "ksfikas"


def test_with_the_ckan_rule_an_editor_is_refused_even_with_the_keycloak_role(ckan_rule, keycloak):
    keycloak.roles = ["loader-admin"]
    ckan_rule.users["user_" + SUB] = {"orgs": []}
    browser = _browser()
    resp = _call_back(browser, _start(browser, keycloak))
    assert "ksfikas" in _flash(resp) and "not an administrator of any organization" in _flash(resp)
    assert admin_session.COOKIE_NAME not in browser.cookies


def test_with_the_ckan_rule_a_ckan_outage_refuses_the_login_with_a_message(ckan_rule, keycloak):
    ckan_rule.users["user_" + SUB] = {"sysadmin": True}
    ckan_rule.down = True
    browser = _browser()
    resp = _call_back(browser, _start(browser, keycloak))
    assert "could not be asked" in _flash(resp) and admin_session.COOKIE_NAME not in browser.cookies


def test_the_default_rule_is_still_the_keycloak_role_and_ignores_ckan(keycloak, ckan):
    assert oidc_login.access_rule() == "role"
    browser = _browser()
    assert _call_back(browser, _start(browser, keycloak)).status_code == 303 and ckan.calls == []
