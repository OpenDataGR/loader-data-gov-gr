"""The Security box's whitelist and blacklist: the rules and their guards, the
per-request enforcement on loader, the page that manages them, and what the FTP
box is told in its heartbeat reply."""

import datetime
import json
import re

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from providerharvest_service import access_control, db
from providerharvest_service.access_control import ALLOW, BLOCK, AccessError
from providerharvest_service.app import app
from providerharvest_service.models import AccessRule, FtpBoxStatus, FtpLoginAttempt, FtpUnbanRequest

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"
SYNC_KEY = "test-ftp-sync-key"
SYNC_HEADERS = {"X-Ftp-Sync-Key": SYNC_KEY}
PAGE = "/admin/security/failed-logins"


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
    monkeypatch.setattr(db, "SessionLocal", session_factory)  # what the middleware and the recorder read

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
def session(_isolated_db):
    with _isolated_db() as s:
        yield s


@pytest.fixture
def machine():
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def admin():
    c = TestClient(app, base_url="https://testserver")
    c.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/admin/ftp"})
    return c


def _from(ip):
    """Behind the gateway the real client is the second-to-last entry."""
    return {"X-Forwarded-For": "%s:5555, 10.0.0.4" % ip}


def _rules(factory):
    with factory() as s:
        return sorted((r.kind, r.cidr) for r in s.scalars(select(AccessRule)).all())


def _add_box(factory, hostname="snf-899358", banned=(), address=None, access_version=None):
    with factory() as s:
        s.add(FtpBoxStatus(
            hostname=hostname, proftpd_active=True, ftp_port_listening=True, sftp_port_listening=True,
            relay_service_active=True, account_count=0, sync_ok=True, banned_ips=json.dumps(list(banned)),
            fail2ban_active=True, address=address, access_version=access_version,
        ))
        s.commit()


# -- the rules and their guards ----------------------------------------------------------

def test_a_rule_is_stored_in_canonical_form_with_who_added_it_and_a_clean_note(session):
    rule = access_control.add_rule(session, BLOCK, "203.0.113.9", note="  scanner\x00\n#4  ", created_by="kostas")
    assert (rule.kind, rule.cidr, rule.note, rule.created_by, rule.expires_at) == (BLOCK, "203.0.113.9/32", "scanner#4", "kostas", None)
    assert access_control.add_rule(session, ALLOW, "198.51.100.7/24").cidr == "198.51.100.0/24"


@pytest.mark.parametrize("bad", ["", "not-an-ip", "300.1.1.1", "2001:db8::1", "10.0.0.0/7", "0.0.0.0/0", "127.0.0.1", "127.0.0.0/8", "1.2.3.4; rm"])
def test_things_that_are_not_a_listable_address_or_range_are_refused(session, bad):
    for kind in (BLOCK, ALLOW):
        with pytest.raises(AccessError):
            access_control.add_rule(session, kind, bad)
    assert session.scalar(select(AccessRule)) is None


def test_the_same_entry_twice_and_the_same_address_on_both_lists_are_refused(session):
    access_control.add_rule(session, BLOCK, "203.0.113.9")
    with pytest.raises(AccessError, match="already on the blacklist"):
        access_control.add_rule(session, BLOCK, "203.0.113.9/32")
    with pytest.raises(AccessError, match="move it instead"):
        access_control.add_rule(session, ALLOW, "203.0.113.9")


def test_a_list_has_a_size_limit(session, monkeypatch):
    monkeypatch.setattr(access_control, "MAX_RULES_PER_KIND", 2)
    access_control.add_rule(session, BLOCK, "203.0.113.1")
    access_control.add_rule(session, BLOCK, "203.0.113.2")
    with pytest.raises(AccessError, match="full"):
        access_control.add_rule(session, BLOCK, "203.0.113.3")
    access_control.add_rule(session, ALLOW, "203.0.113.3")  # the other list has its own room


def test_you_cannot_blacklist_your_own_address_unless_it_is_whitelisted(session):
    with pytest.raises(AccessError, match="your own address"):
        access_control.add_rule(session, BLOCK, "203.0.113.0/24", requester="203.0.113.50")
    access_control.add_rule(session, ALLOW, "203.0.113.50")
    access_control.add_rule(session, BLOCK, "203.0.113.0/24", requester="203.0.113.50")  # now it is fine
    assert access_control.snapshot(session).block == ("203.0.113.0/24",)


def test_you_cannot_blacklist_the_ftp_boxs_address(session, _isolated_db):
    _add_box(_isolated_db, address="198.51.100.20")
    with pytest.raises(AccessError, match="FTP box"):
        access_control.add_rule(session, BLOCK, "198.51.100.0/24")
    access_control.add_rule(session, BLOCK, "198.51.101.0/24")  # a range that does not cover it is fine


def test_a_temporary_rule_stops_applying_when_it_expires(session):
    rule = access_control.add_rule(session, BLOCK, "203.0.113.9", duration="1h")
    assert datetime.timedelta(minutes=55) < rule.expires_at - datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) <= datetime.timedelta(hours=1, minutes=1)
    assert access_control.snapshot(session).verdict("203.0.113.9") == BLOCK
    rule.expires_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(seconds=1)
    session.commit()
    assert access_control.snapshot(session).verdict("203.0.113.9") is None
    assert access_control.rules(session) == []
    with pytest.raises(AccessError, match="duration"):
        access_control.add_rule(session, BLOCK, "203.0.113.10", duration="a fortnight")


def test_whitelist_wins_over_blacklist_and_unknown_or_ipv6_addresses_match_nothing(session):
    access_control.add_rule(session, BLOCK, "203.0.113.0/24")
    access_control.add_rule(session, ALLOW, "203.0.113.50")
    snap = access_control.snapshot(session)
    assert snap.verdict("203.0.113.7") == BLOCK
    assert snap.verdict("203.0.113.50") == ALLOW  # trusted, though inside a blocked range
    assert snap.verdict("8.8.8.8") is None
    for odd in (None, "", "garbage", "2001:db8::1"):
        assert snap.verdict(odd) is None


def test_the_version_depends_on_the_rules_not_on_when_or_in_what_order_they_were_added(session, _isolated_db):
    access_control.add_rule(session, BLOCK, "203.0.113.1")
    access_control.add_rule(session, ALLOW, "198.51.100.1")
    first = access_control.snapshot(session).version
    assert len(first) == 16 and access_control.version_of(["198.51.100.1/32"], ["203.0.113.1/32"]) == first
    access_control.add_rule(session, BLOCK, "203.0.113.2")
    assert access_control.snapshot(session).version != first


def test_moving_an_address_between_the_lists(session):
    access_control.add_rule(session, BLOCK, "203.0.113.9", note="was a scanner")
    moved = access_control.move(session, "203.0.113.9", ALLOW, created_by="kostas")
    assert (moved.kind, moved.cidr, moved.note) == (ALLOW, "203.0.113.9/32", "was a scanner")
    assert [(r.kind, r.cidr) for r in access_control.rules(session)] == [("allow", "203.0.113.9/32")]
    access_control.move(session, "203.0.113.9", BLOCK)
    assert [(r.kind, r.cidr) for r in access_control.rules(session)] == [("block", "203.0.113.9/32")]


def test_moving_to_none_takes_it_off_whichever_list_holds_it(session):
    access_control.add_rule(session, ALLOW, "203.0.113.9")
    assert access_control.move(session, "203.0.113.9", "none") is None
    assert access_control.rules(session) == []
    with pytest.raises(AccessError, match="neither"):
        access_control.move(session, "203.0.113.9", "none")


def test_a_move_the_new_list_refuses_leaves_the_old_entry_exactly_as_it_was(session):
    access_control.add_rule(session, ALLOW, "203.0.113.50", note="the office")
    with pytest.raises(AccessError, match="your own address"):
        access_control.move(session, "203.0.113.50", BLOCK, requester="203.0.113.50")  # would lock the admin out
    with pytest.raises(AccessError):
        access_control.move(session, "203.0.113.50", "sideways")
    kept = access_control.rules(session)
    assert [(r.kind, r.cidr, r.note) for r in kept] == [("allow", "203.0.113.50/32", "the office")]


def test_the_payload_for_the_box_lists_active_rules_with_the_version(session):
    access_control.add_rule(session, BLOCK, "203.0.113.9")
    access_control.add_rule(session, ALLOW, "198.51.100.0/24")
    payload = access_control.box_payload(session)
    assert payload["block"] == ["203.0.113.9/32"] and payload["allow"] == ["198.51.100.0/24"]
    assert payload["version"] == access_control.snapshot(session).version


def test_the_in_process_cache_serves_the_last_rules_until_told_or_expired_and_survives_a_database_error(session, monkeypatch):
    access_control.add_rule(session, BLOCK, "203.0.113.9")  # (add_rule invalidates)
    assert access_control.cached_snapshot().verdict("203.0.113.9") == BLOCK
    session.add(AccessRule(kind=BLOCK, cidr="203.0.113.77/32"))  # changed behind its back
    session.commit()
    assert access_control.cached_snapshot().verdict("203.0.113.77") is None  # still the cached copy
    access_control.invalidate()
    assert access_control.cached_snapshot().verdict("203.0.113.77") == BLOCK

    class Broken:
        def __enter__(self):
            raise RuntimeError("database is locked")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(db, "SessionLocal", lambda: Broken())
    monkeypatch.setattr(access_control, "CACHE_TTL_S", 0)  # force a re-read
    assert access_control.cached_snapshot().verdict("203.0.113.77") == BLOCK  # the last good rules, not "everybody"


def test_a_database_error_with_nothing_cached_blocks_and_admits_nobody(monkeypatch):
    class Broken:
        def __enter__(self):
            raise RuntimeError("down")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(db, "SessionLocal", lambda: Broken())
    access_control.invalidate()
    assert access_control.cached_snapshot().verdict("203.0.113.9") is None


# -- enforcement on loader ---------------------------------------------------------------

def test_a_blacklisted_address_is_refused_everywhere_before_anything_else(machine, session):
    access_control.add_rule(session, BLOCK, "203.0.113.0/24")
    for path in ("/health", "/admin/login", "/provider/sources", "/proxy/anything", "/push/x/upload", "/admin/ftp-box-status"):
        resp = machine.get(path, headers=_from("203.0.113.9"), follow_redirects=False)
        assert resp.status_code == 403 and resp.text == "Forbidden", path  # not the login redirect, not a 404


def test_other_addresses_and_requests_with_no_determinable_address_are_untouched(machine, session):
    access_control.add_rule(session, BLOCK, "203.0.113.0/24")
    assert machine.get("/health", headers=_from("203.0.114.9")).status_code == 200
    assert machine.get("/health").status_code == 200  # no proxy header: nothing to match


def test_a_whitelisted_address_inside_a_blocked_range_gets_through(machine, session):
    access_control.add_rule(session, BLOCK, "203.0.113.0/24")
    access_control.add_rule(session, ALLOW, "203.0.113.50")
    assert machine.get("/health", headers=_from("203.0.113.50")).status_code == 200
    assert machine.get("/health", headers=_from("203.0.113.51")).status_code == 403


def test_a_forged_forwarded_for_cannot_dodge_or_borrow_a_rule(machine, session):
    access_control.add_rule(session, BLOCK, "203.0.113.9")
    access_control.add_rule(session, ALLOW, "198.51.100.1")
    # The attacker prepends a trusted address; the entry the gateway added is what counts.
    resp = machine.get("/health", headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.9:5555, 10.0.0.4"})
    assert resp.status_code == 403


def test_a_change_takes_effect_at_once_on_the_worker_that_made_it(machine, admin, session):
    assert machine.get("/health", headers=_from("203.0.113.9")).status_code == 200
    admin.post("/admin/security/rules", data={"kind": "block", "cidr": "203.0.113.9"})
    assert machine.get("/health", headers=_from("203.0.113.9")).status_code == 403
    rule_id = session.scalar(select(AccessRule.id))
    admin.post("/admin/security/rules/%s/delete" % rule_id)
    assert machine.get("/health", headers=_from("203.0.113.9")).status_code == 200


def test_a_refusal_is_recorded_in_the_security_box_and_a_flood_is_capped(machine, session):
    from providerharvest_service import failed_auth

    access_control.add_rule(session, BLOCK, "203.0.113.9")
    for _ in range(failed_auth.MAX_PER_ADDRESS_PER_MINUTE + 10):
        machine.get("/health", headers=_from("203.0.113.9"))
    rows = session.scalars(select(FtpLoginAttempt)).all()
    assert len(rows) == failed_auth.MAX_PER_ADDRESS_PER_MINUTE
    assert {(r.channel, r.ip, r.code, r.port) for r in rows} == {("Blocked address", "203.0.113.9", 403, 443)}


# -- the page ----------------------------------------------------------------------------

def test_the_rules_routes_need_the_login(machine):
    for path, data in (("/admin/security/rules", {"kind": "block", "cidr": "203.0.113.9"}),
                       ("/admin/security/rules/move", {"cidr": "203.0.113.9", "to": "allow"}),
                       ("/admin/security/rules/x/delete", {})):
        resp = machine.post(path, data=data, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login"), path


def test_adding_by_hand_records_who_and_shows_it_in_its_list(admin, session):
    resp = admin.post("/admin/security/rules", data={"kind": "block", "cidr": "203.0.113.9", "note": "port scanner", "duration": "24h"},
                      follow_redirects=False)
    assert resp.status_code == 303 and "flash_kind=error" not in resp.headers["location"]
    rule = session.scalar(select(AccessRule))
    assert (rule.kind, rule.cidr, rule.note, rule.created_by) == ("block", "203.0.113.9/32", "port scanner", ADMIN_USERNAME)
    assert rule.expires_at is not None
    html = admin.get(PAGE).text
    blacklist = html.split('id="blacklist"', 1)[1].split("</section>", 1)[0]
    assert "<code>203.0.113.9/32</code>" in blacklist and "port scanner" in blacklist and "by test-admin" in blacklist


@pytest.mark.parametrize("data", [
    {"kind": "block", "cidr": "not-an-ip"}, {"kind": "block", "cidr": "10.0.0.0/4"}, {"kind": "nope", "cidr": "203.0.113.9"},
    {"kind": "block", "cidr": "203.0.113.9", "duration": "forever-and-a-day"}, {"kind": "block", "cidr": ""},
])
def test_a_bad_entry_is_refused_with_a_message_and_nothing_is_stored(admin, session, data):
    resp = admin.post("/admin/security/rules", data=data, follow_redirects=False)
    assert resp.status_code == 303 and "flash_kind=error" in resp.headers["location"]
    assert session.scalar(select(AccessRule)) is None


def test_you_cannot_lock_yourself_out_from_the_page(admin, session):
    resp = admin.post("/admin/security/rules", data={"kind": "block", "cidr": "203.0.113.0/24"}, headers=_from("203.0.113.50"),
                      follow_redirects=False)
    assert "flash_kind=error" in resp.headers["location"] and "your+own+address" in resp.headers["location"]
    assert session.scalar(select(AccessRule)) is None


def test_removing_a_rule_and_removing_an_unknown_one(admin, session):
    rule = access_control.add_rule(session, ALLOW, "203.0.113.9")
    resp = admin.post("/admin/security/rules/%s/delete" % rule.id, follow_redirects=False)
    assert "flash_kind=error" not in resp.headers["location"] and session.scalar(select(AccessRule)) is None
    assert "flash_kind=error" in admin.post("/admin/security/rules/no-such-rule/delete", follow_redirects=False).headers["location"]


def test_moving_a_banned_address_to_the_blacklist_and_to_the_whitelist(admin, session, _isolated_db):
    _add_box(_isolated_db, banned=["203.0.113.9", "198.51.100.4"])
    admin.post("/admin/security/rules/move", data={"cidr": "203.0.113.9", "to": "block"})
    assert _rules(_isolated_db) == [("block", "203.0.113.9/32")]

    resp = admin.post("/admin/security/rules/move", data={"cidr": "198.51.100.4", "to": "allow"}, follow_redirects=False)
    assert ("allow", "198.51.100.4/32") in _rules(_isolated_db)
    assert "lift+its+ban" in resp.headers["location"]  # it also asked the box to let it go now
    with _isolated_db() as s:
        assert [(r.hostname, r.ip) for r in s.scalars(select(FtpUnbanRequest)).all()] == [("snf-899358", "198.51.100.4")]


def test_whitelisting_a_range_lifts_the_bans_inside_it_only(admin, _isolated_db):
    _add_box(_isolated_db, banned=["198.51.100.4", "198.51.100.9", "203.0.113.9"])
    admin.post("/admin/security/rules/move", data={"cidr": "198.51.100.0/24", "to": "allow"})
    with _isolated_db() as s:
        assert sorted(r.ip for r in s.scalars(select(FtpUnbanRequest)).all()) == ["198.51.100.4", "198.51.100.9"]


def test_moving_between_the_two_lists_and_off_both(admin, session, _isolated_db):
    access_control.add_rule(session, BLOCK, "203.0.113.9")
    admin.post("/admin/security/rules/move", data={"cidr": "203.0.113.9", "to": "allow"})
    assert _rules(_isolated_db) == [("allow", "203.0.113.9/32")]
    admin.post("/admin/security/rules/move", data={"cidr": "203.0.113.9", "to": "block"})
    assert _rules(_isolated_db) == [("block", "203.0.113.9/32")]
    admin.post("/admin/security/rules/move", data={"cidr": "203.0.113.9", "to": "none"})
    assert _rules(_isolated_db) == []


def test_a_refused_move_changes_nothing_and_says_why(admin, session, _isolated_db):
    access_control.add_rule(session, ALLOW, "203.0.113.50")
    resp = admin.post("/admin/security/rules/move", data={"cidr": "203.0.113.50", "to": "block"}, headers=_from("203.0.113.50"),
                      follow_redirects=False)
    assert "flash_kind=error" in resp.headers["location"]
    assert _rules(_isolated_db) == [("allow", "203.0.113.50/32")]


def test_after_an_action_you_land_back_on_the_view_you_were_on_with_its_filters(admin):
    referer = "https://loader.data.gov.gr%s?view=attempts&address=203.0.113.9&sort=ip&dir=asc&flash=old" % PAGE
    resp = admin.post("/admin/security/rules", data={"kind": "block", "cidr": "203.0.113.9"}, headers={"referer": referer},
                      follow_redirects=False)
    location = resp.headers["location"]
    assert location.startswith(PAGE + "?") and "view=attempts" in location and "address=203.0.113.9" in location
    assert "sort=ip" in location and "flash=Added" in location and "flash=old" not in location


def test_a_foreign_referer_is_never_followed(admin):
    resp = admin.post("/admin/security/rules", data={"kind": "block", "cidr": "203.0.113.9"},
                      headers={"referer": "https://evil.example/steal?x=1"}, follow_redirects=False)
    assert resp.headers["location"].startswith(PAGE + "?") and "evil" not in resp.headers["location"] and "x=1" not in resp.headers["location"]


def test_the_page_shows_three_lists_and_buttons_to_move_between_them(admin, session, _isolated_db):
    _add_box(_isolated_db, banned=["198.51.100.4"])
    access_control.add_rule(session, BLOCK, "203.0.113.9")
    access_control.add_rule(session, ALLOW, "192.0.2.1")
    html = admin.get(PAGE).text
    for name in ("banned-list", "blacklist", "whitelist"):
        assert 'id="%s"' % name in html
    banned = html.split('id="banned-list"', 1)[1].split("</section>", 1)[0]
    assert "<code>198.51.100.4</code>" in banned and "Unban" in banned
    assert banned.count('name="to" value="block"') == 1 and banned.count('name="to" value="allow"') == 1
    black = html.split('id="blacklist"', 1)[1].split("</section>", 1)[0]
    assert 'name="to" value="allow"' in black and "Remove" in black
    white = html.split('id="whitelist"', 1)[1].split("</section>", 1)[0]
    assert 'name="to" value="block"' in white and "Remove" in white


def test_empty_lists_say_so(admin):
    html = admin.get(PAGE).text
    assert "Nothing blacklisted." in html and "Nothing whitelisted." in html


def test_a_note_is_escaped_in_the_page(admin, session):
    access_control.add_rule(session, BLOCK, "203.0.113.9", note="<script>alert(1)</script>")
    html = admin.get(PAGE).text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_address_rows_show_their_list_and_offer_the_other_moves(admin, machine, session):
    machine.post("/admin/ftp-login-attempts", headers=SYNC_HEADERS, json={"hostname": "box1", "attempts": [
        {"at": int(datetime.datetime.now(datetime.timezone.utc).timestamp()) - 30, "ip": ip, "port": 2222, "username": "root", "code": 530}
        for ip in ("203.0.113.9", "198.51.100.7", "192.0.2.3")]})
    access_control.add_rule(session, BLOCK, "203.0.113.9")
    access_control.add_rule(session, ALLOW, "198.51.100.7")
    html = admin.get(PAGE).text
    rows = {m.group(1): m.group(0) for m in re.finditer(r"<tr>\s*<td><a class=\"cell\"[^>]*><code>([\d.]+)</code>.*?</tr>", html, re.S)}
    assert "blacklisted" in rows["203.0.113.9"] and 'value="block"' not in rows["203.0.113.9"] and 'value="allow"' in rows["203.0.113.9"]
    assert "whitelisted" in rows["198.51.100.7"] and 'value="allow"' not in rows["198.51.100.7"] and 'value="block"' in rows["198.51.100.7"]
    assert "listed" not in rows["192.0.2.3"].replace("blacklisted", "").replace("whitelisted", "") and 'value="block"' in rows["192.0.2.3"] and 'value="allow"' in rows["192.0.2.3"]


# -- the FTP box -------------------------------------------------------------------------

BOX_PAYLOAD = {"hostname": "snf-899358", "proftpd_active": True, "ftp_port_listening": True, "sftp_port_listening": True,
               "relay_service_active": True, "account_count": 0, "sync_ok": True, "sync_message": "ok"}


def _beat(machine, ip="198.51.100.20", **extra):
    return machine.put("/admin/ftp-box-status", json=dict(BOX_PAYLOAD, **extra), headers={**SYNC_HEADERS, **_from(ip)})


def test_the_heartbeat_reply_carries_the_rules_and_their_version(machine, session):
    access_control.add_rule(session, BLOCK, "203.0.113.9")
    access_control.add_rule(session, ALLOW, "192.0.2.0/24")
    body = _beat(machine).json()
    assert body["access"]["block"] == ["203.0.113.9/32"] and body["access"]["allow"] == ["192.0.2.0/24"]
    assert body["access"]["version"] == access_control.snapshot(session).version


def test_the_heartbeat_with_no_rules_still_carries_an_empty_set_with_a_version(machine):
    body = _beat(machine).json()
    assert body["access"]["block"] == [] and body["access"]["allow"] == [] and body["access"]["version"]


def test_the_box_address_and_applied_version_are_remembered(machine, session):
    _beat(machine, ip="198.51.100.20", access_version="abc123")
    box = session.get(FtpBoxStatus, "snf-899358")
    assert (box.address, box.access_version) == ("198.51.100.20", "abc123")
    _beat(machine, ip="198.51.100.21")  # a report from an older box: no version, new address
    session.expire_all()
    assert (box.address, box.access_version) == ("198.51.100.21", None)


def test_the_box_cannot_be_blacklisted_once_its_address_is_known(machine, session):
    _beat(machine, ip="198.51.100.20")
    with pytest.raises(AccessError, match="FTP box"):
        access_control.add_rule(session, BLOCK, "198.51.100.0/24")


def test_the_page_says_whether_the_box_has_applied_the_current_rules(admin, machine, session):
    access_control.add_rule(session, BLOCK, "203.0.113.9")
    current = access_control.snapshot(session).version
    _beat(machine, access_version=current)
    assert "rules applied" in admin.get(PAGE).text
    _beat(machine, access_version="an-older-version")
    assert "waiting for the box" in admin.get(PAGE).text
    _beat(machine)  # a box on older code never reports one
    assert "rules status unknown" in admin.get(PAGE).text


def test_a_blacklisted_box_address_cannot_reach_loader_so_the_guard_matters(machine, session):
    # What the guard prevents: were the box's own address blocked, its heartbeat would be refused.
    session.add(AccessRule(kind=BLOCK, cidr="198.51.100.20/32"))
    session.commit()
    access_control.invalidate()
    assert _beat(machine, ip="198.51.100.20").status_code == 403
