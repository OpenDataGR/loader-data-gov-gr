import base64
import os

import pytest

from providerharvest_service import connection_check, db, provider_sources
from providerharvest_service.engine.transport.base import Entry, Page, Transport


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


@pytest.fixture
def session(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "health-test.db"))
    return db.make_session_factory(engine)()


class FakeTransport(Transport):
    """A minimal real Transport subclass (not a mock of behavior) --
    connect()/close() just flip flags so tests can assert the context
    manager protocol was actually exercised, matching this repo's
    "fakes, not mocks" convention (see test_ckan_client.py)."""

    def __init__(self, entries=None, fail_on_connect=None, fail_on_list=None):
        self.connected = False
        self.closed = False
        self._entries = entries or []
        self._fail_on_connect = fail_on_connect
        self._fail_on_list = fail_on_list

    def connect(self):
        if self._fail_on_connect:
            raise self._fail_on_connect
        self.connected = True

    def list_entries(self, cursor):
        if self._fail_on_list:
            raise self._fail_on_list
        return Page(entries=self._entries, next_cursor=None)

    def open_entry(self, entry):
        raise NotImplementedError

    def close(self):
        self.closed = True


def _register_http(session, **overrides):
    form = {
        "name": "acme-open-data",
        "title": "Acme open data",
        "endpoint_url": "https://provider.example.gr/api/records",
        "transport_type": "http",
        "frequency": "DAILY",
        "auth_type": "api_key",
        "api_key": "secret-key-123",
    }
    form.update(overrides)
    return provider_sources.register(session, form)


def test_successful_check_records_ok_status_and_entry_count(session):
    source = _register_http(session)
    fake = FakeTransport(entries=[Entry(ref="r1"), Entry(ref="r2")])

    checked = connection_check.check_connection(session, source.id, transport=fake)

    assert fake.connected and fake.closed
    assert checked.last_check_status == "ok"
    assert "2 records" in checked.last_check_message
    assert checked.last_check_at is not None


def test_singular_record_count_is_grammatically_correct(session):
    source = _register_http(session)
    fake = FakeTransport(entries=[Entry(ref="r1")])

    checked = connection_check.check_connection(session, source.id, transport=fake)
    assert "1 record." in checked.last_check_message


def test_connect_failure_records_error_status_and_message(session):
    source = _register_http(session)
    fake = FakeTransport(fail_on_connect=ConnectionRefusedError("connection refused"))

    checked = connection_check.check_connection(session, source.id, transport=fake)

    assert checked.last_check_status == "error"
    assert "connection refused" in checked.last_check_message
    # connect() raised, so list_entries never ran and close() -- via
    # Transport.__exit__ -- is only called if __enter__ succeeded.
    assert not fake.connected


def test_list_entries_failure_still_closes_the_transport(session):
    source = _register_http(session)
    fake = FakeTransport(fail_on_list=RuntimeError("bad response"))

    checked = connection_check.check_connection(session, source.id, transport=fake)

    assert checked.last_check_status == "error"
    assert "bad response" in checked.last_check_message
    assert fake.connected and fake.closed


def test_check_unknown_source_raises_not_found(session):
    with pytest.raises(provider_sources.NotFound):
        connection_check.check_connection(session, "no-such-id", transport=FakeTransport())


def test_file_transports_use_file_noun_not_record(session):
    form = {
        "name": "acme-sftp",
        "title": "Acme SFTP drop",
        "endpoint_url": "sftp.example.gr",
        "transport_type": "sftp",
        "frequency": "DAILY",
        "sftp_username": "acme",
        "sftp_password": "hunter2",
        "host_key_fingerprint": "SHA256:abcdef",
    }
    source = provider_sources.register(session, form)
    fake = FakeTransport(entries=[Entry(ref="f1"), Entry(ref="f2"), Entry(ref="f3")])

    checked = connection_check.check_connection(session, source.id, transport=fake)
    assert "3 files" in checked.last_check_message


def test_build_transport_dispatches_on_transport_type(session):
    from providerharvest_service.engine.transport.direct_https import DirectHTTPSTransport
    from providerharvest_service.engine.transport.ftp import FTPTransport
    from providerharvest_service.engine.transport.scp import ScpTransport
    from providerharvest_service.engine.transport.sftp import SFTPTransport

    http_source = _register_http(session)
    secret = provider_sources.credentials(session, http_source.id)
    transport = connection_check._build_transport(http_source, secret)
    assert isinstance(transport, DirectHTTPSTransport)
    assert transport.base_url == http_source.endpoint_url

    sftp_source = provider_sources.register(session, {
        "name": "acme-sftp-2", "title": "t", "endpoint_url": "sftp.example.gr",
        "transport_type": "sftp", "sftp_username": "u", "sftp_password": "p",
        "host_key_fingerprint": "SHA256:abcdef",
    })
    secret = provider_sources.credentials(session, sftp_source.id)
    assert isinstance(connection_check._build_transport(sftp_source, secret), SFTPTransport)

    scp_source = provider_sources.register(session, {
        "name": "acme-scp", "title": "t", "endpoint_url": "scp.example.gr",
        "transport_type": "scp", "sftp_username": "u", "sftp_password": "p",
        "host_key_fingerprint": "SHA256:abcdef",
    })
    secret = provider_sources.credentials(session, scp_source.id)
    assert isinstance(connection_check._build_transport(scp_source, secret), ScpTransport)

    ftp_source = provider_sources.register(session, {
        "name": "acme-ftp-2", "title": "t", "endpoint_url": "ftp.example.gr",
        "transport_type": "ftp", "ftp_username": "u", "ftp_password": "p",
        "ftp_use_tls": "on",
    })
    secret = provider_sources.credentials(session, ftp_source.id)
    assert isinstance(connection_check._build_transport(ftp_source, secret), FTPTransport)
