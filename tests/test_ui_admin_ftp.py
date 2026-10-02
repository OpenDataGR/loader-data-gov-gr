import base64
import datetime
import os

import bcrypt
import pytest
from fastapi.testclient import TestClient

from providerharvest_service import db, provider_sources
from providerharvest_service.app import app
from sqlalchemy import select

from providerharvest_service.models import FtpAccount, FtpAllowedIp, FtpBoxStatus

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


def test_the_login_bans_column_shows_whether_fail2ban_is_running(client, _isolated_db):
    _add_box(_isolated_db, fail2ban_active=False)
    text = client.get("/admin/ftp").text
    assert "<th>Login bans</th>" in text
    assert "fail2ban is not running" in text  # the "off" badge, with its explanation


def test_the_login_bans_column_says_unknown_for_a_box_that_does_not_report_it(client, _isolated_db):
    _add_box(_isolated_db, fail2ban_active=None)
    assert '<span class="muted">unknown</span>' in client.get("/admin/ftp").text


# -- allowed addresses (per-account login limit, enforced on the box) ----------------------

def _allowed(session_factory, username="legacy-account"):
    with session_factory() as session:
        account = session.scalar(select(FtpAccount).where(FtpAccount.username == username))
        return sorted(a.cidr for a in account.allowed_ips)


def _allow(client, account_id, cidr):
    return client.post("/admin/ftp/%s/allowed-ips" % account_id, data={"cidr": cidr}, follow_redirects=False)


def test_an_account_with_no_allowed_addresses_says_anywhere(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    text = client.get("/admin/ftp").text
    assert "<th>Allowed from</th>" in text
    assert '<span class="muted">anywhere</span>' in text


def test_allowing_an_address_stores_it_in_canonical_form_and_shows_it(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    account_id = _account_id(client)
    resp = _allow(client, account_id, "203.0.113.9")
    assert resp.status_code == 303 and "flash_kind=error" not in resp.headers["location"]
    _allow(client, account_id, " 198.51.100.7/24 ")  # a host address with a prefix: canonical network, whitespace trimmed
    assert _allowed(_isolated_db) == ["198.51.100.0/24", "203.0.113.9/32"]
    text = client.get("/admin/ftp").text
    assert "<code>203.0.113.9/32</code>" in text and '<span class="muted">anywhere</span>' not in text


def test_allowing_the_same_address_twice_does_not_duplicate_it(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    account_id = _account_id(client)
    _allow(client, account_id, "203.0.113.9")
    _allow(client, account_id, "203.0.113.9/32")
    assert _allowed(_isolated_db) == ["203.0.113.9/32"]


def test_junk_and_ipv6_are_refused_with_a_message(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    account_id = _account_id(client)
    for junk in ("", "not-an-ip", "300.1.1.1", "203.0.113.0/99", "2001:db8::/32", "1.2.3.4; reboot", "any"):
        resp = _allow(client, account_id, junk)
        assert resp.status_code == 303 and "flash_kind=error" in resp.headers["location"], junk
    assert _allowed(_isolated_db) == []


def test_removing_the_last_address_says_the_account_is_open_again(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    account_id = _account_id(client)
    _allow(client, account_id, "203.0.113.9")
    with _isolated_db() as session:
        ip_id = session.scalar(select(FtpAllowedIp.id))
    resp = client.post("/admin/ftp/%s/allowed-ips/%s/delete" % (account_id, ip_id), follow_redirects=False)
    assert resp.status_code == 303 and "flash_kind=error" not in resp.headers["location"]
    assert "anywhere" in resp.headers["location"]
    assert _allowed(_isolated_db) == []


def test_allowed_address_actions_on_unknown_things_are_refused(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    assert "flash_kind=error" in _allow(client, "no-such-account", "203.0.113.9").headers["location"]
    account_id = _account_id(client)
    resp = client.post("/admin/ftp/%s/allowed-ips/no-such-id/delete" % account_id, follow_redirects=False)
    assert "flash_kind=error" in resp.headers["location"]


def test_one_accounts_address_cannot_be_removed_through_another(client, _isolated_db):
    _add_admin_made_account(_isolated_db, "first")
    _add_admin_made_account(_isolated_db, "second")
    with _isolated_db() as session:
        first = session.scalar(select(FtpAccount).where(FtpAccount.username == "first"))
        second = session.scalar(select(FtpAccount).where(FtpAccount.username == "second"))
        session.add(FtpAllowedIp(ftp_account_id=first.id, cidr="203.0.113.9/32"))
        session.commit()
        first_ip, second_id = session.scalar(select(FtpAllowedIp.id)), second.id
    resp = client.post("/admin/ftp/%s/allowed-ips/%s/delete" % (second_id, first_ip), follow_redirects=False)
    assert "flash_kind=error" in resp.headers["location"]
    assert _allowed(_isolated_db, "first") == ["203.0.113.9/32"]


def test_allowed_address_actions_need_the_login(_isolated_db):
    _add_admin_made_account(_isolated_db)
    anonymous = TestClient(app, base_url="https://testserver")
    resp = anonymous.post("/admin/ftp/x/allowed-ips", data={"cidr": "203.0.113.9"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login")


def test_the_box_receives_what_the_page_set(client, _isolated_db, monkeypatch):
    _add_admin_made_account(_isolated_db)
    _allow(client, _account_id(client), "203.0.113.0/24")
    monkeypatch.setenv("LOADER_FTP_SYNC_KEY", "box-key")
    synced = client.get("/admin/ftp-accounts-sync", headers={"X-Ftp-Sync-Key": "box-key"}).json()
    assert synced[0]["allowed_ips"] == ["203.0.113.0/24"]
