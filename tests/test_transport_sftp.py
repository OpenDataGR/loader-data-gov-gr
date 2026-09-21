import base64
import hashlib
import stat

import pytest

from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.transport.sftp import (
    HostKeyMismatchError,
    SFTPTransport,
    fetch_host_key_fingerprint,
)

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


class FakeAttr:
    def __init__(self, filename, is_dir=False, mtime=0, size=0):
        self.filename = filename
        self.st_mode = stat.S_IFDIR if is_dir else stat.S_IFREG
        self.st_mtime = mtime
        self.st_size = size


class FakeFileHandle:
    def __init__(self):
        self.prefetched = False

    def prefetch(self):
        self.prefetched = True


class FakeSFTPClient:
    def __init__(self, transport, listing=()):
        self.transport = transport
        self._listing = list(listing)
        self.opened = []
        self.closed = False

    def listdir_attr(self, path):
        return self._listing

    def open(self, path, mode):
        handle = FakeFileHandle()
        self.opened.append((path, mode, handle))
        return handle

    def close(self):
        self.closed = True


def _sftp_client_factory(listing=()):
    client_holder = {}

    def factory(transport):
        client = FakeSFTPClient(transport, listing=listing)
        client_holder["client"] = client
        return client

    return factory, client_holder


@pytest.fixture(autouse=True)
def patch_resolver(monkeypatch):
    # The actual call now lives in ssh_common (shared with ScpTransport),
    # not in this module -- see transport/ssh_common.py.
    import providerharvest_service.engine.transport.ssh_common as ssh_common_mod
    monkeypatch.setattr(
        ssh_common_mod, "assert_safe_network_target",
        lambda host, port, allow_private_ranges=False: ["203.0.113.10"],
    )
    yield


def test_fetch_host_key_fingerprint_matches_sha256_of_key_bytes():
    fingerprint = fetch_host_key_fingerprint(
        "provider.example.com", 22, transport_factory=_transport_factory()
    )
    assert fingerprint == SERVER_KEY_FINGERPRINT


def test_connect_succeeds_when_fingerprint_matches():
    sftp_factory, _ = _sftp_client_factory()
    transport = SFTPTransport(
        "provider.example.com",
        secret=SecretBundle(fields={"username": "u", "password": "p"}),
        pinned_host_key_fingerprint=SERVER_KEY_FINGERPRINT,
        transport_factory=_transport_factory(),
        sftp_client_factory=sftp_factory,
    )
    transport.connect()
    transport.close()


def test_connect_rejects_mismatched_fingerprint():
    transport = SFTPTransport(
        "provider.example.com",
        secret=SecretBundle(fields={"username": "u", "password": "p"}),
        pinned_host_key_fingerprint="SHA256:not-the-right-one",
        transport_factory=_transport_factory(),
    )
    with pytest.raises(HostKeyMismatchError):
        transport.connect()


def test_connect_rejects_when_nothing_pinned_yet():
    transport = SFTPTransport(
        "provider.example.com",
        secret=SecretBundle(fields={"username": "u", "password": "p"}),
        pinned_host_key_fingerprint=None,
        transport_factory=_transport_factory(),
    )
    with pytest.raises(HostKeyMismatchError):
        transport.connect()


def test_authenticate_uses_password_when_no_key_given():
    sftp_factory, _ = _sftp_client_factory()
    fake_transport_cls = _transport_factory()
    transport = SFTPTransport(
        "provider.example.com",
        secret=SecretBundle(fields={"username": "alice", "password": "s3cret"}),
        pinned_host_key_fingerprint=SERVER_KEY_FINGERPRINT,
        transport_factory=fake_transport_cls,
        sftp_client_factory=sftp_factory,
    )
    transport.connect()
    assert transport._transport.auth_calls == [("password", "alice", "s3cret")]


def test_authenticate_requires_username():
    transport = SFTPTransport(
        "provider.example.com",
        secret=SecretBundle(fields={"password": "s3cret"}),
        pinned_host_key_fingerprint=SERVER_KEY_FINGERPRINT,
        transport_factory=_transport_factory(),
        sftp_client_factory=_sftp_client_factory()[0],
    )
    with pytest.raises(ValueError):
        transport.connect()


def test_authenticate_requires_password_or_key():
    transport = SFTPTransport(
        "provider.example.com",
        secret=SecretBundle(fields={"username": "alice"}),
        pinned_host_key_fingerprint=SERVER_KEY_FINGERPRINT,
        transport_factory=_transport_factory(),
        sftp_client_factory=_sftp_client_factory()[0],
    )
    with pytest.raises(ValueError):
        transport.connect()


def test_list_entries_filters_directories_and_glob():
    listing = [
        FakeAttr("data-2024.csv", mtime=100, size=10),
        FakeAttr("data-2023.csv", mtime=90, size=20),
        FakeAttr("readme.txt", mtime=80, size=5),
        FakeAttr("subdir", is_dir=True),
    ]
    sftp_factory, holder = _sftp_client_factory(listing=listing)
    transport = SFTPTransport(
        "provider.example.com",
        secret=SecretBundle(fields={"username": "u", "password": "p"}),
        pinned_host_key_fingerprint=SERVER_KEY_FINGERPRINT,
        remote_path="/exports",
        glob_pattern="*.csv",
        transport_factory=_transport_factory(),
        sftp_client_factory=sftp_factory,
    )
    with transport:
        page = transport.list_entries(cursor=None)

    assert sorted(e.ref for e in page.entries) == [
        "/exports/data-2023.csv", "/exports/data-2024.csv",
    ]
    assert page.next_cursor is None
    by_ref = {e.ref: e for e in page.entries}
    assert by_ref["/exports/data-2024.csv"].metadata == {"mtime": 100, "size": 10}


def test_list_entries_second_page_is_empty():
    sftp_factory, _ = _sftp_client_factory(listing=[FakeAttr("a.csv", mtime=1, size=1)])
    transport = SFTPTransport(
        "provider.example.com",
        secret=SecretBundle(fields={"username": "u", "password": "p"}),
        pinned_host_key_fingerprint=SERVER_KEY_FINGERPRINT,
        transport_factory=_transport_factory(),
        sftp_client_factory=sftp_factory,
    )
    with transport:
        transport.list_entries(cursor=None)
        second = transport.list_entries(cursor="anything")
    assert second.entries == []
    assert second.next_cursor is None


def test_open_entry_prefetches_and_returns_handle():
    sftp_factory, holder = _sftp_client_factory(
        listing=[FakeAttr("a.csv", mtime=1, size=1)]
    )
    transport = SFTPTransport(
        "provider.example.com",
        secret=SecretBundle(fields={"username": "u", "password": "p"}),
        pinned_host_key_fingerprint=SERVER_KEY_FINGERPRINT,
        remote_path="/exports",
        transport_factory=_transport_factory(),
        sftp_client_factory=sftp_factory,
    )
    with transport:
        page = transport.list_entries(cursor=None)
        handle = transport.open_entry(page.entries[0])

    assert handle.prefetched is True
    assert holder["client"].opened[0][0] == "/exports/a.csv"


def test_on_request_reports_connect_and_list_events():
    events = []
    sftp_factory, _ = _sftp_client_factory(listing=[])
    transport = SFTPTransport(
        "provider.example.com",
        secret=SecretBundle(fields={"username": "u", "password": "p"}),
        pinned_host_key_fingerprint=SERVER_KEY_FINGERPRINT,
        transport_factory=_transport_factory(),
        sftp_client_factory=sftp_factory,
        on_request=events.append,
        harvest_source_id="src-1",
        harvest_job_id="job-1",
    )
    with transport:
        transport.list_entries(cursor=None)

    methods = [e.method for e in events]
    assert methods == ["CONNECT", "LIST"]
    assert all(e.harvest_source_id == "src-1" for e in events)
    assert all(e.error is None for e in events)
