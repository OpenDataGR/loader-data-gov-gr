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


# -- checksums: the box says what it holds, the loader says what it stored -----------------------------------------------------

import hashlib
import json


def _sha(content=b"a,b\n1,2\n"):
    return hashlib.sha256(content).hexdigest()


def test_send_file_sends_its_checksum_and_accepts_a_matching_answer(tmp_path):
    account, file_path = _account(tmp_path), _file(tmp_path)
    session = FakeSession([FakeResponse(200, json.dumps({"status": "success", "sha256": _sha()}))])

    send_file(session, "https://loader.data.gov.gr", "the-sync-key", account, file_path, sleep=lambda s: None)

    assert session.calls[0]["headers"]["X-Content-SHA256"] == _sha()
    assert len(session.calls) == 1


def test_a_loader_that_stored_something_else_is_retried_and_never_counted_as_sent(tmp_path):
    account, file_path = _account(tmp_path), _file(tmp_path)
    session = FakeSession([FakeResponse(200, json.dumps({"sha256": "0" * 64}))] * 3)
    sleeps = []

    with pytest.raises(SendError, match="checksum mismatch"):
        send_file(session, "https://loader.data.gov.gr", "the-sync-key", account, file_path, max_retries=3, sleep=sleeps.append)

    assert len(session.calls) == 3 and len(sleeps) == 2


def test_a_mismatch_that_heals_on_the_next_try_succeeds(tmp_path):
    account, file_path = _account(tmp_path), _file(tmp_path)
    session = FakeSession([FakeResponse(200, json.dumps({"sha256": "0" * 64})), FakeResponse(200, json.dumps({"sha256": _sha()}))])

    send_file(session, "https://loader.data.gov.gr", "the-sync-key", account, file_path, sleep=lambda s: None)

    assert len(session.calls) == 2


def test_a_file_whose_send_is_not_confirmed_stays_where_it_was(tmp_path):
    account, file_path = _account(tmp_path), _file(tmp_path)
    config = _config(max_retries=2, retry_backoff_s=0)
    session = FakeSession([FakeResponse(200, json.dumps({"sha256": "f" * 64}))] * 2)

    with pytest.raises(SendError):
        process_one(session, config, account, file_path)

    assert file_path.exists()  # not archived: the sweep will find it again


# -- a checksum file from the provider (<name>.sha256) -----------------------------------------------------------------------

def _sidecar(file_path, text):
    side = file_path.with_name(file_path.name + ".sha256")
    side.write_text(text, encoding="utf-8")
    return side


def test_a_file_that_matches_the_providers_checksum_file_is_sent_and_both_are_archived(tmp_path):
    account, file_path = _account(tmp_path), _file(tmp_path)
    side = _sidecar(file_path, "%s  data.csv\n" % _sha())
    session = FakeSession([FakeResponse(200, json.dumps({"sha256": _sha()}))])

    assert process_one(session, _config(), account, file_path) is True

    assert not file_path.exists() and not side.exists() and len(session.calls) == 1
    assert len(list((tmp_path / ".forwarded").rglob("data.csv*"))) == 2


def test_a_file_that_does_not_match_is_put_aside_not_sent_and_not_retried_for_ever(tmp_path):
    account, file_path = _account(tmp_path), _file(tmp_path)
    _sidecar(file_path, "%s *data.csv\n" % ("a" * 64))
    session = FakeSession([])

    assert process_one(session, _config(), account, file_path) is False

    assert session.calls == [] and not file_path.exists()
    kept = sorted(p.name for p in (tmp_path / ".rejected").iterdir())
    assert len(kept) == 2 and any(n.endswith("data.csv") for n in kept) and any(n.endswith("data.csv.sha256") for n in kept)


@pytest.mark.parametrize("text", ["", "\n\n", "not-a-checksum data.csv", "abc123"])
def test_an_unusable_checksum_file_also_puts_the_file_aside(tmp_path, text):
    account, file_path = _account(tmp_path), _file(tmp_path)
    _sidecar(file_path, text)
    session = FakeSession([])

    assert process_one(session, _config(), account, file_path) is False
    assert session.calls == [] and (tmp_path / ".rejected").is_dir()


def test_the_checksum_file_itself_is_never_sent_as_data(tmp_path):
    account, file_path = _account(tmp_path), _file(tmp_path)
    side = _sidecar(file_path, _sha())
    session = FakeSession([])

    assert process_one(session, _config(), account, side) is False
    assert session.calls == [] and side.exists()


def test_a_file_without_a_checksum_file_is_sent_as_before(tmp_path):
    account, file_path = _account(tmp_path), _file(tmp_path)
    session = FakeSession([FakeResponse(200)])

    assert process_one(session, _config(), account, file_path) is True


def test_the_checksum_is_accepted_in_capitals_and_with_a_byte_order_mark(tmp_path):
    from ftp_forwarder.sender import read_sidecar

    path = tmp_path / "x.sha256"
    path.write_bytes(b"\xef\xbb\xbf" + _sha().upper().encode() + b"  x\r\n")
    assert read_sidecar(path) == _sha()


def test_a_stale_checksum_file_from_an_earlier_upload_does_not_reject_a_good_file(tmp_path):
    import os
    import time

    account, file_path = _account(tmp_path), _file(tmp_path)
    side = _sidecar(file_path, "%s  data.csv\n" % ("b" * 64))  # describes some earlier version of the file
    old = time.time() - 7200
    os.utime(side, (old, old))
    session = FakeSession([FakeResponse(200)])

    assert process_one(session, _config(), account, file_path) is True
    assert not (tmp_path / ".rejected").exists() and side.exists()  # left alone, not archived with this file
