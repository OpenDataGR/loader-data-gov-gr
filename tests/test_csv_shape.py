"""A CSV that is not a plain table: lines before the table, no header row (the columns are named from a list), and marks for "no value" -- the shape of the
two-line files the weather stations of EMY send. Engine and Transform Box page."""

import pytest

from providerharvest_service import transform_engine as te
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _go, _isolated_db, _isolated_harvest_db, _master_key, _upload_data, client, source,
)

STATION_FILE = b"M0194,060,202604010150\nM0194,60,202604010150,X,LGTT,14.0,12.8,12.9,100.0,////,//////\n"
NAMES = ["CONFID", "AWSID", "yyyyMMddHHmm", "XCHAR", "NAME", "aBV", "aTA1", "m5TA1N", "aRH1", "m5PR2S", "m5PR2IX"]


def _step(**extra):
    return {"type": "csv_records", "skip_lines": 1, "column_names": NAMES, "empty_regex": "/+", **extra}


def test_the_lines_before_the_table_are_skipped_the_columns_named_and_slashes_made_empty():
    (record,) = te.read_csv(STATION_FILE, _step())
    assert record["AWSID"] == "60" and record["yyyyMMddHHmm"] == "202604010150" and record["aTA1"] == "12.8" and record["aRH1"] == "100.0"
    assert record["m5PR2S"] is None and record["m5PR2IX"] is None  # (the //// of a sensor the station does not have)
    assert len(record) == len(NAMES)


def test_names_can_be_given_as_text_and_a_chosen_few_are_kept():
    step = _step(column_names="CONFID, AWSID\nyyyyMMddHHmm XCHAR NAME aBV aTA1 m5TA1N aRH1 m5PR2S m5PR2IX",
                 fields=[{"name": "station", "path": "AWSID"}, {"name": "temp", "path": "aTA1"}])
    assert te.read_csv(STATION_FILE, step) == [{"station": "60", "temp": "12.8"}]


def test_without_these_options_nothing_changes():
    assert te.read_csv(b"a;b\n1;////\n", {"type": "csv_records", "delimiter": ";"}) == [{"a": "1", "b": "////"}]


@pytest.mark.parametrize("extra,message", [
    ({"skip_lines": "x"}, "whole number"), ({"skip_lines": 500}, "Skip between"),
    ({"column_names": ["a", "a"]}, "used twice"), ({"column_names": ["1bad"]}, "column name"),
    ({"empty_regex": "("}, "not valid"), ({"empty_regex": "x" * 101}, "longer than 100"),
])
def test_a_wrong_option_is_explained(extra, message):
    with pytest.raises(te.TransformError, match=message):
        te.csv_options(extra)
    assert any(message in problem for problem in te.validate([dict(_step(), **extra)]))


def test_a_date_and_time_without_separators_is_read_as_utc():
    assert te._convert("202604010150", "datetime") == "2026-04-01T01:50:00Z"
    assert te._convert("20260401", "datetime") == "2026-04-01T00:00:00Z" and te._convert("20260401015030", "datetime") == "2026-04-01T01:50:30Z"
    assert te._convert("2026-04-01T01:50:00Z", "datetime") == "2026-04-01T01:50:00Z"  # (the usual forms still work)


def test_describing_a_headerless_file_lists_the_given_names_with_examples():
    info = te.describe_record(STATION_FILE, "csv_records", "", csv_shape={"skip_lines": "1", "column_names": "\n".join(NAMES)})
    assert [f["path"] for f in info["fields"]] == NAMES and {f["path"]: f["example"] for f in info["fields"]}["aTA1"] == "12.8"


def test_the_reader_page_offers_the_options_and_saves_them(client, source, _isolated_db):
    _upload_data(client, source, content=STATION_FILE, name="M0194_060_202604010150.csv")
    _go(client, source, "create")
    base = "/transform/sources/%s/steps/new?type=read&format=csv_records" % source["id"]
    plain = client.get(base).text
    assert 'name="skip_lines"' in plain and 'name="column_names"' in plain and 'name="empty_regex"' in plain and 'id="header-file"' in plain
    page = client.get(base + "&skip_lines=1&empty_regex=%2F%2B&column_names=" + "%0A".join(NAMES)).text
    assert "<code>aTA1</code>" in page and "12.8" in page and 'name="path" value="AWSID"' in page
    form = {"kind": "read", "format": "csv_records", "delimiter": ",", "skip_lines": "1", "empty_regex": "/+", "column_names": "\n".join(NAMES),
            "path": ["AWSID", "yyyyMMddHHmm", "aTA1", "m5PR2S"], "name::AWSID": "station", "name::yyyyMMddHHmm": "time", "name::aTA1": "temp", "name::m5PR2S": "rain"}
    assert "added" in _flash(_go(client, source, "steps/new", data=form))
    from tests.test_transform_ui import _steps

    step = _steps(_isolated_db, source["id"])[0]
    assert step["skip_lines"] == 1 and step["column_names"] == NAMES and step["empty_regex"] == "/+"
    preview = client.post("/transform/sources/%s/preview" % source["id"]).text
    assert ">60<" in preview and ">12.8<" in preview
    bad = dict(form, column_names="a\na")
    assert "used twice" in _go(client, source, "steps/new", data=bad).text  # (the page is shown again with the reason)
