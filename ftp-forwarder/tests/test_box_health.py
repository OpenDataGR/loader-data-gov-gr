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


def test_collect_status_aggregates_all_four_checks():
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
    }
