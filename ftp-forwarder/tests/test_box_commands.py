import subprocess

import pytest

from ftp_forwarder import box_commands
from ftp_forwarder.box_commands import Executor


class Fake:
    def __init__(self, active="active", fail=None, timeout=None):
        self.calls, self.active, self.fail, self.timeout = [], active, fail, timeout

    def __call__(self, cmd):
        self.calls.append(cmd)
        text = " ".join(cmd)
        if self.timeout and self.timeout in text:
            raise subprocess.TimeoutExpired(cmd, 60)
        if self.fail and self.fail in text:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        if cmd[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(cmd, 0, self.active + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, "out of " + cmd[0] + "\n", "")


def test_the_action_list_is_exactly_this():
    assert Executor(run=Fake()).actions == ("restart-proftpd", "restart-relay", "reload-fail2ban", "diagnostics")


@pytest.mark.parametrize("action, unit", [("restart-proftpd", "proftpd"), ("restart-relay", "loader-ftp-forwarder")])
def test_a_restart_restarts_that_unit_and_reports_whether_it_came_back(action, unit):
    fake = Fake()
    result = Executor(run=fake).execute({"id": "c1", "action": action})
    assert fake.calls == [["systemctl", "restart", unit], ["systemctl", "is-active", unit]]
    assert (result.id, result.ok) == ("c1", True) and "now active" in result.output


def test_a_service_that_does_not_come_back_is_a_failure():
    result = Executor(run=Fake(active="failed")).execute({"id": "c1", "action": "restart-proftpd"})
    assert result.ok is False and "now failed" in result.output


def test_a_failing_restart_command_is_a_failure_with_the_reason():
    result = Executor(run=Fake(fail="restart")).execute({"id": "c1", "action": "restart-proftpd"})
    assert result.ok is False and "boom" in result.output


def test_a_command_that_hangs_is_a_failure_not_a_crash():
    result = Executor(run=Fake(timeout="restart")).execute({"id": "c1", "action": "restart-relay"})
    assert result.ok is False and "timed out" in result.output


def test_fail2ban_reload():
    fake = Fake()
    result = Executor(run=fake).execute({"id": "c1", "action": "reload-fail2ban"})
    assert fake.calls == [["fail2ban-client", "reload"]] and result.ok


def test_diagnostics_is_read_only_and_covers_units_and_logs():
    fake = Fake()
    result = Executor(run=fake).execute({"id": "c1", "action": "diagnostics"})
    assert result.ok and "== uptime" in result.output and "== log proftpd" in result.output
    for cmd in fake.calls:  # nothing here changes anything
        assert cmd[0] in ("uptime", "free", "df", "fail2ban-client", "journalctl") or cmd[:2] == ["systemctl", "is-active"], cmd
        assert cmd[:2] != ["fail2ban-client", "reload"] and "restart" not in cmd


@pytest.mark.parametrize("action", ["rm -rf /", "shell", "", None, 5, "restart-sshd", "Restart-Proftpd"])
def test_anything_not_on_the_list_is_refused_and_runs_nothing(action):
    fake = Fake()
    result = Executor(run=fake).execute({"id": "c1", "action": action})
    assert result.ok is False and "does not run" in result.output and fake.calls == []


@pytest.mark.parametrize("command", [None, "restart-proftpd", [], {"action": "diagnostics"}, {"id": "../x", "action": "diagnostics"},
                                      {"id": "x" * 65, "action": "diagnostics"}, {"id": 7, "action": "diagnostics"}])
def test_something_not_shaped_like_a_command_is_ignored(command):
    fake = Fake()
    assert Executor(run=fake).execute(command) is None and fake.calls == []


def test_extra_fields_in_a_command_are_never_used():
    fake = Fake()
    Executor(run=fake).execute({"id": "c1", "action": "reload-fail2ban", "cmd": ["reboot"], "args": ["--now"]})
    assert fake.calls == [["fail2ban-client", "reload"]]


def test_output_is_cut_to_a_safe_size_keeping_the_end():
    class Loud(Fake):
        def __call__(self, cmd):
            return subprocess.CompletedProcess(cmd, 0, "A" * 50000 + "THE-END", "")

    out = Executor(run=Loud()).execute({"id": "c1", "action": "reload-fail2ban"}).output
    assert len(out) < box_commands.MAX_OUTPUT + 100 and out.endswith("THE-END")


def test_an_unexpected_exception_in_an_action_is_a_failed_result():
    def run(cmd):
        raise RuntimeError("weird")

    result = Executor(run=run).execute({"id": "c1", "action": "diagnostics"})
    assert result is not None and result.id == "c1"  # reported, never raised into the sync loop
