"""The Security box's whitelist and blacklist: the rules and their guards, the
per-request enforcement on loader, the page that manages them, and what the FTP
box is told in its heartbeat reply."""

import base64
import datetime
import json
import os
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
def _master_key(monkeypatch):
    # Registering a source touches envelope encryption (the tests below register push sources).
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())


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


@pytest.mark.parametrize("bad", [
    "", "not-an-ip", "300.1.1.1", "10.0.0.0/7", "0.0.0.0/0", "127.0.0.1", "127.0.0.0/8", "1.2.3.4; rm",
    "2000::/16", "::/0", "2001:db8::/31", "::1", "::", "::ffff:1.2.3.4", "::ffff:0:0/96", "2001:db8::1; rm", "[2001:db8::1]",
])
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


# -- IPv6 ---------------------------------------------------------------------------------

V6 = "2001:db8:abcd::5"  # a documentation address: perfectly good for a rule (they are only exempt from auto-bans)


def test_ipv6_rules_are_stored_in_canonical_form(session):
    assert access_control.add_rule(session, BLOCK, "2001:DB8::1").cidr == "2001:db8::1/128"
    assert access_control.add_rule(session, ALLOW, "2001:db8:1:2::9/64").cidr == "2001:db8:1:2::/64"  # a host with a prefix -> its network
    assert access_control.add_rule(session, BLOCK, "2001:db8::/32").cidr == "2001:db8::/32"  # exactly the limit


def test_ipv6_ranges_are_judged_independently_of_ipv4_ones(session):
    access_control.add_rule(session, BLOCK, "2001:db8::/32")
    access_control.add_rule(session, ALLOW, "2001:db8:1::/48")
    snap = access_control.snapshot(session)
    assert snap.verdict("2001:db8:2::7") == BLOCK
    assert snap.verdict("2001:db8:1::7") == ALLOW  # the whitelist still wins, in IPv6 too
    assert snap.verdict("2001:db9::1") is None
    assert snap.verdict("203.0.113.9") is None  # a v6 rule never matches a v4 address, nor the reverse
    access_control.add_rule(session, BLOCK, "203.0.113.0/24")
    snap = access_control.snapshot(session)
    assert snap.verdict("203.0.113.9") == BLOCK and snap.verdict("2001:db8:2::7") == BLOCK


def test_an_ipv4_mapped_ipv6_address_is_judged_as_the_ipv4_address_it_wraps(session):
    access_control.add_rule(session, BLOCK, "203.0.113.0/24")
    snap = access_control.snapshot(session)
    assert snap.verdict("::ffff:203.0.113.9") == BLOCK  # writing it that way is no way round a rule
    assert snap.verdict("::ffff:198.51.100.9") is None


def test_you_cannot_blacklist_your_own_ipv6_address_or_the_boxs(session, _isolated_db):
    with pytest.raises(AccessError, match="your own address"):
        access_control.add_rule(session, BLOCK, "2001:db8:abcd::/48", requester=V6)
    _add_box(_isolated_db, address="2001:db8:ffff::1")
    with pytest.raises(AccessError, match="FTP box"):
        access_control.add_rule(session, BLOCK, "2001:db8:ffff::/48")
    access_control.add_rule(session, ALLOW, V6)
    access_control.add_rule(session, BLOCK, "2001:db8:abcd::/48", requester=V6)  # whitelisted: fine


def test_moving_an_ipv6_entry_between_lists(session):
    access_control.add_rule(session, BLOCK, "2001:db8::/32", note="a scanner network")
    moved = access_control.move(session, "2001:db8::/32", ALLOW, created_by="maria")
    assert (moved.kind, moved.cidr) == (ALLOW, "2001:db8::/32")
    assert access_control.snapshot(session).block == ()


def test_a_blacklisted_ipv6_client_gets_the_403_even_behind_the_gateways_bracketed_format(machine, session):
    access_control.add_rule(session, BLOCK, "2001:db8:abcd::/48")
    behind_gateway = {"X-Forwarded-For": "[%s]:5555, 10.0.0.4" % V6}  # how a v6 client appears: bracketed, with a port
    assert machine.get("/health", headers=behind_gateway).status_code == 403
    assert machine.get("/health", headers={"X-Forwarded-For": "[2001:db9::1]:5555, 10.0.0.4"}).status_code == 200


def test_a_whitelisted_ipv6_address_gets_through_a_blocked_range(machine, session):
    access_control.add_rule(session, BLOCK, "2001:db8::/32")
    access_control.add_rule(session, ALLOW, V6)
    assert machine.get("/health", headers={"X-Forwarded-For": "[%s]:5555, 10.0.0.4" % V6}).status_code == 200
    assert machine.get("/health", headers={"X-Forwarded-For": "[2001:db8:abcd::6]:5555, 10.0.0.4"}).status_code == 403


def test_the_box_is_told_about_ipv6_rules_too(session):
    access_control.add_rule(session, BLOCK, "2001:db8::/32")
    access_control.add_rule(session, ALLOW, V6)
    payload = access_control.box_payload(session)
    assert payload["block"] == ["2001:db8::/32"] and payload["allow"] == [V6 + "/128"]


def test_the_page_adds_and_shows_ipv6_entries_without_mangling_a_slash_32_network(admin, session):
    admin.post("/admin/security/rules", data={"kind": "block", "cidr": "2001:db8::/32", "note": "a v6 range"})
    admin.post("/admin/security/rules", data={"kind": "allow", "cidr": V6})
    html = admin.get(PAGE).text
    black = html.split('id="blacklist"', 1)[1].split("</section>", 1)[0]
    white = html.split('id="whitelist"', 1)[1].split("</section>", 1)[0]
    assert "<code>2001:db8::/32</code>" in black  # the /32 is a real prefix here, not a host suffix
    assert "<code>%s/128</code>" % V6 in white
    assert 'name="cidr" value="2001:db8::/32"' in black  # so the menu's actions act on the right entry
    assert "<code>2001:db8::/32</code>" in html.split('id="history"', 1)[1] or "2001:db8::/32" in html.split('id="history"', 1)[1]


@pytest.mark.parametrize("cidr, shown", [
    ("203.0.113.9/32", "203.0.113.9"), ("203.0.113.0/24", "203.0.113.0/24"),
    ("2001:db8::1/128", "2001:db8::1"), ("2001:db8::/32", "2001:db8::/32"), ("2001:db8::/64", "2001:db8::/64"),
    ("garbage", "garbage"),
])
def test_a_host_loses_its_prefix_but_a_range_never_does(cidr, shown):
    assert access_control.host_form(cidr) == shown


def test_the_address_filter_matches_a_whole_ipv6_address_exactly(admin, machine):
    machine.post("/admin/ftp-login-attempts", headers=SYNC_HEADERS, json={"hostname": "box1", "attempts": [
        {"at": int(datetime.datetime.now(datetime.timezone.utc).timestamp()) - 30, "ip": ip, "port": 2222, "username": "root", "code": 530}
        for ip in ("2001:db8::1", "2001:db8::10", "2001:db8::1000")]})
    lines = lambda q: len(admin.get("/admin/security/failed-logins.csv?" + q).text.splitlines()) - 1
    assert lines("address=2001:db8::1") == 1  # that address, not 2001:db8::10 and ::1000 as well
    assert lines("address=2001:0db8:0:0:0:0:0:1") == 1  # however it is spelled
    assert lines("address=2001:db8::") == 3  # a prefix still works


# -- editing a rule -----------------------------------------------------------------------

def test_editing_the_note_keeps_the_expiry_and_the_address(session):
    rule = access_control.add_rule(session, BLOCK, "203.0.113.9", note="old", created_by="kostas", duration="24h")
    expires = rule.expires_at
    got = access_control.update_rule(session, rule.id, note="  a better note \x00 ", who="maria")
    assert (got.note, got.cidr, got.kind, got.expires_at) == ("a better note", "203.0.113.9/32", BLOCK, expires)
    assert _log(session)[0][:2] == ("maria", "changed") and "a better note" in _log(session)[0][4] and "was: old" in _log(session)[0][4]


@pytest.mark.parametrize("duration, hours", [("1h", 1), ("24h", 24), ("7d", 168), ("30d", 720)])
def test_the_expiry_can_be_set_from_now(session, duration, hours):
    rule = access_control.add_rule(session, BLOCK, "203.0.113.9")
    got = access_control.update_rule(session, rule.id, note="", duration=duration)
    assert datetime.timedelta(hours=hours) - datetime.timedelta(minutes=2) < got.expires_at - _now() <= datetime.timedelta(hours=hours, seconds=5)


def test_the_expiry_can_be_removed(session):
    rule = access_control.add_rule(session, BLOCK, "203.0.113.9", duration="1h")
    assert access_control.update_rule(session, rule.id, note="", duration="forever").expires_at is None


def test_an_unknown_duration_is_refused_and_changes_nothing(session):
    rule = access_control.add_rule(session, BLOCK, "203.0.113.9", note="keep me", duration="1h")
    with pytest.raises(AccessError, match="duration"):
        access_control.update_rule(session, rule.id, note="changed", duration="a fortnight")
    session.refresh(rule)
    assert rule.note == "keep me" and len(_log(session)) == 1


def test_editing_a_missing_or_expired_rule_does_nothing(session):
    assert access_control.update_rule(session, "no-such-rule", note="x") is None
    rule = access_control.add_rule(session, BLOCK, "203.0.113.9", duration="1h")
    rule.expires_at = _now() - datetime.timedelta(seconds=1)
    session.commit()
    assert access_control.update_rule(session, rule.id, note="x") is None


def test_extending_a_temporary_block_takes_effect_on_the_site(machine, admin, session):
    rule = access_control.add_rule(session, BLOCK, "203.0.113.9", duration="1h")
    admin.post("/admin/security/rules/%s/edit" % rule.id, data={"note": "", "duration": "forever"})
    session.refresh(rule)
    assert rule.expires_at is None
    assert machine.get("/health", headers=_from("203.0.113.9")).status_code == 403


def test_the_edit_page_shows_the_current_values_and_the_menu_links_to_it(admin, session):
    rule = access_control.add_rule(session, BLOCK, "203.0.113.9", note='say "hi" <b>', duration="7d")
    menu = admin.get(PAGE).text.split('id="blacklist"', 1)[1].split("</section>", 1)[0]
    assert 'href="/admin/security/rules/%s/edit"' % rule.id in menu and "Edit note / expiry" in menu
    page = admin.get("/admin/security/rules/%s/edit" % rule.id).text
    assert "203.0.113.9" in page and "blacklist" in page and "Keep as it is (until " in page
    assert 'value="say &#34;hi&#34; &lt;b&gt;"' in page and "<b>" not in page.split("<h1>", 1)[1]  # escaped, quotes included


def test_saving_the_edit_page_changes_the_rule_and_says_so(admin, session):
    rule = access_control.add_rule(session, ALLOW, "203.0.113.9", note="old")
    resp = admin.post("/admin/security/rules/%s/edit" % rule.id, data={"note": "the office", "duration": "keep"}, follow_redirects=False)
    assert resp.status_code == 303 and "Updated+203.0.113.9+on+the+whitelist" in resp.headers["location"]
    session.refresh(rule)
    assert rule.note == "the office" and rule.created_by == ""  # who created it is not rewritten
    assert _log(session)[0][:2] == ("test-admin", "changed")


def test_a_bad_edit_is_refused_with_a_message(admin, session):
    rule = access_control.add_rule(session, ALLOW, "203.0.113.9", note="old")
    bad = admin.post("/admin/security/rules/%s/edit" % rule.id, data={"note": "new", "duration": "never-ever"}, follow_redirects=False)
    assert "flash_kind=error" in bad.headers["location"]
    gone = admin.post("/admin/security/rules/nope/edit", data={"note": "x"}, follow_redirects=False)
    assert "flash_kind=error" in gone.headers["location"]
    assert "flash_kind=error" in admin.get("/admin/security/rules/nope/edit", follow_redirects=False).headers["location"]
    session.refresh(rule)
    assert rule.note == "old"


def test_the_edit_routes_need_the_login(machine):
    for method, path in (("get", "/admin/security/rules/x/edit"), ("post", "/admin/security/rules/x/edit")):
        resp = getattr(machine, method)(path, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login"), path


# -- the history of changes -----------------------------------------------------------------

def _log(session):
    return [(h.who, h.action, h.kind, h.cidr, h.detail) for h in access_control.history(session, 50)]


def test_adding_a_rule_is_recorded_with_who_and_what(session):
    access_control.add_rule(session, BLOCK, "203.0.113.9", note="scanner", created_by="kostas", duration="24h")
    (who, action, kind, cidr, detail), = _log(session)
    assert (who, action, kind, cidr) == ("kostas", "added", "block", "203.0.113.9/32")
    assert "scanner" in detail and "until " in detail
    access_control.add_rule(session, ALLOW, "198.51.100.7", created_by="kostas")
    assert _log(session)[0][4] == "no expiry"  # newest first


def test_a_rule_refused_leaves_no_trace_in_the_history(session):
    for bad in ("not-an-ip", "10.0.0.0/4"):
        with pytest.raises(AccessError):
            access_control.add_rule(session, BLOCK, bad, created_by="kostas")
    assert _log(session) == []


def test_removing_is_recorded_and_so_is_who_did_it(session):
    rule = access_control.add_rule(session, BLOCK, "203.0.113.9", created_by="kostas")
    access_control.remove_rule(session, rule.id, who="maria")
    assert [(w, a) for w, a, *_ in _log(session)] == [("maria", "removed"), ("kostas", "added")]


def test_a_move_is_one_entry_not_a_removal_plus_an_addition(session):
    access_control.add_rule(session, BLOCK, "203.0.113.9", note="was a scanner", created_by="kostas")
    access_control.move(session, "203.0.113.9", ALLOW, created_by="maria")
    newest = _log(session)[0]
    assert (newest[0], newest[1], newest[2]) == ("maria", "moved", "allow") and "from the blacklist" in newest[4]
    assert [a for _, a, *_ in _log(session)] == ["moved", "added"]  # exactly one new entry


def test_moving_off_both_lists_is_a_removal(session):
    access_control.add_rule(session, ALLOW, "203.0.113.9", created_by="kostas")
    access_control.move(session, "203.0.113.9", "none", created_by="maria")
    assert _log(session)[0][:2] == ("maria", "removed")


def test_a_refused_move_records_nothing(session):
    access_control.add_rule(session, ALLOW, "203.0.113.50", created_by="kostas")
    with pytest.raises(AccessError):
        access_control.move(session, "203.0.113.50", BLOCK, created_by="maria", requester="203.0.113.50")
    assert len(_log(session)) == 1  # just the original "added"


def test_automatic_bans_their_lifting_and_their_conversion_are_recorded(machine, admin, session):
    for _ in range(10):
        machine.post("/admin/login", data={"username": "root", "password": "nope"}, headers=_from("8.8.4.4"))
    assert _log(session)[0][:2] == ("auto-ban", "banned") and "10 failed logins" in _log(session)[0][4]
    rule_id = session.scalar(select(AccessRule.id))
    admin.post("/admin/security/rules/%s/delete" % rule_id)
    assert _log(session)[0][:2] == ("test-admin", "lifted")


def test_converting_an_automatic_ban_is_recorded(machine, admin, session):
    for _ in range(10):
        machine.post("/admin/login", data={"username": "root", "password": "nope"}, headers=_from("8.8.4.4"))
    admin.post("/admin/security/rules/move", data={"cidr": "8.8.4.4", "to": "block"})
    assert _log(session)[0][:2] == ("test-admin", "converted")


def test_the_history_is_newest_first_limited_and_old_entries_are_pruned(session):
    for i in range(1, 21):
        access_control.add_rule(session, BLOCK, "203.0.113.%d" % i, created_by="kostas")
    assert len(access_control.history(session)) == 15 and access_control.history(session)[0].cidr == "203.0.113.20/32"
    from providerharvest_service.models import AccessLog

    session.add(AccessLog(who="x", action="added", kind="block", cidr="1.2.3.4/32",
                          at=_now() - access_control.AUDIT_RETENTION - datetime.timedelta(days=1)))
    session.commit()
    access_control.add_rule(session, BLOCK, "203.0.113.99", created_by="kostas")  # writing prunes
    assert "1.2.3.4/32" not in [h.cidr for h in access_control.history(session, 100)]


def test_the_page_shows_recent_changes_with_who_and_what_and_escapes_them(admin, session):
    admin.post("/admin/security/rules", data={"kind": "block", "cidr": "203.0.113.9", "note": "<script>x</script>"})
    html = admin.get(PAGE).text
    section = html.split('id="history"', 1)[1].split("</details>", 1)[0]
    assert "test-admin" in section and "added <code>203.0.113.9</code> to the blacklist" in section
    assert "<script>x</script>" not in section and "&lt;script&gt;" in section
    assert 'Recent changes <span class="count">1</span>' in html


def test_the_page_says_so_when_there_is_no_history(admin):
    assert "No changes yet." in admin.get(PAGE).text


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# -- blocking an address a push source accepts data from ---------------------------------

def _source(factory, name="acme-push", allowed="", transport="push", title=None, **extra):
    from providerharvest_service import provider_sources

    form = {"name": name, "title": title or name.title(), "transport_type": transport, "allowed_senders": allowed}
    if transport != "push":
        form.update({"endpoint_url": "https://provider.example.gr/api", "frequency": "DAILY", "auth_type": "api_key", "api_key": "k"})
    form.update(extra)
    with factory() as s:
        return provider_sources.register(s, form).id


def test_a_block_that_overlaps_a_sources_allowed_sender_names_that_source(session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9")
    _source(_isolated_db, "other-push", "198.51.100.0/24")
    got = access_control.affected_sources(session, "203.0.113.0/24")
    assert [(s.name, s.entries) for s in got] == [("acme-push", ("203.0.113.9/32",))]


def test_overlap_works_both_ways_and_across_several_sources(session, _isolated_db):
    _source(_isolated_db, "wide-source", "203.0.113.0/24")
    _source(_isolated_db, "narrow-source", "203.0.113.9, 203.0.113.77")
    got = access_control.affected_sources(session, "203.0.113.9")  # inside the wide one, equal to one of the narrow ones
    assert [s.name for s in got] == ["narrow-source", "wide-source"]
    assert dict((s.name, s.entries) for s in got)["narrow-source"] == ("203.0.113.9/32",)


def test_sources_with_no_allowed_senders_other_ranges_or_other_kinds_are_not_affected(session, _isolated_db):
    _source(_isolated_db, "open-push", "")  # accepts from anywhere: nothing to cut off
    _source(_isolated_db, "far-push", "192.0.2.0/24")
    _source(_isolated_db, "pull-source", "", transport="http")
    assert access_control.affected_sources(session, "203.0.113.0/24") == []


def test_an_allowed_sender_wholly_inside_a_whitelisted_range_is_not_affected(session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9")
    access_control.add_rule(session, ALLOW, "203.0.113.0/24")
    assert access_control.affected_sources(session, "203.0.0.0/16") == []  # the whitelist wins: it stays in


def test_every_status_counts_a_pending_source_will_be_approved_later(session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9")  # registered: pending
    assert access_control.affected_sources(session, "203.0.113.9")[0].status == "pending"


def _admin_account(factory, username="legacy-account", allowed=("203.0.113.9",), enabled=True):
    from providerharvest_service.models import FtpAccount, FtpAllowedIp

    with factory() as s:
        account = FtpAccount(username=username, password_hash="$2a$x", home_directory="/srv/ftp/" + username,
                             protocol="sftp", enabled=enabled)
        account.allowed_ips = [FtpAllowedIp(cidr=c + "/32" if "/" not in c else c) for c in allowed]
        s.add(account)
        s.commit()


def test_a_block_that_overlaps_an_admin_made_accounts_allowed_address_names_that_account(session, _isolated_db):
    _admin_account(_isolated_db, "legacy-account", ("203.0.113.9", "198.51.100.0/24"))
    got = access_control.affected_accounts(session, "203.0.113.0/24")
    assert [(a.username, a.enabled, a.entries) for a in got] == [("legacy-account", True, ("203.0.113.9/32",))]
    assert access_control.affected_accounts(session, "192.0.2.0/24") == []


def test_a_providers_own_account_is_not_listed_twice(session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9", transport="push", push_method="ftp")  # its account mirrors the source
    assert [s.name for s in access_control.affected_sources(session, "203.0.113.9")] == ["acme-push"]
    assert access_control.affected_accounts(session, "203.0.113.9") == []


def test_accounts_without_allowed_addresses_or_wholly_inside_the_whitelist_are_not_affected(session, _isolated_db):
    _admin_account(_isolated_db, "open-account", ())
    _admin_account(_isolated_db, "trusted-account", ("203.0.113.9",))
    access_control.add_rule(session, ALLOW, "203.0.113.0/24")
    assert access_control.affected_accounts(session, "203.0.0.0/16") == []


def test_blacklisting_an_admin_made_accounts_address_asks_first_and_names_it(admin, session, _isolated_db):
    _admin_account(_isolated_db, "legacy-account", ("203.0.113.9",), enabled=False)
    resp = _post_block(admin)
    assert resp.status_code == 200 and "would cut off an FTP account" in resp.text
    assert "legacy-account" in resp.text and "<code>203.0.113.9/32</code>" in resp.text and "disabled" in resp.text
    assert session.scalar(select(AccessRule)) is None
    assert _post_block(admin, confirm="1").status_code == 303 and session.scalar(select(AccessRule)) is not None


def test_sources_and_accounts_together_are_counted_together(admin, session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9")
    _admin_account(_isolated_db, "legacy-account", ("203.0.113.77",))
    text = _post_block(admin).text
    assert "would cut off 2 providers and accounts" in text and "acme-push" in text and "legacy-account" in text


def test_two_accounts_say_so_and_the_move_route_asks_too(admin, session, _isolated_db):
    _admin_account(_isolated_db, "first-account", ("203.0.113.9",))
    _admin_account(_isolated_db, "second-account", ("203.0.113.20",))
    assert "would cut off 2 FTP accounts" in _post_block(admin).text
    resp = admin.post("/admin/security/rules/move", data={"cidr": "203.0.113.0/24", "to": "block"}, follow_redirects=False)
    assert resp.status_code == 200 and 'action="/admin/security/rules/move"' in resp.text


def _post_block(admin, **extra):
    data = {"kind": "block", "cidr": "203.0.113.0/24"}
    data.update(extra)
    return admin.post("/admin/security/rules", data=data, follow_redirects=False)


def test_blacklisting_an_allowed_sender_asks_first_and_saves_nothing(admin, session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9", title="Acme Ltd")
    resp = _post_block(admin, note="scanner", duration="24h")
    assert resp.status_code == 200  # a page, not a redirect
    assert "would cut off a provider" in resp.text and "Acme Ltd" in resp.text and "acme-push" in resp.text
    assert "<code>203.0.113.9/32</code>" in resp.text
    assert session.scalar(select(AccessRule)) is None  # nothing was saved
    # The confirm form carries everything that was typed, plus the confirmation.
    assert 'name="cidr" value="203.0.113.0/24"' in resp.text and 'name="note" value="scanner"' in resp.text
    assert 'name="duration" value="24h"' in resp.text and 'name="confirm" value="1"' in resp.text
    assert 'action="/admin/security/rules"' in resp.text


def test_confirming_saves_the_block_with_the_original_note_and_duration(admin, session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9")
    resp = _post_block(admin, note="scanner", duration="24h", confirm="1")
    assert resp.status_code == 303 and "flash_kind=error" not in resp.headers["location"]
    rule = session.scalar(select(AccessRule))
    assert (rule.kind, rule.cidr, rule.note) == ("block", "203.0.113.0/24", "scanner") and rule.expires_at is not None


def test_a_block_that_affects_nobody_is_saved_straight_away(admin, session, _isolated_db):
    _source(_isolated_db, "acme-push", "198.51.100.9")
    assert _post_block(admin).status_code == 303
    assert session.scalar(select(AccessRule)) is not None


def test_the_whitelist_is_never_asked_about(admin, session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9")
    resp = admin.post("/admin/security/rules", data={"kind": "allow", "cidr": "203.0.113.0/24"}, follow_redirects=False)
    assert resp.status_code == 303 and session.scalar(select(AccessRule)).kind == "allow"


def test_several_affected_sources_are_all_listed(admin, session, _isolated_db):
    _source(_isolated_db, "first-push", "203.0.113.9")
    _source(_isolated_db, "second-push", "203.0.113.200")
    text = _post_block(admin).text
    assert "would cut off 2 providers" in text and "first-push" in text and "second-push" in text


def test_moving_a_whitelisted_provider_to_the_blacklist_asks_because_the_move_takes_its_protection_away(admin, session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9")
    access_control.add_rule(session, ALLOW, "203.0.113.9")
    # Whitelisted, the address would stay in even if blocked -- but the move removes that whitelist entry.
    assert [s.name for s in access_control.affected_sources(session, "203.0.113.9")] == ["acme-push"]
    resp = admin.post("/admin/security/rules/move", data={"cidr": "203.0.113.9", "to": "block"}, follow_redirects=False)
    assert resp.status_code == 200 and "acme-push" in resp.text
    assert [(r.kind, r.cidr) for r in access_control.rules(session)] == [("allow", "203.0.113.9/32")]  # untouched so far

    done = admin.post("/admin/security/rules/move", data={"cidr": "203.0.113.9", "to": "block", "confirm": "1"}, follow_redirects=False)
    assert done.status_code == 303
    assert [(r.kind, r.cidr) for r in access_control.rules(session)] == [("block", "203.0.113.9/32")]


def test_a_wider_whitelist_range_still_protects_a_provider_so_blocking_inside_it_asks_nothing(admin, session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9")
    access_control.add_rule(session, ALLOW, "203.0.113.0/24")  # a different, wider entry: it stays
    assert _post_block(admin, cidr="203.0.113.9").status_code == 303  # saved without a question


def test_the_confirmation_for_a_move_goes_back_to_the_move_route(admin, session, _isolated_db):
    _source(_isolated_db, "acme-push", "198.51.100.9")
    resp = admin.post("/admin/security/rules/move", data={"cidr": "198.51.100.0/24", "to": "block"}, follow_redirects=False)
    assert resp.status_code == 200 and 'action="/admin/security/rules/move"' in resp.text
    assert 'name="to" value="block"' in resp.text and session.scalar(select(AccessRule)) is None
    done = admin.post("/admin/security/rules/move", data={"cidr": "198.51.100.0/24", "to": "block", "confirm": "1"}, follow_redirects=False)
    assert done.status_code == 303 and session.scalar(select(AccessRule)).cidr == "198.51.100.0/24"


def test_an_invalid_entry_is_not_asked_about_it_just_gets_the_normal_error(admin, session, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9")
    resp = _post_block(admin, cidr="not-an-ip")
    assert resp.status_code == 303 and "flash_kind=error" in resp.headers["location"]


def test_the_confirmation_page_escapes_what_it_shows_and_cancel_returns_to_the_page(admin, _isolated_db):
    _source(_isolated_db, "acme-push", "203.0.113.9", title="<script>alert(1)</script>")
    resp = admin.post("/admin/security/rules", data={"kind": "block", "cidr": "203.0.113.9", "note": '"><script>x</script>'},
                      headers={"referer": "https://loader.data.gov.gr%s?view=attempts" % PAGE}, follow_redirects=False)
    assert "<script>alert(1)</script>" not in resp.text and "&lt;script&gt;" in resp.text
    assert '"><script>x</script>' not in resp.text
    assert 'href="%s?view=attempts"' % PAGE in resp.text  # Cancel goes back to where you were


def test_the_confirmation_needs_the_login(machine):
    resp = machine.post("/admin/security/rules", data={"kind": "block", "cidr": "203.0.113.0/24"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login")


# -- enforcement on loader ---------------------------------------------------------------

def test_a_blacklisted_address_is_refused_everywhere_before_anything_else(machine, session):
    access_control.add_rule(session, BLOCK, "203.0.113.0/24")
    for path in ("/health", "/admin/login", "/provider/sources", "/data/anything", "/push/x/upload", "/admin/ftp-box-status"):
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
    assert "<code>203.0.113.9/32</code>" in blacklist and "port scanner" in blacklist and "test-admin" in blacklist


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


def test_long_lists_show_the_first_few_and_fold_the_rest_behind_show_more(admin, session, _isolated_db):
    ips = ["198.51.100.%d" % i for i in range(1, 21)]
    _add_box(_isolated_db, banned=ips)
    for i in range(1, 9):
        access_control.add_rule(session, BLOCK, "203.0.113.%d" % i)
    html = admin.get(PAGE).text
    banned = html.split('id="banned-list"', 1)[1].split("</section>", 1)[0]
    visible, folded = banned.split('<details class="more">', 1)
    assert visible.count("<code>") == 6 and "Show 14 more" in folded and folded.count("<code>") == 14  # all 20 are there
    black = html.split('id="blacklist"', 1)[1].split("</section>", 1)[0]
    assert "Show 2 more" in black and black.count("<code>") == 8


def test_each_entry_has_one_actions_menu_not_a_row_of_buttons(admin, session, _isolated_db):
    _add_box(_isolated_db, banned=["198.51.100.4"])
    access_control.add_rule(session, BLOCK, "203.0.113.9")
    html = admin.get(PAGE).text
    banned = html.split('id="banned-list"', 1)[1].split("</section>", 1)[0]
    assert banned.count('class="actions-menu"') == 1 and "Move to blacklist" in banned and "Move to whitelist" in banned
    black = html.split('id="blacklist"', 1)[1].split("</section>", 1)[0]
    assert black.count('class="actions-menu"') == 1 and "Move to whitelist" in black and "Remove from the blacklist" in black


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
