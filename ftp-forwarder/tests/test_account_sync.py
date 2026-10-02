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


# -- home directory creation -----------------------------------------------


def _root(tmp_path):
    root = tmp_path / "srv-ftp"
    root.mkdir()
    return root.as_posix()


def _acct(root, username):
    return {
        "username": username, "password_hash": "$2a$h", "protocol": "sftp",
        "home_directory": "%s/%s" % (root, username),
    }


def test_ensure_home_directories_creates_a_missing_directory_under_the_root(tmp_path):
    from ftp_forwarder.account_sync import ensure_home_directories

    root = _root(tmp_path)
    created = ensure_home_directories([_acct(root, "acme")], root)

    assert created == ["%s/acme" % root]
    assert (tmp_path / "srv-ftp" / "acme").is_dir()


def test_ensure_home_directories_is_idempotent_and_keeps_existing_contents(tmp_path):
    from ftp_forwarder.account_sync import ensure_home_directories

    root = _root(tmp_path)
    ensure_home_directories([_acct(root, "acme")], root)
    (tmp_path / "srv-ftp" / "acme" / "upload.csv").write_text("x", encoding="utf-8")

    assert ensure_home_directories([_acct(root, "acme")], root) == []
    assert (tmp_path / "srv-ftp" / "acme" / "upload.csv").exists()


def test_ensure_home_directories_refuses_a_path_outside_the_root(tmp_path):
    from ftp_forwarder.account_sync import ensure_home_directories

    root = _root(tmp_path)
    outside = {
        "username": "evil", "password_hash": "$2a$h", "protocol": "ftp",
        "home_directory": (tmp_path / "elsewhere").as_posix(),
    }
    traversal = dict(outside, username="evil2", home_directory="%s/../elsewhere2" % root)

    assert ensure_home_directories([outside, traversal], root) == []
    assert not (tmp_path / "elsewhere").exists()
    assert not (tmp_path / "elsewhere2").exists()


def test_ensure_home_directories_refuses_the_root_itself(tmp_path):
    from ftp_forwarder.account_sync import ensure_home_directories

    root = _root(tmp_path)
    at_root = dict(_acct(root, "x"), home_directory=root)
    assert ensure_home_directories([at_root], root) == []


def test_sync_once_creates_directories_only_when_an_upload_root_is_given(tmp_path):
    root = _root(tmp_path)
    accounts = [_acct(root, "acme")]
    session = FakeSession(FakeResponse(200, accounts))

    sync_once(session, "https://loader.data.gov.gr", "k", str(tmp_path / "a.db"))
    assert not (tmp_path / "srv-ftp" / "acme").exists()

    sync_once(session, "https://loader.data.gov.gr", "k", str(tmp_path / "a.db"), upload_root=root)
    assert (tmp_path / "srv-ftp" / "acme").is_dir()


# -- reporting provider logins ------------------------------------------------

_HEALTH = {"proftpd_active": True, "ftp_port_listening": True, "sftp_port_listening": True, "relay_service_active": True}


def _report(session, **overrides):
    kwargs = dict(hostname="snf-899358", health=_HEALTH, account_count=1, sync_ok=True, sync_message="ok")
    kwargs.update(overrides)
    return report_status(session, "https://loader.data.gov.gr", "the-sync-key", **kwargs)


def test_report_status_sends_the_logins_it_was_given():
    session = FakeSession(FakeResponse(200), put_response=FakeResponse(200))
    clients = [{"username": "acme", "ip": "203.0.113.9"}]

    _report(session, clients=clients)

    assert session.put_calls[0]["json"]["clients"] == clients


def test_report_status_sends_an_empty_list_when_there_are_no_logins():
    session = FakeSession(FakeResponse(200), put_response=FakeResponse(200))
    _report(session)
    assert session.put_calls[0]["json"]["clients"] == []


def test_report_status_says_whether_loader_accepted_the_report():
    # The caller keeps unsent logins until this is True.
    assert _report(FakeSession(FakeResponse(200), put_response=FakeResponse(200))).accepted is True
    assert _report(FakeSession(FakeResponse(200), put_response=FakeResponse(401, text="no"))).accepted is False

    class Unreachable(FakeSession):
        def put(self, *args, **kwargs):
            import requests
            raise requests.ConnectionError("down")

    assert _report(Unreachable(FakeResponse(200))).accepted is False


def test_report_status_sends_the_ban_list_and_omits_it_when_unreadable():
    session = FakeSession(FakeResponse(200), put_response=FakeResponse(200))
    _report(session, banned_ips=["84.205.215.132"])
    assert session.put_calls[0]["json"]["banned_ips"] == ["84.205.215.132"]

    session = FakeSession(FakeResponse(200), put_response=FakeResponse(200))
    _report(session, banned_ips=None)  # fail2ban could not be asked: say nothing, keep the last known list
    assert "banned_ips" not in session.put_calls[0]["json"]

    session = FakeSession(FakeResponse(200), put_response=FakeResponse(200))
    _report(session, banned_ips=[])  # asked, and nobody is banned: that IS worth saying
    assert session.put_calls[0]["json"]["banned_ips"] == []


def test_report_status_returns_the_unban_requests_in_the_reply():
    report = _report(FakeSession(FakeResponse(200), put_response=FakeResponse(200, json_body={"unban": ["1.2.3.4", "5.6.7.8"]})))
    assert report.accepted is True
    assert report.unban == ("1.2.3.4", "5.6.7.8")


def test_report_status_copes_with_a_reply_that_has_no_unban_list():
    for body in ({}, [], {"unban": None}, {"unban": [1, None, "1.2.3.4"]}):
        report = _report(FakeSession(FakeResponse(200), put_response=FakeResponse(200, json_body=body)))
        assert report.accepted is True
        assert report.unban in ((), ("1.2.3.4",)), body


def test_a_refused_report_asks_for_nothing_to_be_unbanned():
    report = _report(FakeSession(FakeResponse(200), put_response=FakeResponse(401, text="no")))
    assert report.accepted is False and report.unban == ()


# -- loader's whitelist/blacklist in the heartbeat ----------------------------------------

def test_report_status_sends_the_applied_access_version_and_returns_the_rules():
    rules = {"version": "abc", "allow": ["198.51.100.7/32"], "block": ["203.0.113.0/24"]}
    session = FakeSession(FakeResponse(200), put_response=FakeResponse(200, json_body={"unban": [], "access": rules}))
    report = _report(session, access_version="")
    assert session.put_calls[0]["json"]["access_version"] == ""  # nothing applied yet is still said, so loader shows "waiting"
    assert report.access == rules

    session = FakeSession(FakeResponse(200), put_response=FakeResponse(200))
    _report(session, access_version="abc")
    assert session.put_calls[0]["json"]["access_version"] == "abc"
    session = FakeSession(FakeResponse(200), put_response=FakeResponse(200))
    _report(session)
    assert "access_version" not in session.put_calls[0]["json"]


def test_a_reply_without_rules_has_none():
    for body in ({}, [], {"access": None}):
        assert _report(FakeSession(FakeResponse(200), put_response=FakeResponse(200, json_body=body))).access is None


# -- per-account login limits during a sync ---------------------------------------

def test_sync_applies_the_limits_before_the_accounts_become_loginable(tmp_path):
    order = []
    db = str(tmp_path / "accounts.db")
    conf = str(tmp_path / "limits.conf")
    accounts = [dict(ACCOUNTS[0], allowed_ips=["203.0.113.9"]), dict(ACCOUNTS[1], allowed_ips=[])]

    import subprocess

    def run(cmd):
        order.append(("limits", _read_accounts(db) if (tmp_path / "accounts.db").exists() else []))
        return subprocess.CompletedProcess([], 0, "", "")

    sync_once(FakeSession(FakeResponse(200, json_body=accounts)), "https://x", "k", db,
              ip_limits_conf=conf, ip_limits_run=run)
    assert order and all(seen == [] for _, seen in order)  # nobody was in the database yet
    assert "<IfUser acme>" in open(conf).read()
    assert len(_read_accounts(db)) == 2


def test_a_limit_that_cannot_be_applied_fails_the_sync_loudly(tmp_path):
    import subprocess

    accounts = [dict(ACCOUNTS[0], allowed_ips=["203.0.113.9"])]
    with pytest.raises(SyncError, match="login limits"):
        sync_once(FakeSession(FakeResponse(200, json_body=accounts)), "https://x", "k", str(tmp_path / "a.db"),
                  ip_limits_conf=str(tmp_path / "limits.conf"),
                  ip_limits_run=lambda c: subprocess.CompletedProcess([], 1, "", "nope"))


def test_without_a_limits_file_configured_nothing_is_generated(tmp_path):
    sync_once(FakeSession(FakeResponse(200, json_body=[dict(ACCOUNTS[0], allowed_ips=["203.0.113.9"])])),
              "https://x", "k", str(tmp_path / "a.db"))
    assert list(tmp_path.glob("*.conf")) == []


# -- shipping refused logins ------------------------------------------------------

class PostSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def post(self, url, *, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.error:
            raise self.error
        return self.response


def test_report_attempts_posts_the_batch_with_the_sync_key():
    from ftp_forwarder.account_sync import report_attempts

    session = PostSession(FakeResponse(200))
    batch = [{"at": 1, "ip": "1.1.1.1", "port": 21, "username": "root", "code": 530}]
    assert report_attempts(session, "https://loader.data.gov.gr/", "k", hostname="box", attempts=batch) is True
    call = session.calls[0]
    assert call["url"] == "https://loader.data.gov.gr/admin/ftp-login-attempts"
    assert call["headers"] == {"X-Ftp-Sync-Key": "k"}
    assert call["json"] == {"hostname": "box", "attempts": batch}


def test_report_attempts_is_false_on_any_failure_so_the_batch_is_retried():
    import requests
    from ftp_forwarder.account_sync import report_attempts

    for session in (PostSession(FakeResponse(401, text="no")), PostSession(FakeResponse(500)),
                    PostSession(error=requests.ConnectionError("down"))):
        assert report_attempts(session, "https://x", "k", hostname="box", attempts=[]) is False
