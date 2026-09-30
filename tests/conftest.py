import bcrypt
import pytest
from fastapi.testclient import TestClient

from providerharvest_service.app import app

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"


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
