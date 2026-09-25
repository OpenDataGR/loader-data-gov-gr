import base64
import io
import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from providerharvest_service import connection_check, db, harvest_runner, provider_sources
from providerharvest_service.engine.transport.base import Entry, Page, Transport
from providerharvest_service.harvest_models import HarvestBase


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


@pytest.fixture
def config_session(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "config-test.db"))
    return db.make_session_factory(engine)()


@pytest.fixture
def harvest_session(tmp_path):
    # A real database, just not Postgres -- same "prove the plumbing
    # without needing a live Postgres in CI" trick test_harvest_db.py
    # already uses.
    engine = create_engine("sqlite:///%s" % (tmp_path / "harvest-test.db"))
    HarvestBase.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _register_active_http(config_session, **overrides):
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
    source = provider_sources.register(config_session, form)
    provider_sources.activate(config_session, source.id)
    return source


def _register_active_sftp(config_session, **overrides):
    form = {
        "name": "acme-sftp", "title": "Acme SFTP", "endpoint_url": "sftp.example.gr",
        "transport_type": "sftp", "sftp_username": "u", "sftp_password": "p",
        "host_key_fingerprint": "SHA256:abcdef", "frequency": "DAILY",
    }
    form.update(overrides)
    source = provider_sources.register(config_session, form)
    provider_sources.activate(config_session, source.id)
    return source


class FakeTransport(Transport):
    def __init__(self, entries=None, error=None, files=None):
        self._entries = entries or []
        self._error = error
        # ref -> bytes, for file-transport tests; open_entry raises
        # NotImplementedError for anything not listed here, same as an
        # HTTP-only test that never expects open_entry to be called at
        # all.
        self._files = files or {}

    def connect(self):
        if self._error:
            raise self._error

    def list_entries(self, cursor):
        return Page(entries=self._entries, next_cursor=None)

    def open_entry(self, entry):
        if entry.ref not in self._files:
            raise NotImplementedError
        return io.BytesIO(self._files[entry.ref])

    def close(self):
        pass


def test_run_harvest_stores_records_on_success(config_session, harvest_session, monkeypatch):
    source = _register_active_http(config_session)
    entries = [
        Entry(ref="r1", inline_data=json.dumps({"id": 1, "name": "a"}).encode("utf-8")),
        Entry(ref="r2", inline_data=json.dumps({"id": 2, "name": "b"}).encode("utf-8")),
    ]
    monkeypatch.setattr(connection_check, "_build_transport", lambda src, secret: FakeTransport(entries=entries))

    run = harvest_runner.run_harvest(config_session, harvest_session, source.id)

    assert run.status == "success"
    assert run.records_count == 2
    assert run.finished_at is not None
    stored = harvest_runner.list_records(harvest_session, run.id)
    assert [r.ref for r in stored] == ["r1", "r2"]
    assert stored[0].payload == {"id": 1, "name": "a"}
    assert stored[1].payload == {"id": 2, "name": "b"}


def test_run_harvest_records_error_without_raising(config_session, harvest_session, monkeypatch):
    source = _register_active_http(config_session)
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda src, secret: FakeTransport(error=ConnectionError("boom")),
    )

    run = harvest_runner.run_harvest(config_session, harvest_session, source.id)

    assert run.status == "error"
    assert "boom" in run.error_message
    assert run.records_count == 0
    assert harvest_runner.list_records(harvest_session, run.id) == []


def test_run_harvest_unknown_source_raises_not_found(config_session, harvest_session):
    with pytest.raises(provider_sources.NotFound):
        harvest_runner.run_harvest(config_session, harvest_session, "no-such-id")


def test_run_harvest_rejects_a_non_active_source(config_session, harvest_session):
    # A direct call/POST must be refused the same way the UI hides the
    # button -- not just a client-side convenience.
    form = {
        "name": "acme-open-data", "title": "t",
        "endpoint_url": "https://provider.example.gr/api", "transport_type": "http",
        "auth_type": "api_key", "api_key": "k",
    }
    source = provider_sources.register(config_session, form)  # still "pending"

    with pytest.raises(provider_sources.InvalidTransition):
        harvest_runner.run_harvest(config_session, harvest_session, source.id)


def test_run_harvest_rejects_a_genuinely_unsupported_transport_type(config_session, harvest_session):
    # No real registration path can produce this any more --
    # TRANSPORT_TYPES (what `register` accepts) and SUPPORTED_
    # TRANSPORT_TYPES (what `run_harvest` can execute) are exactly the
    # same set now that sftp/scp/ftp are supported too. Constructed
    # directly to prove run_harvest's own guard still exists as
    # defense-in-depth, not solely relying on registration-time
    # validation -- same reasoning as the active-status check just
    # below it.
    source = _register_active_http(config_session)
    source.transport_type = "carrier-pigeon"
    config_session.commit()

    with pytest.raises(harvest_runner.UnsupportedTransportType):
        harvest_runner.run_harvest(config_session, harvest_session, source.id)


def test_list_runs_orders_newest_first(config_session, harvest_session, monkeypatch):
    source = _register_active_http(config_session)
    monkeypatch.setattr(connection_check, "_build_transport", lambda src, secret: FakeTransport(entries=[]))

    run1 = harvest_runner.run_harvest(config_session, harvest_session, source.id)
    run2 = harvest_runner.run_harvest(config_session, harvest_session, source.id)

    runs = harvest_runner.list_runs(harvest_session, source.id)
    assert [r.id for r in runs] == [run2.id, run1.id]


def test_list_runs_scoped_to_the_right_source(config_session, harvest_session, monkeypatch):
    source_a = _register_active_http(config_session, name="source-a")
    source_b = _register_active_http(config_session, name="source-b")
    monkeypatch.setattr(connection_check, "_build_transport", lambda src, secret: FakeTransport(entries=[]))

    run_a = harvest_runner.run_harvest(config_session, harvest_session, source_a.id)
    harvest_runner.run_harvest(config_session, harvest_session, source_b.id)

    runs = harvest_runner.list_runs(harvest_session, source_a.id)
    assert [r.id for r in runs] == [run_a.id]


def test_record_ref_and_payload_survive_a_missing_inline_body(config_session, harvest_session, monkeypatch):
    source = _register_active_http(config_session)
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda src, secret: FakeTransport(entries=[Entry(ref="file-only")]),
    )

    run = harvest_runner.run_harvest(config_session, harvest_session, source.id)

    assert run.status == "success"
    stored = harvest_runner.list_records(harvest_session, run.id)
    assert stored[0].payload == {}


# -- sftp/scp/ftp (whole-file) sources -------------------------------------


def test_run_harvest_stores_whole_files_for_an_sftp_source(config_session, harvest_session, monkeypatch):
    source = _register_active_sftp(config_session)
    entries = [
        Entry(ref="/incoming/data.csv", metadata={"size": 13}),
        Entry(ref="/incoming/report.json", metadata={"size": 2}),
    ]
    files = {"/incoming/data.csv": b"a,b,c\n1,2,3\n", "/incoming/report.json": b"{}"}
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda src, secret: FakeTransport(entries=entries, files=files),
    )

    run = harvest_runner.run_harvest(config_session, harvest_session, source.id)

    assert run.status == "success"
    assert run.records_count == 2
    stored = harvest_runner.list_files(harvest_session, run.id)
    assert [f.filename for f in stored] == ["data.csv", "report.json"]
    assert stored[0].content == b"a,b,c\n1,2,3\n"
    assert stored[0].size == len(b"a,b,c\n1,2,3\n")
    assert stored[0].content_type == "text/csv"
    assert stored[1].content_type == "application/json"
    # HarvestedRecord is the HTTP-only shape -- a file source must
    # never populate it.
    assert harvest_runner.list_records(harvest_session, run.id) == []


def test_run_harvest_supports_scp_and_ftp_too(config_session, harvest_session, monkeypatch):
    for transport_type, extra in [
        ("scp", {"name": "acme-scp", "sftp_username": "u", "sftp_password": "p", "host_key_fingerprint": "SHA256:abcdef"}),
        ("ftp", {"name": "acme-ftp", "ftp_username": "u", "ftp_password": "p", "ftp_use_tls": "on"}),
    ]:
        source = provider_sources.register(config_session, {
            "title": "t", "endpoint_url": "provider.example.gr", "transport_type": transport_type,
            "frequency": "DAILY", **extra,
        })
        provider_sources.activate(config_session, source.id)
        monkeypatch.setattr(
            connection_check, "_build_transport",
            lambda src, secret: FakeTransport(
                entries=[Entry(ref="/f.txt", metadata={"size": 5})], files={"/f.txt": b"hello"}
            ),
        )

        run = harvest_runner.run_harvest(config_session, harvest_session, source.id)

        assert run.status == "success", "%s: %s" % (transport_type, run.error_message)
        stored = harvest_runner.list_files(harvest_session, run.id)
        assert stored[0].content == b"hello"


def test_run_harvest_errors_a_file_over_the_configured_max_size(config_session, harvest_session, monkeypatch):
    source = _register_active_sftp(config_session, sftp_max_file_size_bytes="10")
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda src, secret: FakeTransport(
            entries=[Entry(ref="/big.bin")], files={"/big.bin": b"x" * 11}
        ),
    )

    run = harvest_runner.run_harvest(config_session, harvest_session, source.id)

    assert run.status == "error"
    assert "max_file_size_bytes" in run.error_message
    assert harvest_runner.list_files(harvest_session, run.id) == []


def test_get_file_returns_none_for_an_unknown_id(harvest_session):
    assert harvest_runner.get_file(harvest_session, "no-such-id") is None


def test_get_file_returns_the_stored_file(config_session, harvest_session, monkeypatch):
    source = _register_active_sftp(config_session)
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda src, secret: FakeTransport(entries=[Entry(ref="/f.txt")], files={"/f.txt": b"hello"}),
    )
    run = harvest_runner.run_harvest(config_session, harvest_session, source.id)
    stored = harvest_runner.list_files(harvest_session, run.id)[0]

    fetched = harvest_runner.get_file(harvest_session, stored.id)
    assert fetched.content == b"hello"
