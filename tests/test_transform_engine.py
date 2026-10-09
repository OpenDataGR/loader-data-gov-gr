"""The Transform Box's engine: readers for different formats, the steps after them, the outputs, the limits.
Nothing here is specific to one source -- the XML sample is modelled on a real SOAP feed but the same steps
are shown working on JSON and CSV of quite different shapes."""

import json

import pytest

from providerharvest_service import transform_engine as te
from providerharvest_service.transform_engine import TransformError, run

# A SOAP envelope like a real traffic feed: the same element names arrive under different namespace
# prefixes (ns7, ns8, ...), a BOM leads the file, a record can hold one or two readings.
SOAP = """﻿<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <soapenv:Body><inquireAllResponse xmlns="">
  <ns1:lastStart xmlns:ns1="http://x/PD">2026-09-07T13:20:38.311Z</ns1:lastStart>
  <ns6:dataList xmlns:ns6="http://x/PD">
   <ns6:ds>
    <ns6:tstore>2026-10-09T08:42:05.823Z</ns6:tstore>
    <ns6:identifier><ns6:ident>MS812</ns6:ident></ns6:identifier>
    <ns6:data xsi:type="ns7:currentValueType" xmlns:ns7="http://x/traffic">
     <ns7:id>MS812</ns7:id>
     <ns7:timeline><ns7:timestamp>2026-10-09T08:41:00.000Z</ns7:timestamp><ns7:cycle>60</ns7:cycle></ns7:timeline>
     <ns7:value count="1720" occ="5" speed="58" vehicle="all" />
     <ns7:detectorState>4</ns7:detectorState>
    </ns6:data>
   </ns6:ds>
   <ns6:ds>
    <ns6:tstore>2026-10-09T08:42:05.823Z</ns6:tstore>
    <ns6:identifier><ns6:ident>MS810</ns6:ident></ns6:identifier>
    <ns6:data xsi:type="ns8:currentValueType" xmlns:ns8="http://x/traffic">
     <ns8:id>MS810</ns8:id>
     <ns8:timeline><ns8:timestamp>2026-10-09T08:41:00.000Z</ns8:timestamp><ns8:cycle>60</ns8:cycle></ns8:timeline>
     <ns8:value count="900" occ="3" speed="61" vehicle="car" />
     <ns8:value count="40" occ="1" speed="55" vehicle="truck" />
     <ns8:detectorState>1</ns8:detectorState>
    </ns6:data>
   </ns6:ds>
   <ns6:ds>
    <ns6:tstore>2026-10-09T08:42:05.823Z</ns6:tstore>
    <ns6:identifier><ns6:ident>MS999</ns6:ident></ns6:identifier>
    <ns6:data xmlns:ns9="http://x/traffic"><ns9:id>MS999</ns9:id><ns9:detectorState>9</ns9:detectorState></ns6:data>
   </ns6:ds>
  </ns6:dataList></inquireAllResponse></soapenv:Body></soapenv:Envelope>"""

XML_STEP = {
    "type": "xml_records", "record": "ds",
    "fields": [{"name": "id", "path": "identifier/ident"}, {"name": "time", "path": "data/timeline/timestamp"}, {"name": "state", "path": "data/detectorState"}],
    "explode": {"path": "data/value", "fields": [{"name": "count", "path": "@count"}, {"name": "occ", "path": "@occ"},
                                                   {"name": "speed", "path": "@speed"}, {"name": "vehicle", "path": "@vehicle"}]},
}

STATIONS = te.lookup_table(["id", "name", "lat", "lon"], [
    {"id": "MS812", "name": "Kifisias", "lat": "38.03", "lon": "23.80"},
    {"id": "MS810", "name": "Mesogeion", "lat": "38.00", "lon": "23.80"},
])


def _read(step, content):
    return te.READERS[step["type"]]["read"](content if isinstance(content, bytes) else content.encode("utf-8"), step)


# -- XML ------------------------------------------------------------------------------------------------

def test_xml_records_ignore_namespace_prefixes_and_explode_repeated_readings():
    rows = _read(XML_STEP, SOAP)
    assert [(r["id"], r["vehicle"]) for r in rows] == [("MS812", "all"), ("MS810", "car"), ("MS810", "truck")]
    assert rows[0] == {"id": "MS812", "time": "2026-10-09T08:41:00.000Z", "state": "4", "count": "1720", "occ": "5", "speed": "58", "vehicle": "all"}
    assert rows[2]["id"] == "MS810" and rows[2]["count"] == "40" and rows[2]["time"] == rows[1]["time"]  # record fields repeat per reading


def test_a_record_with_no_readings_is_dropped_unless_asked_to_keep_it():
    assert "MS999" not in [r["id"] for r in _read(XML_STEP, SOAP)]
    kept = _read({**XML_STEP, "explode": {**XML_STEP["explode"], "keep_empty": True}}, SOAP)
    empty = [r for r in kept if r["id"] == "MS999"]
    assert len(empty) == 1 and empty[0]["count"] is None and empty[0]["state"] == "9"


def test_without_explode_each_record_is_one_row_and_a_missing_field_is_none():
    step = {"type": "xml_records", "record": "ds", "fields": [{"name": "id", "path": "identifier/ident"}, {"name": "ts", "path": "data/timeline/timestamp"}]}
    rows = _read(step, SOAP)
    assert [r["id"] for r in rows] == ["MS812", "MS810", "MS999"] and rows[2]["ts"] is None


def test_an_attribute_on_the_record_itself_is_reachable():
    xml = '<list><item code="A1"><n>one</n></item><item code="B2"><n>two</n></item></list>'
    rows = _read({"type": "xml_records", "record": "item", "fields": [{"name": "code", "path": "@code"}, {"name": "n", "path": "n"}]}, xml)
    assert rows == [{"code": "A1", "n": "one"}, {"code": "B2", "n": "two"}]


@pytest.mark.parametrize("bad, message", [
    ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "boom">]><x>&a;</x>', "refused"),
    ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]><lolz>&lol2;</lolz>', "refused"),
    ('<?xml version="1.0"?><!DOCTYPE x SYSTEM "http://evil.example/x.dtd"><x/>', "refused"),
    ("<not xml", "not valid XML"),
    ("", "not valid XML"),
])
def test_dangerous_or_broken_xml_is_refused_with_a_message(bad, message):
    with pytest.raises(TransformError, match=message):
        _read({"type": "xml_records", "record": "x", "fields": [{"name": "a", "path": "b"}]}, bad)


@pytest.mark.parametrize("step", [
    {"type": "xml_records", "record": "", "fields": [{"name": "a", "path": "b"}]},
    {"type": "xml_records", "record": "a/b", "fields": [{"name": "a", "path": "b"}]},
    {"type": "xml_records", "record": "ds", "fields": []},
    {"type": "xml_records", "record": "ds", "fields": [{"name": "1bad", "path": "b"}]},
    {"type": "xml_records", "record": "ds", "fields": [{"name": "a", "path": "b"}, {"name": "a", "path": "c"}]},
    {"type": "xml_records", "record": "ds", "fields": [{"name": "a", "path": "b c"}]},
])
def test_an_incomplete_reader_step_is_refused_before_it_reads_anything(step):
    with pytest.raises(TransformError):
        run(SOAP.encode("utf-8"), [step])


# -- JSON, of quite a different shape ---------------------------------------------------------------------------

JSON_DOC = {"meta": {"v": 2}, "stations": [
    {"code": "S1", "geo": {"lat": 38.0, "lon": 23.7}, "readings": [{"t": "2026-01-01T00:00:00Z", "v": 3}, {"t": "2026-01-01T00:05:00Z", "v": 4}]},
    {"code": "S2", "geo": {"lat": 37.9, "lon": 23.6}, "readings": []},
    "not an object",
]}


def test_json_records_at_a_nested_path_with_nested_fields_and_exploded_lists():
    step = {"type": "json_records", "record": "stations", "fields": [{"name": "code", "path": "code"}, {"name": "lat", "path": "geo/lat"}],
            "explode": {"path": "readings", "fields": [{"name": "t", "path": "t"}, {"name": "v", "path": "v"}]}}
    rows = _read(step, json.dumps(JSON_DOC))
    assert rows == [{"code": "S1", "lat": 38.0, "t": "2026-01-01T00:00:00Z", "v": 3}, {"code": "S1", "lat": 38.0, "t": "2026-01-01T00:05:00Z", "v": 4}]


def test_json_without_a_field_list_takes_every_top_level_field_and_a_root_list_works():
    rows = _read({"type": "json_records", "record": ""}, json.dumps([{"a": 1, "b": {"c": 2}}, {"a": 3}]))
    assert rows == [{"a": 1, "b": '{"c":2}'}, {"a": 3}]


def test_json_problems_are_messages_not_crashes():
    with pytest.raises(TransformError, match="not valid JSON"):
        _read({"type": "json_records", "record": ""}, "{nope")
    with pytest.raises(TransformError, match="No 'nothing'"):
        _read({"type": "json_records", "record": "nothing"}, json.dumps(JSON_DOC))


# -- CSV -----------------------------------------------------------------------------------------------------------

def test_csv_with_a_bom_and_semicolons_and_blank_cells():
    rows = _read({"type": "csv_records", "delimiter": ";"}, "﻿id;name;note\nA;Alpha;\nB;Beta;ok\n")
    assert rows == [{"id": "A", "name": "Alpha", "note": None}, {"id": "B", "name": "Beta", "note": "ok"}]


def test_csv_columns_can_be_chosen_and_renamed():
    rows = _read({"type": "csv_records", "fields": [{"name": "station", "path": "Station ID"}]}, "Station ID,Other\nX1,y\n")
    assert rows == [{"station": "X1"}]


def test_a_csv_without_a_header_is_refused():
    with pytest.raises(TransformError, match="header"):
        _read({"type": "csv_records"}, "")


def test_the_format_is_recognised_from_the_name_or_the_content():
    assert te.detect_format(b"x", "a.XML") == "xml_records" and te.detect_format(b"x", "a.json") == "json_records"
    assert te.detect_format(b"\xef\xbb\xbf<?xml", "") == "xml_records" and te.detect_format(b" [1]", "") == "json_records"
    assert te.detect_format(b"a,b", "") is None


# -- looking at a sample -----------------------------------------------------------------------------------------------

def test_candidates_and_fields_are_found_in_an_xml_sample_for_a_person_to_choose_from():
    found = {c["path"]: c["count"] for c in te.candidates(SOAP.encode("utf-8"), "xml_records")}
    assert found["ds"] == 3 and found["identifier"] == 3
    info = te.describe_record(SOAP.encode("utf-8"), "xml_records", "ds")
    fields = {f["path"]: f for f in info["fields"]}
    assert fields["identifier/ident"]["example"] == "MS812" and not fields["identifier/ident"]["repeats"]
    assert fields["data/value@count"]["repeats"] is True and fields["data/value@vehicle"]["example"] == "all"
    assert "data/value" in info["repeating"]


def test_candidates_and_fields_for_json_and_csv_samples():
    data = json.dumps(JSON_DOC).encode()
    assert {"path": "stations", "count": 3} in te.candidates(data, "json_records") or any(c["path"] == "stations" for c in te.candidates(data, "json_records"))
    info = te.describe_record(data, "json_records", "stations")
    assert {"code", "geo/lat", "readings/t"} <= {f["path"] for f in info["fields"]}
    csv_info = te.describe_record(b"id,name\nA,Alpha\n", "csv_records", "")
    assert [f["path"] for f in csv_info["fields"]] == ["id", "name"] and csv_info["fields"][1]["example"] == "Alpha"


# -- the steps --------------------------------------------------------------------------------------------------------------

def _steps(*steps):
    return [XML_STEP, *steps]


def _go(*steps, content=SOAP, lookups=None, output="json"):
    return run(content.encode("utf-8"), _steps(*steps), output=output, lookups=lookups or {"stations": STATIONS})


LOOKUP = {"type": "lookup", "table": "stations", "key": "id", "table_key": "id", "add": [{"column": "name"}, {"column": "lat", "as": "latitude"}]}


def test_lookup_adds_columns_and_counts_what_it_could_not_match():
    result = _go(LOOKUP)
    rows = json.loads(result.content)
    assert rows[0]["name"] == "Kifisias" and rows[0]["latitude"] == "38.03" and rows[1]["name"] == "Mesogeion"
    assert result.stats["lookup_missing"] == 0  # every reading's id is in the table here
    result = _go(LOOKUP, lookups={"stations": te.lookup_table(["id", "name", "lat"], [{"id": "MS812", "name": "K", "lat": "1"}])})
    assert result.stats["lookup_missing"] == 2 and result.stats["missing_keys"] == ["MS810"]  # both MS810 readings; the key listed once


def test_lookup_can_drop_what_it_cannot_match_and_matches_despite_stray_spaces():
    table = te.lookup_table(["id", "name"], [{"id": " MS812 ", "name": "K"}])
    kept = _go({**LOOKUP, "add": [{"column": "name"}]}, lookups={"stations": table})
    assert [r["name"] for r in json.loads(kept.content)] == ["K", None, None]
    dropped = _go({**LOOKUP, "add": [{"column": "name"}], "on_missing": "drop"}, lookups={"stations": table})
    assert [r["id"] for r in json.loads(dropped.content)] == ["MS812"] and dropped.stats["dropped_by_lookup"] == 2


@pytest.mark.parametrize("change, message", [
    ({"table": "nope"}, "does not exist"), ({"table_key": "zzz"}, "no column 'zzz'"),
    ({"add": [{"column": "zzz"}]}, "no column 'zzz'"), ({"add": []}, "at least one column"),
    ({"key": "bad key"}, "letters"), ({"on_missing": "explode"}, "keep or drop"),
])
def test_lookup_mistakes_are_explained(change, message):
    with pytest.raises(TransformError, match=message):
        _go({**LOOKUP, **change})


@pytest.mark.parametrize("op, value, expected", [
    ("eq", "4", ["MS812"]), ("ne", "4", ["MS810", "MS810"]), ("in", "1, 4", ["MS812", "MS810", "MS810"]), ("not_in", "1", ["MS812"]),
    ("gt", "1", ["MS812"]), ("ge", "1", ["MS812", "MS810", "MS810"]), ("lt", "4", ["MS810", "MS810"]), ("le", "1", ["MS810", "MS810"]),
    ("contains", "4", ["MS812"]), ("exists", None, ["MS812", "MS810", "MS810"]), ("not_exists", None, []),
])
def test_filter_tests(op, value, expected):
    result = _go({"type": "filter", "field": "state", "op": op, "value": value})
    assert [r["id"] for r in json.loads(result.content)] == expected
    assert result.stats["dropped_by_filter"] == 3 - len(expected)


def test_filter_compares_numbers_as_numbers_and_text_as_text_and_missing_values_sensibly():
    rows = lambda *vs: [{"v": v} for v in vs]  # noqa: E731
    ctx = te.Context(lookups={})
    assert [r["v"] for r in te.step_filter(rows("9", "10", None), {"field": "v", "op": "gt", "value": "9"}, ctx)] == ["10"]  # 10 > 9, not "10" < "9"
    assert [r["v"] for r in te.step_filter(rows("b", "a"), {"field": "v", "op": "gt", "value": "a"}, ctx)] == ["b"]
    assert te.step_filter(rows(None), {"field": "v", "op": "ne", "value": "x"}, ctx) == [{"v": None}]  # nothing is "not x"
    with pytest.raises(TransformError, match="value"):
        te.step_filter(rows("1"), {"field": "v", "op": "eq"}, ctx)


def test_rename_select_drop_and_add_field():
    result = _go({"type": "rename", "map": {"count": "vehicles"}}, {"type": "add_field", "name": "key", "kind": "template", "value": "{id}-{vehicle}-{nothing}"},
                 {"type": "add_field", "name": "source", "value": "attica"}, {"type": "drop", "fields": ["occ", "speed", "state", "time"]})
    assert json.loads(result.content)[1] == {"id": "MS810", "vehicles": "900", "vehicle": "car", "key": "MS810-car-", "source": "attica"}
    selected = _go({"type": "select", "fields": ["vehicle", "id", "missing"]})
    assert json.loads(selected.content)[0] == {"vehicle": "all", "id": "MS812", "missing": None}  # in this order


def test_renaming_two_fields_to_one_name_is_refused():
    with pytest.raises(TransformError, match="share a name"):
        _go({"type": "rename", "map": {"count": "occ"}})


def test_convert_types_and_count_what_would_not_convert():
    rows = [{"n": "12", "f": "1.5", "b": "yes", "d": "2026-10-09T08:41:00.000Z", "bad": "x12"}, {"n": "1.5", "f": "abc", "b": "maybe", "d": "2026-10-09 08:41:00", "bad": "7"}]
    ctx = te.Context(lookups={})
    out = te.step_convert(rows, {"fields": {"n": "int", "f": "float", "b": "bool", "d": "datetime", "bad": "int"}}, ctx)
    assert out[0] == {"n": 12, "f": 1.5, "b": True, "d": "2026-10-09T08:41:00Z", "bad": None}
    assert out[1] == {"n": None, "f": None, "b": None, "d": "2026-10-09T08:41:00Z", "bad": 7}  # a naive time is taken as UTC
    assert ctx.stats["conversion_errors"] == {"n": 1, "f": 1, "b": 1, "bad": 1}


def test_a_failed_conversion_never_fails_the_run():
    result = _go({"type": "convert", "fields": {"vehicle": "int", "count": "int"}})
    rows = json.loads(result.content)
    assert rows[0]["count"] == 1720 and rows[0]["vehicle"] is None and result.stats["conversion_errors"]["vehicle"] == 3


def test_steps_are_validated():
    assert te.validate([XML_STEP, LOOKUP]) == []
    assert "Step 1 must read" in te.validate([LOOKUP])[0]
    assert "unknown step" in te.validate([XML_STEP, {"type": "exec", "cmd": "rm"}])[0]
    assert te.validate([]) and te.validate("x") and te.validate([XML_STEP] + [LOOKUP] * 25)[0].startswith("At most")


# -- outputs ----------------------------------------------------------------------------------------------------------------

def test_json_and_json_lines_keep_non_ascii_text():
    records = [{"name": "Λεωφόρος Κηφισίας", "n": 1}, {"name": None, "n": 2}]
    assert json.loads(te.write_output(records, "json")) == records
    lines = te.write_output(records, "jsonl").decode().split("\n")
    assert [json.loads(line) for line in lines] == records and "Κηφισίας" in lines[0]


def test_csv_header_is_every_field_in_order_of_appearance_with_blanks_for_none():
    text = te.write_output([{"a": 1, "b": "x,y"}, {"a": 2, "c": None}], "csv").decode()
    assert text.split("\n")[0] == "a,b,c" and '"x,y"' in text and text.split("\n")[2] == "2,,"


def test_xml_output_escapes_text_marks_empty_values_and_refuses_bad_names():
    xml = te.write_output([{"a": "<&>", "b": None}], "xml", root="rows", record="row").decode()
    assert "<rows><row><a>&lt;&amp;&gt;</a><b nil=\"true\" /></row></rows>" in xml and xml.startswith("<?xml")
    with pytest.raises(TransformError):
        te.write_output([{"bad name": 1}], "xml")
    with pytest.raises(TransformError):
        te.write_output([{"a": 1}], "xml", root="1bad")
    with pytest.raises(TransformError, match="Unknown output"):
        te.write_output([], "pdf")


def test_a_whole_transform_reports_counts_and_a_preview():
    result = _go(LOOKUP, output="csv")
    assert (result.records_in, result.records_out, result.content_type, result.extension) == (3, 3, "text/csv; charset=utf-8", "csv")
    assert result.content.decode().split("\n")[0] == "id,time,state,count,occ,speed,vehicle,name,latitude"
    assert result.preview[0]["name"] == "Kifisias"


def test_the_preview_is_limited_but_the_output_is_complete():
    many = "<l>" + "".join("<r><i>%d</i></r>" % n for n in range(120)) + "</l>"
    result = run(many.encode(), [{"type": "xml_records", "record": "r", "fields": [{"name": "i", "path": "i"}]}])
    assert result.records_out == 120 and len(result.preview) == te.PREVIEW_RECORDS and len(json.loads(result.content)) == 120


# -- limits ----------------------------------------------------------------------------------------------------------------------

def test_too_many_records_and_too_big_an_input_are_refused(monkeypatch):
    many = "<l>" + "<r><i>1</i></r>" * 30 + "</l>"
    step = {"type": "xml_records", "record": "r", "fields": [{"name": "i", "path": "i"}]}
    monkeypatch.setattr(te, "MAX_RECORDS", 10)
    with pytest.raises(TransformError, match="More than 10 records"):
        run(many.encode(), [step])
    monkeypatch.setattr(te, "MAX_INPUT_BYTES", 5)
    with pytest.raises(TransformError, match="larger than"):
        run(many.encode(), [step])


def test_an_unknown_output_and_a_step_that_does_not_fit_the_data_are_messages():
    with pytest.raises(TransformError, match="Unknown output"):
        _go(output="pdf")
    with pytest.raises(TransformError):
        run(b"[1, 2]", [{"type": "xml_records", "record": "x", "fields": [{"name": "a", "path": "b"}]}])


# -- index tables ------------------------------------------------------------------------------------------------------------------

def test_a_csv_index_with_semicolons_and_a_json_index_both_load():
    columns, rows = te.parse_lookup("id;name\nA;Alpha\nB;Beta\n".encode("utf-8-sig"), "stations.csv")
    assert columns == ["id", "name"] and rows[1] == {"id": "B", "name": "Beta"}
    columns, rows = te.parse_lookup(json.dumps([{"id": "A", "lat": 1.5}, {"id": "B"}]).encode(), "stations.json")
    assert columns == ["id", "lat"] and rows[0]["lat"] == "1.5" and rows[1]["lat"] is None
    columns, rows = te.parse_lookup(json.dumps({"stations": [{"id": "A"}]}).encode(), "x.json")
    assert rows == [{"id": "A"}]


def test_a_greek_windows_csv_index_is_decoded():
    columns, rows = te.parse_lookup("id,όνομα\nA,Αθήνα\n".encode("cp1253"), "x.csv")
    assert columns == ["id", "όνομα"] and rows[0]["όνομα"] == "Αθήνα"


@pytest.mark.parametrize("content, name, message", [
    (b"", "x.csv", "header"), (b"id,name\n", "x.csv", "no rows"), (b"id,id\nA,B\n", "x.csv", "own non-empty name"),
    (b"<a/>", "x.xml", "CSV or JSON"), (b'{"a": 1}', "x.json", "list of objects"), (b"[1,2]", "x.json", "list of objects"),
])
def test_bad_index_files_are_explained(content, name, message):
    with pytest.raises(TransformError, match=message):
        te.parse_lookup(content, name)


def test_index_tables_have_size_limits(monkeypatch):
    monkeypatch.setattr(te, "MAX_LOOKUP_ROWS", 2)
    with pytest.raises(TransformError, match="At most"):
        te.parse_lookup(b"id\n1\n2\n3\n", "x.csv")
    monkeypatch.setattr(te, "MAX_LOOKUP_BYTES", 3)
    with pytest.raises(TransformError, match="larger than"):
        te.parse_lookup(b"id\n1\n", "x.csv")


def test_a_csv_sample_shows_its_real_column_names_and_finds_the_delimiter():
    sample = "Station ID;Vehicle count\nA1;5\nB2;\n".encode("utf-8-sig")
    assert te.sniff_delimiter(sample) == ";" and te.sniff_delimiter(b"a,b\n") == "," and te.sniff_delimiter(b"single\n") == ","
    info = te.describe_record(sample, "csv_records", "")
    assert [(f["path"], f["example"]) for f in info["fields"]] == [("Station ID", "A1"), ("Vehicle count", "5")]
    assert te.describe_record(sample, "csv_records", "", delimiter=",")["fields"][0]["path"] == "Station ID;Vehicle count"  # a chosen delimiter wins


def test_add_field_needs_a_value():
    for value in (None, ""):
        with pytest.raises(TransformError, match="Give a value"):
            te.step_add_field([{"a": 1}], {"name": "z", "kind": "constant", "value": value}, te.Context(lookups={}))


def test_the_record_that_holds_the_most_is_offered_first_and_plain_text_leaves_never_are():
    found = te.candidates(SOAP.encode("utf-8"), "xml_records")
    paths = [c["path"] for c in found]
    assert paths[0] == "ds"  # a whole detector, before the readings or the single-value elements inside it
    assert "ident" not in paths and "tstore" not in paths and "detectorState" not in paths  # text leaves: never a record
    assert "value" in paths  # a leaf with attributes can be a record (as in a flat list of items)
    flat = "<l><i code='A'/><i code='B'/><i code='C'/></l>"
    assert [c["path"] for c in te.candidates(flat.encode(), "xml_records")] == ["i"]


# -- CSV that Excel opens correctly -------------------------------------------------------------------------------------------

GREEK_ROWS = [{"όνομα": "Λεωφόρος Κηφισίας", "n": 1}, {"όνομα": "Α;Β", "n": None}]


def test_a_csv_for_excel_starts_with_a_utf_8_byte_order_mark_and_reads_back_as_greek():
    data = te.write_output(GREEK_ROWS, "csv", bom=True)
    assert data[:3] == b"\xef\xbb\xbf" and data.decode("utf-8-sig").split("\n")[1] == "Λεωφόρος Κηφισίας,1"


def test_without_the_mark_the_file_is_plain_utf_8():
    data = te.write_output(GREEK_ROWS, "csv", bom=False)
    assert data[:3] != b"\xef\xbb\xbf" and data.decode("utf-8").startswith("όνομα,n")
    assert te.write_output(GREEK_ROWS, "csv").decode("utf-8").startswith("όνομα,n")  # the engine's own default: no mark


@pytest.mark.parametrize("delimiter, header, second_row", [
    (",", "όνομα,n", "Α;Β,"), (";", "όνομα;n", '"Α;Β";'), ("\t", "όνομα\tn", "Α;Β\t"), ("|", "όνομα|n", "Α;Β|"),
])
def test_the_separator_is_a_choice_and_a_value_containing_it_is_quoted(delimiter, header, second_row):
    lines = te.write_output(GREEK_ROWS, "csv", delimiter=delimiter).decode("utf-8").split("\n")
    assert lines[0] == header and lines[2] == second_row  # "Α;Β" is quoted only where ";" is the separator


@pytest.mark.parametrize("bad", ["", "::", "x", " ", "\n"])
def test_a_separator_that_is_not_offered_is_refused(bad):
    with pytest.raises(TransformError, match="separators"):
        te.write_output(GREEK_ROWS, "csv", delimiter=bad)


def test_the_settings_only_affect_csv_output():
    assert te.write_output([{"a": "ς"}], "json", delimiter=";", bom=True) == te.write_output([{"a": "ς"}], "json")
    assert te.write_output([{"a": "ς"}], "xml", delimiter=";", bom=True) == te.write_output([{"a": "ς"}], "xml")


def test_run_passes_the_csv_settings_through():
    result = te.run(b"<l><r><n>\xce\xa9</n></r></l>", [{"type": "xml_records", "record": "r", "fields": [{"name": "n", "path": "n"}]}], output="csv", delimiter=";", bom=True)
    assert result.content[:3] == b"\xef\xbb\xbf" and result.content_type == "text/csv; charset=utf-8"


# -- UTM coordinates ---------------------------------------------------------------------------------------------------------

def _utm_forward(lat, lon, zone):
    """Independent check: the forward (latitude/longitude -> UTM) series, so the step's inverse is tested against it."""
    import math

    a, f, k0 = 6378137.0, 1 / 298.257223563, 0.9996
    n = f / (2 - f)
    big_a = a / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64)
    alpha = (n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16 + 41 * n ** 4 / 180, 13 * n ** 2 / 48 - 3 * n ** 3 / 5 + 557 * n ** 4 / 1440,
             61 * n ** 3 / 240 - 103 * n ** 4 / 140, 49561 * n ** 4 / 161280)
    phi, lam = math.radians(lat), math.radians(lon - (zone * 6 - 183))
    e = math.sqrt(f * (2 - f))
    t = math.sinh(math.atanh(math.sin(phi)) - e * math.atanh(e * math.sin(phi)))
    xi0, eta0 = math.atan2(t, math.cos(lam)), math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    xi = xi0 + sum(al * math.sin(2 * j * xi0) * math.cosh(2 * j * eta0) for j, al in enumerate(alpha, 1))
    eta = eta0 + sum(al * math.cos(2 * j * xi0) * math.sinh(2 * j * eta0) for j, al in enumerate(alpha, 1))
    return 500000 + k0 * big_a * eta, k0 * big_a * xi


@pytest.mark.parametrize("lat,lon,zone", [(37.9755, 23.7348, 34), (40.6401, 22.9444, 34), (35.3387, 25.1442, 35), (0.0, 21.0, 34), (-33.9, 18.4, 34)])
def test_utm_coordinates_come_back_to_the_latitude_and_longitude_they_came_from(lat, lon, zone):
    easting, northing = _utm_forward(lat, lon, zone)
    got = te.utm_to_latlon(easting, northing, zone, north=lat >= 0) if lat >= 0 else te.utm_to_latlon(easting, northing + 10_000_000, zone, north=False)
    assert got == pytest.approx((lat, lon), abs=1e-7)


def test_the_central_meridian_at_the_equator_is_where_it_should_be():
    assert te.utm_to_latlon(500000, 0, 34) == pytest.approx((0.0, 21.0), abs=1e-9)


def _utm_records(rows):
    return json.dumps({"p": rows}).encode()


UTM_READ = {"type": "json_records", "record": "p", "fields": [{"name": "name", "path": "name"}, {"name": "x", "path": "x"}, {"name": "y", "path": "y"}]}


def test_the_utm_step_adds_latitude_and_longitude_and_counts_what_it_could_not_read():
    x, y = _utm_forward(37.9755, 23.7348, 34)
    rows = [{"name": "ok", "x": x, "y": y}, {"name": "comma", "x": ("%f" % x).replace(".", ","), "y": str(y)},
            {"name": "bad", "x": "n/a", "y": y}, {"name": "empty", "x": None, "y": None}]
    step = {"type": "utm", "x": "x", "y": "y", "zone": 34, "hemisphere": "N", "lat_name": "latitude", "lon_name": "longitude", "decimals": 6}
    result = run(_utm_records(rows), [UTM_READ, step], output="json")
    out = json.loads(result.content)["record"] if isinstance(json.loads(result.content), dict) else json.loads(result.content)
    flat = out if isinstance(out, list) else list(out)
    by = {r["name"]: r for r in flat}
    assert by["ok"]["latitude"] == pytest.approx(37.9755, abs=1e-6) and by["ok"]["longitude"] == pytest.approx(23.7348, abs=1e-6)
    assert by["comma"]["latitude"] == pytest.approx(37.9755, abs=1e-5)
    assert by["bad"]["latitude"] is None and by["empty"]["longitude"] is None
    assert result.stats["conversion_errors"] == {"latitude": 1}  # "bad" counted, "empty" is just missing


@pytest.mark.parametrize("change,message", [({"zone": 0}, "zone"), ({"zone": "x"}, "zone"), ({"decimals": 11}, "Decimals"),
                                            ({"lon_name": "latitude"}, "different names"), ({"x": "bad name"}, "easting")])
def test_a_wrong_utm_step_is_refused_with_a_reason(change, message):
    step = {"type": "utm", "x": "x", "y": "y", "zone": 34, "hemisphere": "N", "lat_name": "latitude", "lon_name": "longitude", "decimals": 6, **change}
    with pytest.raises(TransformError, match=message):
        te.step_utm([], step, te.Context(lookups={}))


def test_a_failed_utm_conversion_shows_an_example_of_what_it_could_not_read():
    step = {"type": "utm", "x": "x", "y": "y", "zone": 34, "hemisphere": "N", "lat_name": "latitude", "lon_name": "longitude", "decimals": 6}
    result = run(_utm_records([{"name": "a", "x": "23.7", "y": "n/a"}]), [UTM_READ, step], output="json")
    assert result.stats["conversion_examples"] == {"latitude": "x='23.7', y='n/a'"}
