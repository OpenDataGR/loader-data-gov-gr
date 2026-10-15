import base64
import os

import bcrypt
import pytest
from fastapi.testclient import TestClient

from providerharvest_service import connection_check, db, provider_sources
from providerharvest_service.app import app
from providerharvest_service.engine.transport.base import Entry, Page, Transport
from tests.test_push_ingest import _isolated_harvest_db  # noqa: F401  (a harvest database for the tests that delete a source)

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
def _isolated_db(tmp_path, monkeypatch):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "test.db"))
    session_factory = db.make_session_factory(engine)
    monkeypatch.setattr(db, "SessionLocal", session_factory)  # what the masthead's pending count reads

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


def _approval_panel(html):
    """The 'Awaiting approval' panel's markup, or '' when the page has none."""
    if 'id="pending"' not in html:
        return ""
    return html.split('id="pending"', 1)[1].split("My registered sources", 1)[0]


def test_the_dashboard_has_an_approval_panel_listing_pending_sources(client, _isolated_db):
    _register(_isolated_db)
    panel = _approval_panel(client.get("/provider/sources").text)
    assert "Awaiting approval" in client.get("/provider/sources").text
    assert "acme-open-data" in panel
    assert "/activate" in panel and "/reject" in panel  # the buttons moved with it


def test_there_is_no_approval_panel_when_nothing_is_pending(client, _isolated_db):
    source_id = _register(_isolated_db)
    client.post("/admin/sources/%s/activate" % source_id)
    text = client.get("/provider/sources").text
    assert 'id="pending"' not in text and "Awaiting approval" not in text


def test_only_pending_sources_are_in_the_approval_panel(client, _isolated_db):
    first = _register(_isolated_db)
    _register(_isolated_db, name="second-source")
    client.post("/admin/sources/%s/activate" % first)
    panel = _approval_panel(client.get("/provider/sources").text)
    assert "second-source" in panel and "acme-open-data" not in panel


def test_the_old_pending_page_redirects_to_the_dashboard_panel(client):
    resp = client.get("/admin/sources/pending", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/provider/sources#pending"


def test_the_old_pending_page_still_needs_the_login(_isolated_db):
    anonymous = TestClient(app, base_url="https://testserver")
    resp = anonymous.get("/admin/sources/pending", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login")


def test_there_is_no_pending_approval_tab_any_more(client, _isolated_db):
    _register(_isolated_db)
    nav_html = client.get("/provider/sources").text.split('<nav class="masthead-nav">', 1)[1].split("</nav>", 1)[0]
    assert "Pending approval" not in nav_html and 'href="/admin/sources/pending"' not in nav_html
    assert "Admin dashboard" not in nav_html and 'href="/admin/sources"' not in nav_html  # no tab of its own: it is on the Extract Box


def _nav(html):
    return html.split('<nav class="masthead-nav">', 1)[1].split("</nav>", 1)[0]


def test_the_dashboard_tab_shows_how_many_sources_await_approval(client, _isolated_db):
    _register(_isolated_db)
    _register(_isolated_db, name="second-source")
    _register(_isolated_db, name="third-source")
    nav_html = _nav(client.get("/provider/sources").text)
    assert '<span class="nav-count"' in nav_html and ">3</span>" in nav_html
    assert "3 sources awaiting approval" in nav_html


def test_the_count_is_singular_for_one_and_absent_for_none(client, _isolated_db):
    source_id = _register(_isolated_db)
    assert "1 source awaiting approval" in _nav(client.get("/provider/sources").text)
    client.post("/admin/sources/%s/activate" % source_id)
    assert "nav-count" not in _nav(client.get("/provider/sources").text)


def test_the_count_appears_on_every_page_not_just_the_dashboard(client, _isolated_db):
    _register(_isolated_db)
    for path in ("/provider/sources", "/admin/ftp", "/admin/security/failed-logins"):
        assert ">1</span>" in _nav(client.get(path).text), path


def test_rejecting_a_source_lowers_the_count(client, _isolated_db):
    source_id = _register(_isolated_db)
    _register(_isolated_db, name="second-source")
    client.post("/admin/sources/%s/reject" % source_id, data={"reason": "no"})
    assert "1 source awaiting approval" in _nav(client.get("/provider/sources").text)


def test_the_login_page_does_not_show_the_queue_count(_isolated_db):
    _register(_isolated_db)
    anonymous = TestClient(app, base_url="https://testserver")
    assert "nav-count" not in _nav(anonymous.get("/admin/login").text)


def test_a_failing_count_costs_the_badge_not_the_page(client, monkeypatch, _isolated_db):
    from providerharvest_service import nav

    class Broken:
        def __enter__(self):
            raise RuntimeError("database is locked")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(db, "SessionLocal", lambda: Broken())
    assert nav.pending_approvals() == 0
    assert client.get("/provider/sources").status_code == 200


def test_activate_moves_source_out_of_pending_queue(client, _isolated_db):
    source_id = _register(_isolated_db)

    resp = client.post("/admin/sources/%s/activate" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/provider/sources?")  # back to the dashboard

    assert 'id="pending"' not in client.get("/provider/sources").text

    all_sources = client.get("/provider/sources")
    assert "active" in all_sources.text


def test_reject_records_reason_and_shows_it_on_the_dashboard(client, _isolated_db):
    source_id = _register(_isolated_db)

    resp = client.post(
        "/admin/sources/%s/reject" % source_id,
        data={"reason": "endpoint unreachable"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    all_sources = client.get("/provider/sources")
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
    assert resp.headers["location"].startswith("/provider/sources")
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

    all_sources = client.get("/provider/sources")
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

    all_sources = client.get("/provider/sources")
    assert "unreachable" in all_sources.text


def test_admin_can_delete_a_source(client, _isolated_db):
    source_id = _register(_isolated_db)
    resp = client.post("/admin/sources/%s/delete" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash=Source+deleted" in resp.headers["location"]

    all_sources = client.get("/provider/sources")
    assert "acme-open-data" not in all_sources.text


def test_admin_edit_link_points_to_shared_edit_route(client, _isolated_db):
    _register(_isolated_db)
    resp = client.get("/provider/sources")
    assert "/provider/sources/" in resp.text
    assert "/edit" in resp.text


def test_admin_lists_badge_pull_and_push_sources(client, _isolated_db):
    _register(_isolated_db)  # http, pull
    with _isolated_db() as session:
        provider_sources.register(session, {"name": "acme-push", "title": "Push", "transport_type": "push"})

    text = client.get("/provider/sources").text
    assert '<span class="badge badge-pull"' in text
    assert '<span class="badge badge-push"' in text
    assert "<span>https</span>" in text
    # ...and in the approval panel too, where pending sources are also listed.
    panel = _approval_panel(text)
    assert '<span class="badge badge-pull"' in panel and '<span class="badge badge-push"' in panel
