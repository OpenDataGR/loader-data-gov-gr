"""SQLAlchemy engine/session for this service's harvested-data store --
a dedicated PostgreSQL instance (see deploy/postgres/), independent of
db.py's own SQLite file (which holds config: ProviderSource,
FtpAccount, CkanUserToken). A real server database for potentially
large, high-churn harvested records/files; SQLite stays for the small,
low-churn config data it already handles fine.

Deliberately lazy, unlike db.py's own module-level engine: db.py's
SQLite file always has a sensible default path, so creating it eagerly
at import time is safe. LOADER_HARVEST_DB_URL has no such default --
requiring it eagerly at import time would make importing this module
(even just for type-checking, even in the existing test suite, which
runs with no Postgres available at all) a hard failure. It only
becomes a hard requirement the first time `get_session()` or
`check_connectivity()` is actually called, the same lazy-until-needed
shape ckan_tokens.py's own env lookups already use.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class HarvestDbNotConfigured(RuntimeError):
    pass


def _required_db_url() -> str:
    url = os.environ.get("LOADER_HARVEST_DB_URL")
    if not url:
        raise HarvestDbNotConfigured(
            "LOADER_HARVEST_DB_URL is not set -- the harvested-data "
            "PostgreSQL connection must be provisioned outside this "
            "process (see deploy/postgres/ and provision-postgres.yml)."
        )
    return url


def make_engine(url: str) -> Engine:
    # pool_pre_ping: a stale/dropped connection (e.g. the container
    # restarted) is detected and replaced on next checkout rather than
    # surfacing as a confusing mid-request error.
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _ensure_initialized() -> sessionmaker:
    global _engine, _SessionLocal
    if _SessionLocal is None:
        _engine = make_engine(_required_db_url())
        _SessionLocal = make_session_factory(_engine)
    return _SessionLocal


def get_session() -> Session:
    session_factory = _ensure_initialized()
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def check_connectivity() -> None:
    """A real round-trip, not just 'the engine object was constructed
    without raising' -- proves the actual network path (this process
    -> 127.0.0.1:5432 -> loader-postgres) and the actual credential
    work, same discipline every other piece of this service's
    infrastructure was proven with before anything was built on it."""
    session_factory = _ensure_initialized()
    with session_factory() as session:
        session.execute(text("SELECT 1"))
