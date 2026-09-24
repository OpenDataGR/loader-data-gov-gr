import sqlite3

from sqlalchemy import inspect

from providerharvest_service import db, provider_sources


def test_make_engine_adds_missing_columns_to_a_preexisting_table(tmp_path):
    """Regression test for a real production incident: create_all() only
    creates missing tables, it never alters one that already exists.
    Deploying a model change that adds a nullable column (like
    ProviderSource.last_check_at) against a pre-existing SQLite file
    broke every page that queried it with "no such column" until
    db._sync_schema existed. Simulates that exact situation: a
    provider_source table shaped like the schema *before* the
    last_check_* columns were added."""
    path = tmp_path / "pre-migration.db"

    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE provider_source (
            id VARCHAR PRIMARY KEY,
            name VARCHAR UNIQUE NOT NULL,
            title VARCHAR NOT NULL,
            transport_type VARCHAR NOT NULL,
            endpoint_url VARCHAR NOT NULL,
            frequency VARCHAR NOT NULL,
            auth_type VARCHAR NOT NULL,
            config VARCHAR NOT NULL,
            secret_ciphertext BLOB NOT NULL,
            secret_data_key_wrapped BLOB NOT NULL,
            secret_algorithm VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            rejection_reason VARCHAR,
            notification_email VARCHAR,
            created_at DATETIME
        )
    """)
    conn.commit()
    conn.close()

    engine = db.make_engine("sqlite:///%s" % path)
    session = db.make_session_factory(engine)()

    # Would raise sqlalchemy.exc.OperationalError: no such column before
    # _sync_schema existed.
    assert provider_sources.list_all(session) == []

    columns = {col["name"] for col in inspect(engine).get_columns("provider_source")}
    assert {"last_check_at", "last_check_status", "last_check_message"} <= columns


def test_make_engine_is_idempotent_on_an_already_current_schema(tmp_path):
    path = tmp_path / "current.db"
    db.make_engine("sqlite:///%s" % path)
    # A second call against the same file must not error (every column
    # already exists) -- this is what happens on every real process
    # restart against the persistent production DB file.
    engine = db.make_engine("sqlite:///%s" % path)
    session = db.make_session_factory(engine)()
    assert provider_sources.list_all(session) == []
