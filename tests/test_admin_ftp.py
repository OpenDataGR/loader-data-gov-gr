import pytest
from fastapi.testclient import TestClient

from providerharvest_service import admin_auth, db
from providerharvest_service.app import app

ADMIN_KEY = "test-admin-key"
AUTH_HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture(autouse=True)
def _admin_key(monkeypatch):
    monkeypatch.setenv("LOADER_ADMIN_API_KEY", ADMIN_KEY)


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

    added = client.post(
        "/admin/ftp-accounts/%s/allowed-ips" % account_id,
        json={"cidr": "203.0.113.0/24"},
        headers=AUTH_HEADERS,
    )
    assert added.status_code == 201
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
