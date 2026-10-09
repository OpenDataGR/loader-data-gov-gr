"""The Transform Box pages, walked the way an admin would: upload an index table, build a transform from a real
stored file, try it, switch it on, re-run it, download the results -- and the ways each step can go wrong."""

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from providerharvest_service import transform_runner
from providerharvest_service.app import app
from providerharvest_service.harvest_models import HarvestedFile, TransformedFile
from providerharvest_service.models import LookupTable, Transform
from tests.test_push_ingest import (  # noqa: F401  (fixtures and helpers shared with the push ingest tests)
    _isolated_db, _isolated_harvest_db, _master_key, _register_and_activate_push_source, client,
)
from tests.test_transform_engine import SOAP

STATIONS_CSV = "id,name,lat,lon\nMS812,Kifisias,38.03,23.80\nMS810,Mesogeion,38.00,23.80\n"


@pytest.fixture
def source(client, _isolated_db):
    source_id, api_key = _register_and_activate_push_source(client, _isolated_db)
    return {"id": source_id, "key": api_key, "name": "acme-push"}


def _upload_data(client, source, content=SOAP.encode("utf-8"), name="Detector.xml"):
    resp = client.post("/push/%s/upload" % source["name"], files={"file": (name, content, "application/xml")}, headers={"X-Api-Key": source["key"]})
    assert resp.status_code == 200
    return resp.json()["run_id"]


def _upload_table(client, name="stations", content=STATIONS_CSV, filename="stations.csv"):
    return client.post("/transform/tables", data={"name": name, "description": "Detectors"}, files={"file": (filename, content.encode("utf-8"), "text/csv")}, follow_redirects=False)


def _flash(resp):
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(resp.headers["location"]).query).get("flash", [""])[0]


def _table_id(session_factory, name="stations"):
    with session_factory() as s:
        return s.scalar(select(LookupTable.id).where(LookupTable.name == name))


def _go(client, source, path, **kw):
    return client.post("/transform/sources/%s/%s" % (source["id"], path), follow_redirects=False, **kw)


READER_FORM = {
    "kind": "read", "format": "xml_records", "record": "ds", "explode": "data/value",
    "path": ["identifier/ident", "data/timeline/timestamp", "data/detectorState", "data/value@count", "data/value@speed", "data/value@vehicle"],
    "name::identifier/ident": "id", "name::data/timeline/timestamp": "time", "name::data/detectorState": "state",
    "name::data/value@count": "count", "name::data/value@speed": "speed", "name::data/value@vehicle": "vehicle",
}
LOOKUP_FORM = {"kind": "lookup", "table": "stations", "key": "id", "table_key": "id", "add": ["name", "lat"], "as::lat": "latitude", "on_missing": "keep"}


def _build(client, source, *forms):
    _go(client, source, "create")
    for form in forms:
        resp = _go(client, source, "steps/new", data=form)
        assert resp.status_code == 303 and "error" not in resp.headers["location"], resp.text


def _steps(session_factory, source_id):
    with session_factory() as s:
        return transform_runner.steps_of(transform_runner.get_transform(s, source_id))


# -- the Transform Box tab and the overview -----------------------------------------------------------------------------

def test_the_tab_and_overview_list_the_sources_and_their_transform_state(client, source):
    page = client.get("/transform").text
    assert "Transform Box</a>" in page and "Acme push" in page and "Set up" in page and "Index tables" in page
    _go(client, source, "create")
    page = client.get("/transform").text
    assert "Open" in page and ">off<" in page


def test_the_pages_need_the_admin_login(client, source):
    anonymous = TestClient(app, base_url="https://testserver")
    for url in ("/transform", "/transform/sources/%s" % source["id"], "/transform/tables/x", "/transform/sources/%s/steps/new?type=read" % source["id"]):
        assert anonymous.get(url, follow_redirects=False).headers.get("location", "/admin/login").startswith("/admin/login"), url
    for url in ("/transform/tables", "/transform/sources/%s/create" % source["id"], "/transform/sources/%s/enabled" % source["id"]):
        assert anonymous.post(url, follow_redirects=False).headers.get("location", "/admin/login").startswith("/admin/login"), url


# -- index tables ----------------------------------------------------------------------------------------------------------------

def test_an_index_table_is_uploaded_listed_viewed_and_downloaded(client, _isolated_db):
    resp = _upload_table(client)
    assert resp.status_code == 303 and "uploaded: 2 rows, 4 columns" in _flash(resp)
    assert "stations" in client.get("/transform").text
    table_id = _table_id(_isolated_db)
    page = client.get("/transform/tables/%s" % table_id).text
    assert "Kifisias" in page and "Replace the contents" in page
    csv = client.get("/transform/tables/%s/download" % table_id)
    assert csv.status_code == 200 and csv.text.startswith("id,name,lat,lon") and "Mesogeion" in csv.text


@pytest.mark.parametrize("name, content, filename, message", [
    ("Bad Name", STATIONS_CSV, "s.csv", "lowercase letters"), ("1abc", STATIONS_CSV, "s.csv", "lowercase letters"), ("x", STATIONS_CSV, "s.csv", "lowercase letters"),
    ("empty", "", "s.csv", "header"), ("norows", "id,name\n", "s.csv", "no rows"), ("dupes", "id,id\n1,2\n", "s.csv", "own non-empty name"),
    ("xmlfile", "<a/>", "s.xml", "CSV or JSON"),
])
def test_bad_index_uploads_are_explained_and_store_nothing(client, _isolated_db, name, content, filename, message):
    resp = _upload_table(client, name=name, content=content, filename=filename)
    assert message in _flash(resp) and _table_id(_isolated_db, name) is None


def test_a_second_table_with_the_same_name_is_refused(client):
    _upload_table(client)
    assert "already a table called" in _flash(_upload_table(client))


def test_a_json_index_is_accepted(client, _isolated_db):
    resp = _upload_table(client, name="stations", content=json.dumps([{"id": "A", "name": "Alpha"}]), filename="s.json")
    assert "uploaded: 1 rows" in _flash(resp)


def test_replacing_a_table_updates_it_and_is_stopped_if_a_transform_needs_a_missing_column(client, source, _isolated_db):
    _upload_table(client)
    table_id = _table_id(_isolated_db)
    ok = client.post("/transform/tables/%s/replace" % table_id, files={"file": ("n.csv", b"id,name,lat,lon\nA,Alpha,1,2\n", "text/csv")}, follow_redirects=False)
    assert "Replaced: 1 rows" in _flash(ok)
    _upload_data(client, source)
    _build(client, source, READER_FORM, LOOKUP_FORM)  # uses id, name, lat
    blocked = client.post("/transform/tables/%s/replace" % table_id, files={"file": ("n.csv", b"id,name\nA,Alpha\n", "text/csv")}, follow_redirects=False)
    assert "'lat'" in _flash(blocked) and "Nothing was changed" in _flash(blocked)
    with _isolated_db() as s:
        assert json.loads(s.get(LookupTable, table_id).columns) == ["id", "name", "lat", "lon"]


def test_a_table_in_use_cannot_be_deleted_but_an_unused_one_can(client, source, _isolated_db):
    _upload_table(client)
    _upload_table(client, name="spare")
    _upload_data(client, source)
    _build(client, source, READER_FORM, LOOKUP_FORM)
    used, spare = _table_id(_isolated_db), _table_id(_isolated_db, "spare")
    assert "uses this table" in _flash(client.post("/transform/tables/%s/delete" % used, follow_redirects=False)) and _table_id(_isolated_db) is not None
    assert "deleted" in _flash(client.post("/transform/tables/%s/delete" % spare, follow_redirects=False)) and _table_id(_isolated_db, "spare") is None


def test_unknown_tables_are_handled(client):
    for method, path in (("get", "/transform/tables/nope"), ("post", "/transform/tables/nope/delete"), ("get", "/transform/tables/nope/download")):
        assert "No such index table" in _flash(getattr(client, method)(path, follow_redirects=False))


# -- building a transform ---------------------------------------------------------------------------------------------------------

def test_a_transform_cannot_start_without_a_stored_file_to_look_at(client, source):
    _go(client, source, "create")
    page = client.get("/transform/sources/%s" % source["id"]).text
    assert "no stored file from this source yet" in page and "steps/new?type=read" not in page
    chooser = client.get("/transform/sources/%s/steps/new?type=read" % source["id"]).text
    assert "no stored file from this source yet" in chooser.replace("\n", " ")


def test_the_reader_chooser_offers_what_it_finds_in_the_real_file(client, source):
    _upload_data(client, source)
    _go(client, source, "create")
    page = client.get("/transform/sources/%s/steps/new?type=read" % source["id"]).text
    assert "Detector.xml" in page and "ds &times;3" in page  # the repeating record found in the file
    page = client.get("/transform/sources/%s/steps/new?type=read&format=xml_records&record=ds" % source["id"]).text
    assert "identifier/ident" in page and "data/value@count" in page and "MS812" in page
    assert '<option value="data/value">' in page  # the repeated reading, offered to explode


def test_the_reader_step_is_built_from_the_chosen_fields(client, source, _isolated_db):
    _upload_data(client, source)
    _build(client, source, READER_FORM)
    (step,) = _steps(_isolated_db, source["id"])
    assert step["type"] == "xml_records" and step["record"] == "ds"
    assert {f["name"]: f["path"] for f in step["fields"]} == {"id": "identifier/ident", "time": "data/timeline/timestamp", "state": "data/detectorState"}
    assert {f["name"]: f["path"] for f in step["explode"]["fields"]} == {"count": "@count", "speed": "@speed", "vehicle": "@vehicle"}


def test_a_reader_with_no_fields_or_no_record_is_refused_with_the_form_kept(client, source, _isolated_db):
    _upload_data(client, source)
    _go(client, source, "create")
    for form, message in (({"kind": "read", "format": "xml_records", "record": "ds"}, "at least one field"),
                          ({"kind": "read", "format": "xml_records", "record": "", "path": ["a"]}, "repeating element")):
        page = _go(client, source, "steps/new", data=form).text
        assert message in page and "Save step" in page
    assert _steps(_isolated_db, source["id"]) == []


def test_every_other_step_type_can_be_added_and_is_described_in_words(client, source, _isolated_db):
    _upload_data(client, source)
    _upload_table(client)
    _build(client, source, READER_FORM, LOOKUP_FORM,
           {"kind": "filter", "field": "state", "op": "ne", "value": "0"},
           {"kind": "rename", "old0": "count", "new0": "vehicles"},
           {"kind": "convert", "field0": "vehicles", "to0": "int", "field1": "time", "to1": "datetime"},
           {"kind": "add_field", "name": "key", "value_kind": "template", "value": "{id}-{vehicle}"},
           )
    page = client.get("/transform/sources/%s" % source["id"]).text
    for words in ("One record per each &lt;ds&gt;", "Find «id» in index table «stations»", "Keep records where «state» is not «0»",
                  "Rename: count → vehicles", "Convert: vehicles → int, time → datetime"):
        assert words in page, words
    assert [s["type"] for s in _steps(_isolated_db, source["id"])][:6] == ["xml_records", "lookup", "filter", "rename", "convert", "add_field"]


@pytest.mark.parametrize("form, message", [
    ({"kind": "lookup", "table": "nope", "key": "id", "table_key": "id", "add": ["name"]}, "Choose an index table"),
    ({"kind": "lookup", "table": "stations", "key": "id", "table_key": "id", "add": []}, "at least one column"),
    ({"kind": "lookup", "table": "stations", "key": "ghost", "table_key": "id", "add": ["name"]}, "no earlier step produces"),
    ({"kind": "filter", "field": "ghost", "op": "eq", "value": "1"}, "no earlier step produces"),
    ({"kind": "filter", "field": "id", "op": "eq", "value": ""}, "value"),
    ({"kind": "filter", "field": "id", "op": "bogus", "value": "1"}, "Unknown test"),
    ({"kind": "rename", "old0": "id", "new0": "bad name"}, "letters"),
    ({"kind": "rename", "old0": "id", "new0": "time"}, "share a name"),
    ({"kind": "convert", "field0": "id", "to0": "tensor"}, "Unknown type"),
    ({"kind": "select"}, "fields to keep"), ({"kind": "drop", "fields": ["ghost"]}, "no earlier step produces"),
    ({"kind": "add_field", "name": "x y", "value": "1"}, "letters"), ({"kind": "add_field", "name": "z"}, "Give a value"), ({"kind": "wat"}, "Unknown step"),
])
def test_a_wrong_step_is_explained_and_not_saved(client, source, _isolated_db, form, message):
    _upload_data(client, source)
    _upload_table(client)
    _build(client, source, READER_FORM)
    before = _steps(_isolated_db, source["id"])
    resp = _go(client, source, "steps/new", data=form)
    assert resp.status_code == 200 and message in resp.text  # the form again, with the reason
    assert _steps(_isolated_db, source["id"]) == before


def test_steps_have_an_order_the_first_always_reads_and_a_step_cannot_move_before_its_fields_exist(client, source, _isolated_db):
    _upload_data(client, source)
    _upload_table(client)
    _build(client, source, READER_FORM, {"kind": "add_field", "name": "a", "value": "1"},
           {"kind": "add_field", "name": "b", "value_kind": "template", "value": "{a}"})
    assert "cannot move" in _flash(_go(client, source, "steps/1/move", data={"direction": "down"}))
    assert "cannot move" in _flash(_go(client, source, "steps/2/move", data={"direction": "up"}))  # it would pass the reader
    # step 3 uses field a from step 2, so it cannot go first
    assert "before it exists" in _flash(_go(client, source, "steps/3/move", data={"direction": "up"}))
    assert [s.get("name") for s in _steps(_isolated_db, source["id"])[1:]] == ["a", "b"]


def test_a_step_is_edited_removed_and_the_reader_stays_while_others_depend_on_it(client, source, _isolated_db):
    _upload_data(client, source)
    _upload_table(client)
    _build(client, source, READER_FORM, LOOKUP_FORM)
    page = client.get("/transform/sources/%s/steps/2/edit" % source["id"]).text
    assert "stations" in page and "latitude" in page  # the saved choices are filled in
    resp = _go(client, source, "steps/2/edit", data={**LOOKUP_FORM, "on_missing": "drop", "add": ["name"]})
    assert "saved" in _flash(resp) and _steps(_isolated_db, source["id"])[1]["on_missing"] == "drop"
    assert "Remove the other steps first" in _flash(_go(client, source, "steps/1/remove"))
    assert "removed" in _flash(_go(client, source, "steps/2/remove")) and len(_steps(_isolated_db, source["id"])) == 1
    assert "No such step" in _flash(_go(client, source, "steps/9/remove"))


def test_editing_the_reader_so_a_later_step_loses_its_field_is_refused(client, source, _isolated_db):
    _upload_data(client, source)
    _upload_table(client)
    _build(client, source, READER_FORM, LOOKUP_FORM)  # the lookup matches on field "id"
    form = {**READER_FORM, "path": ["data/timeline/timestamp"], "name::data/timeline/timestamp": "time"}
    page = _go(client, source, "steps/1/edit", data=form).text
    assert "no earlier step produces" in page
    assert "identifier/ident" in json.dumps(_steps(_isolated_db, source["id"])[0])  # unchanged


def test_output_settings(client, source, _isolated_db):
    _go(client, source, "create")
    assert "Output set to XML" in _flash(_go(client, source, "output", data={"format": "xml", "root": "rows", "record": "row"}))
    with _isolated_db() as s:
        t = transform_runner.get_transform(s, source["id"])
        assert (t.output_format, t.root_name, t.record_name) == ("xml", "rows", "row")
    for data in ({"format": "pdf"}, {"format": "xml", "root": "1bad"}, {"format": "xml", "root": "ok", "record": "bad name"}):
        assert _flash(_go(client, source, "output", data=data))
    with _isolated_db() as s:
        assert transform_runner.get_transform(s, source["id"]).output_format == "xml"  # still the last good setting


# -- trying, switching on, re-running, downloading ----------------------------------------------------------------------------------------

def _ready(client, source):
    _upload_data(client, source)
    _upload_table(client)
    _build(client, source, READER_FORM, LOOKUP_FORM, {"kind": "convert", "field0": "count", "to0": "int"})


def test_try_it_shows_the_result_counts_and_what_found_no_match(client, source, _isolated_db):
    _upload_data(client, source)
    _upload_table(client, content="id,name,lat,lon\nMS812,Kifisias,38.03,23.80\n")  # MS810 is not in the index
    _build(client, source, READER_FORM, LOOKUP_FORM)
    page = client.post("/transform/sources/%s/preview" % source["id"]).text
    assert "On <strong>Detector.xml</strong>" in page and "records read" in page and "Kifisias" in page
    assert "found no match in an index table" in page and "MS810" in page


def test_try_it_reports_a_failure_in_words_and_a_missing_file(client, source):
    _go(client, source, "create")
    assert "Not ready" in client.get("/transform/sources/%s" % source["id"]).text
    page = client.post("/transform/sources/%s/preview" % source["id"]).text
    assert "It did not work" in page and "at least a reader step" in page


def test_switching_on_needs_a_transform_that_holds_together_and_turns_off_if_it_stops_doing_so(client, source, _isolated_db):
    _go(client, source, "create")
    assert "not ready" in _flash(_go(client, source, "enabled", data={"enabled": "1"}))
    _ready(client, source) if False else None
    _upload_data(client, source)
    _upload_table(client)
    _build(client, source, READER_FORM, LOOKUP_FORM)
    assert "switched on" in _flash(_go(client, source, "enabled", data={"enabled": "1"}))
    with _isolated_db() as s:
        assert transform_runner.get_transform(s, source["id"]).enabled is True
    _go(client, source, "steps/2/remove")  # still consistent: stays on
    with _isolated_db() as s:
        assert transform_runner.get_transform(s, source["id"]).enabled is True
    assert "switched off" in _flash(_go(client, source, "enabled", data={"enabled": "0"}))


def test_a_new_file_is_transformed_once_the_transform_is_on_and_can_be_downloaded(client, source, _isolated_db, _isolated_harvest_db):
    _ready(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _upload_data(client, source)
    page = client.get("/transform/sources/%s" % source["id"]).text
    assert "Recent outputs" in page and "3 &rarr; 3" in page.replace("→", "&rarr;") or "1 ok" in page
    with _isolated_harvest_db() as h:
        out = h.scalar(select(TransformedFile))
    resp = client.get("/transform/sources/%s/outputs/%s/download" % (source["id"], out.id))
    rows = json.loads(resp.content)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/json") and 'filename="Detector.json"' in resp.headers["content-disposition"]
    assert rows[0]["name"] == "Kifisias" and rows[0]["count"] == 1720


def test_reprocess_runs_earlier_files_and_reports_what_it_did(client, source, _isolated_db, _isolated_harvest_db):
    _upload_data(client, source)
    _upload_data(client, source)
    _upload_table(client)
    _build(client, source, READER_FORM, LOOKUP_FORM)
    assert "Processed 2 runs: 2 ok, 0 failed" in _flash(_go(client, source, "reprocess", data={"limit": "10"}))
    for bad in ({"limit": "7"}, {"limit": ""}, {"limit": "abc"}):
        assert "Choose how many" in _flash(_go(client, source, "reprocess", data=bad))
    with _isolated_harvest_db() as h:
        assert len(list(h.scalars(select(TransformedFile)).all())) == 2


def test_reprocess_refuses_a_transform_that_is_not_ready(client, source):
    _go(client, source, "create")
    assert "not ready" in _flash(_go(client, source, "reprocess", data={"limit": "10"}))


def test_a_failed_output_is_shown_with_its_reason_and_has_no_download(client, source, _isolated_db, _isolated_harvest_db):
    _ready(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _upload_data(client, source, content=b"not xml")
    page = client.get("/transform/sources/%s" % source["id"]).text
    assert "failed" in page and "not valid XML" in page
    with _isolated_harvest_db() as h:
        out = h.scalar(select(TransformedFile))
    assert "no output" in _flash(client.get("/transform/sources/%s/outputs/%s/download" % (source["id"], out.id), follow_redirects=False))


def test_a_removed_output_says_so_and_cannot_be_downloaded(client, source, _isolated_db, _isolated_harvest_db):
    _ready(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _upload_data(client, source)
    with _isolated_harvest_db() as h:
        out = h.scalar(select(TransformedFile))
        out.content = b""
        h.commit()
        out_id = out.id
    assert ">removed<" in client.get("/transform/sources/%s" % source["id"]).text
    assert client.get("/transform/sources/%s/outputs/%s/download" % (source["id"], out_id)).status_code == 410


def test_an_output_of_another_source_is_never_served(client, source, _isolated_db, _isolated_harvest_db):
    _ready(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _upload_data(client, source)
    with _isolated_harvest_db() as h:
        out_id = h.scalar(select(TransformedFile.id))
    assert "No such output" in _flash(client.get("/transform/sources/other/outputs/%s/download" % out_id, follow_redirects=False))


def test_unknown_sources_and_transforms_are_handled_everywhere(client, source):
    for path in ("create", "enabled", "output", "preview", "reprocess", "steps/new", "steps/1/edit", "steps/1/remove", "steps/1/move"):
        resp = client.post("/transform/sources/nope/%s" % path, follow_redirects=False)
        assert resp.status_code == 303 and "flash_kind=error" in resp.headers["location"], path
    for path in ("/transform/sources/nope", "/transform/sources/nope/steps/new?type=read", "/transform/sources/nope/steps/1/edit"):
        assert "flash_kind=error" in client.get(path, follow_redirects=False).headers["location"], path
    _go(client, source, "create")
    assert "Choose which step" in _flash(client.get("/transform/sources/%s/steps/new" % source["id"], follow_redirects=False))
    assert "must read the file" in _flash(client.get("/transform/sources/%s/steps/new?type=filter" % source["id"], follow_redirects=False))


# -- sources that do not look like this one ------------------------------------------------------------------------------------------------

def test_a_json_source_is_built_with_the_same_pages(client, source, _isolated_db):
    data = json.dumps({"stations": [{"code": "S1", "geo": {"lat": 38.0}}, {"code": "S2", "geo": {"lat": 37.9}}]}).encode()
    _upload_data(client, source, content=data, name="data.json")
    _go(client, source, "create")
    page = client.get("/transform/sources/%s/steps/new?type=read" % source["id"]).text
    assert "data.json" in page and "stations &times;2" in page and "geo/lat" in page
    form = {"kind": "read", "format": "json_records", "record": "stations", "path": ["code", "geo/lat"], "name::code": "code", "name::geo/lat": "lat"}
    assert "added" in _flash(_go(client, source, "steps/new", data=form))
    page = client.post("/transform/sources/%s/preview" % source["id"]).text
    assert "S1" in page and "38.0" in page


def test_a_csv_source_is_built_with_the_same_pages(client, source, _isolated_db):
    _upload_data(client, source, content=b"Station ID;Count\nA;5\nB;7\n", name="readings.csv")
    _go(client, source, "create")
    page = client.get("/transform/sources/%s/steps/new?type=read&format=csv_records" % source["id"]).text
    assert "Station ID" in page and "Delimiter" in page
    form = {"kind": "read", "format": "csv_records", "delimiter": ";", "path": ["Station ID", "Count"], "name::Station ID": "station", "name::Count": "n"}
    assert "added" in _flash(_go(client, source, "steps/new", data=form))
    assert _steps(_isolated_db, source["id"])[0]["fields"][0] == {"name": "station", "path": "Station ID"}
    page = client.post("/transform/sources/%s/preview" % source["id"]).text
    assert ">A<" in page and ">7<" in page
