import bcrypt
import pytest
from fastapi.testclient import TestClient

from providerharvest_service import db
from providerharvest_service.admin_session import verify_session_cookie
from providerharvest_service.app import app

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "test.db"))
    session_factory = db.make_session_factory(engine)

    def _override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db.get_session] = _override
    yield session_factory
    app.dependency_overrides.pop(db.get_session, None)


@pytest.fixture(autouse=True)
def _admin_session_env(monkeypatch):
    monkeypatch.setenv("LOADER_ADMIN_UI_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv(
        "LOADER_ADMIN_UI_PASSWORD_HASH",
        bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode(),
    )
    monkeypatch.setenv("LOADER_ADMIN_SESSION_SECRET", "test-session-secret")


@pytest.fixture
def client():
    return TestClient(app, base_url="https://testserver")


def test_admin_sources_redirects_to_login_when_not_authenticated(client):
    resp = client.get("/admin/sources", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login?next=")


def test_admin_ftp_redirects_to_login_when_not_authenticated(client):
    resp = client.get("/admin/ftp", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login?next=")


def test_provider_portal_is_never_gated(client):
    resp = client.get("/provider/sources")
    assert resp.status_code == 200


def test_ftp_sync_json_api_is_never_gated_by_the_session(client, monkeypatch):
    monkeypatch.setenv("LOADER_FTP_SYNC_KEY", "the-sync-key")
    resp = client.get("/admin/ftp-accounts-sync", headers={"X-Ftp-Sync-Key": "the-sync-key"})
    assert resp.status_code == 200  # never redirected to /admin/login


def test_login_with_wrong_password_shows_flash_and_stays_logged_out(client):
    resp = client.post(
        "/admin/login", data={"username": ADMIN_USERNAME, "password": "wrong"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "flash=Invalid" in resp.headers["location"]
    assert client.cookies.get("loader_admin_session") is None


def test_login_with_wrong_username_fails_too(client):
    resp = client.post(
        "/admin/login", data={"username": "someone-else", "password": ADMIN_PASSWORD}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "flash=Invalid" in resp.headers["location"]


def test_login_not_configured_shows_its_own_flash(client, monkeypatch):
    monkeypatch.delenv("LOADER_ADMIN_UI_PASSWORD_HASH", raising=False)
    resp = client.post(
        "/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "not+configured" in resp.headers["location"]


def test_successful_login_grants_access_and_redirects_to_next(client):
    resp = client.post(
        "/admin/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/admin/ftp"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ftp"
    assert client.cookies.get("loader_admin_session") is not None

    resp = client.get("/admin/sources", follow_redirects=False)
    assert resp.status_code == 200


def test_login_rejects_an_open_redirect_via_next(client):
    resp = client.post(
        "/admin/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "https://evil.example/steal"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/sources"  # falls back to the default, never off-site


def test_logout_clears_the_session(client):
    client.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert client.get("/admin/sources", follow_redirects=False).status_code == 200

    client.post("/admin/logout")
    resp = client.get("/admin/sources", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login")


def test_a_tampered_cookie_is_rejected(client):
    client.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    good_cookie = client.cookies.get("loader_admin_session")
    client.cookies.set("loader_admin_session", good_cookie[:-1] + ("0" if good_cookie[-1] != "0" else "1"))
    resp = client.get("/admin/sources", follow_redirects=False)
    assert resp.status_code == 303


def test_rotating_the_session_secret_invalidates_existing_sessions(client, monkeypatch):
    client.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert client.get("/admin/sources", follow_redirects=False).status_code == 200

    monkeypatch.setenv("LOADER_ADMIN_SESSION_SECRET", "a-different-secret")
    resp = client.get("/admin/sources", follow_redirects=False)
    assert resp.status_code == 303


def test_verify_session_cookie_rejects_an_expired_token(monkeypatch):
    from providerharvest_service.admin_session import create_session_cookie

    token = create_session_cookie("kostas", ttl_s=-1)
    assert verify_session_cookie(token) is None
