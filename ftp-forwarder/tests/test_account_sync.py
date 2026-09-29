import sqlite3

import pytest

from ftp_forwarder.account_sync import (
    SyncError,
    fetch_accounts,
    local_account_count,
    reconcile,
    report_status,
    sync_once,
)


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else []
        self.text = text

    def json(self):
        return self._json_body


class FakeSession:
    def __init__(self, response, put_response=None):
        self._response = response
        self._put_response = put_response
        self.calls = []
        self.put_calls = []

    def get(self, url, *, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        return self._response

    def put(self, url, *, json, headers, timeout):
        self.put_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self._put_response is None:
            raise AssertionError("FakeSession.put() called without a put_response configured")
        return self._put_response


ACCOUNTS = [
    {"username": "acme", "password_hash": "$2a$hash1", "home_directory": "/srv/ftp/acme", "protocol": "ftp"},
    {"username": "other", "password_hash": "$2a$hash2", "home_directory": "/srv/ftp/other", "protocol": "sftp"},
]


def test_fetch_accounts_sends_the_sync_key_header():
    session = FakeSession(FakeResponse(200, ACCOUNTS))
    result = fetch_accounts(session, "https://loader.data.gov.gr", "the-sync-key")
    assert result == ACCOUNTS
    assert session.calls[0]["url"] == "https://loader.data.gov.gr/admin/ftp-accounts-sync"
    assert session.calls[0]["headers"]["X-Ftp-Sync-Key"] == "the-sync-key"


def test_fetch_accounts_raises_on_a_bad_status():
    session = FakeSession(FakeResponse(401, text="invalid ftp sync key"))
    with pytest.raises(SyncError, match="401"):
        fetch_accounts(session, "https://loader.data.gov.gr", "wrong-key")


def _read_accounts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT username, password_hash, home_directory, protocol FROM ftp_account").fetchall()
    finally:
        conn.close()
    return {row[0]: row[1:] for row in rows}


def test_reconcile_creates_the_schema_and_inserts_accounts(tmp_path):
    db_path = str(tmp_path / "proftpd.db")
    reconcile(db_path, ACCOUNTS)

    rows = _read_accounts(db_path)
    assert rows["acme"] == ("$2a$hash1", "/srv/ftp/acme", "ftp")
    assert rows["other"] == ("$2a$hash2", "/srv/ftp/other", "sftp")

    # The empty groups table must exist too -- ProFTPD's mod_sql
    # queries it unconditionally during auth.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("SELECT * FROM groups")
    finally:
        conn.close()


def test_reconcile_updates_an_existing_account_in_place(tmp_path):
    db_path = str(tmp_path / "proftpd.db")
    reconcile(db_path, ACCOUNTS)

    updated = [dict(ACCOUNTS[0], password_hash="$2a$rotated")]
    reconcile(db_path, updated + [ACCOUNTS[1]])

    rows = _read_accounts(db_path)
    assert rows["acme"][0] == "$2a$rotated"


def test_reconcile_removes_an_account_no_longer_present(tmp_path):
    db_path = str(tmp_path / "proftpd.db")
    reconcile(db_path, ACCOUNTS)

    reconcile(db_path, [ACCOUNTS[0]])  # "other" disabled/deleted in loader

    rows = _read_accounts(db_path)
    assert set(rows) == {"acme"}


def test_reconcile_with_no_accounts_empties_the_table(tmp_path):
    db_path = str(tmp_path / "proftpd.db")
    reconcile(db_path, ACCOUNTS)
    reconcile(db_path, [])
    assert _read_accounts(db_path) == {}


def test_sync_once_fetches_then_reconciles(tmp_path):
    db_path = str(tmp_path / "proftpd.db")
    session = FakeSession(FakeResponse(200, ACCOUNTS))

    sync_once(session, "https://loader.data.gov.gr", "the-sync-key", db_path)

    rows = _read_accounts(db_path)
    assert set(rows) == {"acme", "other"}


# -- box-health reporting ---------------------------------------------------


def test_local_account_count_reflects_the_local_database(tmp_path):
    db_path = str(tmp_path / "proftpd.db")
    assert local_account_count(db_path) == 0  # creates the schema too, no crash
    reconcile(db_path, ACCOUNTS)
    assert local_account_count(db_path) == 2


def test_report_status_sends_the_sync_key_and_payload():
    session = FakeSession(FakeResponse(200), put_response=FakeResponse(200))
    report_status(
        session, "https://loader.data.gov.gr", "the-sync-key",
        hostname="snf-899358",
        health={"proftpd_active": True, "ftp_port_listening": True, "sftp_port_listening": True, "relay_service_active": False},
        account_count=2,
        sync_ok=True,
        sync_message="ok",
    )
    assert len(session.put_calls) == 1
    call = session.put_calls[0]
    assert call["url"] == "https://loader.data.gov.gr/admin/ftp-box-status"
    assert call["headers"]["X-Ftp-Sync-Key"] == "the-sync-key"
    assert call["json"]["hostname"] == "snf-899358"
    assert call["json"]["account_count"] == 2
    assert call["json"]["relay_service_active"] is False


def test_report_status_swallows_a_bad_response_status(caplog):
    session = FakeSession(FakeResponse(200), put_response=FakeResponse(401, text="invalid ftp sync key"))
    report_status(
        session, "https://loader.data.gov.gr", "wrong-key",
        hostname="snf-899358",
        health={"proftpd_active": True, "ftp_port_listening": True, "sftp_port_listening": True, "relay_service_active": True},
        account_count=0,
        sync_ok=False,
        sync_message="boom",
    )  # must not raise


def test_report_status_swallows_a_network_error():
    class RaisingSession(FakeSession):
        def put(self, *args, **kwargs):
            import requests
            raise requests.ConnectionError("unreachable")

    session = RaisingSession(FakeResponse(200))
    report_status(
        session, "https://loader.data.gov.gr", "the-sync-key",
        hostname="snf-899358",
        health={"proftpd_active": False, "ftp_port_listening": False, "sftp_port_listening": False, "relay_service_active": False},
        account_count=0,
        sync_ok=False,
        sync_message="boom",
    )  # must not raise
