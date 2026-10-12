"""The signed-in person is shown in the masthead: name, user, email, how they signed in, what gives them access."""

import pytest

from providerharvest_service import admin_session
from tests.test_ckan_access import SUB, FakeCkan, ckan, ckan_rule  # noqa: F401
from tests.test_oidc_login import (  # noqa: F401  (fixtures and helpers shared with the Keycloak login tests)
    _admin_session_env, _browser, _call_back, _isolated_db, _master_key, _start, _upload_root, keycloak,
)
from tests.test_ui_admin_ftp import ADMIN_PASSWORD, ADMIN_USERNAME

GREEK_NAME = "ΚΩΝΣΤΑΝΤΙΝΟΣ ΣΦΗΚΑΣ"


def _sign_in(browser, keycloak, **claims):
    keycloak.id_overrides = {**keycloak.id_overrides, "name": GREEK_NAME, "email": "k.sfikas@example.gr", **claims}
    assert _call_back(browser, _start(browser, keycloak)).status_code == 303


def test_a_keycloak_login_shows_who_signed_in_and_the_role_that_let_them_in(keycloak):
    browser = _browser()
    _sign_in(browser, keycloak)
    page = browser.get("/provider/sources").text
    assert GREEK_NAME in page and "ksfikas" in page and "k.sfikas@example.gr" in page
    assert "Keycloak (data.gov.gr)" in page and "Role loader-admin" in page and '<span class="user-avatar"' in page


def test_a_ckan_sysadmin_is_shown_as_one(ckan_rule, keycloak):
    ckan_rule.users["user_" + SUB] = {"sysadmin": True}
    browser = _browser()
    _sign_in(browser, keycloak)
    assert "CKAN sysadmin" in browser.get("/provider/sources").text


def test_an_organization_admin_is_shown_with_their_organizations(ckan_rule, keycloak):
    ckan_rule.users["user_" + SUB] = {"orgs": ["attica-region", "other-org"]}
    browser = _browser()
    _sign_in(browser, keycloak)
    assert "Administrator of 2 organizations (attica-region, other-org)" in browser.get("/provider/sources").text


def test_many_organizations_are_summarised_not_listed_in_full(ckan_rule, keycloak):
    ckan_rule.users["user_" + SUB] = {"orgs": ["org-%d" % i for i in range(30)]}
    browser = _browser()
    _sign_in(browser, keycloak)
    page = browser.get("/provider/sources").text
    assert "Administrator of 30 organizations (org-0, org-1, org-2, org-3, ...)" in page and "org-29" not in page


def test_the_shared_account_is_shown_as_such(monkeypatch):
    browser = _browser()
    browser.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/"})
    page = browser.get("/provider/sources").text
    assert ADMIN_USERNAME in page and "Shared account" in page and '<span class="user-avatar"' in page


def test_the_login_page_and_a_signed_out_visitor_show_no_account(keycloak):
    page = _browser().get("/admin/login").text
    assert '<details class="user-menu">' not in page and 'Signed in with' not in page


def test_a_forged_or_foreign_profile_cookie_adds_nothing(keycloak):
    browser = _browser()
    _sign_in(browser, keycloak)
    browser.cookies.set(admin_session.PROFILE_COOKIE, "e30.forged")  # not validly signed
    page = browser.get("/provider/sources").text
    assert GREEK_NAME not in page and "Role loader-admin" not in page and "ksfikas" in page
    someone_else = admin_session.seal("profile", {"u": "somebody-else", "n": "Mallory", "sys": True})
    browser.cookies.set(admin_session.PROFILE_COOKIE, someone_else)  # signed, but about another user name
    assert "Mallory" not in browser.get("/provider/sources").text


def test_a_profile_cookie_cannot_be_used_as_a_session_and_logout_clears_it(keycloak):
    profile = admin_session.seal("profile", {"u": "ksfikas"})
    assert admin_session.verify_session_cookie(profile) is None  # different purpose, different signature
    browser = _browser()
    _sign_in(browser, keycloak)
    assert admin_session.PROFILE_COOKIE in browser.cookies
    browser.post("/admin/logout")
    assert admin_session.PROFILE_COOKIE not in browser.cookies


def test_the_account_card_closes_on_an_outside_click_and_on_escape(keycloak):
    browser = _browser()
    _sign_in(browser, keycloak)
    page = browser.get("/provider/sources").text
    assert "menu.contains(e.target)" in page and "menu.open = false" in page and "e.key === 'Escape'" in page
