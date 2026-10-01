"""The failed-login report: the box's intake endpoint and the admin page
built on what it stores."""

import datetime
import time

import bcrypt
import pytest
from fastapi.testclient import TestClient

from providerharvest_service import db
from providerharvest_service.admin_ftp import ATTEMPT_RETENTION, MAX_USERNAME_LENGTH, clean_username
from providerharvest_service.app import app
from providerharvest_service.models import FtpBoxStatus, FtpLoginAttempt

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"
SYNC_KEY = "test-ftp-sync-key"
SYNC_HEADERS = {"X-Ftp-Sync-Key": SYNC_KEY}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("LOADER_FTP_SYNC_KEY", SYNC_KEY)
    monkeypatch.setenv("LOADER_ADMIN_UI_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv(
        "LOADER_ADMIN_UI_PASSWORD_HASH", bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
    )
    monkeypatch.setenv("LOADER_ADMIN_SESSION_SECRET", "test-session-secret")


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "test.db"))
    session_factory = db.make_session_factory(engine)
    monkeypatch.setattr(db, "SessionLocal", session_factory)  # where failed_auth.py writes

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
def machine():
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def admin():
    c = TestClient(app, base_url="https://testserver")
    c.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/admin/ftp"})
    return c


def _attempt(ip="188.166.1.1", port=2222, username="admin", code=530, ago_s=60):
    return {"at": int(time.time()) - ago_s, "ip": ip, "port": port, "username": username, "code": code}


def _send(machine, attempts, headers=SYNC_HEADERS):
    return machine.post("/admin/ftp-login-attempts", json={"hostname": "box1", "attempts": attempts}, headers=headers)


def _rows(session_factory):
    with session_factory() as session:
        return session.query(FtpLoginAttempt).order_by(FtpLoginAttempt.id).all()


# -- intake ---------------------------------------------------------------------------

def test_intake_needs_the_sync_key(machine):
    assert _send(machine, [_attempt()], headers={}).status_code == 401
    assert _send(machine, [_attempt()], headers={"X-Ftp-Sync-Key": "wrong"}).status_code == 401


def test_intake_stores_every_field(machine, _isolated_db):
    resp = _send(machine, [_attempt(ip="1.2.3.4", port=21, username="root", code=530)])
    assert resp.json() == {"stored": 1, "rejected": 0}
    (row,) = _rows(_isolated_db)
    assert (row.hostname, row.ip, row.port, row.username, row.code) == ("box1", "1.2.3.4", 21, "root", 530)


def test_intake_skips_nonsense_entries_but_keeps_the_rest(machine, _isolated_db):
    now = int(time.time())
    bad = [
        _attempt(ip="not-an-address"),
        _attempt(port=0),
        _attempt(port=70000),
        {"at": now - int(ATTEMPT_RETENTION.total_seconds()) - 3600, "ip": "1.1.1.1", "port": 21, "code": 530},
        {"at": now + 10 * 86400, "ip": "1.1.1.1", "port": 21, "code": 530},
    ]
    resp = _send(machine, bad + [_attempt(ip="9.9.9.9")])
    assert resp.json() == {"stored": 1, "rejected": 5}
    assert [r.ip for r in _rows(_isolated_db)] == ["9.9.9.9"]


def test_intake_refuses_an_oversized_batch(machine):
    assert _send(machine, [_attempt()] * 2001).status_code == 422


def test_intake_cleans_the_username(machine, _isolated_db):
    _send(machine, [_attempt(username="ro\x00ot\n\x1b[31mfake"), _attempt(username="x" * 500)])
    names = [r.username for r in _rows(_isolated_db)]
    assert names[0] == "root[31mfake"
    assert len(names[1]) == MAX_USERNAME_LENGTH


def test_clean_username_drops_control_characters():
    assert clean_username("a\r\nb\tc") == "abc"
    assert clean_username("") == ""


def test_old_attempts_are_pruned_on_intake(machine, _isolated_db):
    long_ago = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - ATTEMPT_RETENTION - datetime.timedelta(days=1)
    with _isolated_db() as session:
        session.add(FtpLoginAttempt(hostname="box1", at=long_ago, ip="5.5.5.5", port=21, username="old", code=530))
        session.commit()
    _send(machine, [_attempt(ip="6.6.6.6")])
    assert [r.ip for r in _rows(_isolated_db)] == ["6.6.6.6"]


# -- the report page ------------------------------------------------------------------

def test_the_report_needs_a_login(machine):
    resp = machine.get("/admin/security/failed-logins", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login")
    assert machine.get("/admin/security/failed-logins.csv", follow_redirects=False).status_code == 303


def _seed(machine):
    _send(machine, [
        _attempt(ip="188.166.1.1", port=2222, username="root", ago_s=300),
        _attempt(ip="188.166.1.1", port=2222, username="admin", ago_s=200),
        _attempt(ip="188.166.1.1", port=21, username="root", ago_s=100),
        _attempt(ip="45.9.9.9", port=21, username="ubuntu", ago_s=50),
    ])


def test_the_report_counts_attempts_per_address(machine, admin):
    _seed(machine)
    html = admin.get("/admin/security/failed-logins").text
    assert "188.166.1.1" in html and "45.9.9.9" in html
    # most attempts first
    assert html.index("188.166.1.1") < html.index("45.9.9.9")
    csv_text = admin.get("/admin/security/failed-logins-by-address.csv").text.splitlines()
    assert csv_text[0].startswith("source_address,attempts")
    row = csv_text[1].split(",")
    assert row[0] == "188.166.1.1" and row[1] == "3"  # attempts
    assert row[4] == "FTP; SFTP"  # methods
    assert row[5] == "2"  # distinct usernames: root, admin
    assert row[6] == "no"


def test_the_attempt_log_shows_port_protocol_and_username(admin, machine):
    _seed(machine)
    csv_lines = admin.get("/admin/security/failed-logins.csv").text.splitlines()
    assert csv_lines[0] == "time_utc,box,source_address,port,method,username_tried,refusal_code"
    assert len(csv_lines) == 5
    assert any(",45.9.9.9,21,FTP,ubuntu,530" in line for line in csv_lines)
    assert any(",188.166.1.1,2222,SFTP,root,530" in line for line in csv_lines)
    assert any(",188.166.1.1,2222,SFTP,admin,530" in line for line in csv_lines)


def test_filters_narrow_the_report(admin, machine):
    _seed(machine)
    assert len(admin.get("/admin/security/failed-logins.csv?method=FTP").text.splitlines()) == 3
    assert len(admin.get("/admin/security/failed-logins.csv?method=SFTP").text.splitlines()) == 3
    assert len(admin.get("/admin/security/failed-logins.csv?method=Web+login").text.splitlines()) == 1
    assert len(admin.get("/admin/security/failed-logins.csv?address=45.").text.splitlines()) == 2
    assert len(admin.get("/admin/security/failed-logins.csv?username=roo").text.splitlines()) == 3
    assert len(admin.get("/admin/security/failed-logins.csv?address=188.166.1.1&method=SFTP").text.splitlines()) == 3


def test_the_time_window_filter(admin, machine):
    _send(machine, [_attempt(ip="1.1.1.1", ago_s=30 * 60), _attempt(ip="2.2.2.2", ago_s=5 * 3600)])
    assert len(admin.get("/admin/security/failed-logins.csv?window=1").text.splitlines()) == 2
    assert len(admin.get("/admin/security/failed-logins.csv?window=24").text.splitlines()) == 3
    assert len(admin.get("/admin/security/failed-logins.csv?window=all").text.splitlines()) == 3
    assert len(admin.get("/admin/security/failed-logins.csv?window=bogus").text.splitlines()) == 3  # falls back to 24h


def test_percent_and_underscore_in_filters_are_literal(admin, machine):
    _send(machine, [_attempt(username="root"), _attempt(username="r_t")])
    assert len(admin.get("/admin/security/failed-logins.csv?username=r_t").text.splitlines()) == 2  # not "r?t"
    assert len(admin.get("/admin/security/failed-logins.csv?username=%25").text.splitlines()) == 1  # no wildcard


def test_junk_filter_values_do_not_break_the_page(admin):
    assert admin.get("/admin/security/failed-logins?method=abc&window=zzz&page=-4&address=%00").status_code == 200


def test_a_hostile_username_is_escaped_in_the_page(admin, machine):
    _send(machine, [_attempt(username="<script>alert(1)</script>")])
    html = admin.get("/admin/security/failed-logins").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_csv_defuses_spreadsheet_formulas(admin, machine):
    _send(machine, [_attempt(username="=HYPERLINK(\"http://evil\")"), _attempt(username="+1"), _attempt(username="@x")])
    body = admin.get("/admin/security/failed-logins.csv").text
    assert ",=HYPERLINK" not in body
    assert "'=HYPERLINK" in body and "'+1" in body and "'@x" in body


def test_the_page_paginates(admin, machine):
    _send(machine, [_attempt(ip="3.3.3.%d" % (i % 250), ago_s=i + 1) for i in range(150)])
    page1 = admin.get("/admin/security/failed-logins").text
    assert "Page 1 of 2" in page1
    assert "Page 2 of 2" in admin.get("/admin/security/failed-logins?page=2").text
    assert "Page 2 of 2" in admin.get("/admin/security/failed-logins?page=99").text  # clamped


def test_an_empty_report_renders(admin):
    resp = admin.get("/admin/security/failed-logins")
    assert resp.status_code == 200
    assert "No refused logins" in resp.text


def test_currently_banned_addresses_are_marked(admin, machine, _isolated_db):
    _seed(machine)
    with _isolated_db() as session:
        session.add(FtpBoxStatus(
            hostname="box1", proftpd_active=True, ftp_port_listening=True, sftp_port_listening=True,
            relay_service_active=True, account_count=1, sync_ok=True, banned_ips='["188.166.1.1"]',
        ))
        session.commit()
    assert admin.get("/admin/security/failed-logins-by-address.csv").text.splitlines()[1].endswith(",yes")
    assert "banned now" in admin.get("/admin/security/failed-logins").text


# -- HTTP: the web login and the API/sync keys ----------------------------------------------

def _http_rows(session_factory):
    return [(r.channel, r.ip, r.port, r.username, r.code) for r in _rows(session_factory) if r.hostname == "loader"]


def _from(ip):
    # Behind the gateway the client is the second-to-last entry.
    return {"X-Forwarded-For": "%s:5555, 10.0.0.4" % ip}


def test_a_wrong_web_login_is_recorded_with_the_typed_username(machine, _isolated_db):
    machine.post("/admin/login", data={"username": "root", "password": "nope"}, headers=_from("203.0.113.7"))
    assert _http_rows(_isolated_db) == [("Web login", "203.0.113.7", 443, "root", 401)]


def test_a_correct_web_login_records_nothing(machine, _isolated_db):
    machine.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert _http_rows(_isolated_db) == []


def test_the_password_is_never_recorded(machine, _isolated_db):
    machine.post("/admin/login", data={"username": "root", "password": "hunter2-secret"}, headers=_from("203.0.113.7"))
    assert "hunter2" not in repr([vars(r) for r in _rows(_isolated_db)])


def test_bad_keys_are_recorded_under_the_credential_they_were_for(machine, _isolated_db, monkeypatch):
    monkeypatch.setenv("LOADER_ADMIN_API_KEY", "real-admin-key")
    h = _from("203.0.113.8")
    machine.get("/admin/ftp-accounts", headers={**h, "X-Admin-Key": "wrong"})
    machine.get("/admin/ftp-accounts-sync", headers={**h, "X-Ftp-Sync-Key": "wrong"})
    machine.put("/admin/ftp-box-status", json={}, headers={**h, "X-Ftp-Sync-Key": "wrong"})
    machine.get("/me", headers=h)
    channels = sorted(c for c, *_ in _http_rows(_isolated_db))
    assert channels == ["Admin API key", "Bearer token", "FTP box key", "FTP box key"]


def test_push_paths_are_filed_under_the_push_key_with_the_source_name():
    from providerharvest_service import failed_auth

    assert failed_auth.channel_for_path("/push/acme/upload") == "Push API key"
    assert failed_auth.channel_for_path("/admin/ftp-accounts-sync") == "FTP box key"
    assert failed_auth.channel_for_path("/admin/ftp-accounts") == "Admin API key"
    assert failed_auth.channel_for_path("/admin/ftp-accounts/x/allowed-ips") == "Admin API key"
    assert failed_auth.channel_for_path("/pushy") == "HTTP"  # a prefix match needs the "/" boundary


def test_successful_and_unrelated_responses_record_nothing(machine, _isolated_db):
    machine.get("/health")
    machine.get("/provider/sources", follow_redirects=False)  # a redirect to the login page, not a failure
    assert _http_rows(_isolated_db) == []


def test_http_failures_show_up_in_the_report_with_their_method(machine, admin, _isolated_db):
    machine.post("/admin/login", data={"username": "root", "password": "x"}, headers=_from("203.0.113.7"))
    _send(machine, [_attempt(ip="203.0.113.7", port=21)])
    csv_lines = admin.get("/admin/security/failed-logins-by-address.csv").text.splitlines()
    assert csv_lines[1].split(",")[4] == "FTP; Web login"
    assert len(admin.get("/admin/security/failed-logins.csv?method=Web+login").text.splitlines()) == 2
    assert "Web login" in admin.get("/admin/security/failed-logins").text


def test_a_flood_from_one_address_is_capped(machine, _isolated_db):
    from providerharvest_service import failed_auth

    for _ in range(failed_auth.MAX_PER_ADDRESS_PER_MINUTE + 10):
        machine.get("/me", headers=_from("203.0.113.50"))
    assert len(_http_rows(_isolated_db)) == failed_auth.MAX_PER_ADDRESS_PER_MINUTE


def test_a_recording_failure_never_breaks_the_refusal(machine, monkeypatch):
    from providerharvest_service import failed_auth

    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(failed_auth, "_record", boom)
    assert machine.get("/me", headers=_from("203.0.113.51")).status_code == 401


def test_old_rows_without_a_channel_are_told_apart_by_port(admin, _isolated_db):
    with _isolated_db() as session:
        session.add(FtpLoginAttempt(
            hostname="box1", at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
            ip="7.7.7.7", port=2222, username="old", code=530, channel=None,
        ))
        session.commit()
    assert ",7.7.7.7,2222,SFTP,old,530" in admin.get("/admin/security/failed-logins.csv").text
    assert len(admin.get("/admin/security/failed-logins.csv?method=SFTP").text.splitlines()) == 2
