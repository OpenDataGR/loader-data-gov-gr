import base64
import datetime
import os

import bcrypt
import pytest
from fastapi.testclient import TestClient

from providerharvest_service import db, provider_sources
from providerharvest_service.app import app
from providerharvest_service.models import FtpAccount, FtpBoxStatus

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _upload_root(monkeypatch):
    monkeypatch.setenv("LOADER_FTP_UPLOAD_ROOT", "/srv/ftp")


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


def _register_ftp_push_source(session_factory, name="acme-push", activate=False):
    """What a provider does: register a push source with FTP/SFTP, which
    creates their account (disabled until the source is approved)."""
    with session_factory() as session:
        source = provider_sources.register(
            session, {"name": name, "title": name, "transport_type": "push", "push_method": "ftp"}
        )
        source_id = source.id
        if activate:
            provider_sources.activate(session, source_id)
        return source_id


def _add_admin_made_account(session_factory, username="legacy-account"):
    with session_factory() as session:
        session.add(FtpAccount(
            username=username, password_hash="$2a$x", home_directory="/srv/ftp/%s" % username,
            protocol="ftp", enabled=True,
        ))
        session.commit()


def _extract_account_id(html: str) -> str:
    # The rotate-password form's action is always "/admin/ftp/{id}/rotate-password"
    # -- found by working backward from that fixed suffix so this stays
    # correct regardless of what other columns/forms sit earlier in the row.
    marker = "/admin/ftp/"
    end = html.index("/rotate-password")
    start = html.rindex(marker, 0, end) + len(marker)
    return html[start:end]


def _account_id(client):
    return _extract_account_id(client.get("/admin/ftp").text)


def test_overview_lists_no_accounts_or_boxes_initially(client):
    resp = client.get("/admin/ftp")
    assert resp.status_code == 200
    assert "No FTP accounts yet" in resp.text
    assert "No box has reported in yet." in resp.text


def test_there_is_no_manual_create_account_form(client):
    # Markup, not prose: the footer's changelog renders git commit messages,
    # which can legitimately mention "New FTP account".
    text = client.get("/admin/ftp").text
    assert "<legend>New FTP account</legend>" not in text
    assert 'action="/admin/ftp/accounts"' not in text
    resp = client.post(
        "/admin/ftp/accounts",
        data={"username": "acme", "home_directory": "/srv/ftp/acme", "protocol": "ftp"},
        follow_redirects=False,
    )
    assert resp.status_code in (404, 405)


def test_a_providers_account_shows_its_source_and_is_disabled_until_approval(client, _isolated_db):
    source_id = _register_ftp_push_source(_isolated_db)

    text = client.get("/admin/ftp").text
    assert "acme-push" in text
    assert "<span>disabled</span>" in text
    assert '<div class="muted">created by an admin' not in text  # it came from the provider's own registration

    with _isolated_db() as session:
        provider_sources.activate(session, source_id)
    assert "<span>enabled</span>" in client.get("/admin/ftp").text


def test_an_account_made_by_an_admin_is_labelled_as_such(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    text = client.get("/admin/ftp").text
    assert "legacy-account" in text
    assert '<div class="muted">created by an admin' in text


def test_the_forwarding_target_is_shown_read_only(client, _isolated_db):
    _register_ftp_push_source(_isolated_db)
    text = client.get("/admin/ftp").text
    assert '/forward-target"' not in text  # no dropdown that could break the source link
    resp = client.post("/admin/ftp/anything/forward-target", follow_redirects=False)
    assert resp.status_code in (404, 405)


def test_rotate_password_shows_new_password_once(client, _isolated_db):
    _register_ftp_push_source(_isolated_db)
    account_id = _account_id(client)

    resp = client.post("/admin/ftp/%s/rotate-password" % account_id)
    assert resp.status_code == 200
    assert "password-field" in resp.text
    assert "rotated" in resp.text


def test_disable_then_enable_account(client, _isolated_db):
    _register_ftp_push_source(_isolated_db, activate=True)
    account_id = _account_id(client)

    resp = client.post("/admin/ftp/%s/disable" % account_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "<span>disabled</span>" in client.get("/admin/ftp").text

    resp = client.post("/admin/ftp/%s/enable" % account_id, follow_redirects=False)
    assert resp.status_code == 303
    text = client.get("/admin/ftp").text
    assert "<span>enabled</span>" in text and "<span>disabled</span>" not in text


def test_delete_account(client, _isolated_db):
    _register_ftp_push_source(_isolated_db)
    account_id = _account_id(client)

    resp = client.post("/admin/ftp/%s/delete" % account_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "acme-push" not in client.get("/admin/ftp").text


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
    assert "<span>down</span>" in resp.text  # relay_service_active False
    assert "<span>stale</span>" not in resp.text  # reported just now


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
    assert "<span>stale</span>" in resp.text


# -- ban list and unban ---------------------------------------------------------------


def _add_box(session_factory, banned=(), fail2ban_active=True, hostname="snf-899358"):
    import json

    with session_factory() as session:
        session.add(FtpBoxStatus(
            hostname=hostname, reported_at=datetime.datetime.now(datetime.timezone.utc),
            proftpd_active=True, ftp_port_listening=True, sftp_port_listening=True,
            relay_service_active=True, account_count=0, sync_ok=True, sync_message=None,
            banned_ips=json.dumps(list(banned)), fail2ban_active=fail2ban_active,
        ))
        session.commit()


def _requests(session_factory):
    from sqlalchemy import select

    from providerharvest_service.models import FtpUnbanRequest

    with session_factory() as session:
        return sorted((r.hostname, r.ip) for r in session.scalars(select(FtpUnbanRequest)).all())


def test_the_page_lists_banned_addresses_with_an_unban_button(client, _isolated_db):
    _add_box(_isolated_db, banned=["169.58.53.194", "84.205.215.132"])
    text = client.get("/admin/ftp").text
    assert "<code>169.58.53.194</code>" in text and "<code>84.205.215.132</code>" in text
    assert text.count('action="/admin/ftp/boxes/snf-899358/unban"') == 2


def test_the_page_says_so_when_nobody_is_banned(client, _isolated_db):
    _add_box(_isolated_db, banned=[])
    assert "No addresses banned right now." in client.get("/admin/ftp").text


def test_the_login_bans_column_shows_whether_fail2ban_is_running(client, _isolated_db):
    _add_box(_isolated_db, fail2ban_active=False)
    text = client.get("/admin/ftp").text
    assert "<th>Login bans</th>" in text
    assert "fail2ban is not running" in text  # the "off" badge, with its explanation


def test_the_login_bans_column_says_unknown_for_a_box_that_does_not_report_it(client, _isolated_db):
    _add_box(_isolated_db, fail2ban_active=None)
    assert '<span class="muted">unknown</span>' in client.get("/admin/ftp").text


def test_unbanning_records_a_request_and_the_page_shows_it_waiting(client, _isolated_db):
    _add_box(_isolated_db, banned=["84.205.215.132"])

    resp = client.post("/admin/ftp/boxes/snf-899358/unban", data={"ip": "84.205.215.132"}, follow_redirects=False)
    assert resp.status_code == 303 and "flash_kind=error" not in resp.headers["location"]
    assert _requests(_isolated_db) == [("snf-899358", "84.205.215.132")]

    text = client.get("/admin/ftp").text
    assert "unban requested" in text
    assert text.count('action="/admin/ftp/boxes/snf-899358/unban"') == 0  # no second button for it


def test_asking_twice_does_not_queue_it_twice(client, _isolated_db):
    _add_box(_isolated_db, banned=["84.205.215.132"])
    for _ in range(2):
        client.post("/admin/ftp/boxes/snf-899358/unban", data={"ip": "84.205.215.132"})
    assert len(_requests(_isolated_db)) == 1


def test_only_an_address_that_is_actually_banned_can_be_unbanned(client, _isolated_db):
    _add_box(_isolated_db, banned=["84.205.215.132"])
    for ip in ("203.0.113.9", "not-an-ip", "", "84.205.215.132; reboot"):
        resp = client.post("/admin/ftp/boxes/snf-899358/unban", data={"ip": ip}, follow_redirects=False)
        assert resp.status_code == 303 and "flash_kind=error" in resp.headers["location"], ip
    assert _requests(_isolated_db) == []


def test_unbanning_on_an_unknown_box_is_refused(client):
    resp = client.post("/admin/ftp/boxes/no-such-box/unban", data={"ip": "84.205.215.132"}, follow_redirects=False)
    assert resp.status_code == 303 and "flash_kind=error" in resp.headers["location"]


def test_the_unban_needs_the_login(_isolated_db):
    _add_box(_isolated_db, banned=["84.205.215.132"])
    anonymous = TestClient(app, base_url="https://testserver")
    resp = anonymous.post("/admin/ftp/boxes/snf-899358/unban", data={"ip": "84.205.215.132"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login")
    assert _requests(_isolated_db) == []


def test_a_request_travels_from_the_page_to_the_box_and_back(client, _isolated_db, monkeypatch):
    # The whole loop: an admin clicks Unban, the box's next heartbeat reply carries it,
    # and the heartbeat after the box has acted closes it.
    monkeypatch.setenv("LOADER_FTP_SYNC_KEY", "box-key")
    headers = {"X-Ftp-Sync-Key": "box-key"}
    payload = {
        "hostname": "snf-899358", "proftpd_active": True, "ftp_port_listening": True,
        "sftp_port_listening": True, "relay_service_active": True, "account_count": 0,
        "sync_ok": True, "sync_message": "ok", "fail2ban_active": True,
    }

    def beat(banned):
        return client.put("/admin/ftp-box-status", json=dict(payload, banned_ips=banned), headers=headers).json()

    beat(["84.205.215.132"])
    assert "84.205.215.132" in client.get("/admin/ftp").text

    client.post("/admin/ftp/boxes/snf-899358/unban", data={"ip": "84.205.215.132"})
    assert beat(["84.205.215.132"])["unban"] == ["84.205.215.132"]  # the box is told to act

    assert beat([])["unban"] == []  # it did
    assert _requests(_isolated_db) == []
    text = client.get("/admin/ftp").text
    assert "<code>84.205.215.132</code>" not in text and "No addresses banned right now." in text
