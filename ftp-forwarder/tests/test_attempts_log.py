import os

from ftp_forwarder.attempts_log import Attempt, AttemptsLogTail, parse_line

TOKEN = "3f9c2a7e5b1d4c68"


def line(epoch, ip, port, command, code, username=None, token=TOKEN):
    """A line as ProFTPD writes it: ``%{%s}t %a %p %m %s <token> %U``."""
    return " ".join(str(p) for p in (epoch, ip, port, command, code, token) + (() if username is None else (username,)))


def test_a_refused_login_line_parses():
    assert parse_line(line(1790764941, "188.166.1.1", 2222, "PASS", 530, "admin"), TOKEN) == Attempt(
        1790764941, "188.166.1.1", 2222, "admin", 530
    )


def test_the_username_may_contain_spaces_and_may_be_absent():
    assert parse_line(line(1, "1.1.1.1", 21, "PASS", 530, "john smith"), TOKEN).username == "john smith"
    assert parse_line(line(1, "1.1.1.1", 21, "PASS", 530, "-"), TOKEN).username == ""
    assert parse_line(line(1, "1.1.1.1", 21, "PASS", 530), TOKEN).username == ""


def test_successes_and_other_commands_are_ignored():
    assert parse_line(line(1, "1.1.1.1", 21, "PASS", 230, "alice"), TOKEN) is None
    assert parse_line(line(1, "1.1.1.1", 21, "USER", 331, "alice"), TOKEN) is None


def test_malformed_lines_are_ignored():
    for text in ("", "garbage", "x 1.1.1.1 21 PASS 530 %s a" % TOKEN, "1 1.1.1.1 ftp PASS 530 %s a" % TOKEN,
                 "1 1.1.1.1 21 PASS %s" % TOKEN):
        assert parse_line(text, TOKEN) is None, text


def test_a_line_without_the_token_is_not_believed():
    # What a stranger can write: a whole line of their own, any address, any user --
    # but not the secret token. (Also covers lines from before the token existed.)
    assert parse_line("1790000000 9.9.9.9 2222 PASS 530 forged-entry", TOKEN) is None
    assert parse_line(line(1, "9.9.9.9", 2222, "PASS", 530, "x", token="0000000000000000"), TOKEN) is None
    assert parse_line(line(1, "9.9.9.9", 2222, "PASS", 530, "x", token=""), TOKEN) is None


def test_an_empty_token_believes_nothing():
    assert parse_line("1 1.1.1.1 21 PASS 530  a", "") is None
    assert parse_line(line(1, "1.1.1.1", 21, "PASS", 530, "a", token=""), "") is None


def _append(path, text):
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _tail(tmp_path, token=TOKEN):
    log = str(tmp_path / "attempts.log")
    return AttemptsLogTail(log, str(tmp_path / "state.json"), token), log


def test_the_forgery_found_on_the_box_is_dropped_whole(tmp_path):
    # Exactly what ProFTPD wrote for an SFTP username of "zz-nl-probe\n1790000000 9.9.9.9 2222 PASS 530 forged-entry":
    # the genuine line (cut at the newline) and a second, attacker-written one.
    tail, log = _tail(tmp_path)
    _append(log, line(1790930479, "84.205.215.133", 2222, "PASS", 530, "zz-nl-probe") + "\n"
                 "1790000000 9.9.9.9 2222 PASS 530 forged-entry\n")
    attempts, _ = tail.poll()
    assert [(a.ip, a.username) for a in attempts] == [("84.205.215.133", "zz-nl-probe")]


def test_a_forger_who_guesses_the_format_but_not_the_token_gets_nothing(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, line(1, "1.1.1.1", 21, "PASS", 530, "a") + "\n"
                 + "".join(line(2, "6.6.6.%d" % i, 21, "PASS", 530, "x", token="deadbeefdeadbeef") + "\n" for i in range(5)))
    assert [a.ip for a in tail.poll()[0]] == ["1.1.1.1"]


def test_without_a_token_nothing_is_shipped_but_the_position_still_advances(tmp_path):
    tail, log = _tail(tmp_path, token="")
    _append(log, line(1, "1.1.1.1", 21, "PASS", 530, "a") + "\n")
    attempts, _ = tail.poll()
    assert attempts == []


def test_nothing_is_lost_or_repeated_across_commits(tmp_path):
    tail, log = _tail(tmp_path)
    assert tail.poll() == ([], None)  # no file yet
    _append(log, line(1, "1.1.1.1", 21, "PASS", 530, "a") + "\n" + line(2, "2.2.2.2", 2222, "PASS", 230, "ok") + "\n"
                 + line(3, "3.3.3.3", 21, "PASS", 530, "b") + "\n")
    attempts, pos = tail.poll()
    assert [a.username for a in attempts] == ["a", "b"]
    tail.commit(pos)

    assert tail.poll()[0] == []
    _append(log, line(4, "4.4.4.4", 21, "PASS", 530, "c") + "\n")
    assert [a.username for a in tail.poll()[0]] == ["c"]


def test_an_uncommitted_batch_is_offered_again(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, line(1, "1.1.1.1", 21, "PASS", 530, "a") + "\n")
    first, _ = tail.poll()
    again, _ = tail.poll()  # delivery failed, nothing committed
    assert first == again and len(again) == 1


def test_the_position_survives_a_restart(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, line(1, "1.1.1.1", 21, "PASS", 530, "a") + "\n")
    tail.commit(tail.poll()[1])
    _append(log, line(2, "2.2.2.2", 21, "PASS", 530, "b") + "\n")

    restarted = AttemptsLogTail(log, str(tmp_path / "state.json"), TOKEN)
    assert [a.username for a in restarted.poll()[0]] == ["b"]


def test_a_line_still_being_written_waits(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, line(1, "1.1.1.1", 21, "PASS", 530, "al"))
    attempts, pos = tail.poll()
    assert attempts == []
    tail.commit(pos)
    _append(log, "ice\n")
    assert [a.username for a in tail.poll()[0]] == ["alice"]


def test_batches_are_capped_and_the_rest_follows(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, "".join(line(i, "1.1.1.1", 21, "PASS", 530, "u%d" % i) + "\n" for i in range(5)))
    first, pos = tail.poll(limit=2)
    assert [a.username for a in first] == ["u0", "u1"]
    tail.commit(pos)
    second, pos = tail.poll(limit=2)
    assert [a.username for a in second] == ["u2", "u3"]
    tail.commit(pos)
    assert [a.username for a in tail.poll(limit=2)[0]] == ["u4"]


def test_a_rotated_log_is_read_from_its_start(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, (line(1, "1.1.1.1", 21, "PASS", 530, "aaaaaaaaaaaaaaaa") + "\n") * 5)
    tail.commit(tail.poll()[1])
    os.replace(log, str(tmp_path / "attempts.log.1"))
    _append(log, line(9, "9.9.9.9", 21, "PASS", 530, "new") + "\n")
    assert [a.username for a in tail.poll()[0]] == ["new"]


def test_a_corrupt_state_file_starts_over_rather_than_crashing(tmp_path):
    tail, log = _tail(tmp_path)
    _append(log, line(1, "1.1.1.1", 21, "PASS", 530, "a") + "\n")
    (tmp_path / "state.json").write_text("{not json")
    assert len(tail.poll()[0]) == 1
