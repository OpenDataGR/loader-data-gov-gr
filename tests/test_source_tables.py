"""Index tables belong to a source: uploaded and managed on the source, used by that source's pipelines only, and the existing global tables
are bound to the sources that used them when the model changed."""

import json

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from providerharvest_service import db, transform_runner
from providerharvest_service.models import LookupTable, ProviderSource, Transform
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    LOOKUP_FORM, READER_FORM, STATIONS_CSV, _build, _flash, _go, _isolated_db, _isolated_harvest_db, _master_key, _table_id, _upload_data,
    _upload_table, client, source,
)


def _second_source(client, _isolated_db):
    resp = client.post("/provider/sources/new", data={"name": "other-push", "title": "Other push", "transport_type": "push", "frequency": "MANUAL"}, follow_redirects=False)
    assert resp.status_code == 200
    with _isolated_db() as s:
        row = s.query(ProviderSource).filter_by(name="other-push").one()
        return {"id": row.id, "name": "other-push"}


def test_the_source_has_an_index_tables_page_and_the_menu_links_to_it(client, source):
    page = client.get("/provider/sources/%s/tables" % source["id"]).text
    assert "Index tables -- Acme push" in page and 'action="/provider/sources/%s/tables"' % source["id"] in page and "Upload a new index table" in page
    assert 'href="/provider/sources/%s/tables"' % source["id"] in client.get("/provider/sources").text
    assert "No such source" in _flash(client.get("/provider/sources/nope/tables", follow_redirects=False))


def test_the_same_table_name_can_be_used_by_two_sources_and_each_sees_only_its_own(client, source, _isolated_db):
    other = _second_source(client, _isolated_db)
    _upload_table(client, source)
    assert "uploaded" in _flash(_upload_table(client, other, content="id,name\nZ,Zeta\n"))  # the name 'stations' again, on another source
    assert "already a table called" in _flash(_upload_table(client, source))  # but not twice on the same one
    with _isolated_db() as s:
        mine = {t.source_id: json.loads(t.columns) for t in s.query(LookupTable).all()}
    assert mine == {source["id"]: ["id", "name", "lat", "lon"], other["id"]: ["id", "name"]}
    assert "Zeta" not in client.get("/provider/sources/%s/tables" % source["id"]).text
    mine_id = _table_id(_isolated_db)
    foreign = client.get("/provider/sources/%s/tables/%s" % (other["id"], mine_id), follow_redirects=False)  # one source's table through another's address
    assert "No such index table" in _flash(foreign)
    for path in ("delete", "replace"):
        assert "No such index table" in _flash(client.post("/provider/sources/%s/tables/%s/%s" % (other["id"], mine_id, path), follow_redirects=False))
    assert "No such index table" in _flash(client.get("/provider/sources/%s/tables/%s/download" % (other["id"], mine_id), follow_redirects=False))


def test_a_pipeline_looks_up_only_in_its_own_sources_tables(client, source, _isolated_db):
    other = _second_source(client, _isolated_db)
    _upload_table(client, other)  # 'stations' exists, but on the other source
    _upload_data(client, source)
    _go(client, source, "create")
    _go(client, source, "steps/new", data=READER_FORM)
    page = client.get("/transform/sources/%s/steps/new?type=lookup" % source["id"]).text
    assert "no index table yet" in page and "/provider/sources/%s/tables" % source["id"] in page
    _upload_table(client, source)
    page = client.get("/transform/sources/%s/steps/new?type=lookup" % source["id"]).text
    assert "no index table yet" not in page and '<option value="stations"' in page


def test_the_transform_box_has_no_upload_for_tables_any_more(client, source):
    _upload_table(client, source)
    page = client.get("/transform").text
    assert "Upload a new index table" not in page and "Index tables without a source" not in page
    assert client.post("/transform/tables", data={"name": "x"}, follow_redirects=False).status_code in (404, 405)


# -- tables that existed before they belonged to a source ---------------------------------------------------------------------------------------------

def _old_database(tmp_path, steps_by_source, tables):
    """A database as it was: a global UNIQUE(name) on lookup_table and no source_id."""
    url = "sqlite:///%s" % (tmp_path / "old.db")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE lookup_table (id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL UNIQUE, description VARCHAR NOT NULL DEFAULT '', "
                          "columns VARCHAR NOT NULL DEFAULT '[]', rows VARCHAR NOT NULL DEFAULT '[]', row_count INTEGER NOT NULL DEFAULT 0, "
                          "uploaded_at DATETIME, uploaded_by VARCHAR NOT NULL DEFAULT '')"))
        for n, name in enumerate(tables):
            conn.execute(text("INSERT INTO lookup_table (id, name, columns, rows, row_count) VALUES (:id, :n, '[\"id\"]', '[{\"id\": \"A\"}]', 1)"), {"id": "t%d" % n, "n": name})
    engine.dispose()
    # the transforms go in through the real model once the new schema exists
    engine = db.make_engine(url)
    return url, engine


def test_existing_tables_move_to_the_source_that_uses_them_are_copied_for_a_second_one_and_left_unassigned_when_unused(tmp_path):
    url = "sqlite:///%s" % (tmp_path / "old.db")
    old = create_engine(url)
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE transform (id VARCHAR PRIMARY KEY, source_id VARCHAR NOT NULL, steps VARCHAR NOT NULL, output_format VARCHAR NOT NULL, "
                          "root_name VARCHAR NOT NULL, record_name VARCHAR NOT NULL, csv_delimiter VARCHAR, csv_bom BOOLEAN, output_single BOOLEAN, "
                          "request_swap BOOLEAN, request_template VARCHAR, sample VARCHAR, enabled BOOLEAN NOT NULL, created_at DATETIME, updated_at DATETIME, "
                          "updated_by VARCHAR NOT NULL)"))
        conn.execute(text("CREATE UNIQUE INDEX ix_transform_source_id ON transform (source_id)"))
        conn.execute(text("CREATE TABLE lookup_table (id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL UNIQUE, description VARCHAR NOT NULL DEFAULT '', "
                          "columns VARCHAR NOT NULL DEFAULT '[]', rows VARCHAR NOT NULL DEFAULT '[]', row_count INTEGER NOT NULL DEFAULT 0, "
                          "uploaded_at DATETIME, uploaded_by VARCHAR NOT NULL DEFAULT '')"))
        for table_id, name in (("t1", "stations"), ("t2", "shared"), ("t3", "unused")):
            conn.execute(text("INSERT INTO lookup_table (id, name, columns, rows, row_count) VALUES (:id, :n, '[\"id\"]', '[{\"id\": \"A\"}]', 1)"), {"id": table_id, "n": name})
        uses = {"s1": [{"type": "lookup", "table": "stations"}, {"type": "lookup", "table": "shared"}], "s2": [{"type": "lookup", "table": "shared"}]}
        for source_id, steps in uses.items():
            conn.execute(text("INSERT INTO transform (id, source_id, steps, output_format, root_name, record_name, enabled, updated_by) "
                              "VALUES (:id, :s, :steps, 'json', 'records', 'record', 1, '')"), {"id": "x" + source_id, "s": source_id, "steps": json.dumps(steps)})
    old.dispose()

    engine = db.make_engine(url)
    with Session(engine) as s:
        rows = sorted((t.source_id, t.name) for t in s.query(LookupTable).all() if t.source_id)
        assert rows == [("s1", "shared"), ("s1", "stations"), ("s2", "shared")]  # 'shared' was copied: neither source lost its table
        assert [t.name for t in s.query(LookupTable).filter(LookupTable.source_id.is_(None)).all()] == ["unused"]
        assert s.get(LookupTable, "t1").source_id == "s1" and s.get(LookupTable, "t2").source_id == "s1"
    unique = {ix["name"]: ix["unique"] for ix in inspect(engine).get_indexes("lookup_table")}
    assert unique["ix_lookup_table_source_name"] and not inspect(engine).get_unique_constraints("lookup_table")

    db._migrate_lookup_tables(engine)  # a second start changes nothing
    with Session(engine) as s:
        assert s.query(LookupTable).count() == 4


def test_a_table_no_source_used_is_listed_in_the_transform_box_to_bind_or_delete(client, source, _isolated_db):
    with _isolated_db() as s:
        s.add_all([LookupTable(name="orphan", columns='["id"]', rows='[{"id": "A"}]', row_count=1), LookupTable(name="orphan2", columns='["id"]', rows="[]", row_count=0)])
        s.commit()
    page = client.get("/transform").text
    assert "Index tables without a source" in page and "orphan" in page and "/provider/tables/" in page
    with _isolated_db() as s:
        orphan = s.query(LookupTable).filter_by(name="orphan").one().id
        orphan2 = s.query(LookupTable).filter_by(name="orphan2").one().id
    bound = client.post("/provider/tables/%s/bind" % orphan, data={"source_id": source["id"]}, follow_redirects=False)
    assert bound.headers["location"].startswith("/provider/sources/%s/tables" % source["id"]) and "now Acme push" in _flash(bound)
    assert "orphan" in client.get("/provider/sources/%s/tables" % source["id"]).text
    assert "Choose the source" in _flash(client.post("/provider/tables/%s/bind" % orphan2, data={"source_id": "nope"}, follow_redirects=False))
    assert "No such unassigned" in _flash(client.post("/provider/tables/%s/bind" % orphan, data={"source_id": source["id"]}, follow_redirects=False))  # already bound
    assert "deleted" in _flash(client.post("/provider/tables/%s/delete" % orphan2, follow_redirects=False))
    assert "Index tables without a source" not in client.get("/transform").text
    assert "No such unassigned" in _flash(client.post("/provider/tables/%s/delete" % orphan, follow_redirects=False))  # a bound table is not deleted this way


def test_binding_is_refused_when_the_source_already_has_a_table_of_that_name(client, source, _isolated_db):
    _upload_table(client, source)
    with _isolated_db() as s:
        s.add(LookupTable(name="stations", columns='["id"]', rows="[]", row_count=0))
        s.commit()
        orphan = s.query(LookupTable).filter(LookupTable.source_id.is_(None)).one().id
    assert "already has a table called" in _flash(client.post("/provider/tables/%s/bind" % orphan, data={"source_id": source["id"]}, follow_redirects=False))
