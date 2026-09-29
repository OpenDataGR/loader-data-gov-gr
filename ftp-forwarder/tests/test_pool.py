import threading
import time

from ftp_forwarder.accounts import RelayAccount
from ftp_forwarder.config import ForwarderConfig
from ftp_forwarder.pool import ForwarderPool


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = ""


class FakeSession:
    def __init__(self, delay_s=0.0):
        self._delay_s = delay_s
        self.calls = []
        self._lock = threading.Lock()

    def post(self, url, *, files, headers, timeout):
        with self._lock:
            self.calls.append(url)
        time.sleep(self._delay_s)
        return FakeResponse(200)


def _account(tmp_path, name="acme") -> RelayAccount:
    return RelayAccount(username=name, watch_dir=tmp_path)


def _config(**overrides) -> ForwarderConfig:
    defaults = dict(loader_base_url="https://loader.data.gov.gr", sync_key="the-sync-key", retry_backoff_s=0.001)
    defaults.update(overrides)
    return ForwarderConfig(**defaults)


def test_pool_processes_multiple_files_concurrently(tmp_path):
    account = _account(tmp_path)
    session = FakeSession(delay_s=0.2)
    config = _config(worker_count=4)
    pool = ForwarderPool(config, session=session)

    files = []
    for i in range(4):
        path = tmp_path / ("f%d.txt" % i)
        path.write_bytes(b"x")
        files.append(path)

    started = time.monotonic()
    futures = [pool.submit(account, f) for f in files]
    for future in futures:
        future.result(timeout=5)
    elapsed = time.monotonic() - started

    pool.shutdown()
    # Serial would take >= 0.8s (4 * 0.2s) -- concurrent should be
    # close to a single 0.2s delay, with generous headroom for a slow
    # CI machine.
    assert elapsed < 0.6
    assert len(session.calls) == 4


def test_pool_does_not_submit_the_same_path_twice_while_in_flight(tmp_path):
    account = _account(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class BlockingSession:
        def post(self, url, *, files, headers, timeout):
            started.set()
            release.wait(timeout=5)
            return FakeResponse(200)

    config = _config(worker_count=4)
    pool = ForwarderPool(config, session=BlockingSession())

    file_path = tmp_path / "f.txt"
    file_path.write_bytes(b"x")

    first = pool.submit(account, file_path)
    started.wait(timeout=5)
    second = pool.submit(account, file_path)  # still in flight -- must be skipped

    assert second is None

    release.set()
    first.result(timeout=5)
    pool.shutdown()


def test_pool_one_failing_file_does_not_block_another(tmp_path):
    account = _account(tmp_path)

    class FlakySession:
        def post(self, url, *, files, headers, timeout):
            if "bad" in str(files["file"][0]):
                return FakeResponse(500)
            return FakeResponse(200)

    config = _config(worker_count=2, max_retries=1)
    pool = ForwarderPool(config, session=FlakySession())

    good = tmp_path / "good.txt"
    good.write_bytes(b"x")
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"x")

    good_future = pool.submit(account, good)
    bad_future = pool.submit(account, bad)
    good_future.result(timeout=5)
    bad_future.result(timeout=5)
    pool.shutdown()

    assert not good.exists()  # archived
    assert bad.exists()  # left in place after every retry failed
