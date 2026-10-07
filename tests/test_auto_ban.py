"""Automatic bans on loader's HTTP side: what triggers one, everything that must NOT
(wrongly banning someone is the failure that matters), and how an admin deals with one."""

import datetime

import pytest
from sqlalchemy import select

from providerharvest_service import access_control, auto_ban, failed_auth
from providerharvest_service.access_control import ALLOW, BLOCK
from providerharvest_service.models import AccessRule, FtpLoginAttempt
from tests.test_access_control import (  # noqa: F401  (fixtures shared with the access-control tests)
    PAGE, _add_box, _env, _from, _isolated_db, _master_key, _rules, admin, machine, session,
)

SCANNER = "8.8.4.4"  # a public address (the documentation ranges, 203.0.113.0/24 etc., are not "global")
WEB = failed_auth.WEB_LOGIN


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _fail(session, ip, n, channel=WEB, minutes_ago=0.0, code=401, hostname="loader"):
    for i in range(n):
        session.add(FtpLoginAttempt(
            hostname=hostname, at=_now() - datetime.timedelta(minutes=minutes_ago, seconds=i), ip=ip, port=443,
            username="x", code=code, channel=channel,
        ))
    session.commit()


def _consider(session, ip=SCANNER):
    return auto_ban.consider(session, ip, failed_auth.AUTO_BAN_CHANNELS)


# -- when it bans -------------------------------------------------------------------------

def test_nine_failures_in_ten_minutes_do_not_ban_but_ten_do_for_an_hour(session):
    _fail(session, SCANNER, 9)
    assert _consider(session) is None
    _fail(session, SCANNER, 1)
    rule = _consider(session)
    assert (rule.kind, rule.cidr, rule.created_by) == (BLOCK, SCANNER + "/32", "auto-ban")
    assert "10 failed logins or keys in 10 minutes" in rule.note
    assert datetime.timedelta(minutes=58) < rule.expires_at - _now() <= datetime.timedelta(hours=1, seconds=5)


def test_thirty_failures_in_a_day_ban_for_a_day_even_if_never_ten_in_ten_minutes(session):
    for hour in range(1, 7):  # 6 bursts of 5, hours apart: never 10 in 10 minutes
        _fail(session, SCANNER, 5, minutes_ago=hour * 60)
    assert _consider(session) is not None  # 30 in 24 hours
    rule = session.scalar(select(AccessRule))
    assert "30 failed logins or keys in 24 hours" in rule.note
    assert datetime.timedelta(hours=23, minutes=55) < rule.expires_at - _now() <= datetime.timedelta(hours=24, seconds=5)


def test_failures_outside_the_window_do_not_count(session):
    _fail(session, SCANNER, 10, minutes_ago=30)  # ten, but half an hour ago
    assert _consider(session) is None


def test_every_counted_credential_counts(session):
    for channel in failed_auth.AUTO_BAN_CHANNELS:
        assert _consider(session) is None
        _fail(session, SCANNER, 2, channel=channel)
    assert _consider(session) is not None  # 10 across the channels together


def test_push_key_failures_are_not_counted_so_a_provider_with_a_stale_key_is_not_banned(session):
    assert failed_auth.PUSH_KEY not in failed_auth.AUTO_BAN_CHANNELS and failed_auth.WEB_LOGIN in failed_auth.AUTO_BAN_CHANNELS
    _fail(session, SCANNER, 60, channel=failed_auth.PUSH_KEY)
    assert _consider(session) is None


@pytest.mark.parametrize("code", [403, 404, 200])
def test_only_authentication_failures_count(session, code):
    _fail(session, SCANNER, 60, code=code)
    assert _consider(session) is None


def test_other_addresses_and_the_ftp_boxs_own_rows_do_not_count(session):
    _fail(session, "1.1.1.1", 9)
    _fail(session, SCANNER, 9, hostname="snf-box")  # a row the FTP box reported, not one loader recorded
    _fail(session, SCANNER, 1)
    assert _consider(session) is None


# -- who it never bans --------------------------------------------------------------------

@pytest.mark.parametrize("ip", ["10.1.2.3", "192.168.0.7", "172.16.5.5", "127.0.0.1", "169.254.1.1", "203.0.113.9", "198.51.100.7", "0.0.0.0"])
def test_non_public_addresses_are_never_banned(session, ip):
    _fail(session, ip, 100)
    assert _consider(session, ip) is None


@pytest.mark.parametrize("ip", ["not-an-ip", "", "8.8.4", "::1", "fe80::1", "fd00::5", "2001:db8::1", "::ffff:8.8.4.4"])
def test_garbage_and_non_public_ipv6_are_never_banned(session, ip):
    if ip:
        _fail(session, ip, 100)
    assert auto_ban.consider(session, ip, failed_auth.AUTO_BAN_CHANNELS) is None


def test_a_public_ipv6_address_is_banned_as_its_whole_slash_64(session):
    ip = "2001:4860:4860::8888"
    _fail(session, ip, 10)
    rule = _consider(session, ip)
    assert (rule.kind, rule.cidr, rule.created_by) == (BLOCK, "2001:4860:4860::/64", "auto-ban")
    assert access_control.snapshot(session).verdict("2001:4860:4860:0:1:2:3:4") == BLOCK  # a neighbour in the /64 too
    assert access_control.snapshot(session).verdict("2001:4860:4861::1") is None


def test_failures_from_different_addresses_of_one_slash_64_add_up(session):
    for i in range(10):  # hopping between addresses, written in different ways
        _fail(session, ["2001:4860:4860::%x" % (i + 1), "2001:4860:4860:0:0:0:0:%X" % (i + 1)][i % 2], 1)
    rule = _consider(session, "2001:4860:4860::99")
    assert rule is not None and rule.cidr == "2001:4860:4860::/64"
    assert "10 failed logins or keys in 10 minutes" in rule.note


def test_another_slash_64_and_ipv4_failures_do_not_count_toward_an_ipv6_ban(session):
    _fail(session, "2001:4860:4861::1", 9)
    _fail(session, SCANNER, 9)
    _fail(session, "2001:4860:4860::1", 1)
    assert _consider(session, "2001:4860:4860::1") is None


def test_a_whitelisted_ipv6_address_is_never_banned(session):
    access_control.add_rule(session, ALLOW, "2001:4860:4860::8888")
    _fail(session, "2001:4860:4860::8888", 100)
    assert _consider(session, "2001:4860:4860::8888") is None


def test_a_whitelisted_address_is_never_banned_however_often_it_fails(session):
    access_control.add_rule(session, ALLOW, SCANNER)
    _fail(session, SCANNER, 100)
    assert _consider(session) is None
    assert [(r.kind) for r in access_control.rules(session)] == ["allow"]


def test_an_address_inside_a_whitelisted_range_is_never_banned(session):
    access_control.add_rule(session, ALLOW, "8.8.4.0/24")
    _fail(session, SCANNER, 100)
    assert _consider(session) is None


def test_an_already_blocked_address_is_not_banned_again(session):
    access_control.add_rule(session, BLOCK, SCANNER)
    _fail(session, SCANNER, 100)
    assert _consider(session) is None
    assert session.scalar(select(AccessRule.created_by)) == ""  # still the manual entry, not replaced


def test_the_ftp_boxs_address_is_never_banned_so_a_wrong_sync_key_stays_recoverable(session, _isolated_db):
    _add_box(_isolated_db, address=SCANNER)
    _fail(session, SCANNER, 100, channel=failed_auth.BOX_KEY)
    assert _consider(session) is None


def test_the_kill_switch_turns_it_off(session, monkeypatch):
    _fail(session, SCANNER, 100)
    for value in ("off", "OFF", "0", "false", "no"):
        monkeypatch.setenv("LOADER_HTTP_AUTOBAN", value)
        assert _consider(session) is None, value
    monkeypatch.setenv("LOADER_HTTP_AUTOBAN", "on")
    assert _consider(session) is not None


def test_two_workers_banning_at_once_make_one_ban(session):
    _fail(session, SCANNER, 10)
    first = _consider(session)
    second = _consider(session)  # the address is now blocked: nothing more to do
    assert first is not None and second is None
    assert len(session.scalars(select(AccessRule)).all()) == 1


# -- end to end ---------------------------------------------------------------------------

def _wrong_login(machine, ip=SCANNER):
    return machine.post("/admin/login", data={"username": "root", "password": "nope"}, headers=_from(ip), follow_redirects=False)


def test_ten_wrong_web_logins_get_the_address_refused_everywhere(machine):
    for _ in range(9):
        assert _wrong_login(machine).status_code == 303  # an ordinary "wrong password"
    assert machine.get("/health", headers=_from(SCANNER)).status_code == 200  # not yet
    _wrong_login(machine)  # the tenth
    for path in ("/health", "/admin/login", "/provider/sources", "/me"):
        resp = machine.get(path, headers=_from(SCANNER))
        assert resp.status_code == 403 and resp.text == "Forbidden", path
    assert machine.get("/health", headers=_from("1.1.1.1")).status_code == 200  # everyone else is fine


def test_wrong_bearer_and_sync_keys_count_too(machine):
    for _ in range(10):
        machine.get("/me", headers=_from(SCANNER))  # no credentials: a 401 each
    assert machine.get("/health", headers=_from(SCANNER)).status_code == 403


def test_a_whitelisted_address_can_fail_forever_and_still_log_in(machine, admin, session):
    admin.post("/admin/security/rules", data={"kind": "allow", "cidr": "1.1.1.1"})
    for _ in range(25):
        _wrong_login(machine, "1.1.1.1")
    assert machine.get("/health", headers=_from("1.1.1.1")).status_code == 200
    assert not [r for r in access_control.rules(session) if r.kind == BLOCK]


def test_the_kill_switch_stops_banning_for_real_requests(machine, monkeypatch):
    monkeypatch.setenv("LOADER_HTTP_AUTOBAN", "off")
    for _ in range(25):
        _wrong_login(machine)
    assert machine.get("/health", headers=_from(SCANNER)).status_code == 200


def test_an_automatic_ban_reaches_the_ftp_boxs_rules(machine, session):
    for _ in range(10):
        _wrong_login(machine)
    assert SCANNER + "/32" in access_control.box_payload(session)["block"]


# -- the Security box ----------------------------------------------------------------------

def _ban(machine):
    for _ in range(10):
        _wrong_login(machine)


def test_an_automatic_ban_is_listed_under_banned_now_not_in_the_blacklist(admin, machine):
    _ban(machine)
    html = admin.get(PAGE).text
    banned = html.split('id="banned-list"', 1)[1].split("</section>", 1)[0]
    assert "loader (web)" in banned and "<code>%s</code>" % SCANNER in banned and "10 failed logins or keys in 10 minutes" in banned
    black = html.split('id="blacklist"', 1)[1].split("</section>", 1)[0]
    assert SCANNER not in black and "Nothing blacklisted." in black
    assert 'Banned now <span class="count">1</span>' in html


def test_its_menu_offers_unban_and_the_two_moves(admin, machine):
    _ban(machine)
    banned = admin.get(PAGE).text.split('id="banned-list"', 1)[1].split("</section>", 1)[0]
    assert banned.count('class="actions-menu"') == 1
    assert "Unban" in banned and "Move to blacklist" in banned and "Move to whitelist" in banned


def test_unbanning_lets_the_address_back_in_at_once(admin, machine, session):
    _ban(machine)
    rule_id = session.scalar(select(AccessRule.id))
    resp = admin.post("/admin/security/rules/%s/delete" % rule_id, follow_redirects=False)
    assert "Lifted+the+automatic+ban" in resp.headers["location"] and "flash_kind=error" not in resp.headers["location"]
    assert machine.get("/health", headers=_from(SCANNER)).status_code == 200


def test_moving_an_automatic_ban_to_the_blacklist_makes_it_permanent_in_place(admin, machine, session, _isolated_db):
    _ban(machine)
    resp = admin.post("/admin/security/rules/move", data={"cidr": SCANNER, "to": "block"}, follow_redirects=False)
    assert resp.status_code == 303 and "flash_kind=error" not in resp.headers["location"]
    (rule,) = session.scalars(select(AccessRule)).all()
    session.refresh(rule)
    assert rule.created_by == "test-admin" and rule.expires_at is None and rule.note.startswith("was an automatic ban")
    html = admin.get(PAGE).text
    assert "<code>%s/32</code>" % SCANNER in html.split('id="blacklist"', 1)[1].split("</section>", 1)[0]
    assert 'Banned now <span class="count">0</span>' in html  # no longer an automatic ban


def test_moving_an_automatic_ban_to_the_whitelist_trusts_the_address(admin, machine, session, _isolated_db):
    _ban(machine)
    admin.post("/admin/security/rules/move", data={"cidr": SCANNER, "to": "allow"})
    assert _rules(_isolated_db) == [("allow", SCANNER + "/32")]
    assert machine.get("/health", headers=_from(SCANNER)).status_code == 200
    for _ in range(30):
        _wrong_login(machine)  # and it is not banned again
    assert machine.get("/health", headers=_from(SCANNER)).status_code == 200


def test_a_permanent_blacklist_entry_is_not_treated_as_an_automatic_ban(admin, machine, session):
    admin.post("/admin/security/rules", data={"kind": "block", "cidr": "9.9.9.9"})
    html = admin.get(PAGE).text
    assert "loader (web)" not in html
    assert "<code>9.9.9.9/32</code>" in html.split('id="blacklist"', 1)[1].split("</section>", 1)[0]


def test_an_expired_automatic_ban_is_gone(admin, machine, session):
    _ban(machine)
    rule = session.scalar(select(AccessRule))
    rule.expires_at = _now() - datetime.timedelta(seconds=1)
    session.commit()
    access_control.invalidate()
    assert machine.get("/health", headers=_from(SCANNER)).status_code == 200
    assert "loader (web)" not in admin.get(PAGE).text


def test_banned_addresses_filter_and_badge_include_the_automatic_ones(admin, machine, session):
    for _ in range(10):
        _wrong_login(machine)  # recorded as web-login failures from the scanner
    resp = admin.get(PAGE + "?banned=1&view=attempts")
    assert SCANNER in resp.text  # "Banned now only" covers loader's own bans too
