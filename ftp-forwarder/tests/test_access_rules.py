import subprocess

import pytest

from ftp_forwarder import access_rules
from ftp_forwarder.access_rules import Access, AccessError, Enforcer, parse, render_fail2ban

JAILS = ("loader-ftp", "loader-ftp-slow", "sshd")


def _done(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class FakeSystem:
    """Stands in for iptables and fail2ban-client: remembers the commands, answers like the real thing."""

    def __init__(self, banned=(), chain_exists=False, jump_exists=False, fail=None):
        self.calls = []
        self.banned = list(banned)
        self.chain_exists = chain_exists
        self.jump_exists = jump_exists
        self.fail = fail  # a substring of a command to make fail

    def __call__(self, cmd):
        self.calls.append(cmd)
        text = " ".join(cmd)
        if self.fail and self.fail in text:
            return _done(1, stderr="boom")
        if cmd[:2] == ["iptables", "-S"]:
            return _done(0 if self.chain_exists else 1)
        if cmd[:2] == ["iptables", "-N"]:
            self.chain_exists = True
        if cmd[:3] == ["iptables", "-C", "INPUT"]:
            return _done(0 if self.jump_exists else 1)
        if cmd[:3] == ["iptables", "-I", "INPUT"]:
            self.jump_exists = True
        if cmd[0] == "fail2ban-client" and cmd[1] == "status":
            return _done(0, stdout="`- Actions\n   `- Banned IP list:\t%s\n" % " ".join(self.banned))
        if cmd[0] == "fail2ban-client" and cmd[1] == "set" and cmd[3] == "unbanip":
            if cmd[4] in self.banned:
                self.banned.remove(cmd[4])
            return _done(0)
        return _done(0)

    def iptables(self):
        return [c for c in self.calls if c[0] == "iptables"]

    def rules(self):
        return [c[3:] for c in self.calls if c[:2] == ["iptables", "-A"]]


def _enforcer(tmp_path, system):
    return Enforcer(jails=JAILS, f2b_conf=str(tmp_path / "access.local"), chain="LOADER-ACCESS", run=system)


# -- parsing what loader sent ----------------------------------------------------------

def test_a_well_formed_set_of_rules_is_parsed_and_canonicalised():
    got = parse({"version": "abc123", "allow": ["198.51.100.7", "198.51.100.7/32"], "block": ["203.0.113.0/24"]})
    assert got == Access("abc123", allow=("198.51.100.7/32",), block=("203.0.113.0/24",))


def test_no_access_object_means_nothing_to_do():
    assert parse(None) is None


@pytest.mark.parametrize("bad", [
    "text", [], {"allow": [], "block": []}, {"version": "", "allow": []}, {"version": "x y", "allow": []},
    {"version": "v", "allow": "1.2.3.4"}, {"version": "v", "block": [None]},
    {"version": "v", "block": ["2001:db8::/32"]}, {"version": "v", "block": ["0.0.0.0/0"]}, {"version": "v", "block": ["10.0.0.0/7"]},
    {"version": "v", "allow": ["127.0.0.1"]}, {"version": "v", "allow": ["127.0.0.0/8"]},
    {"version": "v", "block": ["1.2.3.4; reboot"]}, {"version": "v", "block": ["1.2.3.4\nDROP"]},
    {"version": "v", "block": ["203.0.113.1", "not-an-ip"]},  # one bad entry refuses the whole set
])
def test_anything_malformed_or_dangerous_refuses_the_whole_set(bad):
    with pytest.raises(AccessError):
        parse(bad)


def test_the_size_is_capped():
    entries = ["10.%d.%d.1" % (i // 250, i % 250) for i in range(access_rules.MAX_ENTRIES + 1)]
    with pytest.raises(AccessError, match="more than"):
        parse({"version": "v", "block": entries})


# -- the firewall ---------------------------------------------------------------------

def test_the_chain_is_created_hooked_in_first_and_filled_trusted_before_blocked(tmp_path):
    system = FakeSystem()
    assert _enforcer(tmp_path, system).apply(Access("v1", allow=("198.51.100.7/32",), block=("203.0.113.0/24", "192.0.2.9/32"))) is True
    commands = system.iptables()
    assert commands[:4] == [
        ["iptables", "-S", "LOADER-ACCESS"], ["iptables", "-N", "LOADER-ACCESS"],
        ["iptables", "-C", "INPUT", "-j", "LOADER-ACCESS"], ["iptables", "-I", "INPUT", "1", "-j", "LOADER-ACCESS"],
    ]
    assert ["iptables", "-F", "LOADER-ACCESS"] in commands
    assert system.rules() == [
        ["-s", "198.51.100.7/32", "-j", "RETURN"],  # trusted first: RETURN leaves the chain
        ["-s", "203.0.113.0/24", "-j", "DROP"],
        ["-s", "192.0.2.9/32", "-j", "DROP"],
    ]
    assert commands.index(["iptables", "-F", "LOADER-ACCESS"]) < commands.index(["iptables", "-A", "LOADER-ACCESS", "-s", "198.51.100.7/32", "-j", "RETURN"])


def test_an_existing_chain_and_hook_are_reused_not_duplicated(tmp_path):
    system = FakeSystem(chain_exists=True, jump_exists=True)
    _enforcer(tmp_path, system).apply(Access("v1", block=("203.0.113.9/32",)))
    assert not any(c[:2] == ["iptables", "-N"] or c[:3] == ["iptables", "-I", "INPUT"] for c in system.calls)
    assert ["iptables", "-F", "LOADER-ACCESS"] in system.calls  # but its contents are replaced


def test_commands_are_argument_lists_with_validated_values_only(tmp_path):
    system = FakeSystem()
    _enforcer(tmp_path, system).apply(Access("v1", block=("203.0.113.9/32",)))
    assert all(isinstance(c, list) and all(isinstance(a, str) for a in c) for c in system.calls)


# -- fail2ban ------------------------------------------------------------------------

def test_the_whitelist_goes_into_every_jails_ignoreip_and_fail2ban_is_reloaded(tmp_path):
    system = FakeSystem()
    _enforcer(tmp_path, system).apply(Access("v1", allow=("198.51.100.7/32", "192.0.2.0/24")))
    text = (tmp_path / "access.local").read_text()
    for jail in JAILS:
        assert "[%s]\nignoreip = 127.0.0.1/8 ::1 198.51.100.7/32 192.0.2.0/24\n" % jail in text
    assert ["fail2ban-client", "reload"] in system.calls


def test_an_unchanged_whitelist_does_not_reload_fail2ban_again(tmp_path):
    system = FakeSystem()
    enforcer = _enforcer(tmp_path, system)
    enforcer.apply(Access("v1", allow=("198.51.100.7/32",), block=("203.0.113.9/32",)))
    system.calls.clear()
    enforcer.apply(Access("v2", allow=("198.51.100.7/32",), block=("203.0.113.9/32", "203.0.113.10/32")))  # only the blocks changed
    assert ["fail2ban-client", "reload"] not in system.calls


def test_a_trusted_address_that_is_banned_right_now_is_let_go(tmp_path):
    system = FakeSystem(banned=["198.51.100.4", "198.51.100.200", "203.0.113.9"])
    _enforcer(tmp_path, system).apply(Access("v1", allow=("198.51.100.0/24",)))
    assert sorted(system.banned) == ["203.0.113.9"]  # the two inside the trusted range are released; the stranger stays banned


def test_render_lists_every_jail_with_the_loopback_default_first():
    text = render_fail2ban([], JAILS)
    assert text.count("ignoreip = 127.0.0.1/8 ::1\n") == 3 and text.startswith("#")


# -- applying once, retrying on failure ----------------------------------------------

def test_a_version_is_applied_once_and_remembered(tmp_path):
    system = FakeSystem()
    enforcer = _enforcer(tmp_path, system)
    assert enforcer.applied_version is None
    assert enforcer.apply(Access("v1", block=("203.0.113.9/32",))) is True and enforcer.applied_version == "v1"
    system.calls.clear()
    assert enforcer.apply(Access("v1", block=("203.0.113.9/32",))) is False  # nothing to do
    assert system.calls == []
    assert enforcer.apply(Access("v2")) is True and enforcer.applied_version == "v2"


def test_a_fresh_process_applies_again_because_the_chain_does_not_survive_a_restart(tmp_path):
    system = FakeSystem()
    _enforcer(tmp_path, system).apply(Access("v1", block=("203.0.113.9/32",)))
    system.calls.clear()
    _enforcer(tmp_path, system).apply(Access("v1", block=("203.0.113.9/32",)))  # a new Enforcer: nothing applied yet
    assert system.rules() == [["-s", "203.0.113.9/32", "-j", "DROP"]]


def test_a_failing_command_leaves_the_version_unapplied_so_the_next_heartbeat_retries(tmp_path):
    system = FakeSystem(fail="-j DROP")
    enforcer = _enforcer(tmp_path, system)
    with pytest.raises(AccessError, match="blocking 203.0.113.9/32"):
        enforcer.apply(Access("v1", block=("203.0.113.9/32",)))
    assert enforcer.applied_version is None
    system.fail = None
    assert enforcer.apply(Access("v1", block=("203.0.113.9/32",))) is True  # retried whole


def test_the_firewall_is_done_before_the_whitelist_so_a_fail2ban_problem_cannot_leave_blocks_unapplied(tmp_path):
    system = FakeSystem(fail="fail2ban-client reload")
    enforcer = _enforcer(tmp_path, system)
    with pytest.raises(AccessError, match="whitelist"):
        enforcer.apply(Access("v1", allow=("198.51.100.7/32",), block=("203.0.113.9/32",)))
    assert system.rules() == [["-s", "198.51.100.7/32", "-j", "RETURN"], ["-s", "203.0.113.9/32", "-j", "DROP"]]
    assert enforcer.applied_version is None


def test_a_missing_iptables_is_a_clean_error_not_a_crash(tmp_path):
    def run(cmd):
        raise FileNotFoundError("iptables")

    with pytest.raises(AccessError, match="firewall command"):
        Enforcer(jails=JAILS, f2b_conf=str(tmp_path / "x.local"), run=run).apply(Access("v1", block=("203.0.113.9/32",)))
