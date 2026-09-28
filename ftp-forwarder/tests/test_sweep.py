from ftp_forwarder.config import AccountConfig, ForwarderConfig
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


def _account(tmp_path, name="acme") -> AccountConfig:
    watch_dir = tmp_path / name
    watch_dir.mkdir()
    key_file = tmp_path / ("%s.key" % name)
    key_file.write_text("k", encoding="utf-8")
    return AccountConfig(name=name, watch_dir=watch_dir, source_name=name, api_key_file=key_file)


def _config(accounts) -> ForwarderConfig:
    return ForwarderConfig(loader_base_url="https://loader.data.gov.gr", accounts=tuple(accounts))


def test_sweep_submits_every_file_already_present(tmp_path):
    account = _account(tmp_path)
    (account.watch_dir / "a.txt").write_bytes(b"1")
    (account.watch_dir / "b.txt").write_bytes(b"2")
    config = _config([account])
    pool = RecordingPool()

    submitted = catch_up_sweep(pool, config)

    assert submitted == 2
    assert {p.name for _, p in pool.submitted} == {"a.txt", "b.txt"}


def test_sweep_ignores_subdirectories(tmp_path):
    account = _account(tmp_path)
    (account.watch_dir / "a.txt").write_bytes(b"1")
    (account.watch_dir / ".forwarded").mkdir()
    config = _config([account])
    pool = RecordingPool()

    catch_up_sweep(pool, config)

    assert [p.name for _, p in pool.submitted] == ["a.txt"]


def test_sweep_scopes_files_to_the_right_account(tmp_path):
    account_a = _account(tmp_path, "a")
    account_b = _account(tmp_path, "b")
    (account_a.watch_dir / "only-a.txt").write_bytes(b"1")
    (account_b.watch_dir / "only-b.txt").write_bytes(b"1")
    config = _config([account_a, account_b])
    pool = RecordingPool()

    catch_up_sweep(pool, config)

    assert {(a.name, p.name) for a, p in pool.submitted} == {("a", "only-a.txt"), ("b", "only-b.txt")}


def test_sweep_skips_a_missing_watch_dir_without_raising(tmp_path):
    account = _account(tmp_path)
    account.watch_dir.rmdir()
    config = _config([account])
    pool = RecordingPool()

    submitted = catch_up_sweep(pool, config)

    assert submitted == 0


def test_sweep_does_not_double_count_an_already_in_flight_file(tmp_path):
    account = _account(tmp_path)
    file_path = account.watch_dir / "a.txt"
    file_path.write_bytes(b"1")
    config = _config([account])
    pool = RecordingPool(already_in_flight={file_path})

    submitted = catch_up_sweep(pool, config)

    assert submitted == 0
