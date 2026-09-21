import base64
import hashlib

import pytest

from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.transport.scp import HostKeyMismatchError, ScpTransport

SERVER_KEY_BYTES = b"fake-host-key-bytes"
SERVER_KEY_FINGERPRINT = "SHA256:" + base64.b64encode(
    hashlib.sha256(SERVER_KEY_BYTES).digest()
).decode("ascii").rstrip("=")


class FakeKey:
    def __init__(self, blob=SERVER_KEY_BYTES):
        self._blob = blob

    def asbytes(self):
        return self._blob


class FakeParamikoTransport:
    def __init__(self, address, server_key=FakeKey(), start_client_error=None, auth_error=None):
        self.address = address
        self._server_key = server_key
        self.start_client_error = start_client_error
        self.auth_error = auth_error
        self.started = False
        self.closed = False
        self.auth_calls = []

    def start_client(self, timeout=None):
        if self.start_client_error:
            raise self.start_client_error
        self.started = True

    def get_remote_server_key(self):
        return self._server_key

    def auth_publickey(self, username, pkey):
        if self.auth_error:
            raise self.auth_error
        self.auth_calls.append(("publickey", username, pkey))

    def auth_password(self, username, password):
        if self.auth_error:
            raise self.auth_error
        self.auth_calls.append(("password", username, password))

    def close(self):
        self.closed = True


def _transport_factory(**kwargs):
    return lambda address: FakeParamikoTransport(address, **kwargs)


class FakeScpClient:
    def __init__(self, transport, fetched=()):
        self.transport = transport
        self._fetched = dict(fetched)  # remote_path -> bytes content
        self.get_calls = []
        self.closed = False

    def get(self, remote_path, local_path):
        self.get_calls.append((remote_path, local_path))
        content = self._fetched.get(remote_path, b"")
        with open(local_path, "wb") as fh:
            fh.write(content)

    def close(self):
        self.closed = True


def _scp_client_factory(fetched=()):
    holder = {}

    def factory(transport):
        client = FakeScpClient(transport, fetched=fetched)
        holder["client"] = client
        return client

    return factory, holder


def _fake_command_runner(stdout: bytes = b"", exit_status: int = 0, error: Exception = None):
    calls = []

    def runner(transport, command, timeout_s):
        calls.append((transport, command, timeout_s))
        if error is not None:
            raise error
        return stdout

    return runner, calls


@pytest.fixture(autouse=True)
def patch_resolver(monkeypatch):
    import providerharvest_service.engine.transport.ssh_common as ssh_common_mod
    monkeypatch.setattr(
        ssh_common_mod, "assert_safe_network_target",
        lambda host, port, allow_private_ranges=False: ["203.0.113.10"],
    )
    yield


def _transport(**overrides):
    runner, calls = _fake_command_runner(
        stdout=b"data-2024.csv\t1700000000.0\t123\ndata-2023.csv\t1690000000.0\t45\n"
    )
    defaults = dict(
        host="provider.example.com",
        secret=SecretBundle(fields={"username": "u", "password": "p"}),
        pinned_host_key_fingerprint=SERVER_KEY_FINGERPRINT,
        remote_path="/exports",
        glob_pattern="*.csv",
        transport_factory=_transport_factory(),
        command_runner=runner,
    )
    defaults.update(overrides)
    return ScpTransport(defaults.pop("host"), **defaults), calls


def test_connect_succeeds_when_fingerprint_matches():
    transport, _ = _transport()
    transport.connect()
    transport.close()


def test_connect_rejects_mismatched_fingerprint():
    transport, _ = _transport(pinned_host_key_fingerprint="SHA256:not-the-right-one")
    with pytest.raises(HostKeyMismatchError):
        transport.connect()


def test_connect_rejects_when_nothing_pinned_yet():
    transport, _ = _transport(pinned_host_key_fingerprint=None)
    with pytest.raises(HostKeyMismatchError):
        transport.connect()


def test_authenticate_uses_password_when_no_key_given():
    transport, _ = _transport(
        secret=SecretBundle(fields={"username": "alice", "password": "s3cret"}),
    )
    transport.connect()
    assert transport._transport.auth_calls == [("password", "alice", "s3cret")]


def test_authenticate_requires_username():
    transport, _ = _transport(secret=SecretBundle(fields={"password": "s3cret"}))
    with pytest.raises(ValueError):
        transport.connect()


def test_list_entries_runs_a_fixed_find_command_and_parses_output():
    transport, calls = _transport()
    with transport:
        page = transport.list_entries(cursor=None)

    assert len(calls) == 1
    _, command, _ = calls[0]
    # shlex.quote() leaves plain paths unquoted (no shell-unsafe chars),
    # but always quotes the glob (it contains '*').
    assert command.startswith("find /exports")
    assert "-name '*.csv'" in command

    assert sorted(e.ref for e in page.entries) == [
        "/exports/data-2023.csv", "/exports/data-2024.csv",
    ]
    assert page.next_cursor is None
    by_ref = {e.ref: e for e in page.entries}
    assert by_ref["/exports/data-2024.csv"].metadata == {"mtime": 1700000000.0, "size": 123}


def test_list_entries_second_page_is_empty():
    transport, _ = _transport()
    with transport:
        transport.list_entries(cursor=None)
        second = transport.list_entries(cursor="anything")
    assert second.entries == []
    assert second.next_cursor is None


def test_list_entries_skips_malformed_lines():
    runner, _ = _fake_command_runner(stdout=b"good.csv\t1.0\t10\nmalformed-line-no-tabs\n")
    transport, _ = _transport(command_runner=runner)
    with transport:
        page = transport.list_entries(cursor=None)
    assert [e.ref for e in page.entries] == ["/exports/good.csv"]


def test_list_entries_rejects_unsafe_remote_path():
    transport, _ = _transport(remote_path="/exports; rm -rf /")
    with transport:
        with pytest.raises(ValueError):
            transport.list_entries(cursor=None)


def test_list_entries_rejects_unsafe_glob_pattern():
    transport, _ = _transport(glob_pattern="*.csv; cat /etc/passwd")
    with transport:
        with pytest.raises(ValueError):
            transport.list_entries(cursor=None)


def test_open_entry_downloads_to_a_temp_file_and_cleans_up_on_close():
    from providerharvest_service.engine.transport.base import Entry

    scp_factory, holder = _scp_client_factory(
        fetched={"/exports/data-2024.csv": b"id,name\n1,Alpha\n"}
    )
    transport, _ = _transport(scp_client_factory=scp_factory)
    with transport:
        handle = transport.open_entry(Entry(ref="/exports/data-2024.csv", metadata={}))
        content = handle.read()
        local_path = handle._path
        import os
        assert os.path.exists(local_path)
        handle.close()
        assert not os.path.exists(local_path)

    assert content == b"id,name\n1,Alpha\n"
    assert holder["client"].get_calls == [("/exports/data-2024.csv", local_path)]
    assert holder["client"].closed is True


def test_open_entry_handle_is_seekable():
    # CKAN's own resource uploader seeks to measure the file size before
    # copying it -- confirmed by a real CI failure the first version of
    # this handle didn't support seek() at all.
    from providerharvest_service.engine.transport.base import Entry

    scp_factory, _ = _scp_client_factory(
        fetched={"/exports/data-2024.csv": b"id,name\n1,Alpha\n"}
    )
    transport, _ = _transport(scp_client_factory=scp_factory)
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
        on_request=events.append, harvest_source_id="src-1", harvest_job_id="job-1",
    )
    with transport:
        transport.list_entries(cursor=None)

    methods = [e.method for e in events]
    assert methods == ["CONNECT", "LIST"]
    assert all(e.harvest_source_id == "src-1" for e in events)
    assert all(e.error is None for e in events)
