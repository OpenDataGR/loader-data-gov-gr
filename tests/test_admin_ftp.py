import base64
import os

import pytest
from fastapi.testclient import TestClient

from providerharvest_service import admin_auth, db, provider_sources
from providerharvest_service.app import app

ADMIN_KEY = "test-admin-key"
AUTH_HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture(autouse=True)
def _admin_key(monkeypatch):
    monkeypatch.setenv("LOADER_ADMIN_API_KEY", ADMIN_KEY)


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    # Only needed by the tests below that register a real push-mode
    # ProviderSource to use as a forwarding target -- envelope
    # encryption still touches every source's (even a push source's
    # empty) credential bundle.
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


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


@pytest.fixture(autouse=True)
def _upload_root(monkeypatch):
    monkeypatch.setenv("LOADER_FTP_UPLOAD_ROOT", "/srv/ftp-uploads")


@pytest.fixture
def client():
    return TestClient(app)


def _create(client, **overrides):
    payload = {
        "username": "acme-provider",
        "home_directory": "/srv/ftp-uploads/acme-provider",
        "protocol": "ftps",
    }
    payload.update(overrides)
    return client.post("/admin/ftp-accounts", json=payload, headers=AUTH_HEADERS)


def test_missing_admin_key_is_unauthorized(client):
    resp = client.get("/admin/ftp-accounts")
    assert resp.status_code == 401


def test_admin_key_not_configured_is_unavailable(client, monkeypatch):
    monkeypatch.delenv("LOADER_ADMIN_API_KEY", raising=False)
    resp = client.get("/admin/ftp-accounts", headers=AUTH_HEADERS)
    assert resp.status_code == 503


def test_stored_password_hash_uses_2a_prefix(client, _isolated_db):
    """Confirmed for real against a live ProFTPD 1.3.8b + mod_sql_passwd
    instance, not a style preference: a $2b$ hash (bcrypt's own
    default) is silently rejected there ("530 Login incorrect", no
    error logged), while the identical password re-hashed as $2a$
    authenticates and uploads successfully. See admin_ftp.hash_password.
    """
    from providerharvest_service.models import FtpAccount

    resp = _create(client)
    assert resp.status_code == 201, resp.text

    session = _isolated_db()
    try:
        account = session.query(FtpAccount).filter_by(username="acme-provider").one()
        assert account.password_hash.startswith("$2a$")
    finally:
        session.close()


def test_create_account_returns_password_once(client):
    resp = _create(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["username"] == "acme-provider"
    assert body["protocol"] == "ftps"
    assert body["enabled"] is True
    assert body["allowed_ips"] == []
    assert "password" in body and len(body["password"]) > 16

    # The password is never returned again -- get/list don't carry it.
    get_resp = client.get("/admin/ftp-accounts/%s" % body["id"], headers=AUTH_HEADERS)
    assert "password" not in get_resp.json()


def test_duplicate_username_is_conflict(client):
    _create(client)
    resp = _create(client)
    assert resp.status_code == 409


def test_home_directory_must_be_under_upload_root(client):
    resp = _create(client, home_directory="/etc/passwd")
    assert resp.status_code == 422

    resp = _create(client, home_directory="/srv/ftp-uploads-evil/x")
    assert resp.status_code == 422

    resp = _create(client, home_directory="relative/path")
    assert resp.status_code == 422


def test_username_must_match_allowed_characters(client):
    resp = _create(client, username="a b/../c")
    assert resp.status_code == 422


def test_list_accounts(client):
    _create(client)
    _create(client, username="second-provider", home_directory="/srv/ftp-uploads/second")
    resp = client.get("/admin/ftp-accounts", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_missing_account_is_404(client):
    resp = client.get("/admin/ftp-accounts/does-not-exist", headers=AUTH_HEADERS)
    assert resp.status_code == 404


def test_update_enabled_and_protocol(client):
    account_id = _create(client).json()["id"]
    resp = client.patch(
        "/admin/ftp-accounts/%s" % account_id,
        json={"enabled": False, "protocol": "sftp"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["protocol"] == "sftp"


def test_rotate_password_returns_a_new_password(client):
    created = _create(client).json()
    resp = client.post(
        "/admin/ftp-accounts/%s/rotate-password" % created["id"], headers=AUTH_HEADERS
    )
    assert resp.status_code == 200
    rotated_password = resp.json()["password"]
    assert rotated_password != created["password"]


def test_delete_account(client):
    account_id = _create(client).json()["id"]
    resp = client.delete("/admin/ftp-accounts/%s" % account_id, headers=AUTH_HEADERS)
    assert resp.status_code == 204
    assert client.get(
        "/admin/ftp-accounts/%s" % account_id, headers=AUTH_HEADERS
    ).status_code == 404


def test_allowed_ips_crud_and_validation(client):
    account_id = _create(client).json()["id"]

    bad = client.post(
        "/admin/ftp-accounts/%s/allowed-ips" % account_id,
        json={"cidr": "not-an-ip"},
        headers=AUTH_HEADERS,
    )
    assert bad.status_code == 422

    # IPv6 is refused: the box enforces the list with IPv4 ProFTPD rules.
    v6 = client.post(
        "/admin/ftp-accounts/%s/allowed-ips" % account_id, json={"cidr": "2001:db8::/32"}, headers=AUTH_HEADERS
    )
    assert v6.status_code == 422

    added = client.post(
        "/admin/ftp-accounts/%s/allowed-ips" % account_id,
        json={"cidr": "203.0.113.0/24"},
        headers=AUTH_HEADERS,
    )
    assert added.status_code == 201

    # Adding the same one again returns it instead of duplicating it.
    again = client.post(
        "/admin/ftp-accounts/%s/allowed-ips" % account_id, json={"cidr": "203.0.113.0/24"}, headers=AUTH_HEADERS
    )
    assert again.json()["id"] == added.json()["id"]
    ip_id = added.json()["id"]

    fetched = client.get("/admin/ftp-accounts/%s" % account_id, headers=AUTH_HEADERS)
    assert fetched.json()["allowed_ips"] == [{"id": ip_id, "cidr": "203.0.113.0/24"}]

    removed = client.delete(
        "/admin/ftp-accounts/%s/allowed-ips/%s" % (account_id, ip_id), headers=AUTH_HEADERS
    )
    assert removed.status_code == 204

    fetched_again = client.get("/admin/ftp-accounts/%s" % account_id, headers=AUTH_HEADERS)
    assert fetched_again.json()["allowed_ips"] == []


def test_allowed_ip_on_missing_account_is_404(client):
    resp = client.post(
        "/admin/ftp-accounts/does-not-exist/allowed-ips",
        json={"cidr": "10.0.0.1/32"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404


def test_deleting_account_cascades_allowed_ips(client):
    account_id = _create(client).json()["id"]
    client.post(
        "/admin/ftp-accounts/%s/allowed-ips" % account_id,
        json={"cidr": "10.0.0.1/32"},
        headers=AUTH_HEADERS,
    )
    client.delete("/admin/ftp-accounts/%s" % account_id, headers=AUTH_HEADERS)

    # Re-creating the same account name must work -- nothing orphaned.
    resp = _create(client)
    assert resp.status_code == 201


# -- forwarding an account's uploads to a push-mode source -----------------


def _register_push_source(session_factory, name="acme-push"):
    with session_factory() as session:
        source = provider_sources.register(
            session, {"name": name, "title": name, "transport_type": "push", "frequency": "MANUAL"}
        )
        return source.id


def test_create_rejects_an_unknown_forward_target(client):
    resp = _create(client, push_target_source_name="no-such-source")
    assert resp.status_code == 400


def test_create_rejects_a_non_push_source_as_forward_target(client, _isolated_db):
    with _isolated_db() as session:
        provider_sources.register(session, {
            "name": "acme-http", "title": "t", "endpoint_url": "https://example.gr/api",
            "transport_type": "http", "auth_type": "api_key", "api_key": "k",
        })
    resp = _create(client, push_target_source_name="acme-http")
    assert resp.status_code == 400


def test_create_accepts_a_valid_push_source_as_forward_target(client, _isolated_db):
    _register_push_source(_isolated_db)
    resp = _create(client, push_target_source_name="acme-push")
    assert resp.status_code == 201
    assert resp.json()["push_target_source_name"] == "acme-push"


def test_forward_target_defaults_to_none(client):
    resp = _create(client)
    assert resp.json()["push_target_source_name"] is None


def test_set_forward_target_updates_an_existing_account(client, _isolated_db):
    _register_push_source(_isolated_db)
    account_id = _create(client).json()["id"]

    resp = client.patch(
        "/admin/ftp-accounts/%s/forward-target" % account_id,
        json={"push_target_source_name": "acme-push"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["push_target_source_name"] == "acme-push"


def test_set_forward_target_can_clear_it(client, _isolated_db):
    _register_push_source(_isolated_db)
    account_id = _create(client, push_target_source_name="acme-push").json()["id"]

    resp = client.patch(
        "/admin/ftp-accounts/%s/forward-target" % account_id,
        json={"push_target_source_name": None},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["push_target_source_name"] is None


def test_set_forward_target_rejects_an_unknown_source(client):
    account_id = _create(client).json()["id"]
    resp = client.patch(
        "/admin/ftp-accounts/%s/forward-target" % account_id,
        json={"push_target_source_name": "no-such-source"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400


def test_set_forward_target_404s_for_an_unknown_account(client):
    resp = client.patch(
        "/admin/ftp-accounts/does-not-exist/forward-target",
        json={"push_target_source_name": None},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404


# -- the remote-box sync endpoint, gated by its own separate key ----------


SYNC_KEY = "test-ftp-sync-key"
SYNC_HEADERS = {"X-Ftp-Sync-Key": SYNC_KEY}


@pytest.fixture(autouse=True)
def _sync_key(monkeypatch):
    monkeypatch.setenv("LOADER_FTP_SYNC_KEY", SYNC_KEY)


def test_sync_rejects_the_admin_key_instead_of_the_sync_key(client):
    # Proves these are genuinely two separate credentials, not one
    # accepted under two header names.
    resp = client.get("/admin/ftp-accounts-sync", headers=AUTH_HEADERS)
    assert resp.status_code == 401


def test_sync_missing_key_is_unauthorized(client):
    resp = client.get("/admin/ftp-accounts-sync")
    assert resp.status_code == 401


def test_sync_key_not_configured_is_unavailable(client, monkeypatch):
    monkeypatch.delenv("LOADER_FTP_SYNC_KEY", raising=False)
    resp = client.get("/admin/ftp-accounts-sync", headers=SYNC_HEADERS)
    assert resp.status_code == 503


def test_sync_returns_the_password_hash_not_the_plaintext(client):
    created = _create(client).json()
    resp = client.get("/admin/ftp-accounts-sync", headers=SYNC_HEADERS)
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["username"] == created["username"]
    assert entry["home_directory"] == created["home_directory"]
    assert entry["protocol"] == created["protocol"]
    assert "id" not in entry  # not needed by a remote box's own account store
    assert "password_hash" in entry and entry["password_hash"] != ""
    assert "password" not in entry  # the plaintext must never appear here either


def test_sync_excludes_disabled_accounts(client):
    account_id = _create(client).json()["id"]
    client.patch("/admin/ftp-accounts/%s" % account_id, json={"enabled": False}, headers=AUTH_HEADERS)
    resp = client.get("/admin/ftp-accounts-sync", headers=SYNC_HEADERS)
    assert resp.json() == []


def test_sync_includes_allowed_ips_as_plain_cidr_strings(client):
    account_id = _create(client).json()["id"]
    client.post(
        "/admin/ftp-accounts/%s/allowed-ips" % account_id,
        json={"cidr": "10.0.0.1/32"},
        headers=AUTH_HEADERS,
    )
    resp = client.get("/admin/ftp-accounts-sync", headers=SYNC_HEADERS)
    assert resp.json()[0]["allowed_ips"] == ["10.0.0.1/32"]


# -- the remote-box heartbeat endpoint, same sync key ----------------------


def _status_payload(**overrides):
    payload = {
        "hostname": "snf-899358",
        "proftpd_active": True,
        "ftp_port_listening": True,
        "sftp_port_listening": True,
        "relay_service_active": True,
        "account_count": 3,
        "sync_ok": True,
        "sync_message": None,
    }
    payload.update(overrides)
    return payload


def test_box_status_requires_the_sync_key_not_the_admin_key(client):
    resp = client.put("/admin/ftp-box-status", json=_status_payload(), headers=AUTH_HEADERS)
    assert resp.status_code == 401


def test_box_status_report_round_trips(client):
    resp = client.put("/admin/ftp-box-status", json=_status_payload(), headers=SYNC_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["hostname"] == "snf-899358"
    assert body["account_count"] == 3
    assert body["sync_ok"] is True
    assert "reported_at" in body


def test_box_status_report_upserts_by_hostname(client):
    client.put("/admin/ftp-box-status", json=_status_payload(account_count=3), headers=SYNC_HEADERS)
    resp = client.put(
        "/admin/ftp-box-status",
        json=_status_payload(account_count=5, proftpd_active=False, sync_ok=False, sync_message="boom"),
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["account_count"] == 5
    assert body["proftpd_active"] is False
    assert body["sync_message"] == "boom"

    # Still one row per hostname, not a growing history.
    again = client.put("/admin/ftp-box-status", json=_status_payload(hostname="other-box"), headers=SYNC_HEADERS)
    assert again.status_code == 200


# -- provider addresses reported by the box -----------------------------------


def _forwarding_account(client, session_factory, username="acme-provider"):
    _register_push_source(session_factory)
    _create(client, username=username, home_directory="/srv/ftp-uploads/%s" % username,
            push_target_source_name="acme-push")


def _last_sender(session_factory):
    with session_factory() as session:
        return provider_sources.list_all(session)[0].last_sender


def test_the_heartbeat_records_where_a_provider_connected_from(client, _isolated_db):
    _forwarding_account(client, _isolated_db)

    resp = client.put(
        "/admin/ftp-box-status",
        json=dict(_status_payload(), clients=[{"username": "acme-provider", "ip": "203.0.113.9"}]),
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 200
    assert _last_sender(_isolated_db) == "203.0.113.9"
    assert "clients" not in resp.json()  # a report field, not part of the stored status


def test_the_heartbeat_ignores_bad_or_unknown_clients_without_failing(client, _isolated_db):
    _forwarding_account(client, _isolated_db)

    resp = client.put(
        "/admin/ftp-box-status",
        json=dict(_status_payload(), clients=[
            {"username": "acme-provider", "ip": "not-an-address"},
            {"username": "nobody", "ip": "203.0.113.9"},
        ]),
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 200
    assert _last_sender(_isolated_db) is None


def test_the_heartbeat_ignores_an_account_that_forwards_nowhere(client, _isolated_db):
    _register_push_source(_isolated_db)
    _create(client, username="loose-account", home_directory="/srv/ftp-uploads/loose-account")

    resp = client.put(
        "/admin/ftp-box-status",
        json=dict(_status_payload(), clients=[{"username": "loose-account", "ip": "203.0.113.9"}]),
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 200
    assert _last_sender(_isolated_db) is None


def test_a_heartbeat_without_clients_still_works(client):
    assert client.put("/admin/ftp-box-status", json=_status_payload(), headers=SYNC_HEADERS).status_code == 200


# -- fail2ban ban list and unban requests -----------------------------------------


def _heartbeat(client, **overrides):
    resp = client.put("/admin/ftp-box-status", json=dict(_status_payload(), **overrides), headers=SYNC_HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _box(session_factory):
    from providerharvest_service.models import FtpBoxStatus

    with session_factory() as session:
        row = session.get(FtpBoxStatus, "snf-899358")
        return row.banned, row.fail2ban_active


def _add_unban_request(session_factory, ip, hostname="snf-899358", age_minutes=0):
    import datetime

    from providerharvest_service.models import FtpUnbanRequest

    with session_factory() as session:
        requested = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(minutes=age_minutes)
        session.add(FtpUnbanRequest(hostname=hostname, ip=ip, requested_at=requested))
        session.commit()


def _pending_requests(session_factory):
    from sqlalchemy import select

    from providerharvest_service.models import FtpUnbanRequest

    with session_factory() as session:
        return sorted((r.hostname, r.ip) for r in session.scalars(select(FtpUnbanRequest)).all())


def test_the_heartbeat_stores_the_ban_list_and_whether_fail2ban_is_running(client, _isolated_db):
    _heartbeat(client, banned_ips=["84.205.215.132", "169.58.53.194"], fail2ban_active=True)
    assert _box(_isolated_db) == (["169.58.53.194", "84.205.215.132"], True)


def test_junk_in_the_reported_ban_list_is_dropped(client, _isolated_db):
    _heartbeat(client, banned_ips=["169.58.53.194", "not-an-address", "1.2.3.4; reboot", "169.58.53.194"])
    assert _box(_isolated_db)[0] == ["169.58.53.194"]


def test_a_report_without_a_ban_list_keeps_the_last_known_one(client, _isolated_db):
    _heartbeat(client, banned_ips=["169.58.53.194"], fail2ban_active=True)
    _heartbeat(client, fail2ban_active=False)  # fail2ban could not be asked this time
    assert _box(_isolated_db) == (["169.58.53.194"], False)


def test_a_reported_empty_list_clears_it(client, _isolated_db):
    _heartbeat(client, banned_ips=["169.58.53.194"])
    _heartbeat(client, banned_ips=[])
    assert _box(_isolated_db)[0] == []


def test_the_reply_hands_back_a_pending_unban_while_the_address_is_still_banned(client, _isolated_db):
    _heartbeat(client, banned_ips=["84.205.215.132", "169.58.53.194"])
    _add_unban_request(_isolated_db, "84.205.215.132")

    reply = _heartbeat(client, banned_ips=["84.205.215.132", "169.58.53.194"])
    assert reply["unban"] == ["84.205.215.132"]
    # ...and keeps handing it back until the box has actually done it.
    assert _heartbeat(client, banned_ips=["84.205.215.132", "169.58.53.194"])["unban"] == ["84.205.215.132"]


def test_a_request_is_closed_once_the_box_reports_the_address_released(client, _isolated_db):
    _heartbeat(client, banned_ips=["84.205.215.132"])
    _add_unban_request(_isolated_db, "84.205.215.132")

    reply = _heartbeat(client, banned_ips=[])  # the box lifted it
    assert reply["unban"] == []
    assert _pending_requests(_isolated_db) == []


def test_a_request_the_box_never_acts_on_expires(client, _isolated_db):
    _heartbeat(client, banned_ips=["84.205.215.132"])
    _add_unban_request(_isolated_db, "84.205.215.132", age_minutes=60)

    assert _heartbeat(client, banned_ips=["84.205.215.132"])["unban"] == []
    assert _pending_requests(_isolated_db) == []


def test_one_boxs_requests_are_not_handed_to_another(client, _isolated_db):
    _heartbeat(client, banned_ips=["84.205.215.132"])
    _add_unban_request(_isolated_db, "84.205.215.132", hostname="some-other-box")

    assert _heartbeat(client, banned_ips=["84.205.215.132"])["unban"] == []
    assert _pending_requests(_isolated_db) == [("some-other-box", "84.205.215.132")]  # not this box's to clear


def test_the_reply_has_no_unban_requests_by_default(client):
    assert _heartbeat(client)["unban"] == []
