import base64
import os

import pytest
from fastapi.testclient import TestClient

from providerharvest_service import ckan_client, db
from providerharvest_service.app import app
from providerharvest_service.auth import require_current_username


class FakeResponse:
    def __init__(self, json_body):
        self.status_code = 200
        self._json_body = json_body

    def json(self):
        return self._json_body


class QueuedFakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CKAN_BASE_URL", "https://data.gov.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "sysadmin-token")
    monkeypatch.setenv(
        "CKAN_PROVIDERHARVEST_MASTER_KEY", base64.b64encode(os.urandom(32)).decode()
    )


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "me-test.db"))
    session_factory = db.make_session_factory(engine)

    def _override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db.get_session] = _override
    yield
    app.dependency_overrides.pop(db.get_session, None)


@pytest.fixture
def authenticated_as_alice():
    app.dependency_overrides[require_current_username] = lambda: "alice"
    yield
    app.dependency_overrides.pop(require_current_username, None)


@pytest.fixture
def client():
    return TestClient(app)


def _stub_ckan_session(monkeypatch, responses):
    fake = QueuedFakeSession(responses)
    monkeypatch.setattr(ckan_client.requests, "Session", lambda: fake)
    return fake


def test_me_provisions_a_token_and_calls_ckan_as_the_user(client, monkeypatch, authenticated_as_alice):
    fake_session = _stub_ckan_session(monkeypatch, [
        FakeResponse({"success": True, "result": {"token": "alices-real-token"}}),
        FakeResponse({"success": True, "result": [{"name": "ministry-of-example"}]}),
    ])

    resp = client.get("/me")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"username": "alice", "admin_of_orgs": ["ministry-of-example"]}

    mint_url, mint_kwargs = fake_session.calls[0]
    assert mint_url.endswith("/api/3/action/api_token_create")
    assert mint_kwargs["json"] == {"user": "alice", "name": "loader-data-gov-gr"}
    assert mint_kwargs["headers"]["Authorization"] == "sysadmin-token"

    org_url, org_kwargs = fake_session.calls[1]
    assert org_url.endswith("/api/3/action/organization_list_for_user")
    # The org-membership check runs AS alice, using her own freshly
    # minted token -- not the sysadmin's. This is the actual point of
    # the per-user-token design: CKAN enforces the permission, not us.
    assert org_kwargs["headers"]["Authorization"] == "alices-real-token"


def test_me_reuses_the_stored_token_on_a_second_call(client, monkeypatch, authenticated_as_alice):
    _stub_ckan_session(monkeypatch, [
        FakeResponse({"success": True, "result": {"token": "alices-real-token"}}),
        FakeResponse({"success": True, "result": []}),
    ])
    client.get("/me")

    # Second request: only one CKAN call this time (the org check) --
    # no second api_token_create.
    second_session = _stub_ckan_session(monkeypatch, [
        FakeResponse({"success": True, "result": []}),
    ])
    resp = client.get("/me")

    assert resp.status_code == 200, resp.text
    assert len(second_session.calls) == 1
    assert second_session.calls[0][0].endswith("/api/3/action/organization_list_for_user")


def test_me_without_auth_header_is_401(client):
    # authenticated_as_alice deliberately NOT requested here -- the real
    # require_current_username dependency runs, sees no Authorization
    # header on the request, and rejects it before anything CKAN-related
    # is even attempted.
    resp = client.get("/me")

    assert resp.status_code == 401
