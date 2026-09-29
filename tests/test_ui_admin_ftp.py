import datetime

import bcrypt
import pytest
from fastapi.testclient import TestClient

from providerharvest_service import db
from providerharvest_service.app import app
from providerharvest_service.models import FtpBoxStatus

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _upload_root(monkeypatch):
    monkeypatch.setenv("LOADER_FTP_UPLOAD_ROOT", "/srv/ftp-uploads")


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
    # extract the id from a rotate-password form action in the rendered HTML
    marker = "/admin/ftp/"
    start = overview.text.index(marker, overview.text.index("acme-provider")) + len(marker)
    end = overview.text.index("/rotate-password", start)
    return overview.text[start:end]


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
