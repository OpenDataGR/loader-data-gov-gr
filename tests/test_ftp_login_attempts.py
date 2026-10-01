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
    resp = machine.get("/admin/ftp/attempts", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login")
    assert machine.get("/admin/ftp/attempts.csv", follow_redirects=False).status_code == 303


def _seed(machine):
    _send(machine, [
        _attempt(ip="188.166.1.1", port=2222, username="root", ago_s=300),
        _attempt(ip="188.166.1.1", port=2222, username="admin", ago_s=200),
        _attempt(ip="188.166.1.1", port=21, username="root", ago_s=100),
        _attempt(ip="45.9.9.9", port=21, username="ubuntu", ago_s=50),
    ])


def test_the_report_counts_attempts_per_address(machine, admin):
    _seed(machine)
    html = admin.get("/admin/ftp/attempts").text
    assert "188.166.1.1" in html and "45.9.9.9" in html
    # most attempts first
    assert html.index("188.166.1.1") < html.index("45.9.9.9")
    csv_text = admin.get("/admin/ftp/attempts-by-address.csv").text.splitlines()
    assert csv_text[0].startswith("source_address,attempts")
    row = csv_text[1].split(",")
    assert row[0] == "188.166.1.1" and row[1] == "3"  # attempts
    assert row[4] == "1" and row[5] == "2"  # ftp, sftp
    assert row[6] == "2"  # distinct usernames: root, admin
    assert row[7] == "no"


def test_the_attempt_log_shows_port_protocol_and_username(admin, machine):
    _seed(machine)
    csv_lines = admin.get("/admin/ftp/attempts.csv").text.splitlines()
    assert csv_lines[0] == "time_utc,box,source_address,port,protocol,username_tried,refusal_code"
    assert len(csv_lines) == 5
    assert any(",45.9.9.9,21,FTP,ubuntu,530" in line for line in csv_lines)
    assert any(",188.166.1.1,2222,SFTP,admin,530" in line for line in csv_lines)


def test_filters_narrow_the_report(admin, machine):
    _seed(machine)
    assert len(admin.get("/admin/ftp/attempts.csv?port=21").text.splitlines()) == 3
    assert len(admin.get("/admin/ftp/attempts.csv?address=45.").text.splitlines()) == 2
    assert len(admin.get("/admin/ftp/attempts.csv?username=roo").text.splitlines()) == 3
    assert len(admin.get("/admin/ftp/attempts.csv?address=188.166.1.1&port=2222").text.splitlines()) == 3


def test_the_time_window_filter(admin, machine):
    _send(machine, [_attempt(ip="1.1.1.1", ago_s=30 * 60), _attempt(ip="2.2.2.2", ago_s=5 * 3600)])
    assert len(admin.get("/admin/ftp/attempts.csv?window=1").text.splitlines()) == 2
    assert len(admin.get("/admin/ftp/attempts.csv?window=24").text.splitlines()) == 3
    assert len(admin.get("/admin/ftp/attempts.csv?window=all").text.splitlines()) == 3
    assert len(admin.get("/admin/ftp/attempts.csv?window=bogus").text.splitlines()) == 3  # falls back to 24h


def test_percent_and_underscore_in_filters_are_literal(admin, machine):
    _send(machine, [_attempt(username="root"), _attempt(username="r_t")])
    assert len(admin.get("/admin/ftp/attempts.csv?username=r_t").text.splitlines()) == 2  # not "r?t"
    assert len(admin.get("/admin/ftp/attempts.csv?username=%25").text.splitlines()) == 1  # no wildcard


def test_junk_filter_values_do_not_break_the_page(admin):
    assert admin.get("/admin/ftp/attempts?port=abc&window=zzz&page=-4&address=%00").status_code == 200


def test_a_hostile_username_is_escaped_in_the_page(admin, machine):
    _send(machine, [_attempt(username="<script>alert(1)</script>")])
    html = admin.get("/admin/ftp/attempts").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_csv_defuses_spreadsheet_formulas(admin, machine):
    _send(machine, [_attempt(username="=HYPERLINK(\"http://evil\")"), _attempt(username="+1"), _attempt(username="@x")])
    body = admin.get("/admin/ftp/attempts.csv").text
    assert ",=HYPERLINK" not in body
    assert "'=HYPERLINK" in body and "'+1" in body and "'@x" in body


def test_the_page_paginates(admin, machine):
    _send(machine, [_attempt(ip="3.3.3.%d" % (i % 250), ago_s=i + 1) for i in range(150)])
    page1 = admin.get("/admin/ftp/attempts").text
    assert "Page 1 of 2" in page1
    assert "Page 2 of 2" in admin.get("/admin/ftp/attempts?page=2").text
    assert "Page 2 of 2" in admin.get("/admin/ftp/attempts?page=99").text  # clamped


def test_an_empty_report_renders(admin):
    resp = admin.get("/admin/ftp/attempts")
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
    assert admin.get("/admin/ftp/attempts-by-address.csv").text.splitlines()[1].endswith(",yes")
    assert "banned now" in admin.get("/admin/ftp/attempts").text
