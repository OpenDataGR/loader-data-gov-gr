import base64
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from providerharvest_service import db, harvest_db, provider_sources
from providerharvest_service.app import app
from providerharvest_service.harvest_models import HarvestBase


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


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
def client():
    return TestClient(app)


def _register_and_activate_push_source(client, config_session_factory, **overrides):
    form = {"name": "acme-push", "title": "Acme push", "transport_type": "push", "frequency": "MANUAL"}
    form.update(overrides)
    resp = client.post("/provider/sources/new", data=form, follow_redirects=False)
    assert resp.status_code == 200  # the one-time push-credentials page, not a redirect
    api_key = _extract_api_key(resp.text)

    with config_session_factory() as session:
        source = provider_sources.list_all(session)[0]
        source_id = source.id
        provider_sources.activate(session, source_id)
    return source_id, api_key


def _extract_api_key(html: str) -> str:
    import re
    m = re.search(r'value="([A-Za-z0-9_-]{20,})"', html)
    assert m, "no API key found in the push-credentials page"
    return m.group(1)


def test_registering_a_push_source_shows_credentials_once(client):
    resp = client.post(
        "/provider/sources/new",
        data={"name": "acme-push", "title": "Acme push", "transport_type": "push", "frequency": "MANUAL"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Push credentials" in resp.text
    assert "/push/acme-push/upload" in resp.text


def test_upload_stores_the_file_for_an_active_push_source(client, _isolated_db):
    source_id, api_key = _register_and_activate_push_source(client, _isolated_db)

    resp = client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        headers={"X-Api-Key": api_key},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    runs_page = client.get("/provider/sources/%s/runs" % source_id)
    assert "success" in runs_page.text

    import re
    run_id = re.search(r"/runs/([^\"]+)\"", runs_page.text).group(1)
    detail = client.get("/provider/sources/%s/runs/%s" % (source_id, run_id))
    assert "data.csv" in detail.text


def test_upload_rejects_a_wrong_api_key(client, _isolated_db):
    _register_and_activate_push_source(client, _isolated_db)

    resp = client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers={"X-Api-Key": "totally-wrong-key"},
    )
    assert resp.status_code == 401


def test_upload_404s_for_an_unknown_source_name(client):
    resp = client.post(
        "/push/no-such-source/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers={"X-Api-Key": "whatever"},
    )
    assert resp.status_code == 404


def test_upload_404s_for_a_still_pending_push_source(client, _isolated_db):
    resp = client.post(
        "/provider/sources/new",
        data={"name": "acme-push", "title": "Acme push", "transport_type": "push", "frequency": "MANUAL"},
        follow_redirects=False,
    )
    api_key = _extract_api_key(resp.text)
    # Deliberately never activated -- still "pending".

    resp = client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers={"X-Api-Key": api_key},
    )
    assert resp.status_code == 404


def test_upload_404s_for_an_http_source_even_with_a_valid_looking_name(client, _isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, {
            "name": "acme-http", "title": "t", "endpoint_url": "https://example.gr/api",
            "transport_type": "http", "auth_type": "api_key", "api_key": "k",
        })
        provider_sources.activate(session, source.id)

    resp = client.post(
        "/push/acme-http/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers={"X-Api-Key": "whatever"},
    )
    assert resp.status_code == 404


def test_upload_413s_for_a_file_over_the_configured_max_size(client, _isolated_db):
    source_id, api_key = _register_and_activate_push_source(
        client, _isolated_db, push_max_file_size_bytes="5"
    )

    resp = client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"x" * 6, "text/csv")},
        headers={"X-Api-Key": api_key},
    )
    assert resp.status_code == 413

    runs_page = client.get("/provider/sources/%s/runs" % source_id)
    assert "No harvest runs yet" in runs_page.text  # rejected before any run was even created
