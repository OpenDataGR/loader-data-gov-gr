"""A load into CKAN's DataStore: the rows of the output written into a table by the loader itself (replace, add, or update by key), and a pipeline
with several loads, each one way and its own line in the Load Box -- against a fake CKAN."""

import json
import types

import pytest
from sqlalchemy import select

from providerharvest_service import ckan_client, ckan_datastore, ckan_load, ui_ckan
from providerharvest_service.models import CkanTarget
from tests.test_ckan_load import BASE, FakeCkan, _pipeline, _source, _target
from tests.test_ckan_service import ServiceCkan
from tests.test_ckan_ui import _connect, _page, _ready, _targets  # noqa: F401
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _isolated_db, _isolated_harvest_db, _master_key, client, source,
)


class DatastoreCkan(ServiceCkan):
    """ServiceCkan plus a DataStore: tables of rows keyed by resource id."""

    def __init__(self):
        super().__init__()
        self.tables, self.fields, self.keys = {}, {}, {}

    def call(self, action, data, files=None):
        if action not in ("datastore_create", "datastore_upsert", "datastore_delete"):
            return super().call(action, data, files)
        self.calls.append((action, data, files))
        if action in self.fail:
            raise self.fail[action]
        rid = data["resource_id"]
        if action == "datastore_delete":
            if rid not in self.tables:
                raise ckan_client.CKANNotFound(action, "Not found")
            del self.tables[rid]
            return {}
        if action == "datastore_create":
            self.tables.setdefault(rid, [])
            self.fields[rid] = data["fields"]
            self.keys[rid] = data.get("primary_key") or []
            return {}
        if rid not in self.tables:
            raise ckan_client.CKANNotFound(action, "Not found")
        for record in data["records"]:
            if data["method"] == "upsert":
                at = next((i for i, row in enumerate(self.tables[rid]) if all(row[k] == record[k] for k in self.keys[rid])), None)
                if at is not None:
                    self.tables[rid][at] = record
                    continue
            self.tables[rid].append(record)
        return {}


def _output(content, filename="out.json", oid="o1"):
    return types.SimpleNamespace(id=oid, content=content, filename=filename, content_type="application/json")


def _datastore(refresh="replace", keys=(), **extra):
    return _target("datastore", datastore_config=json.dumps({"refresh": refresh, "keys": list(keys)}), **extra)


ROWS = json.dumps([{"road": "A1", "at": "2026-10-01T10:00:00", "cars": 12, "note": "007"}, {"road": "A2", "at": "2026-10-01T10:00:00", "cars": "7", "note": ""}]).encode()


# -- reading the output ------------------------------------------------------------------------------------------------------------------------------------

def test_the_columns_and_their_types_are_worked_out_from_the_rows():
    fields, records = ckan_datastore.table_of(ckan_datastore.records_of(ROWS, "out.json"))
    assert fields == [{"id": "road", "type": "text"}, {"id": "at", "type": "timestamp"}, {"id": "cars", "type": "numeric"}, {"id": "note", "type": "text"}]
    assert records[0] == {"road": "A1", "at": "2026-10-01T10:00:00", "cars": 12, "note": "007"}  # (a "007" is an identifier, not a number)
    assert records[1]["cars"] == 7 and records[1]["note"] is None


def test_csv_jsonl_and_a_wrapped_list_are_read_and_nested_values_become_text():
    assert ckan_datastore.records_of(b"a,b\n1,x\n2,y\n", "t.csv") == [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]
    assert ckan_datastore.records_of(b'{"a": 1}\n{"a": 2}\n', "t.jsonl") == [{"a": 1}, {"a": 2}]
    assert ckan_datastore.records_of(b'{"count": 2, "items": [{"a": {"b": 1}}]}', "t.json") == [{"a": '{"b": 1}'}]


def test_output_that_is_not_rows_is_explained():
    with pytest.raises(ckan_datastore.DatastoreError, match="JSON, JSON Lines or CSV"):
        ckan_datastore.records_of(b"<a/>", "out.xml", "application/xml")
    with pytest.raises(ckan_datastore.DatastoreError, match="no rows"):
        ckan_datastore.records_of(b"[]", "out.json")
    with pytest.raises(ckan_datastore.DatastoreError, match="could not be read"):
        ckan_datastore.records_of(b"{nope", "out.json")


def test_upsert_needs_a_key_and_a_refresh_must_be_one_of_the_three():
    with pytest.raises(ckan_datastore.DatastoreError, match="identify a row"):
        ckan_datastore.check_config("upsert", [])
    with pytest.raises(ckan_datastore.DatastoreError, match="replace, add"):
        ckan_datastore.check_config("sometimes", [])
    assert ckan_datastore.check_config("append", ["x"]) == {"refresh": "append", "keys": []}


# -- loading ---------------------------------------------------------------------------------------------------------------------------------------------------

def test_replace_makes_a_resource_without_a_file_and_loads_the_rows_again_each_time():
    ckan, target = DatastoreCkan(), _datastore()
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(ROWS))
    assert target.resource_id == "res1" and target.last_status == "ok"
    assert ckan.resources["res1"]["url"] == "_datastore_only_resource" and ckan.resources["res1"]["package_id"] == "ds1"
    assert len(ckan.tables["res1"]) == 2
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(json.dumps([{"road": "A9", "at": "2026-10-02T10:00:00", "cars": 1, "note": "x"}]).encode(), oid="o2"))
    assert [r["road"] for r in ckan.tables["res1"]] == ["A9"]  # the table mirrors the newest output


def test_append_adds_each_outputs_rows_but_never_the_same_output_twice():
    ckan, target = DatastoreCkan(), _datastore("append")
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(ROWS))
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(ROWS, oid="o2"))
    assert len(ckan.tables["res1"]) == 4
    with pytest.raises(ckan_load.CkanLoadError, match="already in the table"):
        ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(ROWS, oid="o2"))
    assert len(ckan.tables["res1"]) == 4 and [c[0] for c in ckan.calls].count("datastore_create") == 1  # (the table was made once)


def test_upsert_updates_the_rows_with_the_same_key_and_adds_the_rest():
    ckan, target = DatastoreCkan(), _datastore("upsert", ["road", "at"])
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(ROWS))
    assert ckan.keys["res1"] == ["road", "at"]
    newer = json.dumps([{"road": "A1", "at": "2026-10-01T10:00:00", "cars": 99, "note": "n"}, {"road": "A1", "at": "2026-10-01T10:05:00", "cars": 5, "note": "n"}]).encode()
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(newer, oid="o2"))
    rows = ckan.tables["res1"]
    assert len(rows) == 3 and [r["cars"] for r in rows if r["road"] == "A1"] == [99, 5]


def test_a_column_keeps_its_type_once_the_table_exists():
    ckan, target = DatastoreCkan(), _datastore("append")
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(ROWS))
    with pytest.raises(ckan_load.CkanLoadError, match="Column 'cars' is numeric"):
        ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(json.dumps([{"road": "A1", "cars": "n/a"}]).encode(), oid="o2"))


def test_a_missing_key_column_is_explained_and_nothing_is_written():
    ckan, target = DatastoreCkan(), _datastore("upsert", ["id"])
    with pytest.raises(ckan_load.CkanLoadError, match="key column"):
        ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(ROWS))
    assert not ckan.tables and target.last_status == "error"


def test_without_an_output_or_with_an_xml_one_it_says_so():
    with pytest.raises(ckan_load.CkanLoadError, match="no output to load"):
        ckan_load.push(_datastore(), _source(), _pipeline(), DatastoreCkan(), output=None)
    with pytest.raises(ckan_load.CkanLoadError, match="JSON, JSON Lines or CSV"):
        ckan_load.push(_datastore(), _source(), _pipeline(), DatastoreCkan(), output=_output(b"<a/>", "out.xml"))


def test_a_resource_deleted_in_ckan_is_made_again_and_the_table_with_it():
    ckan, target = DatastoreCkan(), _datastore("append")
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(ROWS))
    ckan.tables.clear()  # (the table is gone: the next write is refused as not found)
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(ROWS, oid="o2"))
    assert len(ckan.tables[target.resource_id]) == 2 and len(ckan.resources) == 2


def test_big_outputs_are_written_in_batches(monkeypatch):
    monkeypatch.setattr(ckan_datastore, "BATCH", 2)
    ckan, target = DatastoreCkan(), _datastore()
    rows = json.dumps([{"n": i} for i in range(5)]).encode()
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(rows))
    assert [len(c[1]["records"]) for c in ckan.calls if c[0] == "datastore_upsert"] == [2, 2, 1]


def test_a_new_output_is_loaded_by_every_load_of_the_pipeline_that_wants_rows_or_a_file(monkeypatch):
    """sync_after_output goes through the pipeline's loads one by one; a failing one does not stop the next."""
    ckan = DatastoreCkan()
    monkeypatch.setattr(ckan_load, "_client_for", lambda session, target: ckan)
    committed = []
    session = types.SimpleNamespace(commit=lambda: committed.append(1), rollback=lambda: None)
    loads = [_datastore("replace"), _target("upload"), _datastore("append")]
    loads[0].datastore_config = "{not json"  # (a broken setting falls back to the defaults: replace)
    monkeypatch.setattr(ckan_load, "targets_of", lambda s, pid: loads + [_target("link")])
    ckan.fail["resource_create"] = ckan_client.CKANValidationError("resource_create", "no")
    ckan_load.sync_after_output(session, _source(), _pipeline(), _output(ROWS))
    assert [t.last_status for t in loads] == ["error", "error", "error"] and len(committed) == 3  # each tried, each recorded
    ckan.fail.clear()
    ckan_load.sync_after_output(session, _source(), _pipeline(), _output(ROWS, oid="o2"))
    assert [t.last_status for t in loads] == ["ok", "ok", "ok"]


# -- the page and the Load Box ---------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def ckan(monkeypatch):
    fake = DatastoreCkan()
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "sysadmin-token")
    monkeypatch.setenv("LOADER_PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(ckan_load, "user_client", lambda session, ckan_user: fake)
    monkeypatch.setattr(ui_ckan, "_identity", lambda request: "user_abc")
    return fake


def test_a_datastore_load_is_made_from_the_page_and_the_page_offers_the_columns(client, source, ckan, _isolated_db):
    _ready(client, source)
    page = client.get(_page(source["id"])).text
    assert 'id="mode-datastore"' in page and 'name="ds_refresh"' in page and 'name="target_id" value=""' in page
    resp = _connect(client, source["id"], mode="datastore", ds_refresh="append")
    assert "is now a resource of Attica traffic" in _flash(resp)
    (target,) = _targets(_isolated_db)
    assert target.mode == "datastore" and ckan_datastore.config_of(target.datastore_config)["refresh"] == "append" and ckan.tables[target.resource_id]
    opened = client.get(_page(source["id"])).text
    assert "rows in a DataStore table" in opened and 'name="ds_keys"' in opened  # (the columns of the output, to choose a key from)


def test_upsert_from_the_page_needs_a_key(client, source, ckan, _isolated_db):
    _ready(client, source)
    assert "identify a row" in _flash(_connect(client, source["id"], mode="datastore", ds_refresh="upsert"))
    assert _targets(_isolated_db) == []
    page = client.get(_page(source["id"])).text
    first = ckan_load.search_datasets  # noqa: F841
    columns = ckan_datastore.columns_of(types.SimpleNamespace(content=b'[{"a": 1}]', filename="x.json", content_type="application/json"))
    assert columns == [{"id": "a", "type": "numeric"}] and page


def test_a_pipeline_can_have_several_loads_each_its_own_line_in_the_load_box(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"], mode="datastore", ds_refresh="replace", target_id="")
    _connect(client, source["id"], mode="upload", target_id="", resource_name="File copy")
    _connect(client, source["id"], mode="link", target_id="", resource_name="Link")
    targets = _targets(_isolated_db)
    assert sorted(t.mode for t in targets) == ["datastore", "link", "upload"] and len({t.id for t in targets}) == 3
    box = client.get("/load").text
    assert box.count('class="load-cell"') == 4 and "rows" not in "" and "DataStore table" in box and "uploaded copy" in box  # (three loads and the "add another" line)
    assert "Add another load" in box and box.count("?load=") == 4
    page = client.get(_page(source["id"])).text
    assert page.count('href="/load/sources/%s/ckan?load=' % source["id"]) >= 3  # (each load is listed on its own line)


def test_one_load_is_changed_pushed_and_removed_without_touching_the_others(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"], mode="link", target_id="", resource_name="Link")
    _connect(client, source["id"], mode="upload", target_id="", resource_name="File copy")
    link, upload = sorted(_targets(_isolated_db), key=lambda t: t.mode)
    resp = client.post(_page(source["id"], tail="ckan/disconnect"), data={"target_id": link.id}, follow_redirects=False)
    assert "removed from CKAN" in _flash(resp)
    (left,) = _targets(_isolated_db)
    assert left.id == upload.id and left.resource_id in ckan.resources
    _connect(client, source["id"], mode="upload", target_id=upload.id, resource_name="Renamed")
    (left,) = _targets(_isolated_db)
    assert left.resource_name == "Renamed" and len(_targets(_isolated_db)) == 1
    assert "No such load" in _flash(client.post(_page(source["id"]), data={"mode": "link", "organization_id": "org1", "dataset_id": "ds1", "resource_name": "x", "target_id": "nope"}, follow_redirects=False))


def test_the_overview_draws_each_load_as_its_own_line(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"], mode="datastore", ds_refresh="replace", target_id="")
    _connect(client, source["id"], mode="link", target_id="", resource_name="Link")
    page = client.get("/overview").text
    assert page.count('class="node node-ckan"') == 2 and "CKAN &middot; DataStore table" in page and "CKAN &middot; link" in page
    assert "?load=" in page


def test_unpublishing_leaves_a_datastore_table_alone_and_takes_a_link_out(client, source, ckan, _isolated_db):
    from tests.test_pipeline_publishing import _publish

    _ready(client, source)
    _connect(client, source["id"], mode="datastore", ds_refresh="append", target_id="")
    _connect(client, source["id"], mode="link", target_id="", resource_name="Link")
    datastore = next(t for t in _targets(_isolated_db) if t.mode == "datastore")
    table = ckan.tables[datastore.resource_id]
    _publish(client, source["id"], on=False)
    by_mode = {t.mode: t for t in _targets(_isolated_db)}
    assert by_mode["datastore"].resource_id and by_mode["datastore"].resource_id in ckan.tables and ckan.tables[datastore.resource_id] is table
    assert not by_mode["link"].resource_id


def test_an_old_database_with_one_load_per_pipeline_is_opened_for_several(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from providerharvest_service import db

    engine = create_engine("sqlite:///%s" % (tmp_path / "old.db"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE ckan_target (id VARCHAR PRIMARY KEY, pipeline_id VARCHAR NOT NULL)"))
        conn.execute(text("CREATE UNIQUE INDEX ix_ckan_target_pipeline_id ON ckan_target (pipeline_id)"))
    db._migrate_ckan_targets(engine)
    db._migrate_ckan_targets(engine)  # (a second start changes nothing)
    assert not inspect(engine).get_indexes("ckan_target")[0]["unique"]
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ckan_target VALUES ('a', 'p1'), ('b', 'p1')"))
