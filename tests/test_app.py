import pytest
from fastapi.testclient import TestClient

from providerharvest_service import harvest_db
from providerharvest_service.app import app


def test_health_returns_ok():
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.fixture(autouse=True)
def _admin_key(monkeypatch):
    monkeypatch.setenv("LOADER_ADMIN_API_KEY", "test-admin-key")


@pytest.fixture(autouse=True)
def _reset_harvest_db_lazy_state(monkeypatch):
    monkeypatch.setattr(harvest_db, "_engine", None)
    monkeypatch.setattr(harvest_db, "_SessionLocal", None)


def test_harvest_db_health_requires_admin_key():
    client = TestClient(app)
    resp = client.get("/admin/harvest-db/health")
    assert resp.status_code == 401


def test_harvest_db_health_ok_when_reachable(monkeypatch, tmp_path):
    monkeypatch.setenv("LOADER_HARVEST_DB_URL", "sqlite:///%s" % (tmp_path / "fake-harvest.db"))
    client = TestClient(app)
    resp = client.get("/admin/harvest-db/health", headers={"X-Admin-Key": "test-admin-key"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_harvest_db_health_503_when_unconfigured(monkeypatch):
    monkeypatch.delenv("LOADER_HARVEST_DB_URL", raising=False)
    client = TestClient(app)
    resp = client.get("/admin/harvest-db/health", headers={"X-Admin-Key": "test-admin-key"})
    assert resp.status_code == 503
    assert "harvest DB unreachable" in resp.json()["detail"]
