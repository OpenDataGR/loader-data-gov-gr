import os

from ftp_forwarder.auth_log import AuthLogTail, Login, parse_line


def test_a_successful_login_line_parses():
    assert parse_line("1790764941 84.205.215.133 zz-dryrun PASS 230") == Login("zz-dryrun", "84.205.215.133", 1790764941)


def test_failed_attempts_and_other_commands_are_ignored():
    assert parse_line("1790764934 188.166.183.84 - PASS 530") is None  # a scanner
    assert parse_line("1790764941 84.205.215.133 zz-dryrun PASS 530") is None
    assert parse_line("1790764941 84.205.215.133 - USER 331") is None
    assert parse_line("1790764941 84.205.215.133 zz-dryrun STOR 226") is None


def test_malformed_lines_are_ignored():
    for line in ("", "garbage", "notanumber 1.2.3.4 user PASS 230", "1 2 3 4", "1 1.2.3.4 user PASS 230 extra"):
        assert parse_line(line) is None, line


def _append(path, text):
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)


def test_only_lines_added_since_the_last_poll_are_returned(tmp_path):
    log = str(tmp_path / "auth.log")
    _append(log, "100 1.1.1.1 alice PASS 230\n")
    tail = AuthLogTail(log)
    assert tail.poll() == {"alice": Login("alice", "1.1.1.1", 100)}  # seeded from what was already there

    assert tail.poll() == {}  # nothing new

    _append(log, "200 2.2.2.2 bob PASS 230\n")
    assert tail.poll() == {"bob": Login("bob", "2.2.2.2", 200)}  # alice is not repeated


def test_the_latest_login_per_user_wins_within_a_poll(tmp_path):
    log = str(tmp_path / "auth.log")
    tail = AuthLogTail(log)
    assert tail.poll() == {}  # no file yet
    _append(log, "100 1.1.1.1 alice PASS 230\n150 9.9.9.9 alice PASS 230\n120 5.5.5.5 alice PASS 230\n")
    assert tail.poll()["alice"] == Login("alice", "9.9.9.9", 150)


def test_a_line_still_being_written_is_picked_up_once_complete(tmp_path):
    log = str(tmp_path / "auth.log")
    _append(log, "")
    tail = AuthLogTail(log)
    tail.poll()

    _append(log, "100 1.1.1.1 ali")  # cut off mid-write
    assert tail.poll() == {}
    _append(log, "ce PASS 230\n")
    assert tail.poll() == {"alice": Login("alice", "1.1.1.1", 100)}


def test_a_log_rotated_to_a_new_file_is_read_from_its_start(tmp_path):
    log = str(tmp_path / "auth.log")
    _append(log, "100 1.1.1.1 alice PASS 230\n" * 5)
    tail = AuthLogTail(log)
    tail.poll()

    os.replace(log, str(tmp_path / "auth.log.1"))
    _append(log, "300 3.3.3.3 carol PASS 230\n")  # shorter than what was read: also caught by the size check
    assert tail.poll() == {"carol": Login("carol", "3.3.3.3", 300)}


def test_the_first_poll_of_a_big_log_seeds_from_the_tail_without_a_cut_off_line(tmp_path):
    log = str(tmp_path / "auth.log")
    filler = "1 9.9.9.9 old PASS 230\n" * 1000
    _append(log, filler + "500 4.4.4.4 dave PASS 230\n")
    tail = AuthLogTail(log, seed_bytes=200)  # starts mid-file, inside a filler line

    result = tail.poll()

    assert result["dave"] == Login("dave", "4.4.4.4", 500)
    assert all(login.at == 500 or login.username == "old" for login in result.values())
    assert tail.poll() == {}


def test_a_missing_log_is_not_an_error(tmp_path):
    assert AuthLogTail(str(tmp_path / "nope.log")).poll() == {}
