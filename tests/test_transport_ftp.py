import pytest

from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.transport.ftp import FTPTransport, PlainFtpNotAcknowledgedError


class FakeFtpClient:
    def __init__(self, mlsd_listing=(), files=None, login_error=None):
        self.connected = None
        self.login_calls = []
        self.prot_p_called = False
        self.quit_called = False
        self._mlsd_listing = list(mlsd_listing)
        self._files = files or {}
        self.login_error = login_error
        self.retrbinary_calls = []

    def connect(self, host, port, timeout=None):
        self.connected = (host, port, timeout)

    def login(self, username, password):
        if self.login_error:
            raise self.login_error
        self.login_calls.append((username, password))

    def prot_p(self):
        self.prot_p_called = True

    def mlsd(self, path=""):
        return iter(self._mlsd_listing)

    def retrbinary(self, cmd, callback):
        self.retrbinary_calls.append(cmd)
        remote_path = cmd.split(" ", 1)[1]
        callback(self._files[remote_path])

    def quit(self):
        self.quit_called = True


@pytest.fixture(autouse=True)
def patch_resolver(monkeypatch):
    import providerharvest_service.engine.transport.ftp as mod
    monkeypatch.setattr(
        mod, "assert_safe_network_target",
        lambda host, port, allow_private_ranges=False: ["203.0.113.10"],
    )
    yield


def _transport(client=None, **overrides):
    client = client or FakeFtpClient()
    defaults = dict(
        host="provider.example.com",
        secret=SecretBundle(fields={"username": "u", "password": "p"}),
        remote_path="/exports",
        glob_pattern="*.csv",
        client_factory=lambda: client,
    )
    defaults.update(overrides)
    return FTPTransport(defaults.pop("host"), **defaults), client


def test_connect_logs_in_and_secures_data_channel_by_default():
    transport, client = _transport()
    transport.connect()
    assert client.login_calls == [("u", "p")]
    assert client.prot_p_called is True
    transport.close()
    assert client.quit_called is True


def test_connect_rejects_plain_ftp_without_acknowledgment():
    transport, client = _transport(use_tls=False)
    with pytest.raises(PlainFtpNotAcknowledgedError):
        transport.connect()
    assert client.login_calls == []


def test_connect_allows_plain_ftp_when_acknowledged():
    transport, client = _transport(use_tls=False, plain_ftp_acknowledged=True)
    transport.connect()
    assert client.login_calls == [("u", "p")]
    assert client.prot_p_called is False  # no TLS -- nothing to secure


def test_connect_requires_username():
    transport, client = _transport(secret=SecretBundle(fields={"password": "p"}))
    with pytest.raises(ValueError):
        transport.connect()


def test_list_entries_filters_by_type_and_glob():
    listing = [
        ("data-2024.csv", {"type": "file", "size": "10", "modify": "20240101000000"}),
        ("data-2023.csv", {"type": "file", "size": "20", "modify": "20230101000000"}),
        ("readme.txt", {"type": "file", "size": "5", "modify": "20220101000000"}),
        ("subdir", {"type": "dir"}),
    ]
    transport, _ = _transport(client=FakeFtpClient(mlsd_listing=listing))
    with transport:
        page = transport.list_entries(cursor=None)

    assert sorted(e.ref for e in page.entries) == [
        "/exports/data-2023.csv", "/exports/data-2024.csv",
    ]
    assert page.next_cursor is None
    by_ref = {e.ref: e for e in page.entries}
    assert by_ref["/exports/data-2024.csv"].metadata == {"size": 10, "modify": "20240101000000"}


def test_list_entries_second_page_is_empty():
    transport, _ = _transport(client=FakeFtpClient(
        mlsd_listing=[("a.csv", {"type": "file", "size": "1"})]
    ))
    with transport:
        transport.list_entries(cursor=None)
        second = transport.list_entries(cursor="anything")
    assert second.entries == []
    assert second.next_cursor is None


def test_open_entry_downloads_to_a_temp_file_and_cleans_up_on_close():
    import os

    from providerharvest_service.engine.transport.base import Entry

    client = FakeFtpClient(files={"/exports/data-2024.csv": b"id,name\n1,Alpha\n"})
    transport, _ = _transport(client=client)
    with transport:
        handle = transport.open_entry(Entry(ref="/exports/data-2024.csv", metadata={}))
        content = handle.read()
        local_path = handle._path
        assert os.path.exists(local_path)
        handle.close()
        assert not os.path.exists(local_path)

    assert content == b"id,name\n1,Alpha\n"
    assert client.retrbinary_calls == ["RETR /exports/data-2024.csv"]


def test_open_entry_handle_is_seekable():
    # CKAN's own resource uploader seeks to measure the file size before
    # copying it -- a raw FTP data-connection socket can't support that,
    # confirmed by a real CI failure with the first (streamed-socket)
    # version of this method.
    from providerharvest_service.engine.transport.base import Entry

    client = FakeFtpClient(files={"/exports/data-2024.csv": b"id,name\n1,Alpha\n"})
    transport, _ = _transport(client=client)
    with transport:
        handle = transport.open_entry(Entry(ref="/exports/data-2024.csv", metadata={}))
        try:
            handle.seek(0, 2)  # SEEK_END
            size = handle.tell()
            handle.seek(0)
            assert size == len(b"id,name\n1,Alpha\n")
            assert handle.read() == b"id,name\n1,Alpha\n"
        finally:
            handle.close()


def test_on_request_reports_connect_and_list_events():
    events = []
    transport, _ = _transport(
        client=FakeFtpClient(mlsd_listing=[]),
        on_request=events.append, harvest_source_id="src-1", harvest_job_id="job-1",
    )
    with transport:
        transport.list_entries(cursor=None)

    methods = [e.method for e in events]
    assert methods == ["CONNECT", "LIST"]
    assert all(e.harvest_source_id == "src-1" for e in events)
    assert all(e.error is None for e in events)
