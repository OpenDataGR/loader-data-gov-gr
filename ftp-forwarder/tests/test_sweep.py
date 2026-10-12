from ftp_forwarder.accounts import RelayAccount
from ftp_forwarder.sweep import catch_up_sweep


class RecordingPool:
    def __init__(self, *, already_in_flight=frozenset()):
        self.submitted = []
        self._already_in_flight = already_in_flight

    def submit(self, account, file_path):
        if file_path in self._already_in_flight:
            return None
        self.submitted.append((account, file_path))
        return object()  # a stand-in "future", never awaited by the sweep itself


def _account(tmp_path, name="acme") -> RelayAccount:
    watch_dir = tmp_path / name
    watch_dir.mkdir()
    return RelayAccount(username=name, watch_dir=watch_dir)


def test_sweep_submits_every_file_already_present(tmp_path):
    account = _account(tmp_path)
    (account.watch_dir / "a.txt").write_bytes(b"1")
    (account.watch_dir / "b.txt").write_bytes(b"2")
    pool = RecordingPool()

    submitted = catch_up_sweep(pool, (account,))

    assert submitted == 2
    assert {p.name for _, p in pool.submitted} == {"a.txt", "b.txt"}


def test_sweep_ignores_subdirectories(tmp_path):
    account = _account(tmp_path)
    (account.watch_dir / "a.txt").write_bytes(b"1")
    (account.watch_dir / ".forwarded").mkdir()
    pool = RecordingPool()

    catch_up_sweep(pool, (account,))

    assert [p.name for _, p in pool.submitted] == ["a.txt"]


def test_sweep_scopes_files_to_the_right_account(tmp_path):
    account_a = _account(tmp_path, "a")
    account_b = _account(tmp_path, "b")
    (account_a.watch_dir / "only-a.txt").write_bytes(b"1")
    (account_b.watch_dir / "only-b.txt").write_bytes(b"1")
    pool = RecordingPool()

    catch_up_sweep(pool, (account_a, account_b))

    assert {(a.username, p.name) for a, p in pool.submitted} == {("a", "only-a.txt"), ("b", "only-b.txt")}


def test_sweep_skips_a_missing_watch_dir_without_raising(tmp_path):
    account = _account(tmp_path)
    account.watch_dir.rmdir()
    pool = RecordingPool()

    submitted = catch_up_sweep(pool, (account,))

    assert submitted == 0


def test_sweep_does_not_double_count_an_already_in_flight_file(tmp_path):
    account = _account(tmp_path)
    file_path = account.watch_dir / "a.txt"
    file_path.write_bytes(b"1")
    pool = RecordingPool(already_in_flight={file_path})

    submitted = catch_up_sweep(pool, (account,))

    assert submitted == 0


def test_sweep_leaves_a_file_that_is_still_being_written_for_a_later_pass(tmp_path):
    import os
    import time

    account = _account(tmp_path)
    growing, finished = account.watch_dir / "growing.xml", account.watch_dir / "finished.xml"
    growing.write_bytes(b"<a>")
    finished.write_bytes(b"<a/>")
    now = time.time()
    os.utime(growing, (now - 5, now - 5))       # written to five seconds ago
    os.utime(finished, (now - 600, now - 600))  # quiet for ten minutes
    pool = RecordingPool()

    assert catch_up_sweep(pool, (account,), settle_s=120, now=lambda: now) == 1
    assert [p.name for _, p in pool.submitted] == ["finished.xml"]

    # a later pass, once it has been quiet long enough, takes it
    assert catch_up_sweep(pool, (account,), settle_s=120, now=lambda: now + 200) == 2
    assert "growing.xml" in {p.name for _, p in pool.submitted}


def test_sweep_without_a_settle_time_sends_everything_as_before(tmp_path):
    account = _account(tmp_path)
    (account.watch_dir / "fresh.xml").write_bytes(b"<a/>")
    pool = RecordingPool()

    assert catch_up_sweep(pool, (account,)) == 1
