import base64
import datetime
import os
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from providerharvest_service import connection_check, db, provider_sources, scheduler
from providerharvest_service.engine.transport.base import Entry, Page, Transport
from providerharvest_service.harvest_models import HarvestBase, HarvestRun
from providerharvest_service.models import _utcnow


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


@pytest.fixture
def config_session_factory(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "config-test.db"))
    return db.make_session_factory(engine)


@pytest.fixture
def config_session(config_session_factory):
    session = config_session_factory()
    yield session
    session.close()


@pytest.fixture
def harvest_session_factory(tmp_path):
    engine = create_engine("sqlite:///%s" % (tmp_path / "harvest-test.db"))
    HarvestBase.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def harvest_session(harvest_session_factory):
    session = harvest_session_factory()
    yield session
    session.close()


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


def _add_run(harvest_session, source_id, *, started_at, finished_at, status="success"):
    run = HarvestRun(source_id=source_id, source_name="x", status=status, started_at=started_at, finished_at=finished_at)
    harvest_session.add(run)
    harvest_session.commit()
    return run


# -- due_sources ---------------------------------------------------------


def test_due_sources_includes_a_source_that_has_never_run(config_session, harvest_session):
    source = _register_active_http(config_session)
    assert [s.id for s in scheduler.due_sources(config_session, harvest_session)] == [source.id]


def test_due_sources_excludes_manual_frequency(config_session, harvest_session):
    _register_active_http(config_session, frequency="MANUAL")
    assert scheduler.due_sources(config_session, harvest_session) == []


@pytest.mark.parametrize("status", ["pending", "paused", "rejected"])
def test_due_sources_excludes_a_non_active_source(config_session, harvest_session, status):
    source = _register_active_http(config_session)
    if status == "paused":
        provider_sources.pause(config_session, source.id)
    elif status == "rejected":
        # can't reject an already-active source through the public API;
        # simulate directly, same as any other "shouldn't normally
        # happen but must still be handled safely" case.
        source.status = "rejected"
        config_session.commit()
    else:
        source.status = "pending"
        config_session.commit()
    assert scheduler.due_sources(config_session, harvest_session) == []


def test_due_sources_includes_an_active_sftp_source_too(config_session, harvest_session):
    # sftp/scp/ftp are whole-file sources harvest_runner can execute
    # (see harvest_runner.SUPPORTED_TRANSPORT_TYPES) same as http --
    # the scheduler must pick these up on their own frequency too, not
    # just http ones.
    source = provider_sources.register(config_session, {
        "name": "acme-sftp", "title": "t", "endpoint_url": "sftp.example.gr",
        "transport_type": "sftp", "sftp_username": "u", "sftp_password": "p",
        "host_key_fingerprint": "SHA256:abcdef", "frequency": "DAILY",
    })
    provider_sources.activate(config_session, source.id)
    assert [s.id for s in scheduler.due_sources(config_session, harvest_session)] == [source.id]


def test_due_sources_excludes_a_genuinely_unsupported_transport_type(config_session, harvest_session):
    # No real registration path can produce this -- constructed
    # directly to prove this filter is real defense-in-depth, not
    # dead code now that TRANSPORT_TYPES and SUPPORTED_TRANSPORT_TYPES
    # are the same set.
    source = _register_active_http(config_session)
    source.transport_type = "carrier-pigeon"
    config_session.commit()
    assert scheduler.due_sources(config_session, harvest_session) == []


def test_due_sources_excludes_a_source_run_within_its_own_interval(config_session, harvest_session):
    source = _register_active_http(config_session)  # DAILY
    now = _utcnow()
    _add_run(harvest_session, source.id, started_at=now - datetime.timedelta(hours=2), finished_at=now - datetime.timedelta(hours=1))
    assert scheduler.due_sources(config_session, harvest_session, now=now) == []


def test_due_sources_includes_a_source_past_its_own_interval(config_session, harvest_session):
    source = _register_active_http(config_session)  # DAILY
    now = _utcnow()
    _add_run(harvest_session, source.id, started_at=now - datetime.timedelta(days=3), finished_at=now - datetime.timedelta(days=2))
    assert [s.id for s in scheduler.due_sources(config_session, harvest_session, now=now)] == [source.id]


def test_due_sources_excludes_a_run_still_legitimately_in_progress(config_session, harvest_session):
    source = _register_active_http(config_session)
    now = _utcnow()
    _add_run(harvest_session, source.id, started_at=now - datetime.timedelta(minutes=5), finished_at=None, status="running")
    assert scheduler.due_sources(config_session, harvest_session, now=now) == []


def test_due_sources_includes_a_run_stuck_past_the_threshold(config_session, harvest_session):
    source = _register_active_http(config_session)
    now = _utcnow()
    _add_run(harvest_session, source.id, started_at=now - datetime.timedelta(hours=2), finished_at=None, status="running")
    assert [s.id for s in scheduler.due_sources(config_session, harvest_session, now=now)] == [source.id]


# -- run_due_harvests ------------------------------------------------------


def test_run_due_harvests_harvests_every_due_source(config_session_factory, harvest_session_factory, monkeypatch):
    with config_session_factory() as setup:
        source_id = _register_active_http(setup).id
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda src, secret: FakeTransport(entries=[Entry(ref="r1", inline_data=b'{"id": 1}')]),
    )

    results = scheduler.run_due_harvests(config_session_factory, harvest_session_factory)

    assert len(results) == 1
    assert results[0].source_id == source_id
    assert results[0].status == "success"


def test_run_due_harvests_skips_nothing_when_nothing_is_due(config_session_factory, harvest_session_factory):
    with config_session_factory() as setup:
        _register_active_http(setup, frequency="MANUAL")
    assert scheduler.run_due_harvests(config_session_factory, harvest_session_factory) == []


def test_run_due_harvests_returns_empty_without_the_lock(config_session_factory, harvest_session_factory, monkeypatch):
    with config_session_factory() as setup:
        _register_active_http(setup)
    monkeypatch.setattr(scheduler, "_try_acquire_lock", lambda session: False)
    assert scheduler.run_due_harvests(config_session_factory, harvest_session_factory) == []


def test_run_due_harvests_skips_a_source_that_fails_run_harvest_guards(
    config_session_factory, harvest_session_factory, monkeypatch
):
    with config_session_factory() as setup:
        _register_active_http(setup)

    from providerharvest_service import harvest_runner

    def _boom(config_session, harvest_session, source_id):
        raise harvest_runner.UnsupportedTransportType("nope")

    monkeypatch.setattr(harvest_runner, "run_harvest", _boom)
    assert scheduler.run_due_harvests(config_session_factory, harvest_session_factory) == []


# -- lock behavior (no-op outside Postgres) --------------------------------


def test_lock_acquire_and_release_are_noops_on_sqlite(harvest_session):
    assert scheduler._try_acquire_lock(harvest_session) is True
    scheduler._release_lock(harvest_session)  # must not raise


# -- background thread ------------------------------------------------------


def test_start_background_thread_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("LOADER_SCHEDULER_ENABLED", "0")
    assert scheduler.start_background_thread(lambda: None, lambda: None) is None


def test_start_background_thread_ticks_promptly(monkeypatch):
    monkeypatch.delenv("LOADER_SCHEDULER_ENABLED", raising=False)
    ticked = threading.Event()

    def fake_run_due_harvests(config_factory, harvest_factory, now=None):
        ticked.set()
        return []

    monkeypatch.setattr(scheduler, "run_due_harvests", fake_run_due_harvests)
    thread = scheduler.start_background_thread(lambda: None, lambda: None, interval_s=9999)

    assert thread is not None
    assert thread.daemon
    assert ticked.wait(timeout=2), "background loop never called run_due_harvests"
    thread.stop_event.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_start_background_thread_survives_a_bad_tick(monkeypatch):
    monkeypatch.delenv("LOADER_SCHEDULER_ENABLED", raising=False)
    ticked_again = threading.Event()
    calls = []

    def fake_run_due_harvests(config_factory, harvest_factory, now=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        ticked_again.set()
        return []

    monkeypatch.setattr(scheduler, "run_due_harvests", fake_run_due_harvests)
    thread = scheduler.start_background_thread(lambda: None, lambda: None, interval_s=0.01)

    assert ticked_again.wait(timeout=2), "a raising tick must not kill the loop"
    thread.stop_event.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
