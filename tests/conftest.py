import bcrypt
import pytest
from fastapi.testclient import TestClient

from providerharvest_service.app import app

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _failed_auth_goes_to_a_throwaway_db(tmp_path, monkeypatch):
    """Refused logins/keys are recorded through ``db.SessionLocal`` (see
    failed_auth.py), outside the get_session dependency that tests override.
    Without this every 401 any test provokes would be written to the real
    default database file."""
    from providerharvest_service import db

    monkeypatch.setattr(db, "SessionLocal", db.make_session_factory(db.make_engine("sqlite:///%s" % (tmp_path / "failed-auth.db"))))


@pytest.fixture
def admin_session_env(monkeypatch):
    monkeypatch.setenv("LOADER_ADMIN_UI_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv(
        "LOADER_ADMIN_UI_PASSWORD_HASH",
        bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode(),
    )
    monkeypatch.setenv("LOADER_ADMIN_SESSION_SECRET", "test-session-secret")


@pytest.fixture
def logged_in_client(admin_session_env):
    """A client with a valid login session -- the whole site sits behind
    the login now, so anything driving the provider portal or admin pages
    needs one. https:// because the session cookie is Secure and the test
    client will not resend a Secure cookie over plain http."""
    client = TestClient(app, base_url="https://testserver")
    client.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    return client
