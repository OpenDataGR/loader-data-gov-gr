import subprocess

from ftp_forwarder.box_health import collect_status, is_port_listening, is_unit_active


def _completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_is_unit_active_true_when_systemctl_exits_zero():
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return _completed(returncode=0)

    assert is_unit_active("proftpd", run=fake_run) is True
    assert calls == [["systemctl", "is-active", "--quiet", "proftpd"]]


def test_is_unit_active_false_when_systemctl_exits_nonzero():
    assert is_unit_active("proftpd", run=lambda cmd: _completed(returncode=3)) is False


_SS_OUTPUT = (
    "State   Recv-Q  Send-Q   Local Address:Port   Peer Address:Port\n"
    "LISTEN  0       4096            0.0.0.0:21        0.0.0.0:*\n"
    "LISTEN  0       4096                  *:2222             *:*\n"
    "LISTEN  0       4096              127.0.0.1:8000        0.0.0.0:*\n"
)


def test_is_port_listening_true_for_a_listed_port():
    assert is_port_listening(21, run=lambda cmd: _completed(stdout=_SS_OUTPUT)) is True
    assert is_port_listening(2222, run=lambda cmd: _completed(stdout=_SS_OUTPUT)) is True


def test_is_port_listening_false_for_an_unlisted_port():
    assert is_port_listening(22, run=lambda cmd: _completed(stdout=_SS_OUTPUT)) is False


def test_is_port_listening_does_not_confuse_a_prefix_match():
    # A naive substring search for ":21" would wrongly match ":210"/":2100".
    output = "State Recv-Q Send-Q Local Address:Port Peer Address:Port\nLISTEN 0 4096 *:210 *:*\n"
    assert is_port_listening(21, run=lambda cmd: _completed(stdout=output)) is False


def test_collect_status_aggregates_all_the_checks():
    def fake_run(cmd):
        if cmd[0] == "systemctl":
            unit = cmd[-1]
            return _completed(returncode=0 if unit == "proftpd" else 1)
        return _completed(stdout=_SS_OUTPUT)

    status = collect_status(proftpd_unit="proftpd", relay_unit="loader-ftp-forwarder", run=fake_run)
    assert status == {
        "proftpd_active": True,
        "ftp_port_listening": True,
        "sftp_port_listening": True,
        "relay_service_active": False,
        "fail2ban_active": False,  # only proftpd's unit is up in this fake
    }


# -- fail2ban ban list and unban --------------------------------------------------

_JAIL_STATUS = """Status for the jail: loader-ftp
|- Filter
|  |- Currently failed:\t1
|  |- Total failed:\t19
|  `- File list:\t/var/log/proftpd/loader-auth.log
`- Actions
   |- Currently banned:\t2
   |- Total banned:\t2
   `- Banned IP list:\t169.58.53.194 84.205.215.132
"""


def test_read_banned_parses_the_jail_status():
    from ftp_forwarder.box_health import read_banned

    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return _completed(stdout=_JAIL_STATUS)

    assert read_banned("loader-ftp", run=fake_run) == ["169.58.53.194", "84.205.215.132"]
    assert calls == [["fail2ban-client", "status", "loader-ftp"]]


def test_read_banned_with_nobody_banned_is_an_empty_list_not_none():
    from ftp_forwarder.box_health import read_banned

    empty = _JAIL_STATUS.replace("169.58.53.194 84.205.215.132", "")
    assert read_banned(run=lambda cmd: _completed(stdout=empty)) == []


def test_read_banned_cannot_tell_a_dead_fail2ban_from_an_empty_list():
    from ftp_forwarder.box_health import read_banned

    assert read_banned(run=lambda cmd: _completed(returncode=255, stdout="")) is None  # not running / no such jail
    assert read_banned(run=lambda cmd: _completed(stdout="something unexpected")) is None


def test_read_banned_ignores_anything_that_is_not_an_address():
    from ftp_forwarder.box_health import read_banned

    odd = _JAIL_STATUS.replace("169.58.53.194 84.205.215.132", "169.58.53.194 not-an-ip 2001:db8::1")
    assert read_banned(run=lambda cmd: _completed(stdout=odd)) == ["169.58.53.194", "2001:db8::1"]


def test_unban_runs_one_exact_command():
    from ftp_forwarder.box_health import unban

    calls = []
    assert unban("84.205.215.132", "loader-ftp", run=lambda cmd: calls.append(cmd) or _completed()) is True
    assert calls == [["fail2ban-client", "set", "loader-ftp", "unbanip", "84.205.215.132"]]


def test_unban_refuses_anything_that_is_not_an_ip_address_and_runs_nothing():
    from ftp_forwarder.box_health import unban

    calls = []
    for hostile in ("1.2.3.4; rm -rf /", "$(reboot)", "1.2.3.4 5.6.7.8", "--help", "", "all"):
        assert unban(hostile, run=lambda cmd: calls.append(cmd) or _completed()) is False, hostile
    assert calls == []  # it never reached the command (this runs as root)


def test_unban_reports_a_failed_command():
    from ftp_forwarder.box_health import unban

    assert unban("1.2.3.4", run=lambda cmd: _completed(returncode=1)) is False
