import os

from ftp_forwarder.attempts_log import Attempt, AttemptsLogTail, parse_line


def test_a_refused_login_line_parses():
    assert parse_line("1790764941 188.166.1.1 2222 PASS 530 admin") == Attempt(1790764941, "188.166.1.1", 2222, "admin", 530)


def test_the_username_may_contain_spaces_and_may_be_absent():
    assert parse_line("1 1.1.1.1 21 PASS 530 john smith").username == "john smith"
    assert parse_line("1 1.1.1.1 21 PASS 530 -").username == ""
    assert parse_line("1 1.1.1.1 21 PASS 530").username == ""


def test_successes_and_other_commands_are_ignored():
    assert parse_line("1 1.1.1.1 21 PASS 230 alice") is None
    assert parse_line("1 1.1.1.1 21 USER 331 alice") is None


def test_malformed_lines_are_ignored():
    for line in ("", "garbage", "x 1.1.1.1 21 PASS 530 a", "1 1.1.1.1 ftp PASS 530 a", "1 1.1.1.1 21 PASS"):
        assert parse_line(line) is None, line


def _append(path, text):
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _tail(tmp_path):
    return AttemptsLogTail(str(tmp_path / "attempts.log"), str(tmp_path / "state.json")), str(tmp_path / "attempts.log")


def test_nothing_is_lost_or_repeated_across_commits(tmp_path):
    tail, log = _tail(tmp_path)
    assert tail.poll() == ([], None)  # no file yet
    _append(log, "1 1.1.1.1 21 PASS 530 a\n2 2.2.2.2 2222 PASS 230 ok\n3 3.3.3.3 21 PASS 530 b\n")
    attempts, pos = tail.poll()
    assert [a.username for a in attempts] == ["a", "b"]
    tail.commit(pos)

    assert tail.poll()[0] == []
    _append(log, "4 4.4.4.4 21 PASS 530 c\n")
    assert [a.username for a in tail.poll()[0]] == ["c"]


def test_an_uncommitted_batch_is_offered_again(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, "1 1.1.1.1 21 PASS 530 a\n")
    first, _ = tail.poll()
    again, _ = tail.poll()  # delivery failed, nothing committed
    assert first == again and len(again) == 1


def test_the_position_survives_a_restart(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, "1 1.1.1.1 21 PASS 530 a\n")
    tail.commit(tail.poll()[1])
    _append(log, "2 2.2.2.2 21 PASS 530 b\n")

    restarted = AttemptsLogTail(log, str(tmp_path / "state.json"))
    assert [a.username for a in restarted.poll()[0]] == ["b"]


def test_a_line_still_being_written_waits(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, "1 1.1.1.1 21 PASS 530 al")
    attempts, pos = tail.poll()
    assert attempts == []
    tail.commit(pos)
    _append(log, "ice\n")
    assert [a.username for a in tail.poll()[0]] == ["alice"]


def test_batches_are_capped_and_the_rest_follows(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, "".join("%d 1.1.1.1 21 PASS 530 u%d\n" % (i, i) for i in range(5)))
    first, pos = tail.poll(limit=2)
    assert [a.username for a in first] == ["u0", "u1"]
    tail.commit(pos)
    second, pos = tail.poll(limit=2)
    assert [a.username for a in second] == ["u2", "u3"]
    tail.commit(pos)
    assert [a.username for a in tail.poll(limit=2)[0]] == ["u4"]


def test_a_rotated_log_is_read_from_its_start(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, "1 1.1.1.1 21 PASS 530 aaaaaaaaaaaaaaaa\n" * 5)
    tail.commit(tail.poll()[1])
    os.replace(log, str(tmp_path / "attempts.log.1"))
    _append(log, "9 9.9.9.9 21 PASS 530 new\n")
    assert [a.username for a in tail.poll()[0]] == ["new"]


def test_a_corrupt_state_file_starts_over_rather_than_crashing(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, "1 1.1.1.1 21 PASS 530 a\n")
    (tmp_path / "state.json").write_text("{not json")
    assert len(tail.poll()[0]) == 1
