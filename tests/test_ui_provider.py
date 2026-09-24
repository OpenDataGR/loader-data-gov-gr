import base64
import os

import pytest
from fastapi.testclient import TestClient

from providerharvest_service import db
from providerharvest_service.app import app


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


@pytest.fixture
def client():
    return TestClient(app)


def _valid_form(**overrides):
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
    return form


def test_new_source_form_renders(client):
    resp = client.get("/provider/sources/new")
    assert resp.status_code == 200
    assert "Register a new source" in resp.text


def test_registering_a_source_redirects_to_the_list(client):
    resp = client.post("/provider/sources/new", data=_valid_form(), follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/provider/sources?flash=")

    listing = client.get(resp.headers["location"])
    assert "acme-open-data" in listing.text
    assert "pending" in listing.text


def test_registering_with_missing_required_field_reshows_the_form_with_errors(client):
    resp = client.post("/provider/sources/new", data=_valid_form(api_key=""))
    assert resp.status_code == 400
    assert "Please fix the errors below" in resp.text
    # The rest of what was typed must be preserved, not lost.
    assert "acme-open-data" in resp.text


def test_pause_and_resume_flow(client, _isolated_db):
    session_factory = _isolated_db
    client.post("/provider/sources/new", data=_valid_form())

    with session_factory() as session:
        from providerharvest_service import provider_sources
        source = provider_sources.list_all(session)[0]
        provider_sources.activate(session, source.id)
        source_id = source.id

    resp = client.post("/provider/sources/%s/pause" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    listing = client.get("/provider/sources")
    assert "paused" in listing.text

    client.post("/provider/sources/%s/resume" % source_id)
    listing = client.get("/provider/sources")
    assert "active" in listing.text


def test_flash_message_with_special_characters_does_not_break_redirect(client):
    resp = client.post("/provider/sources/%s/pause" % "no-such-id", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/provider/sources?flash=")
    followed = client.get(location)
    assert followed.status_code == 200
    assert "No such source." in followed.text
