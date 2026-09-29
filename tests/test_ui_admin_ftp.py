import base64
import datetime
import os

import bcrypt
import pytest
from fastapi.testclient import TestClient

from providerharvest_service import db, provider_sources
from providerharvest_service.app import app
from providerharvest_service.models import FtpBoxStatus

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _upload_root(monkeypatch):
    monkeypatch.setenv("LOADER_FTP_UPLOAD_ROOT", "/srv/ftp-uploads")


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
    c.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/admin/ftp"})
    return c


def _create_form(**overrides):
    form = {
        "username": "acme-provider",
        "home_directory": "/srv/ftp-uploads/acme-provider",
        "protocol": "ftps",
    }
    form.update(overrides)
    return form


def test_overview_lists_no_accounts_or_boxes_initially(client):
    resp = client.get("/admin/ftp")
    assert resp.status_code == 200
    assert "No FTP accounts configured yet." in resp.text
    assert "No box has reported in yet." in resp.text


def test_create_account_shows_the_password_exactly_once(client):
    resp = client.post("/admin/ftp/accounts", data=_create_form())
    assert resp.status_code == 200
    assert "acme-provider" in resp.text
    assert "password-field" in resp.text

    overview = client.get("/admin/ftp")
    assert "acme-provider" in overview.text
    assert "enabled" in overview.text


def test_create_account_rejects_duplicate_username(client):
    client.post("/admin/ftp/accounts", data=_create_form())
    resp = client.post("/admin/ftp/accounts", data=_create_form(), follow_redirects=False)
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]


def test_create_account_rejects_invalid_username(client):
    resp = client.post(
        "/admin/ftp/accounts", data=_create_form(username="a"), follow_redirects=False
    )
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]


def _created_account_id(client):
    client.post("/admin/ftp/accounts", data=_create_form())
    overview = client.get("/admin/ftp")
    return _extract_account_id(overview.text)


def _extract_account_id(html: str) -> str:
    # The rotate-password form's action is always "/admin/ftp/{id}/rotate-password"
    # -- found by working backward from that fixed suffix so this stays
    # correct regardless of what other columns/forms sit earlier in the row.
    marker = "/admin/ftp/"
    end = html.index("/rotate-password")
    start = html.rindex(marker, 0, end) + len(marker)
    return html[start:end]


def _register_push_source(session_factory, name="acme-push", activate=True):
    with session_factory() as session:
        source = provider_sources.register(
            session, {"name": name, "title": name, "transport_type": "push", "frequency": "MANUAL"}
        )
        source_id = source.id
        if activate:
            provider_sources.activate(session, source_id)
        return source_id


def test_rotate_password_shows_new_password_once(client):
    account_id = _created_account_id(client)
    resp = client.post("/admin/ftp/%s/rotate-password" % account_id)
    assert resp.status_code == 200
    assert "password-field" in resp.text
    assert "rotated" in resp.text


def test_disable_then_enable_account(client):
    account_id = _created_account_id(client)

    resp = client.post("/admin/ftp/%s/disable" % account_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "disabled" in client.get("/admin/ftp").text

    resp = client.post("/admin/ftp/%s/enable" % account_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "enabled" in client.get("/admin/ftp").text


def test_delete_account(client):
    account_id = _created_account_id(client)
    resp = client.post("/admin/ftp/%s/delete" % account_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "acme-provider" not in client.get("/admin/ftp").text


def test_action_on_unknown_account_shows_not_found(client):
    resp = client.post("/admin/ftp/does-not-exist/disable", follow_redirects=False)
    assert resp.status_code == 303
    followed = client.get(resp.headers["location"])
    assert "No such account." in followed.text


def test_create_form_lists_push_sources_and_can_set_a_forward_target(client, _isolated_db):
    _register_push_source(_isolated_db)
    resp = client.post("/admin/ftp/accounts", data=_create_form(push_target_source_name="acme-push"))
    assert resp.status_code == 200

    overview = client.get("/admin/ftp")
    assert "acme-push" in overview.text


def test_create_rejects_an_unknown_forward_target(client):
    resp = client.post(
        "/admin/ftp/accounts",
        data=_create_form(push_target_source_name="no-such-source"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]


def test_set_forward_target_updates_and_clears_it(client, _isolated_db):
    _register_push_source(_isolated_db)
    account_id = _created_account_id(client)

    resp = client.post(
        "/admin/ftp/%s/forward-target" % account_id,
        data={"push_target_source_name": "acme-push"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "flash_kind=error" not in resp.headers["location"]
    assert "acme-push" in client.get("/admin/ftp").text

    resp = client.post(
        "/admin/ftp/%s/forward-target" % account_id,
        data={"push_target_source_name": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "cleared" in resp.headers["location"]


def test_set_forward_target_rejects_an_unknown_source(client):
    account_id = _created_account_id(client)
    resp = client.post(
        "/admin/ftp/%s/forward-target" % account_id,
        data={"push_target_source_name": "no-such-source"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]


def test_overview_shows_box_status(client, _isolated_db):
    with _isolated_db() as session:
        session.add(
            FtpBoxStatus(
                hostname="snf-899358",
                reported_at=datetime.datetime.now(datetime.timezone.utc),
                proftpd_active=True,
                ftp_port_listening=True,
                sftp_port_listening=True,
                relay_service_active=False,
                account_count=2,
                sync_ok=True,
                sync_message=None,
            )
        )
        session.commit()

    resp = client.get("/admin/ftp")
    assert "snf-899358" in resp.text
    assert "down" in resp.text  # relay_service_active False


def test_overview_flags_a_stale_box(client, _isolated_db):
    with _isolated_db() as session:
        session.add(
            FtpBoxStatus(
                hostname="snf-899358",
                reported_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30),
                proftpd_active=True,
                ftp_port_listening=True,
                sftp_port_listening=True,
                relay_service_active=True,
                account_count=2,
                sync_ok=True,
                sync_message=None,
            )
        )
        session.commit()

    resp = client.get("/admin/ftp")
    assert "stale" in resp.text
