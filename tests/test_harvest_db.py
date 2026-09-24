import pytest

from providerharvest_service import harvest_db


@pytest.fixture(autouse=True)
def _reset_lazy_state(monkeypatch):
    # Module-level cache: reset between tests so one test's engine
    # (real or fake) never leaks into the next.
    monkeypatch.setattr(harvest_db, "_engine", None)
    monkeypatch.setattr(harvest_db, "_SessionLocal", None)
    yield
    monkeypatch.setattr(harvest_db, "_engine", None)
    monkeypatch.setattr(harvest_db, "_SessionLocal", None)


def test_get_session_raises_clearly_when_unconfigured(monkeypatch):
    monkeypatch.delenv("LOADER_HARVEST_DB_URL", raising=False)
    with pytest.raises(harvest_db.HarvestDbNotConfigured):
        next(harvest_db.get_session())


def test_check_connectivity_raises_clearly_when_unconfigured(monkeypatch):
    monkeypatch.delenv("LOADER_HARVEST_DB_URL", raising=False)
    with pytest.raises(harvest_db.HarvestDbNotConfigured):
        harvest_db.check_connectivity()


def test_check_connectivity_does_a_real_round_trip(monkeypatch, tmp_path):
    # A real database connection, just not Postgres -- proves the lazy
    # init + round-trip plumbing without needing a live Postgres in CI.
    monkeypatch.setenv("LOADER_HARVEST_DB_URL", "sqlite:///%s" % (tmp_path / "fake-harvest.db"))
    harvest_db.check_connectivity()  # must not raise


def test_get_session_yields_a_working_session(monkeypatch, tmp_path):
    monkeypatch.setenv("LOADER_HARVEST_DB_URL", "sqlite:///%s" % (tmp_path / "fake-harvest.db"))
    from sqlalchemy import text

    session = next(harvest_db.get_session())
    try:
        result = session.execute(text("SELECT 1")).scalar()
        assert result == 1
    finally:
        session.close()


def test_engine_is_only_constructed_once(monkeypatch, tmp_path):
    monkeypatch.setenv("LOADER_HARVEST_DB_URL", "sqlite:///%s" % (tmp_path / "fake-harvest.db"))
    harvest_db.check_connectivity()
    engine_after_first_call = harvest_db._engine
    harvest_db.check_connectivity()
    assert harvest_db._engine is engine_after_first_call
