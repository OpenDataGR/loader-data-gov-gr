"""SQLite persistence for loader-data-gov-gr's own state.

A plain file on the VM, not CKAN's database -- this service owns its
own state now (see the original split rationale in the repo's own
history/README). ``LOADER_DB_PATH`` controls where the file lives;
defaults to a ``data/`` directory next to the package. In production
this should resolve to a path inside the persistent clone at
/opt/data-projects/loader-data-gov-gr -- `git pull` never touches
untracked files, and `data/` is gitignored, so the default already
survives redeploys without any extra configuration.

Exposes factory functions (not just a single module-level engine) so
tests can point at an isolated database instead of the real default.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from providerharvest_service.models import Base

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "loader.db"


def default_db_url() -> str:
    path = os.environ.get("LOADER_DB_PATH", str(_DEFAULT_DB_PATH))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///%s" % path


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


engine = make_engine(default_db_url())
SessionLocal = make_session_factory(engine)


def get_session() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
