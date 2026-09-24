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

No Alembic/migration framework -- ``_sync_schema`` below is the whole
answer to "a model gained a column since the last deploy": SQLAlchemy's
own ``create_all()`` only ever creates tables that don't exist yet, it
never alters one that's already there (confirmed the hard way: adding
``ProviderSource.last_check_*`` broke every page that queries it in
production with "no such column: provider_source.last_check_at" until
this existed). SQLite's ``ALTER TABLE ... ADD COLUMN`` is the one schema
change it supports without a full rebuild, which is exactly what a new
nullable column needs -- existing rows simply get NULL for it.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from providerharvest_service.models import Base

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "loader.db"


def default_db_url() -> str:
    path = os.environ.get("LOADER_DB_PATH", str(_DEFAULT_DB_PATH))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///%s" % path


def _sync_schema(engine: Engine) -> None:
    """Adds any column a model declares but an already-existing table is
    missing. A no-op for a brand-new table (create_all already made it
    complete) or a NOT NULL column with no default -- SQLite itself
    refuses that combination against a table with existing rows, which
    is the right outcome: that case needs a real backfill, not a silent
    schema patch, so it's left to raise loudly if it's ever hit."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                ddl = "ALTER TABLE %s ADD COLUMN %s %s" % (
                    table.name, column.name, column.type.compile(engine.dialect)
                )
                conn.execute(text(ddl))


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    _sync_schema(engine)
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
