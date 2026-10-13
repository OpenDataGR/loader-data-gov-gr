"""A source can have several transforms ("pipelines"), told apart by name; the ones that existed before are named ``default``."""

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from providerharvest_service import db, transform_runner
from providerharvest_service.models import Transform

OLD_TRANSFORM_TABLE = """
CREATE TABLE transform (
    id VARCHAR NOT NULL PRIMARY KEY, source_id VARCHAR NOT NULL, steps VARCHAR NOT NULL, output_format VARCHAR NOT NULL,
    root_name VARCHAR NOT NULL, record_name VARCHAR NOT NULL, csv_delimiter VARCHAR, csv_bom BOOLEAN, output_single BOOLEAN,
    request_swap BOOLEAN, request_template VARCHAR, sample VARCHAR, enabled BOOLEAN NOT NULL, created_at DATETIME, updated_at DATETIME,
    updated_by VARCHAR NOT NULL
)
"""


def _engine(tmp_path):
    return db.make_engine("sqlite:///%s" % (tmp_path / "x.db"))


def test_a_new_database_allows_several_pipelines_per_source_but_not_two_with_one_name(tmp_path):
    with Session(_engine(tmp_path)) as session:
        session.add_all([Transform(source_id="s1", steps="[]"), Transform(source_id="s1", name="lookup", steps="[]"), Transform(source_id="s2", steps="[]")])
        session.commit()
        assert [t.name for t in transform_runner.list_pipelines(session, "s1")] == ["default", "lookup"]
        session.add(Transform(source_id="s1", name="lookup", steps="[]"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_get_transform_is_the_default_pipeline_and_get_pipeline_finds_the_others(tmp_path):
    with Session(_engine(tmp_path)) as session:
        session.add_all([Transform(source_id="s1", name="lookup", steps="[]"), Transform(source_id="s1", steps="[]")])
        session.commit()
        assert transform_runner.get_transform(session, "s1").name == "default"
        assert transform_runner.get_pipeline(session, "s1", "lookup").name == "lookup"
        assert transform_runner.get_pipeline(session, "s1", "nope") is None
        assert transform_runner.get_transform(session, "other") is None


def test_an_existing_database_keeps_its_transforms_as_the_default_pipeline(tmp_path):
    url = "sqlite:///%s" % (tmp_path / "old.db")
    from sqlalchemy import create_engine

    old = create_engine(url)
    with old.begin() as conn:
        conn.execute(text(OLD_TRANSFORM_TABLE))
        conn.execute(text("CREATE UNIQUE INDEX ix_transform_source_id ON transform (source_id)"))
        conn.execute(text("INSERT INTO transform (id, source_id, steps, output_format, root_name, record_name, enabled, updated_by) "
                          "VALUES ('t1', 's1', '[]', 'xml', 'records', 'record', 1, 'me')"))
    old.dispose()

    engine = db.make_engine(url)
    with Session(engine) as session:
        kept = transform_runner.get_transform(session, "s1")
        assert kept.id == "t1" and kept.name == "default" and kept.output_format == "xml" and kept.enabled
        session.add(Transform(source_id="s1", name="second", steps="[]"))  # the old one-per-source rule is gone
        session.commit()
    indexes = {ix["name"]: ix["unique"] for ix in inspect(engine).get_indexes("transform")}
    assert not indexes["ix_transform_source_id"] and indexes["ix_transform_source_name"]


def test_migrating_twice_changes_nothing(tmp_path):
    engine = _engine(tmp_path)
    db._migrate_pipelines(engine)
    db._migrate_pipelines(engine)
    assert {ix["name"] for ix in inspect(engine).get_indexes("transform")} >= {"ix_transform_source_id", "ix_transform_source_name"}
