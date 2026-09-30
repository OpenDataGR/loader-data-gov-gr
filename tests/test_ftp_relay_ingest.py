import base64
import os
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from providerharvest_service import db, harvest_db, harvest_runner, provider_sources
from providerharvest_service.app import app
from providerharvest_service.harvest_models import HarvestBase
from providerharvest_service.models import FtpAccount

SYNC_KEY = "test-ftp-sync-key"
SYNC_HEADERS = {"X-Ftp-Sync-Key": SYNC_KEY}


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


@pytest.fixture(autouse=True)
def _sync_key(monkeypatch):
    monkeypatch.setenv("LOADER_FTP_SYNC_KEY", SYNC_KEY)


@pytest.fixture(autouse=True)
def _upload_root(monkeypatch):
    monkeypatch.setenv("LOADER_FTP_UPLOAD_ROOT", "/srv/ftp-uploads")


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "test.db"))
    session_factory = db.make_session_factory(engine)

    def _override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db.get_session] = _override
    yield session_factory
    app.dependency_overrides.pop(db.get_session, None)


@pytest.fixture(autouse=True)
def _isolated_harvest_db(tmp_path):
    engine = create_engine("sqlite:///%s" % (tmp_path / "harvest-test.db"))
    HarvestBase.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def _override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[harvest_db.get_session] = _override
    yield session_factory
    app.dependency_overrides.pop(harvest_db.get_session, None)


@pytest.fixture
def client(logged_in_client):
    return logged_in_client


def _register_and_activate_push_source(session_factory, **overrides):
    form = {"name": "acme-push", "title": "Acme push", "transport_type": "push", "frequency": "MANUAL"}
    form.update(overrides)
    with session_factory() as session:
        source = provider_sources.register(session, form)
        source_id = source.id
        provider_sources.activate(session, source_id)
    return source_id


def _create_ftp_account(session_factory, *, username="acme-box-account", push_target_source_name=None, enabled=True):
    with session_factory() as session:
        account = FtpAccount(
            username=username,
            password_hash="$2a$notchecked",
            home_directory="/srv/ftp-uploads/%s" % username,
            protocol="ftp",
            enabled=enabled,
            push_target_source_name=push_target_source_name,
        )
        session.add(account)
        session.commit()
        return account.id


def test_relay_upload_requires_the_sync_key_not_the_admin_key(client, _isolated_db):
    _register_and_activate_push_source(_isolated_db)
    _create_ftp_account(_isolated_db, push_target_source_name="acme-push")

    resp = client.post(
        "/admin/ftp-relay/acme-box-account/upload",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        headers={"X-Admin-Key": "whatever"},
    )
    assert resp.status_code == 401


def test_relay_upload_stores_the_file_for_a_mapped_active_push_source(client, _isolated_db):
    source_id = _register_and_activate_push_source(_isolated_db)
    _create_ftp_account(_isolated_db, push_target_source_name="acme-push")

    resp = client.post(
        "/admin/ftp-relay/acme-box-account/upload",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    runs_page = client.get("/provider/sources/%s/runs" % source_id)
    assert "success" in runs_page.text
    run_id = re.search(r"/runs/([^\"]+)\"", runs_page.text).group(1)
    detail = client.get("/provider/sources/%s/runs/%s" % (source_id, run_id))
    assert "data.csv" in detail.text


def test_relay_upload_404s_for_an_unknown_username(client):
    resp = client.post(
        "/admin/ftp-relay/no-such-account/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 404


def test_relay_upload_404s_for_a_disabled_account(client, _isolated_db):
    _register_and_activate_push_source(_isolated_db)
    _create_ftp_account(_isolated_db, push_target_source_name="acme-push", enabled=False)

    resp = client.post(
        "/admin/ftp-relay/acme-box-account/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 404


def test_relay_upload_404s_when_no_forwarding_target_is_configured(client, _isolated_db):
    _create_ftp_account(_isolated_db, push_target_source_name=None)

    resp = client.post(
        "/admin/ftp-relay/acme-box-account/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 404


def test_relay_upload_404s_when_the_target_source_is_still_pending(client, _isolated_db):
    with _isolated_db() as session:
        provider_sources.register(
            session, {"name": "acme-push", "title": "Acme push", "transport_type": "push", "frequency": "MANUAL"}
        )
    # Deliberately never activated.
    _create_ftp_account(_isolated_db, push_target_source_name="acme-push")

    resp = client.post(
        "/admin/ftp-relay/acme-box-account/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 404


def test_relay_upload_404s_when_the_target_is_not_a_push_source(client, _isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, {
            "name": "acme-http", "title": "t", "endpoint_url": "https://example.gr/api",
            "transport_type": "http", "auth_type": "api_key", "api_key": "k",
        })
        provider_sources.activate(session, source.id)
    _create_ftp_account(_isolated_db, push_target_source_name="acme-http")

    resp = client.post(
        "/admin/ftp-relay/acme-box-account/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 404


def test_relay_upload_413s_for_a_file_over_the_system_wide_max_size(client, _isolated_db, monkeypatch):
    monkeypatch.setattr(harvest_runner, "MAX_FILE_SIZE_BYTES", 5)
    source_id = _register_and_activate_push_source(_isolated_db)
    _create_ftp_account(_isolated_db, push_target_source_name="acme-push")

    resp = client.post(
        "/admin/ftp-relay/acme-box-account/upload",
        files={"file": ("data.csv", b"x" * 6, "text/csv")},
        headers=SYNC_HEADERS,
    )
    assert resp.status_code == 413

    runs_page = client.get("/provider/sources/%s/runs" % source_id)
    assert "No harvest runs yet" in runs_page.text


def test_a_relayed_file_records_arrival_but_not_the_relay_address_as_sender(client, _isolated_db):
    source_id = _register_and_activate_push_source(_isolated_db)
    _create_ftp_account(_isolated_db, push_target_source_name="acme-push")

    resp = client.post(
        "/admin/ftp-relay/acme-box-account/upload",
        files={"file": ("data.csv", b"a,b\n", "text/csv")},
        headers={**SYNC_HEADERS, "X-Forwarded-For": "203.0.113.50:1, 10.0.0.4"},  # the relay box
    )
    assert resp.status_code == 200

    with _isolated_db() as session:
        source = provider_sources.get_or_404(session, source_id)
        assert source.last_received_at is not None
        assert source.last_sender is None  # the provider address comes from the box report instead
