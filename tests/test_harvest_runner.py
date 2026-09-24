import base64
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


class FakeTransport(Transport):
    def __init__(self, entries=None, error=None):
        self._entries = entries or []
        self._error = error

    def connect(self):
        if self._error:
            raise self._error

    def list_entries(self, cursor):
        return Page(entries=self._entries, next_cursor=None)

    def open_entry(self, entry):
        raise NotImplementedError

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


def test_run_harvest_rejects_non_http_transport(config_session, harvest_session):
    source = provider_sources.register(config_session, {
        "name": "acme-sftp", "title": "t", "endpoint_url": "sftp.example.gr",
        "transport_type": "sftp", "sftp_username": "u", "sftp_password": "p",
        "host_key_fingerprint": "SHA256:abcdef",
    })
    provider_sources.activate(config_session, source.id)

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
