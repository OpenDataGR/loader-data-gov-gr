"""Excel (.xlsx) as an index table and as a source's own format: awkward cells, several sheets, damaged and
hostile files, and the pages that take them."""

import datetime
import io
import json
import zipfile

import pytest
from openpyxl import Workbook

from providerharvest_service import transform_engine as te
from providerharvest_service.models import LookupTable
from providerharvest_service.transform_engine import TransformError
from tests.test_push_ingest import (  # noqa: F401  (fixtures and helpers shared with the push ingest tests)
    _isolated_db, _isolated_harvest_db, _master_key, _register_and_activate_push_source, client,
)
from tests.test_transform_ui import _build, _flash, _go, _upload_data, source  # noqa: F401


def xlsx(sheets: dict, *, active: str | None = None) -> bytes:
    """An .xlsx with the given sheets: {name: [row, row, ...]}."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


STATIONS = xlsx({"Stations": [
    ["id", "name", "lat", "lon", "installed", "active"],
    [812, "Λεωφόρος Κηφισίας", 38.03, 23.8, datetime.datetime(2019, 5, 17, 9, 30), True],
    ["MS810", "Mesogeion / Papagou", 38.0, 23.8123456789, datetime.date(2020, 1, 2), False],
    [None, None, None, None, None, None],
    ["MS777", "  padded  ", 1e-5, 12345678.0, None, None],
]})


# -- as an index table -----------------------------------------------------------------------------------------

def test_cells_become_clean_text_whole_numbers_without_a_point_zero_dates_in_iso():
    columns, rows = te.parse_lookup(STATIONS, "stations.xlsx")
    assert columns == ["id", "name", "lat", "lon", "installed", "active"]
    assert rows[0] == {"id": "812", "name": "Λεωφόρος Κηφισίας", "lat": "38.03", "lon": "23.8", "installed": "2019-05-17T09:30:00", "active": "true"}
    assert rows[1]["lon"] == "23.8123456789" and rows[1]["installed"] == "2020-01-02" and rows[1]["active"] == "false"
    assert rows[2] == {"id": "MS777", "name": "padded", "lat": "1e-05", "lon": "12345678", "installed": None, "active": None}
    assert len(rows) == 3  # the blank row is skipped


def test_the_file_is_recognised_by_its_name_or_by_what_it_is():
    assert te.detect_format(b"", "a.XLSX") == "xlsx_records" and te.detect_format(STATIONS, "no-extension") == "xlsx_records"


def test_the_first_sheet_with_data_is_used_and_the_note_says_which_and_how_many_were_ignored():
    book = xlsx({"Notes": [], "Stations": [["id", "name"], ["A", "Alpha"]], "Old": [["x"], ["y"]]})
    columns, rows, note = te.parse_lookup_ex(book, "x.xlsx")
    assert columns == ["id", "name"] and rows == [{"id": "A", "name": "Alpha"}]
    assert "'Stations'" in note and "other 2 sheets were ignored" in note
    assert te.parse_lookup_ex(xlsx({"Only": [["a"], ["1"]]}), "x.xlsx")[2] == "Read from sheet 'Only'."


def test_a_named_sheet_is_used_and_a_wrong_name_lists_the_real_ones():
    book = xlsx({"First": [["a"], ["1"]], "Second": [["b"], ["2"]]})
    assert te.parse_lookup(book, "x.xlsx", sheet="Second")[0] == ["b"]
    with pytest.raises(TransformError, match=r"no sheet 'Third'.*First, Second"):
        te.parse_lookup(book, "x.xlsx", sheet="Third")


@pytest.mark.parametrize("sheets, message", [
    ({"Empty": []}, "no data in any sheet"),
    ({"Head only": [["id", "name"]]}, "no rows"),
    ({"S": [["id", None, "name"], ["1", "orphan", "A"]]}, "Column 2 has values but no name"),
    ({"S": [["id", "id"], ["1", "2"]]}, "own non-empty name"),
])
def test_unusable_spreadsheets_are_explained(sheets, message):
    with pytest.raises(TransformError, match=message):
        te.parse_lookup(xlsx(sheets), "x.xlsx")


def test_an_unnamed_column_with_no_data_is_just_dropped():
    columns, rows = te.parse_lookup(xlsx({"S": [["id", None, "name"], ["1", None, "A"]]}), "x.xlsx")
    assert columns == ["id", "name"] and rows == [{"id": "1", "name": "A"}]


def test_an_empty_named_sheet_says_so():
    with pytest.raises(TransformError, match="sheet is empty"):
        te.parse_lookup(xlsx({"A": [["x"], ["1"]], "B": []}), "x.xlsx", sheet="B")


def test_formulas_are_read_as_their_saved_value_which_a_script_written_file_does_not_have():
    # Excel saves each formula's last result; a file written by a script has none, so the cell is empty rather than the formula text.
    columns, rows = te.parse_lookup(xlsx({"S": [["a", "b"], [1, "=A2*2"]]}), "x.xlsx")
    assert rows == [{"a": "1", "b": None}]


@pytest.mark.parametrize("content, message", [
    (b"\xd0\xcf\x11\xe0" + b"\x00" * 64, "old .xls"),
    (b"PK\x03\x04 this is not really a zip", "not a valid .xlsx"),
    (b"PK\x03\x04", "not a valid .xlsx"),
])
def test_old_and_damaged_files_are_explained_not_crashed_on(content, message):
    with pytest.raises(TransformError, match=message):
        te.parse_lookup(content, "x.xlsx")


def test_a_zip_that_is_not_a_workbook_is_explained():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(TransformError, match="could not be read"):
        te.parse_lookup(buffer.getvalue(), "x.xlsx")


def test_a_zip_bomb_and_a_zip_of_too_many_files_are_refused_before_anything_is_unpacked(monkeypatch):
    monkeypatch.setattr(te, "MAX_XLSX_UNCOMPRESSED", 10)
    with pytest.raises(TransformError, match="too large when unpacked"):
        te.parse_lookup(STATIONS, "x.xlsx")
    monkeypatch.undo()
    monkeypatch.setattr(te, "MAX_XLSX_ENTRIES", 2)
    with pytest.raises(TransformError, match="too large when unpacked"):
        te.parse_lookup(STATIONS, "x.xlsx")


def test_the_row_limit_applies_to_spreadsheets_too(monkeypatch):
    monkeypatch.setattr(te, "MAX_LOOKUP_ROWS", 3)
    with pytest.raises(TransformError, match="At most"):
        te.parse_lookup(xlsx({"S": [["id"]] + [[n] for n in range(10)]}), "x.xlsx")


def test_the_two_value_form_of_parse_lookup_still_works_for_callers_that_do_not_want_the_note():
    columns, rows = te.parse_lookup(STATIONS, "x.xlsx")
    assert columns[0] == "id" and len(rows) == 3


# -- as a source's own format --------------------------------------------------------------------------------------

READINGS = xlsx({"Readings": [["Station", "Time", "Count"], ["MS812", "2026-10-09T08:41:00Z", 1720], ["MS810", "2026-10-09T08:41:00Z", 900]], "Other": [["x"], [1]]})


def test_an_excel_source_is_read_with_the_same_steps_as_any_other():
    steps = [
        {"type": "xlsx_records", "sheet": "Readings", "fields": [{"name": "id", "path": "Station"}, {"name": "time", "path": "Time"}, {"name": "count", "path": "Count"}]},
        {"type": "lookup", "table": "stations", "key": "id", "table_key": "id", "add": [{"column": "name"}]},
        {"type": "convert", "fields": {"count": "int", "time": "datetime"}},
    ]
    stations = te.lookup_table(*te.parse_lookup(xlsx({"S": [["id", "name"], ["MS812", "Kifisias"]]}), "s.xlsx"))
    result = te.run(READINGS, steps, lookups={"stations": stations})
    assert json.loads(result.content)[0] == {"id": "MS812", "time": "2026-10-09T08:41:00Z", "count": 1720, "name": "Kifisias"}
    assert result.records_out == 2 and result.stats["lookup_missing"] == 1


def test_an_excel_reader_without_a_field_list_takes_every_column_and_the_first_sheet():
    result = te.run(READINGS, [{"type": "xlsx_records"}], output="csv")
    assert result.content.decode().split("\n")[0] == "Station,Time,Count"


def test_describing_an_excel_sample_and_listing_its_sheets():
    assert te.xlsx_sheets(READINGS) == ["Readings", "Other"]
    info = te.describe_record(READINGS, "xlsx_records", "")
    assert [(f["path"], f["example"]) for f in info["fields"]] == [("Station", "MS812"), ("Time", "2026-10-09T08:41:00Z"), ("Count", "1720")]
    assert te.describe_record(READINGS, "xlsx_records", "", sheet="Other")["fields"][0]["path"] == "x"


def test_a_wrong_sheet_in_a_reader_step_is_an_error_message():
    with pytest.raises(TransformError, match="no sheet 'Nope'"):
        te.run(READINGS, [{"type": "xlsx_records", "sheet": "Nope"}])


# -- the pages ---------------------------------------------------------------------------------------------------------------

def _table(client, content, name="stations", filename="stations.xlsx", sheet=""):
    return client.post("/transform/tables", data={"name": name, "sheet": sheet}, files={"file": (filename, content, "application/octet-stream")}, follow_redirects=False)


def test_an_excel_index_is_uploaded_and_the_page_says_how_it_was_read(client, _isolated_db):
    resp = _table(client, xlsx({"Notes": [["x"]], "Stations": [["id", "name"], ["A", "Alpha"]], "Old": [["y"], [1]]}))
    assert "uploaded: 1 rows, 2 columns" in _flash(resp) and "sheet 'Stations'" in _flash(resp)
    with _isolated_db() as s:
        assert json.loads(s.query(LookupTable).one().rows) == [{"id": "A", "name": "Alpha"}]


def test_a_chosen_sheet_and_a_wrong_one_and_a_damaged_file(client, _isolated_db):
    book = xlsx({"One": [["a"], ["1"]], "Two": [["b"], ["2"]]})
    assert "sheet 'Two'" in _flash(_table(client, book, sheet="Two"))
    assert "no sheet 'Nope'" in _flash(_table(client, book, name="other", sheet="Nope"))
    assert "not a valid .xlsx" in _flash(_table(client, b"PK\x03\x04 broken", name="broken"))
    with _isolated_db() as s:
        assert [t.name for t in s.query(LookupTable).all()] == ["stations"]  # only the good upload was kept


def test_an_excel_file_can_replace_a_table_and_the_forms_offer_it(client, _isolated_db):
    _table(client, xlsx({"S": [["id", "name"], ["A", "Alpha"]]}))
    with _isolated_db() as s:
        table_id = s.query(LookupTable).one().id
    page = client.get("/transform/tables/%s" % table_id).text
    assert ".xlsx" in page and 'name="sheet"' in page and ".xlsx" in client.get("/transform").text
    resp = client.post("/transform/tables/%s/replace" % table_id, data={"sheet": ""}, files={"file": ("n.xlsx", xlsx({"S": [["id", "name"], ["B", "Beta"], ["C", "Gamma"]]}), "application/octet-stream")}, follow_redirects=False)
    assert "Replaced: 2 rows" in _flash(resp) and "sheet 'S'" in _flash(resp)


def test_an_excel_source_is_built_in_the_pages_like_any_other_and_previewed(client, source, _isolated_db):
    _upload_data(client, source, content=READINGS, name="readings.xlsx")
    _go(client, source, "create")
    page = client.get("/transform/sources/%s/steps/new?type=read" % source["id"]).text
    assert "readings.xlsx" in page and "Excel" in page and "Station" in page and "Sheet" in page and "Readings" in page and "Other" in page
    other = client.get("/transform/sources/%s/steps/new?type=read&format=xlsx_records&sheet=Other" % source["id"]).text
    assert "<code>x</code>" in other
    form = {"kind": "read", "format": "xlsx_records", "sheet": "Readings", "path": ["Station", "Count"], "name::Station": "id", "name::Count": "count"}
    assert "added" in _flash(_go(client, source, "steps/new", data=form))
    page = client.get("/transform/sources/%s" % source["id"]).text
    assert "One record per row of sheet «Readings»" in page
    preview = client.post("/transform/sources/%s/preview" % source["id"]).text
    assert "MS812" in preview and "1720" in preview
