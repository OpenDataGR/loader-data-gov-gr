import base64
import os

import bcrypt
import pytest
from fastapi.testclient import TestClient

from providerharvest_service import connection_check, db, provider_sources
from providerharvest_service.app import app
from providerharvest_service.engine.transport.base import Entry, Page, Transport

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


@pytest.fixture(autouse=True)
def _admin_session_env(monkeypatch):
    monkeypatch.setenv("LOADER_ADMIN_UI_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv(
        "LOADER_ADMIN_UI_PASSWORD_HASH",
        bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode(),
    )
    monkeypatch.setenv("LOADER_ADMIN_SESSION_SECRET", "test-session-secret")


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


@pytest.fixture
def client(_admin_session_env):
    c = TestClient(app, base_url="https://testserver")
    c.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/admin/sources"})
    return c


def _register(session_factory, **overrides):
    form = {
        "name": "acme-open-data",
        "title": "Acme open data",
        "endpoint_url": "https://provider.example.gr/api/records",
        "transport_type": "http",
        "frequency": "DAILY",
        "auth_type": "api_key",
        "api_key": "secret-key-123",
    }
    form.update(overrides)
    with session_factory() as session:
        source = provider_sources.register(session, form)
        return source.id


def test_pending_queue_lists_only_pending_sources(client, _isolated_db):
    _register(_isolated_db)
    resp = client.get("/admin/sources/pending")
    assert resp.status_code == 200
    assert "acme-open-data" in resp.text


def test_activate_moves_source_out_of_pending_queue(client, _isolated_db):
    source_id = _register(_isolated_db)

    resp = client.post("/admin/sources/%s/activate" % source_id, follow_redirects=False)
    assert resp.status_code == 303

    pending = client.get("/admin/sources/pending")
    assert "acme-open-data" not in pending.text

    all_sources = client.get("/admin/sources")
    assert "active" in all_sources.text


def test_reject_records_reason_and_shows_it_on_the_dashboard(client, _isolated_db):
    source_id = _register(_isolated_db)

    resp = client.post(
        "/admin/sources/%s/reject" % source_id,
        data={"reason": "endpoint unreachable"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    all_sources = client.get("/admin/sources")
    assert "rejected" in all_sources.text
    assert "endpoint unreachable" in all_sources.text


def test_pause_resume_redirect_defaults_to_all_sources_without_a_recognized_referer(client, _isolated_db):
    source_id = _register(_isolated_db)
    client.post("/admin/sources/%s/activate" % source_id)

    resp = client.post(
        "/admin/sources/%s/pause" % source_id,
        headers={"referer": "https://evil.example/steal"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Must never redirect off-site, even if Referer claims otherwise.
    assert resp.headers["location"].startswith("/admin/sources")
    assert "evil.example" not in resp.headers["location"]


def test_activating_unknown_source_shows_not_found_message(client):
    resp = client.post("/admin/sources/does-not-exist/activate", follow_redirects=False)
    assert resp.status_code == 303
    followed = client.get(resp.headers["location"])
    assert "No such source." in followed.text


class _FakeTransport(Transport):
    def __init__(self, entries=None, error=None):
        self._entries = entries or []
        self._error = error

    def connect(self):
        if self._error:
            raise self._error

    def list_entries(self, cursor):
        return Page(entries=self._entries, next_cursor=None)

    def open_entry(self, entry):
        raise NotImplementedError

    def close(self):
        pass


def test_check_connection_persists_health_status(client, _isolated_db, monkeypatch):
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda source, secret: _FakeTransport(entries=[Entry(ref="r1"), Entry(ref="r2")]),
    )
    source_id = _register(_isolated_db)

    resp = client.post("/admin/sources/%s/check-connection" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash_kind=success" in resp.headers["location"]

    all_sources = client.get("/admin/sources")
    assert "reachable" in all_sources.text


def test_check_connection_on_unreachable_source_shows_error(client, _isolated_db, monkeypatch):
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda source, secret: _FakeTransport(error=TimeoutError("timed out")),
    )
    source_id = _register(_isolated_db)

    resp = client.post("/admin/sources/%s/check-connection" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]

    all_sources = client.get("/admin/sources")
    assert "unreachable" in all_sources.text


def test_admin_can_delete_a_source(client, _isolated_db):
    source_id = _register(_isolated_db)
    resp = client.post("/admin/sources/%s/delete" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash=Source+deleted" in resp.headers["location"]

    all_sources = client.get("/admin/sources")
    assert "acme-open-data" not in all_sources.text


def test_admin_edit_link_points_to_shared_edit_route(client, _isolated_db):
    _register(_isolated_db)
    resp = client.get("/admin/sources")
    assert "/provider/sources/" in resp.text
    assert "/edit" in resp.text


def test_admin_lists_badge_pull_and_push_sources(client, _isolated_db):
    _register(_isolated_db)  # http, pull
    with _isolated_db() as session:
        provider_sources.register(session, {"name": "acme-push", "title": "Push", "transport_type": "push"})

    for path in ("/admin/sources", "/admin/sources/pending"):
        text = client.get(path).text
        assert '<span class="badge badge-pull"' in text, path
        assert '<span class="badge badge-push"' in text, path
        assert "<span>https</span>" in text, path
