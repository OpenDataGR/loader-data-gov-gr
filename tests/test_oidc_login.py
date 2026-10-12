"""Login with the data.gov.gr Keycloak: the whole authorization-code flow against a fake Keycloak that signs real
tokens with a real (self-made) key pair -- the signature, issuer, audience, nonce and role checks are the production
code, only the two network calls are faked."""

import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from providerharvest_service import admin_session, oidc_login
from providerharvest_service.app import app
from tests.test_ui_admin_ftp import _admin_session_env, _isolated_db, _master_key, _upload_root  # noqa: F401

ISSUER = "https://auth.example.gr/realms/data"
CLIENT = "loader-data-gov-gr"
BASE = "https://loader.example.gr"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def _pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


class FakeKeycloak:
    """Stands in for ``requests``: the discovery document and the token endpoint."""

    def __init__(self):
        self.nonce = None
        self.roles = ["loader-admin"]
        self.username = "ksfikas"
        self.token_status = 200
        self.signing_key = KEY
        self.id_overrides = {}
        self.access_overrides = {}
        self.token_calls = []

    def get(self, url, timeout=None):
        assert url == ISSUER + "/.well-known/openid-configuration"
        return FakeResponse(200, {
            "issuer": ISSUER, "authorization_endpoint": ISSUER + "/protocol/openid-connect/auth",
            "token_endpoint": ISSUER + "/protocol/openid-connect/token", "jwks_uri": ISSUER + "/protocol/openid-connect/certs",
            "end_session_endpoint": ISSUER + "/protocol/openid-connect/logout",
        })

    def post(self, url, *, timeout=None, auth=None, data=None):
        self.token_calls.append({"url": url, "auth": auth, "data": data})
        if self.token_status != 200:
            return FakeResponse(self.token_status, {"error": "invalid_grant"})
        now = int(time.time())
        base = {"iss": ISSUER, "sub": "user-1", "iat": now, "exp": now + 300, "preferred_username": self.username}
        ident = {**base, "aud": CLIENT, "nonce": self.nonce, **self.id_overrides}
        access = {**base, "aud": "account", "azp": CLIENT, "realm_access": {"roles": self.roles}, **self.access_overrides}

        def sign(claims):
            return jwt.encode(claims, _pem(self.signing_key), algorithm="RS256", headers={"kid": "k1"})

        return FakeResponse(200, {"id_token": sign(ident), "access_token": sign(access), "token_type": "Bearer"})


@pytest.fixture
def keycloak(monkeypatch):
    fake = FakeKeycloak()
    monkeypatch.setenv("LOADER_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("LOADER_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("LOADER_OIDC_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("LOADER_PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(oidc_login, "_http", fake)
    monkeypatch.setattr(oidc_login, "_signing_key", lambda token, uri: PUBLIC_PEM)
    oidc_login._discovery_cache.clear()
    return fake


def _browser():
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def _start(browser, keycloak, next_url="/transform"):
    resp = browser.get("/auth/login", params={"next": next_url})
    assert resp.status_code == 303 and resp.headers["location"].startswith(ISSUER + "/protocol/openid-connect/auth?")
    query = parse_qs(urlparse(resp.headers["location"]).query)
    keycloak.nonce = query["nonce"][0]
    return query


def _call_back(browser, query, code="the-code", state=None):
    return browser.get("/auth/callback", params={"code": code, "state": state or query["state"][0]})


def _flash(resp):
    return parse_qs(urlparse(resp.headers["location"]).query).get("flash", [""])[0]


def test_nothing_changes_until_keycloak_is_configured(monkeypatch):
    for name in ("LOADER_OIDC_ISSUER", "LOADER_OIDC_CLIENT_ID", "LOADER_OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    browser = _browser()
    assert browser.get("/auth/login").headers["location"] == "/admin/login"
    page = browser.get("/admin/login").text
    assert "Sign in with data.gov.gr" not in page and 'name="password"' in page


def test_the_login_page_offers_keycloak_and_keeps_the_shared_account_underneath(keycloak):
    page = _browser().get("/admin/login", params={"next": "/transform"}).text
    assert 'href="/auth/login?next=/transform"' in page and "Sign in with data.gov.gr" in page
    assert "Use the shared account instead" in page and 'name="password"' in page


def test_the_request_to_keycloak_carries_pkce_state_nonce_and_the_exact_callback(keycloak):
    query = _start(_browser(), keycloak)
    assert query["client_id"] == [CLIENT] and query["response_type"] == ["code"] and "openid" in query["scope"][0]
    assert query["redirect_uri"] == [BASE + "/auth/callback"]
    assert query["code_challenge_method"] == ["S256"] and len(query["code_challenge"][0]) == 43
    assert len(query["state"][0]) >= 20 and len(query["nonce"][0]) >= 20


def test_a_person_with_the_role_is_signed_in_as_themselves_and_sent_where_they_were_going(keycloak):
    browser = _browser()
    query = _start(browser, keycloak, "/transform")
    resp = _call_back(browser, query)
    assert resp.status_code == 303 and resp.headers["location"] == "/transform"
    assert admin_session.verify_session_cookie(browser.cookies.get(admin_session.COOKIE_NAME)) == "ksfikas"
    call = keycloak.token_calls[0]
    assert call["auth"] == (CLIENT, "s3cret") and call["data"]["code"] == "the-code"
    assert call["data"]["redirect_uri"] == BASE + "/auth/callback" and len(call["data"]["code_verifier"]) >= 43
    assert browser.get("/provider/sources").status_code == 200  # the gate now lets them in


def test_a_person_without_the_role_is_refused_with_the_reason_and_gets_no_session(keycloak):
    keycloak.roles = ["offline_access"]
    browser = _browser()
    query = _start(browser, keycloak)
    resp = _call_back(browser, query)
    assert resp.headers["location"].startswith("/admin/login?") and "loader-admin" in _flash(resp) and "ksfikas" in _flash(resp)
    assert admin_session.COOKIE_NAME not in browser.cookies
    assert browser.get("/provider/sources").headers["location"].startswith("/admin/login")


@pytest.mark.parametrize("break_it,fragment", [
    ({"id_overrides": {"nonce": "someone-elses"}}, "does not belong to this browser"),
    ({"id_overrides": {"aud": "another-app"}}, "could not be verified"),
    ({"id_overrides": {"iss": "https://evil.example/realms/data"}}, "could not be verified"),
    ({"id_overrides": {"exp": 1}}, "could not be verified"),
    ({"access_overrides": {"azp": "another-app"}}, "different application"),
    ({"access_overrides": {"sub": "someone-else"}}, "same person"),
    ({"username": ""}, "no user name"),
])
def test_tokens_that_do_not_check_out_never_start_a_session(keycloak, break_it, fragment):
    for attribute, value in break_it.items():
        setattr(keycloak, attribute, value)
    browser = _browser()
    query = _start(browser, keycloak)
    resp = _call_back(browser, query)
    assert fragment in _flash(resp) and admin_session.COOKIE_NAME not in browser.cookies


def test_a_token_signed_with_some_other_key_is_refused(keycloak):
    keycloak.signing_key = OTHER_KEY
    browser = _browser()
    resp = _call_back(browser, _start(browser, keycloak))
    assert "could not be verified" in _flash(resp) and admin_session.COOKIE_NAME not in browser.cookies


def test_a_response_that_this_browser_did_not_ask_for_is_refused(keycloak):
    browser = _browser()
    query = _start(browser, keycloak)
    assert "does not belong" in _flash(_call_back(browser, query, state="forged-state"))
    assert keycloak.token_calls == []  # the code was never even traded
    stranger = _browser()  # no cookie from a started login
    assert "expired or did not start here" in _flash(_call_back(stranger, query))


def test_an_expired_sign_in_attempt_is_refused(keycloak, monkeypatch):
    browser = _browser()
    query = _start(browser, keycloak)
    real = time.time
    monkeypatch.setattr(oidc_login.time, "time", lambda: real() + oidc_login.TX_TTL_S + 5)
    assert "expired" in _flash(_call_back(browser, query))


def test_keycloak_refusing_the_code_or_the_user_declining_is_explained(keycloak):
    keycloak.token_status = 400
    browser = _browser()
    assert "did not accept" in _flash(_call_back(browser, _start(browser, keycloak)))
    browser = _browser()
    _start(browser, keycloak)
    declined = browser.get("/auth/callback", params={"error": "access_denied", "error_description": "User cancelled"})
    assert "declined" in _flash(declined) and "User cancelled" in _flash(declined)


def test_the_way_back_cannot_be_turned_into_an_open_redirect(keycloak):
    browser = _browser()
    query = _start(browser, keycloak, "//evil.example/steal")
    assert _call_back(browser, query).headers["location"] == "/provider/sources"


def test_logging_out_also_ends_the_keycloak_session(keycloak):
    browser = _browser()
    target = browser.post("/admin/logout").headers["location"]
    assert target.startswith(ISSUER + "/protocol/openid-connect/logout?")
    query = parse_qs(urlparse(target).query)
    assert query["client_id"] == [CLIENT] and query["post_logout_redirect_uri"] == [BASE + "/admin/login"]


def test_the_shared_password_can_be_switched_off_once_keycloak_works(keycloak, monkeypatch):
    monkeypatch.setenv("LOADER_ADMIN_PASSWORD_LOGIN", "off")
    browser = _browser()
    assert 'name="password"' not in browser.get("/admin/login").text
    resp = browser.post("/admin/login", data={"username": "x", "password": "y", "next": "/"})
    assert "data.gov.gr" in _flash(resp) and admin_session.COOKIE_NAME not in browser.cookies


def test_without_keycloak_configured_the_shared_login_still_works_exactly_as_before(monkeypatch):
    from tests.test_ui_admin_ftp import ADMIN_PASSWORD, ADMIN_USERNAME

    for name in ("LOADER_OIDC_ISSUER", "LOADER_OIDC_CLIENT_ID", "LOADER_OIDC_CLIENT_SECRET", "LOADER_ADMIN_PASSWORD_LOGIN"):
        monkeypatch.delenv(name, raising=False)
    browser = _browser()
    resp = browser.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/transform"})
    assert resp.headers["location"] == "/transform" and admin_session.COOKIE_NAME in browser.cookies
