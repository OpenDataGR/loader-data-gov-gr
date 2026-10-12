"""Only super admins may open the Data boxes and the Security box -- enforced on the server for every request (pages and
actions), not just by hiding the menu tabs. A super admin is a CKAN sysadmin (CKAN rule), the realm role
``loader-superadmin`` (role rule) or the shared break-glass account."""

from urllib.parse import parse_qs, urlparse

import pytest

from providerharvest_service import admin_session
from tests.test_ckan_access import SUB, ckan, ckan_rule  # noqa: F401
from tests.test_oidc_login import (  # noqa: F401  (fixtures and helpers shared with the Keycloak login tests)
    _admin_session_env, _browser, _call_back, _isolated_db, _master_key, _start, _upload_root, keycloak,
)
from tests.test_ui_admin_ftp import ADMIN_PASSWORD, ADMIN_USERNAME

LOCKED_GET = ["/admin/ftp", "/admin/security/failed-logins", "/admin/security/failed-logins.csv", "/admin/security/failed-logins-by-address.csv",
              "/admin/security/rules/some-id/edit"]
LOCKED_POST = ["/admin/ftp/http-box/actions/reload-workers", "/admin/ftp/http-box/logs", "/admin/ftp/boxes/box1/actions/restart-proftpd",
               "/admin/ftp/boxes/box1/logs", "/admin/security/rules", "/admin/security/rules/move", "/admin/security/rules/some-id/delete",
               "/admin/security/boxes/box1/unban"]


def _sign_in(browser, keycloak):
    assert _call_back(browser, _start(browser, keycloak)).status_code == 303


def _refused(resp):
    return resp.status_code == 303 and resp.headers["location"].startswith("/provider/sources?") and \
        admin_session.SUPERADMIN_ONLY_MESSAGE in parse_qs(urlparse(resp.headers["location"]).query).get("flash", [""])[0]


@pytest.mark.parametrize("path,locked", [
    ("/admin/ftp", True), ("/admin/ftp/http-box/logs", True), ("/admin/ftp/boxes/box1/actions/restart-proftpd", True),
    ("/admin/security", True), ("/admin/security/failed-logins", True), ("/admin/security/rules/x/edit", True),
    ("/admin/ftp/some-account-id/set-password", False), ("/admin/ftp/some-account-id/rotate-password", False),
    ("/admin/ftp/some-account-id/allowed-ips", False), ("/admin/ftp-accounts-sync", False), ("/admin/sources", False),
    ("/admin/sourcesx", False), ("/admin/ftpx", False), ("/provider/sources", False), ("/transform", False), ("/load", False),
])
def test_which_paths_are_for_super_admins_only(path, locked):
    assert admin_session.needs_superadmin(path) is locked


def test_an_organization_admin_who_is_not_a_sysadmin_is_refused_everywhere_in_those_two_boxes(ckan_rule, keycloak):
    ckan_rule.users["user_" + SUB] = {"orgs": ["attica-region"]}
    browser = _browser()
    _sign_in(browser, keycloak)
    for path in LOCKED_GET:
        assert _refused(browser.get(path)), path
    for path in LOCKED_POST:
        assert _refused(browser.post(path)), path


def test_what_belongs_to_managing_a_source_stays_open_to_them(ckan_rule, keycloak):
    ckan_rule.users["user_" + SUB] = {"orgs": ["attica-region"]}
    browser = _browser()
    _sign_in(browser, keycloak)
    assert browser.get("/provider/sources").status_code == 200 and browser.get("/admin/sources").status_code == 200
    for path in ("/admin/ftp/some-account-id/set-password",):
        assert not _refused(browser.get(path)), path  # (an unknown account: not found, but not turned away as a super-admin matter)


def test_the_menu_hides_the_two_tabs_for_them_and_says_so(ckan_rule, keycloak):
    ckan_rule.users["user_" + SUB] = {"orgs": ["attica-region"]}
    browser = _browser()
    _sign_in(browser, keycloak)
    page = browser.get("/provider/sources").text
    assert 'href="/admin/ftp"' not in page and 'href="/admin/security/failed-logins"' not in page
    assert "Extract Box</a>" in page and "Transform Box</a>" in page and "Admin dashboard" in page
    assert "<dt>Super admin</dt><dd>No</dd>" in page


def test_a_ckan_sysadmin_gets_the_tabs_and_the_pages(ckan_rule, keycloak):
    ckan_rule.users["user_" + SUB] = {"sysadmin": True}
    browser = _browser()
    _sign_in(browser, keycloak)
    page = browser.get("/provider/sources").text
    assert 'href="/admin/ftp"' in page and 'href="/admin/security/failed-logins"' in page and "<dt>Super admin</dt><dd>Yes</dd>" in page
    assert browser.get("/admin/ftp").status_code == 200
    assert browser.get("/admin/security/failed-logins").status_code == 200


def test_the_shared_break_glass_account_counts_as_a_super_admin():
    browser = _browser()
    browser.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/"})
    assert browser.get("/admin/ftp").status_code == 200 and browser.get("/admin/security/failed-logins").status_code == 200
    assert 'href="/admin/ftp"' in browser.get("/provider/sources").text


def test_with_the_role_rule_the_realm_role_loader_superadmin_decides(keycloak):
    keycloak.roles = ["loader-admin"]
    plain = _browser()
    _sign_in(plain, keycloak)
    assert _refused(plain.get("/admin/ftp")) and plain.get("/provider/sources").status_code == 200
    keycloak.roles = ["loader-admin", "loader-superadmin"]
    boss = _browser()
    _sign_in(boss, keycloak)
    assert boss.get("/admin/ftp").status_code == 200


def test_a_session_without_a_valid_profile_is_never_a_super_admin(ckan_rule, keycloak):
    ckan_rule.users["user_" + SUB] = {"sysadmin": True}
    browser = _browser()
    _sign_in(browser, keycloak)
    assert browser.get("/admin/ftp").status_code == 200
    browser.cookies.delete(admin_session.PROFILE_COOKIE)  # e.g. a session from before profiles existed
    assert _refused(browser.get("/admin/ftp"))
    browser.cookies.set(admin_session.PROFILE_COOKIE, "e30.forged")
    assert _refused(browser.get("/admin/ftp"))
    browser.cookies.set(admin_session.PROFILE_COOKIE, admin_session.seal("profile", {"u": "somebody-else", "super": True}))
    assert _refused(browser.get("/admin/ftp"))  # signed, but about another user name
    browser.cookies.set(admin_session.PROFILE_COOKIE, admin_session.seal("profile", {"u": "ksfikas", "super": True}, ttl_s=-5))
    assert _refused(browser.get("/admin/ftp"))  # expired
