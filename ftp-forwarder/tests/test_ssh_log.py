import json
import subprocess

from ftp_forwarder.attempts_log import Attempt
from ftp_forwarder.ssh_log import JournalSshTail, parse_entry


def _entry(message, cursor="c1", micros="1790000000000000"):
    return {"MESSAGE": message, "__CURSOR": cursor, "__REALTIME_TIMESTAMP": micros}


def test_a_failed_password_parses():
    got = parse_entry(_entry("Failed password for root from 202.51.214.98 port 48604 ssh2"))
    assert got == Attempt(at=1790000000, ip="202.51.214.98", port=22, username="root", code=0, channel="SSH")


def test_an_invalid_user_and_a_username_with_spaces_parse():
    assert parse_entry(_entry("Failed password for invalid user vpn from 193.46.255.86 port 37952 ssh2")).username == "vpn"
    assert parse_entry(_entry("Failed password for invalid user a from b from 1.2.3.4 port 5 ssh2")).username == "a from b"
    assert parse_entry(_entry("Failed password for root from 2001:db8::1 port 22 ssh2")).ip == "2001:db8::1"


def test_the_other_lines_a_scanner_leaves_are_not_counted():
    for message in (
        "Invalid user vpn from 193.46.255.86 port 37952",
        "pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0 tty=ssh ruser= rhost=1.2.3.4  user=root",
        "Disconnected from authenticating user root 202.51.214.98 port 48604 [preauth]",
        "Connection closed by authenticating user root 1.2.3.4 port 5 [preauth]",
        "Accepted password for alice from 1.2.3.4 port 5 ssh2",
        "Failed publickey for root from 1.2.3.4 port 5 ssh2",
        "",
    ):
        assert parse_entry(_entry(message)) is None, message


def test_malformed_entries_are_ignored():
    assert parse_entry({"MESSAGE": [70, 97], "__REALTIME_TIMESTAMP": "1"}) is None  # binary message
    assert parse_entry({"MESSAGE": "Failed password for root from 1.2.3.4 port 5 ssh2"}) is None  # no time
    assert parse_entry(_entry("Failed password for root from 1.2.3.4 port 5 ssh2", micros="soon")) is None


FAILED = "Failed password for root from 1.2.3.4 port 5 ssh2"


def _journal(entries, returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout="\n".join(json.dumps(e) for e in entries), stderr=stderr)


def _tail(tmp_path, runs):
    """``runs`` is a list of results handed out one per journalctl call; the commands are kept."""
    calls = []

    def run(cmd):
        calls.append(cmd)
        return runs.pop(0)

    return JournalSshTail(str(tmp_path / "ssh.state"), run=run), calls


def test_the_first_poll_takes_the_last_day_then_continues_after_the_cursor(tmp_path):
    tail, calls = _tail(tmp_path, [
        _journal([_entry(FAILED, "c1"), _entry("noise", "c2")]),
        _journal([_entry(FAILED, "c3")]),
    ])
    attempts, cursor = tail.poll()
    assert len(attempts) == 1 and cursor == "c2"  # skips past the unrelated line too
    assert calls[0][-2:] == ["--since", "-1 day"]

    tail.commit(cursor)
    attempts, cursor = tail.poll()
    assert calls[1][-2:] == ["--after-cursor", "c2"]
    assert len(attempts) == 1 and cursor == "c3"


def test_an_uncommitted_batch_is_offered_again(tmp_path):
    tail, calls = _tail(tmp_path, [_journal([_entry(FAILED, "c1")]), _journal([_entry(FAILED, "c1")])])
    tail.poll()  # delivery failed: no commit
    tail.poll()
    assert calls[1][-2:] == ["--since", "-1 day"]  # still no saved position


def test_batches_are_capped_and_the_cursor_stops_at_the_last_one_included(tmp_path):
    entries = [_entry(FAILED, "c%d" % i) for i in range(5)]
    tail, _ = _tail(tmp_path, [_journal(entries)])
    attempts, cursor = tail.poll(limit=2)
    assert len(attempts) == 2 and cursor == "c1"  # the rest is picked up next time


def test_a_journal_error_returns_nothing_and_does_not_move_the_position(tmp_path):
    tail, _ = _tail(tmp_path, [_journal([], returncode=1, stderr="no journal")])
    assert tail.poll() == ([], None)


def test_a_cursor_the_journal_no_longer_has_falls_back_to_a_bounded_replay(tmp_path):
    calls = []

    def run(cmd):
        calls.append(cmd)
        if "--after-cursor" in cmd:
            return _journal([], returncode=1, stderr="Failed to seek")
        return _journal([_entry(FAILED, "c9")])

    tail = JournalSshTail(str(tmp_path / "ssh.state"), run=run)
    tail.commit("old-cursor")
    attempts, cursor = tail.poll()
    assert len(attempts) == 1 and cursor == "c9"
    assert calls[-1][-2:] == ["--since", "-1 hour"]


def test_the_cursor_survives_a_restart_and_a_corrupt_state_file_starts_over(tmp_path):
    tail, _ = _tail(tmp_path, [])
    tail.commit("c7")
    assert JournalSshTail(str(tmp_path / "ssh.state"))._load() == "c7"
    (tmp_path / "ssh.state").write_text("{not json")
    assert JournalSshTail(str(tmp_path / "ssh.state"))._load() is None
