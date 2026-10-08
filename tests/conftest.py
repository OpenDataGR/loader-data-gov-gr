import sqlite3

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from providerharvest_service.app import app

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"


@event.listens_for(Engine, "connect")
def _throwaway_databases_do_not_wait_for_the_disk(dbapi_connection, _record):
    """Every test builds its own SQLite database (~130 statements to create the schema), and by
    default each one waits for the disk to confirm the write -- most of a test's setup time. These
    databases live for a fraction of a second in a temp directory and are never read back after a
    crash, so they skip that. Only this test process is affected: the service itself sets no such
    pragma (test_sqlite_durability.py guards that)."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.execute("PRAGMA journal_mode=MEMORY")
        cursor.close()

# bcrypt is deliberately slow: at its production strength (cost 12, ~0.25 s a
# hash) the suite spent most of its 7 minutes hashing the same admin password
# in every test's setup, then again at login. Tests get bcrypt's *minimum* cost
# (4) instead. Everything else about the hashing is real -- the same library,
# the same $2a$/$2b$ formats, the same checkpw path -- and a hash's cost is
# stored inside it, so verification is cheap too. Production is untouched: this
# only replaces the default of ``bcrypt.gensalt`` inside the test process, and
# test_bcrypt_cost.py guards that nothing in the service lowers it for real.
_REAL_GENSALT = bcrypt.gensalt
TEST_BCRYPT_ROUNDS = 4


@pytest.fixture(autouse=True)
def _cheap_bcrypt(monkeypatch):
    def gensalt(rounds=TEST_BCRYPT_ROUNDS, prefix=b"2b"):
        return _REAL_GENSALT(rounds=rounds, prefix=prefix)

    monkeypatch.setattr(bcrypt, "gensalt", gensalt)


@pytest.fixture(autouse=True)
def _fresh_access_rules():
    """The blacklist/whitelist is cached in-process for a few seconds (see
    access_control.cached_snapshot); a rule added in one test must not still be
    cached when the next one starts."""
    from providerharvest_service import access_control

    access_control.invalidate()
    yield
    access_control.invalidate()


@pytest.fixture(autouse=True)
def _failed_auth_goes_to_a_throwaway_db(tmp_path, monkeypatch):
    """Refused logins/keys are recorded through ``db.SessionLocal`` (see
    failed_auth.py), outside the get_session dependency that tests override.
    Without this every 401 any test provokes would be written to the real
    default database file."""
    from providerharvest_service import db

    class LazySessionLocal:
        """Same thing as ``db.SessionLocal``, but the database is only built the first
        time something opens a session -- most tests never record a refusal, and
        creating a database for every one of them was a large part of per-test setup."""

        def __init__(self):
            self._factory = None

        def __call__(self, *args, **kwargs):
            if self._factory is None:
                self._factory = db.make_session_factory(db.make_engine("sqlite:///%s" % (tmp_path / "failed-auth.db")))
            return self._factory(*args, **kwargs)

    monkeypatch.setattr(db, "SessionLocal", LazySessionLocal())


@pytest.fixture
def admin_session_env(monkeypatch):
    monkeypatch.setenv("LOADER_ADMIN_UI_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv(
        "LOADER_ADMIN_UI_PASSWORD_HASH",
        bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode(),
    )
    monkeypatch.setenv("LOADER_ADMIN_SESSION_SECRET", "test-session-secret")


@pytest.fixture
def logged_in_client(admin_session_env):
    """A client with a valid login session -- the whole site sits behind
    the login now, so anything driving the provider portal or admin pages
    needs one. https:// because the session cookie is Secure and the test
    client will not resend a Secure cookie over plain http."""
    client = TestClient(app, base_url="https://testserver")
    client.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    return client
