import base64
import datetime
import os
import re

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


def _db_account_id(session_factory):
    """A provider's own account has no controls on the FTP box page any more (they are on its
    source's Credentials page), so its id cannot be scraped from that page."""
    with session_factory() as session:
        return session.scalar(select(FtpAccount.id))


def test_rotate_password_shows_new_password_once(client, _isolated_db):
    _register_ftp_push_source(_isolated_db)
    account_id = _db_account_id(_isolated_db)

    resp = client.post("/admin/ftp/%s/rotate-password" % account_id)
    assert resp.status_code == 200
    assert "password-field" in resp.text
    assert "rotated" in resp.text
    assert "Back to the source&#39;s credentials" in resp.text  # a provider's account returns to its source


def test_disable_then_enable_account(client, _isolated_db):
    _register_ftp_push_source(_isolated_db, activate=True)
    account_id = _db_account_id(_isolated_db)

    resp = client.post("/admin/ftp/%s/disable" % account_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "<span>disabled</span>" in client.get("/admin/ftp").text

    resp = client.post("/admin/ftp/%s/enable" % account_id, follow_redirects=False)
    assert resp.status_code == 303
    text = client.get("/admin/ftp").text
    assert "<span>enabled</span>" in text and "<span>disabled</span>" not in text


def test_delete_account(client, _isolated_db):
    _register_ftp_push_source(_isolated_db)
    account_id = _db_account_id(_isolated_db)

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


# -- setting a defined password / rotating ------------------------------------------------

def _hash_of(session_factory, username="legacy-account"):
    with session_factory() as session:
        return session.scalar(select(FtpAccount).where(FtpAccount.username == username)).password_hash


def _set_password(client, account_id, password, confirm=None):
    return client.post(
        "/admin/ftp/%s/set-password" % account_id,
        data={"password": password, "password_confirm": password if confirm is None else confirm},
        follow_redirects=False,
    )


def test_the_account_menu_offers_set_rotate_disable_and_delete_and_rotate_asks_first(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    text = client.get("/admin/ftp").text
    account_id = _account_id(client)
    assert 'href="/admin/ftp/%s/set-password"' % account_id in text and "Set a password" in text
    rotate = text.split('action="/admin/ftp/%s/rotate-password"' % account_id, 1)[1].split("</form>", 1)[0]
    assert "return confirm(" in rotate and "stops working at once" in rotate  # one click must not break a provider silently
    assert text.count('class="actions-menu"') >= 1 and "Disable" in text


def test_a_providers_account_is_read_only_here_with_a_link_to_its_source(client, _isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, activate=True)
    text = client.get("/admin/ftp").text
    assert 'href="/provider/sources/%s/credentials"' % source_id in text and "Manage on the source" in text
    for control in ("set-password", "rotate-password", "/disable", "/delete"):
        assert control not in text  # one home: all of these are on the source's Credentials page
    assert "<span>enabled</span>" in text  # its status is still shown


def test_an_admin_made_account_keeps_all_its_controls_on_this_page(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    text = client.get("/admin/ftp").text
    for control in ("set-password", "rotate-password", "/disable", "/delete"):
        assert control in text
    assert "Manage on the source" not in text


def test_setting_a_password_on_a_providers_account_returns_to_its_credentials_page(client, _isolated_db):
    source_id = _register_ftp_push_source(_isolated_db)
    account_id = _db_account_id(_isolated_db)
    page = client.get("/admin/ftp/%s/set-password" % account_id).text
    assert 'href="/provider/sources/%s/credentials">Cancel' % source_id in page
    resp = _set_password(client, account_id, "the password the provider gave me")
    assert resp.headers["location"].startswith("/provider/sources/%s/credentials?" % source_id)
    assert "flash_kind=success" in resp.headers["location"]
    bad = _set_password(client, account_id, "short")
    assert bad.status_code == 400 and 'href="/provider/sources/%s/credentials">Cancel' % source_id in bad.text


def test_an_admin_made_accounts_set_password_returns_to_the_ftp_page(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    resp = _set_password(client, _account_id(client), "the password the provider gave me")
    assert resp.headers["location"].startswith("/admin/ftp?")


def test_actions_taken_from_a_credentials_page_return_there_and_a_forged_referer_is_ignored(client, _isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, activate=True)
    account_id = _db_account_id(_isolated_db)
    here = "https://loader.data.gov.gr/provider/sources/%s/credentials" % source_id
    assert client.post("/admin/ftp/%s/disable" % account_id, headers={"referer": here}, follow_redirects=False).headers["location"].startswith(
        "/provider/sources/%s/credentials?" % source_id)
    for forged in ("https://evil.example/provider/sources/x/credentials/../../steal", "https://evil.example/steal",
                   "https://loader.data.gov.gr/provider/sources/a/b/credentials", "https://loader.data.gov.gr/provider/sources//credentials"):
        location = client.post("/admin/ftp/%s/enable" % account_id, headers={"referer": forged}, follow_redirects=False).headers["location"]
        assert location.startswith("/admin/ftp?") and "evil" not in location, forged


def test_the_set_password_page_explains_it_cannot_show_the_old_one(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    page = client.get("/admin/ftp/%s/set-password" % _account_id(client)).text
    assert 'name="password"' in page and 'name="password_confirm"' in page and 'type="password"' in page
    assert "can never be shown again" in page and "replaces the current" in page


def test_setting_a_password_stores_a_proftpd_compatible_hash_of_exactly_that_password(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    before = _hash_of(_isolated_db)
    resp = _set_password(client, _account_id(client), "the password the provider gave me")
    assert resp.status_code == 303 and "flash_kind=error" not in resp.headers["location"]
    stored = _hash_of(_isolated_db)
    assert stored != before and stored.startswith("$2a$")  # the prefix ProFTPD needs
    assert bcrypt.checkpw(b"the password the provider gave me", stored.encode())
    assert not bcrypt.checkpw(b"something else entirely", stored.encode())


def test_the_typed_password_never_appears_in_a_redirect_or_a_page(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    secret = "very-secret-typed-value-123"
    resp = _set_password(client, _account_id(client), secret)
    assert secret not in resp.headers["location"] and secret not in resp.text
    assert secret not in client.get(resp.headers["location"]).text
    bad = _set_password(client, _account_id(client), secret, confirm="does-not-match-at-all")  # an error page re-renders the form
    assert bad.status_code == 400 and secret not in bad.text and "does-not-match-at-all" not in bad.text


@pytest.mark.parametrize("password, confirm, message", [
    ("a-good-password-1", "a-good-password-2", "do not match"),
    ("tiny", None, "at least 6"),
    ("x" * 73, None, "at most 72"),
    ("é" * 40, None, "at most 72"),  # 40 characters but 80 bytes
    ("fine-password-with-\x07-bell", None, "control characters"),
])
def test_a_password_that_cannot_be_used_is_refused_and_the_old_one_stays(client, _isolated_db, password, confirm, message):
    _add_admin_made_account(_isolated_db)
    before = _hash_of(_isolated_db)
    resp = _set_password(client, _account_id(client), password, confirm)
    assert resp.status_code == 400 and message in resp.text
    assert _hash_of(_isolated_db) == before


def test_a_password_of_exactly_the_limits_is_accepted(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    assert _set_password(client, _account_id(client), "x" * 12).status_code == 303
    assert _set_password(client, _account_id(client), "y" * 72).status_code == 303
    assert bcrypt.checkpw(b"y" * 72, _hash_of(_isolated_db).encode())
    assert _set_password(client, _account_id(client), "é" * 36).status_code == 303  # 72 bytes


def test_set_password_on_an_unknown_account_or_without_the_login_is_refused(client, _isolated_db):
    assert "flash_kind=error" in client.get("/admin/ftp/nope/set-password", follow_redirects=False).headers["location"]
    assert "flash_kind=error" in _set_password(client, "nope", "a-good-password-1").headers["location"]
    _add_admin_made_account(_isolated_db)
    anonymous = TestClient(app, base_url="https://testserver")
    for resp in (anonymous.get("/admin/ftp/x/set-password", follow_redirects=False),
                 anonymous.post("/admin/ftp/x/set-password", data={"password": "a-good-password-1"}, follow_redirects=False)):
        assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login")


def test_the_box_gets_the_new_hash_from_the_sync_endpoint(client, _isolated_db, monkeypatch):
    _add_admin_made_account(_isolated_db)
    _set_password(client, _account_id(client), "the password the provider gave me")
    monkeypatch.setenv("LOADER_FTP_SYNC_KEY", "box-key")
    synced = client.get("/admin/ftp-accounts-sync", headers={"X-Ftp-Sync-Key": "box-key"}).json()
    assert bcrypt.checkpw(b"the password the provider gave me", synced[0]["password_hash"].encode())


def test_rotating_still_works_and_shows_the_new_password_once(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    before = _hash_of(_isolated_db)
    resp = client.post("/admin/ftp/%s/rotate-password" % _account_id(client))
    assert resp.status_code == 200 and "shown once" in resp.text
    shown = re.search(r'id="password-field" value="([^"]+)"', resp.text).group(1)
    assert _hash_of(_isolated_db) != before and bcrypt.checkpw(shown.encode(), _hash_of(_isolated_db).encode())


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


def test_junk_and_ipv4_in_ipv6_clothing_are_refused_with_a_message(client, _isolated_db):
    _add_admin_made_account(_isolated_db)
    account_id = _account_id(client)
    for junk in ("", "not-an-ip", "300.1.1.1", "203.0.113.0/99", "::ffff:1.2.3.4", "1.2.3.4; reboot", "any"):
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
