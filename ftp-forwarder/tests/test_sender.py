import datetime
import pathlib

import pytest
import requests

from ftp_forwarder.accounts import RelayAccount
from ftp_forwarder.config import ForwarderConfig
from ftp_forwarder.sender import SendError, archive_file, process_one, send_file


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, *, files, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _account(tmp_path, name="acme") -> RelayAccount:
    return RelayAccount(username=name, watch_dir=tmp_path / "watch")


def _config(**overrides) -> ForwarderConfig:
    defaults = dict(loader_base_url="https://loader.data.gov.gr", sync_key="the-sync-key")
    defaults.update(overrides)
    return ForwarderConfig(**defaults)


def _file(tmp_path, name="data.csv", content=b"a,b\n1,2\n") -> pathlib.Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_send_file_succeeds_on_the_first_try(tmp_path):
    account = _account(tmp_path)
    file_path = _file(tmp_path)
    session = FakeSession([FakeResponse(200)])

    send_file(session, "https://loader.data.gov.gr", "the-sync-key", account, file_path, sleep=lambda s: None)

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://loader.data.gov.gr/admin/ftp-relay/acme/upload"
    assert call["headers"]["X-Ftp-Sync-Key"] == "the-sync-key"


def test_send_file_strips_a_trailing_slash_from_base_url(tmp_path):
    account = _account(tmp_path)
    file_path = _file(tmp_path)
    session = FakeSession([FakeResponse(200)])

    send_file(session, "https://loader.data.gov.gr/", "the-sync-key", account, file_path, sleep=lambda s: None)

    assert session.calls[0]["url"] == "https://loader.data.gov.gr/admin/ftp-relay/acme/upload"


def test_send_file_retries_a_5xx_then_succeeds(tmp_path):
    account = _account(tmp_path)
    file_path = _file(tmp_path)
    session = FakeSession([FakeResponse(503), FakeResponse(200)])
    sleeps = []

    send_file(session, "https://loader.data.gov.gr", "the-sync-key", account, file_path, sleep=sleeps.append)

    assert len(session.calls) == 2
    assert len(sleeps) == 1


def test_send_file_retries_a_network_error_then_succeeds(tmp_path):
    account = _account(tmp_path)
    file_path = _file(tmp_path)
    session = FakeSession([requests.ConnectionError("boom"), FakeResponse(200)])

    send_file(session, "https://loader.data.gov.gr", "the-sync-key", account, file_path, sleep=lambda s: None)

    assert len(session.calls) == 2


def test_send_file_gives_up_after_max_retries(tmp_path):
    account = _account(tmp_path)
    file_path = _file(tmp_path)
    session = FakeSession([FakeResponse(503)] * 3)

    with pytest.raises(SendError):
        send_file(
            session, "https://loader.data.gov.gr", "the-sync-key", account, file_path,
            max_retries=3, sleep=lambda s: None,
        )

    assert len(session.calls) == 3


def test_send_file_does_not_retry_a_4xx(tmp_path):
    account = _account(tmp_path)
    file_path = _file(tmp_path)
    session = FakeSession([FakeResponse(401, "invalid sync key")])

    with pytest.raises(SendError, match="401"):
        send_file(
            session, "https://loader.data.gov.gr", "the-sync-key", account, file_path,
            max_retries=5, sleep=lambda s: None,
        )

    # Only one call -- a 4xx will never succeed on retry, so it must not
    # be retried at all.
    assert len(session.calls) == 1


def test_archive_file_moves_into_an_hour_sharded_subfolder(tmp_path):
    file_path = _file(tmp_path)
    now = datetime.datetime(2026, 9, 28, 14, 30, tzinfo=datetime.timezone.utc)

    dest = archive_file(file_path, ".forwarded", now=now)

    assert dest == tmp_path / ".forwarded" / "2026092814" / "data.csv"
    assert dest.exists()
    assert not file_path.exists()


def test_process_one_sends_and_archives_on_success(tmp_path):
    account = _account(tmp_path)
    file_path = _file(tmp_path)
    session = FakeSession([FakeResponse(200)])
    config = _config()

    sent = process_one(session, config, account, file_path)

    assert sent is True
    assert not file_path.exists()
    archived = list((tmp_path / ".forwarded").rglob("data.csv"))
    assert len(archived) == 1


def test_process_one_skips_a_path_that_is_not_a_real_file(tmp_path):
    account = _account(tmp_path)
    missing = tmp_path / "does-not-exist.csv"
    session = FakeSession([])
    config = _config()

    sent = process_one(session, config, account, missing)

    assert sent is False
    assert session.calls == []


def test_process_one_leaves_the_file_in_place_on_failure(tmp_path):
    account = _account(tmp_path)
    file_path = _file(tmp_path)
    session = FakeSession([FakeResponse(500)] * 5)
    config = _config(retry_backoff_s=0.001)  # keep the test fast

    with pytest.raises(SendError):
        process_one(session, config, account, file_path)

    assert file_path.exists()  # never archived -- the next catch-up sweep must find it again
