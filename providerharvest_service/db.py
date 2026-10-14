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
    complete).

    Only ever adds the column as nullable, regardless of the model's own
    ``nullable=False`` -- SQLite's ``ALTER TABLE ... ADD COLUMN`` doesn't
    support a computed Python-side default (only a fixed SQL literal),
    and every row that predates the column would need one, so silently
    emitting ``NOT NULL`` here would either fail outright (no default at
    all) or succeed with the wrong semantics (a static SQL default that
    doesn't match what the model actually wants each existing row to
    have). A genuinely required new column needs a real, deliberate
    migration (add nullable here, backfill existing rows, tighten to
    NOT NULL in a follow-up) -- raising clearly is better than either
    failure mode above, so that's what this does instead of guessing.
    """
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
                if not column.nullable:
                    raise RuntimeError(
                        "Model %r declares new NOT NULL column %r, but table "
                        "%r already exists -- _sync_schema only auto-adds "
                        "nullable columns (see its own docstring for why). "
                        "Add it as nullable, backfill existing rows, then "
                        "tighten to NOT NULL in a real migration."
                        % (table.name, column.name, table.name)
                    )
                ddl = "ALTER TABLE %s ADD COLUMN %s %s" % (
                    table.name, column.name, column.type.compile(engine.dialect)
                )
                conn.execute(text(ddl))


def _migrate_pipelines(engine: Engine) -> None:
    """A source can have several transforms ("pipelines"), each with a name; until then there was exactly one, and ``source_id`` carried
    a UNIQUE index. Gives every existing row the name ``default`` and swaps that index for a plain one, so the new unique pair
    (source_id, name) is what keeps names apart. Idempotent: a database that is already migrated (or brand new) is left alone."""
    inspector = inspect(engine)
    if "transform" not in inspector.get_table_names():
        return
    indexes = {ix["name"]: ix for ix in inspector.get_indexes("transform")}
    with engine.begin() as conn:
        conn.execute(text("UPDATE transform SET name = 'default' WHERE name IS NULL"))
        if indexes.get("ix_transform_source_id", {}).get("unique"):
            conn.execute(text("DROP INDEX ix_transform_source_id"))
            conn.execute(text("CREATE INDEX ix_transform_source_id ON transform (source_id)"))
        if "ix_transform_source_name" not in indexes:
            conn.execute(text("CREATE UNIQUE INDEX ix_transform_source_name ON transform (source_id, name)"))


def _migrate_lookup_tables(engine: Engine) -> None:
    """Index tables belong to a source now (``source_id``), and a name is unique per source, not across the service. SQLite cannot drop the
    old UNIQUE(name), so the table is rebuilt -- once, when that constraint is still there -- and in the same step every existing table
    is bound: to the one source whose pipelines use it, to each of them when several do (the first keeps the original, the others get
    a copy, so no source loses a table), and to none when nothing uses it (it is then listed in the Transform Box to bind or delete)."""
    inspector = inspect(engine)
    if "lookup_table" not in inspector.get_table_names():
        return
    old_unique = any(u.get("column_names") == ["name"] for u in inspector.get_unique_constraints("lookup_table"))
    with engine.begin() as conn:
        if not old_unique:
            if "ix_lookup_table_source_name" not in {ix["name"] for ix in inspector.get_indexes("lookup_table")}:
                conn.execute(text("CREATE UNIQUE INDEX ix_lookup_table_source_name ON lookup_table (source_id, name)"))
            return
        old_columns = [c["name"] for c in inspector.get_columns("lookup_table")]
        table = Base.metadata.tables["lookup_table"]
        keep = [c.name for c in table.columns if c.name in old_columns]
        conn.execute(text("ALTER TABLE lookup_table RENAME TO lookup_table_old"))
        table.create(conn)
        values = ["COALESCE(uploaded_at, CURRENT_TIMESTAMP)" if name == "uploaded_at" else name for name in keep]
        conn.execute(text("INSERT INTO lookup_table (%s) SELECT %s FROM lookup_table_old" % (", ".join(keep), ", ".join(values))))
        conn.execute(text("DROP TABLE lookup_table_old"))
    _bind_existing_lookup_tables(engine)


def _bind_existing_lookup_tables(engine: Engine) -> None:
    import json
    import uuid

    from sqlalchemy import select

    from providerharvest_service.models import LookupTable, Transform

    with Session(engine) as session:
        users: dict[str, list[str]] = {}  # table name -> source ids whose pipelines use it, in a stable order
        for pipeline in session.scalars(select(Transform).order_by(Transform.source_id, Transform.name)).all():
            try:
                steps = json.loads(pipeline.steps or "[]")
            except ValueError:
                continue
            for step in steps if isinstance(steps, list) else []:
                if isinstance(step, dict) and step.get("type") == "lookup" and isinstance(step.get("table"), str):
                    if pipeline.source_id not in users.setdefault(step["table"], []):
                        users[step["table"]].append(pipeline.source_id)
        for table in session.scalars(select(LookupTable).where(LookupTable.source_id.is_(None))).all():
            owners = users.get(table.name) or []
            if not owners:
                continue
            table.source_id = owners[0]
            for other in owners[1:]:
                session.add(LookupTable(id=str(uuid.uuid4()), source_id=other, name=table.name, description=table.description, columns=table.columns,
                                        rows=table.rows, row_count=table.row_count, uploaded_at=table.uploaded_at, uploaded_by=table.uploaded_by))
        session.commit()


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    _sync_schema(engine)
    _migrate_pipelines(engine)
    _migrate_lookup_tables(engine)
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
