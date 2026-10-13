"""Input and Output are steps too: fixed cards at the two ends of the list (IN, then the processing steps, then OUT), each with its own page,
neither removable nor movable."""

import re
import types

import pytest

from providerharvest_service import transform_forms as tf
from tests.test_live_transform import _build, _registry  # noqa: F401
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    READER_FORM, _flash, _go, _isolated_db, _isolated_harvest_db, _master_key, _upload_data, client, source,
)


def _card(page, label):
    """The step card whose number badge is IN / OUT: from its badge to the start of whatever follows it."""
    marker = '>%s</div>' % label
    start = page.rindex('<div class="step-card">', 0, page.index(marker))
    ends = [i for i in (page.find('<div class="step-card">', start + 10), page.find('<div style="border-top', start)) if i > start]
    return page[start:min(ends)]


def _main(client, source_id):
    return client.get("/transform/sources/%s" % source_id).text


def test_the_list_runs_from_input_through_the_steps_to_output(client, source):
    _upload_data(client, source)
    _go(client, source, "create")
    _go(client, source, "steps/new", data=READER_FORM)
    page = _main(client, source["id"])
    assert page.index(">IN<") < page.index("Read XML") < page.index(">OUT<")
    assert 'href="/transform/sources/%s/steps/input"' % source["id"] in _card(page, "IN")
    assert 'href="/transform/sources/%s/steps/output"' % source["id"] in _card(page, "OUT")


def test_the_two_ends_can_be_edited_but_not_removed_or_moved(client, source):
    _go(client, source, "create")
    page = _main(client, source["id"])
    for label in ("IN", "OUT"):
        card = _card(page, label)
        assert "Edit" in card and "Remove" not in card and "Move" not in card and "/remove" not in card and "/move" not in card


def test_the_add_a_step_list_does_not_offer_input_or_output(client, source):
    _upload_data(client, source)
    _go(client, source, "create")
    _go(client, source, "steps/new", data=READER_FORM)
    select = re.search(r'<select id="addtype".*?</select>', _main(client, source["id"]), re.S).group(0)
    assert 'value="output"' not in select and 'value="input"' not in select and 'value="lookup"' in select


def test_a_source_that_stores_data_describes_its_input_as_the_files_it_sends(client, source):
    _go(client, source, "create")
    assert "Each file this source sends. None has arrived yet" in _card(_main(client, source["id"]), "IN")
    _upload_data(client, source)
    summary = _card(_main(client, source["id"]), "IN")
    assert "Each file this source sends. Newest: Detector.xml" in summary and "kB, received" in summary


def test_the_input_page_of_a_source_that_stores_data_shows_the_newest_file_and_has_no_sample_controls(client, source):
    _go(client, source, "create")
    empty = client.get("/transform/sources/%s/steps/input" % source["id"]).text
    assert "No file has arrived yet" in empty and "/provider/sources/%s/readiness" % source["id"] in empty and "newest stored file" in empty
    _upload_data(client, source)
    page = client.get("/transform/sources/%s/steps/input" % source["id"]).text
    assert "Newest file: <strong>Detector.xml</strong>" in page and "/provider/sources/%s/runs" % source["id"] in page
    assert 'name="sample"' not in page and "/fetch-sample" not in page  # (the Changelog on the page may name the feature in words)


def test_the_input_page_of_an_api_source_holds_the_sample_and_the_fetch(client, source, _isolated_db):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id, enable=False)
    main = _main(client, registry_id)
    assert "An answer of the provider, asked on demand" in _card(main, "IN") and "kB kept" in _card(main, "IN")
    assert 'name="sample"' not in main  # no sample box on the main page any more
    page = client.get("/transform/sources/%s/steps/input" % registry_id).text
    assert 'name="sample"' in page and 'action="/transform/sources/%s/sample"' % registry_id in page and "<h1>Input</h1>" in page
    assert "Give this source a" in page  # no test request on this one: it says where to give one


@pytest.mark.parametrize("data,expected", [
    ({"format": "json"}, "Write JSON (a list of records)."),
    ({"format": "json", "single_present": "1", "single": "1"}, "one object when there is exactly one record"),
    ({"format": "jsonl"}, "Write JSON Lines"),
    ({"format": "xml", "root": "stations", "record": "station"}, "&lt;stations&gt; holding one &lt;station&gt; per record"),
    ({"format": "csv", "delimiter": ";", "bom": "1"}, "separated by semicolon"),
])
def test_the_output_card_says_in_words_what_is_written(client, source, data, expected):
    _go(client, source, "create")
    _go(client, source, "output", data=data)
    assert expected in _card(_main(client, source["id"]), "OUT")


def test_the_output_card_mentions_the_excel_mark_only_when_it_is_on(client, source):
    _go(client, source, "create")
    _go(client, source, "output", data={"format": "csv", "delimiter": ",", "bom": "1"})
    assert "marked as UTF-8 for Excel" in _card(_main(client, source["id"]), "OUT")
    _go(client, source, "output", data={"format": "csv", "delimiter": ","})  # the box left unticked
    assert "marked as UTF-8" not in _card(_main(client, source["id"]), "OUT")


def test_the_output_page_is_the_old_output_form_with_its_hidden_format_blocks_working(client, source):
    _go(client, source, "create")
    page = client.get("/transform/sources/%s/steps/output" % source["id"]).text
    assert "<h1>Output</h1>" in page and 'action="/transform/sources/%s/output"' % source["id"] in page
    assert "[hidden] { display: none !important; }" in page and "Back to the transform" in page
    assert "Output</h2>" not in _main(_client := client, source["id"]) and 'id="output-form"' not in _main(client, source["id"])


def test_both_pages_need_an_existing_transform(client, source):
    for step in ("input", "output"):
        resp = client.get("/transform/sources/%s/steps/%s" % (source["id"], step), follow_redirects=False)  # a source with no transform yet
        assert resp.status_code == 303 and "No+such+transform" in resp.headers["location"]
        resp = client.get("/transform/sources/nope/steps/%s" % step, follow_redirects=False)
        assert resp.status_code == 303


def test_the_summaries_in_words(client):
    t = types.SimpleNamespace(output_format="json", output_single=False, root_name="records", record_name="record", csv_delimiter=None, csv_bom=None)
    assert tf.output_summary(t) == "Write JSON (a list of records)."
    t.output_format, t.csv_bom = "csv", None
    assert "separated by comma" in tf.output_summary(t) and "marked as UTF-8 for Excel" in tf.output_summary(t)  # the defaults: comma, with the mark
    t.output_format = "bogus"
    assert tf.output_summary(t).startswith("Write ")
