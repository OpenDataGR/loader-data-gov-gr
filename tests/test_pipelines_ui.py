"""Several pipelines on one source: add (a copy), switch between them, each with its own steps / output / on-off, remove."""

import pytest

from providerharvest_service import transform_runner
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    READER_FORM, _flash, _go, _isolated_db, _isolated_harvest_db, _master_key, _upload_data, client, source,
)


def _pipeline(client, source, name, path="", **kw):
    return client.post("/transform/sources/%s/pipelines/%s/%s" % (source["id"], name, path), follow_redirects=False, **kw)


def _setup(client, source):
    _upload_data(client, source)
    _go(client, source, "create")
    _go(client, source, "steps/new", data=READER_FORM)


def _add(client, source, name, start="default"):
    return client.post("/transform/pipelines", data={"source_id": source["id"], "name": name, "from": start}, follow_redirects=False)


def _names(_isolated_db, source):
    with _isolated_db() as s:
        return [t.name for t in transform_runner.list_pipelines(s, source["id"])]


def test_a_pipeline_is_added_as_a_switched_off_copy_of_the_one_you_are_on(client, source, _isolated_db):
    _setup(client, source)
    _go(client, source, "output", data={"format": "csv", "delimiter": ";"})
    resp = _add(client, source, "Lookup")
    assert resp.headers["location"].startswith("/transform/sources/%s/pipelines/lookup?" % source["id"]) and "copy of default" in _flash(resp)
    assert _names(_isolated_db, source) == ["default", "lookup"]
    with _isolated_db() as s:
        copy, original = transform_runner.get_pipeline(s, source["id"], "lookup"), transform_runner.get_transform(s, source["id"])
        assert copy.steps == original.steps and copy.output_format == "csv" and copy.csv_delimiter == ";" and not copy.enabled and copy.id != original.id


def test_a_pipelines_own_page_works_on_that_pipeline_only(client, source, _isolated_db):
    _setup(client, source)
    _add(client, source, "lookup")
    _pipeline(client, source, "lookup", "output", data={"format": "xml", "root": "stations", "record": "station"})
    _pipeline(client, source, "lookup", "enabled", data={"enabled": "1"})
    with _isolated_db() as s:
        default, other = transform_runner.get_transform(s, source["id"]), transform_runner.get_pipeline(s, source["id"], "lookup")
        assert default.output_format == "json" and not default.enabled
        assert other.output_format == "xml" and other.root_name == "stations" and other.enabled
    page = client.get("/transform/sources/%s/pipelines/lookup" % source["id"]).text
    assert "/ lookup" in page and 'action="/transform/sources/%s/pipelines/lookup/steps/new"' % source["id"] in page or "/pipelines/lookup/steps/" in page
    assert "Remove this pipeline" in page and "Write XML" in page


def test_every_page_of_a_pipeline_links_back_to_that_pipeline(client, source):
    _setup(client, source)
    _add(client, source, "lookup")
    base = "/transform/sources/%s/pipelines/lookup" % source["id"]
    for sub in ("steps/input", "steps/output", "steps/1/edit"):
        page = client.get("%s/%s" % (base, sub)).text
        assert base in page and 'href="/transform/sources/%s/steps' % source["id"] not in page
    edited = client.post(base + "/steps/1/edit", data=READER_FORM, follow_redirects=False)
    assert edited.headers["location"].startswith(base + "?")


def test_the_switcher_lists_the_pipelines_and_the_default_page_is_unchanged(client, source):
    _setup(client, source)
    _add(client, source, "lookup")
    page = client.get("/transform/sources/%s" % source["id"]).text
    assert ">default</a>" in page and ">lookup</a>" in page and "Remove this pipeline" in page and "+ Add a pipeline" not in page
    assert 'href="/transform/sources/%s/pipelines/lookup"' % source["id"] in page


@pytest.mark.parametrize("name,problem", [("", "Name the pipeline"), ("1abc", "Name the pipeline"), ("a b", "Name the pipeline"), ("a_b", "Name the pipeline"),
                                           ("x" * 31, "Name the pipeline"), ("default", "already a pipeline called")])
def test_bad_or_taken_names_are_refused(client, source, _isolated_db, name, problem):
    _setup(client, source)
    assert problem in _flash(_add(client, source, name))
    assert _names(_isolated_db, source) == ["default"]


def test_a_pipeline_needs_a_first_transform_to_copy(client, source, _isolated_db):
    assert "Set up the first transform" in _flash(_add(client, source, "lookup"))
    assert _names(_isolated_db, source) == []


def test_a_pipeline_is_removed_with_its_outputs(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    _add(client, source, "lookup")
    _pipeline(client, source, "lookup", "enabled", data={"enabled": "1"})
    _pipeline(client, source, "lookup", "reprocess", data={"limit": "5"})
    with _isolated_harvest_db() as h:
        before = len(transform_runner.recent_outputs(h, source["id"]))
    assert "removed" in _flash(_pipeline(client, source, "lookup", "delete"))
    assert _names(_isolated_db, source) == ["default"]
    with _isolated_harvest_db() as h:
        assert len(transform_runner.recent_outputs(h, source["id"])) <= before
    assert "No such pipeline" in _flash(_pipeline(client, source, "nope", "delete"))
    assert _names(_isolated_db, source) == ["default"]


def test_outputs_shown_and_deleted_are_the_pipelines_own(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    _add(client, source, "lookup")
    _pipeline(client, source, "lookup", "enabled", data={"enabled": "1"})
    _go(client, source, "enabled", data={"enabled": "1"})
    _upload_data(client, source)  # one file: both pipelines store an output
    with _isolated_harvest_db() as h:
        assert len(transform_runner.recent_outputs(h, source["id"])) == 2
    _pipeline(client, source, "lookup", "outputs/delete-all")
    with _isolated_harvest_db() as h:
        left = transform_runner.recent_outputs(h, source["id"])
    assert len(left) == 1  # the default's output is still there


def test_an_unknown_pipeline_is_not_a_page(client, source, _isolated_db):
    _setup(client, source)
    resp = client.get("/transform/sources/%s/pipelines/nope" % source["id"], follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/transform/sources/%s?" % source["id"]) and "No such pipeline" in _flash(resp)
    assert _names(_isolated_db, source) == ["default"]  # nothing was created


def test_the_transform_box_lists_each_pipeline_on_its_own_row_with_its_source_as_the_input(client, source):
    assert "Set up</a>" in client.get("/transform").text  # a source with no transform: one row to set it up
    _setup(client, source)
    _add(client, source, "lookup")
    page = client.get("/transform").text
    assert page.count('<div class="name-title">default</div>') == 1 and page.count('<div class="name-title">lookup</div>') == 1
    assert 'href="/transform/sources/%s"' % source["id"] in page and 'href="/transform/sources/%s/pipelines/lookup"' % source["id"] in page
    assert page.count("from ") >= 2 and "Set up</a>" not in page


def test_a_pipeline_is_added_from_the_transform_box_empty_or_as_a_copy(client, source, _isolated_db):
    assert "+ Add a pipeline" not in client.get("/transform").text  # nothing to read from until a source has its first transform
    _setup(client, source)
    page = client.get("/transform").text
    assert 'action="/transform/pipelines"' in page and 'name="source_id"' in page and "a copy of default" in page and ">empty<" in page
    assert "+ Add a pipeline" not in client.get("/transform/sources/%s" % source["id"]).text  # not from inside a pipeline
    resp = _add(client, source, "blank", start="")
    assert resp.headers["location"].startswith("/transform/sources/%s/pipelines/blank?" % source["id"]) and "copy" not in _flash(resp)
    with _isolated_db() as s:
        blank = transform_runner.get_pipeline(s, source["id"], "blank")
        assert transform_runner.steps_of(blank) == [] and not blank.enabled


def test_copying_a_pipeline_that_does_not_exist_or_from_no_source_is_refused(client, source, _isolated_db):
    _setup(client, source)
    assert "no pipeline called" in _flash(_add(client, source, "x1", start="ghost"))
    assert "Choose the source" in _flash(client.post("/transform/pipelines", data={"source_id": "nope", "name": "x1"}, follow_redirects=False))
    assert _names(_isolated_db, source) == ["default"]


def test_the_transform_box_row_of_every_pipeline_has_a_remove_button(client, source, _isolated_db):
    _setup(client, source)
    _add(client, source, "lookup")
    page = client.get("/transform").text
    assert page.count(">Remove</button>") == 2 and 'action="/transform/sources/%s/pipelines/lookup/delete"' % source["id"] in page and 'pipelines/default/delete"' in page
    resp = client.post("/transform/sources/%s/pipelines/lookup/delete" % source["id"], follow_redirects=False)
    assert resp.headers["location"].startswith("/transform/sources/%s?" % source["id"]) and _names(_isolated_db, source) == ["default"]


def test_removing_the_default_pipeline_stops_its_output_and_nothing_takes_its_place(client, source, _isolated_db, _isolated_harvest_db):
    from fastapi.testclient import TestClient

    from providerharvest_service.app import app

    _setup(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _add(client, source, "lookup")
    _pipeline(client, source, "lookup", "enabled", data={"enabled": "1"})
    _upload_data(client, source)
    client.post("/load/sources/%s/api" % source["id"], data={"enabled": "1"}, follow_redirects=False)
    caller = TestClient(app, base_url="https://testserver")
    assert caller.get("/data/acme-push").status_code == 200
    flash = _flash(_pipeline(client, source, "default", "delete"))
    assert "removed" in flash and "switched off" in flash
    assert _names(_isolated_db, source) == ["lookup"]
    assert caller.get("/data/acme-push").status_code == 404  # the published address stopped; the pipeline that is left did not take over
    with _isolated_harvest_db() as h:
        assert len(transform_runner.recent_outputs(h, source["id"])) == 1  # only lookup's own output remains
    with _isolated_db() as s:
        assert transform_runner.get_transform(s, source["id"]) is None  # "the transform" is the default one; no fallback
    resp = client.get("/transform/sources/%s" % source["id"], follow_redirects=False)  # the source's page just opens the pipeline that is left
    assert resp.status_code == 303 and resp.headers["location"].endswith("/pipelines/lookup")
    _pipeline(client, source, "lookup", "delete")
    assert _names(_isolated_db, source) == []
    assert "no transform yet" in client.get("/transform/sources/%s" % source["id"]).text  # back to the start: set one up again


def test_the_published_address_is_answered_by_the_default_pipeline_not_by_the_one_that_wrote_last(client, source, _isolated_db):
    from fastapi.testclient import TestClient

    from providerharvest_service.app import app

    _setup(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _add(client, source, "table")
    _pipeline(client, source, "table", "output", data={"format": "csv", "delimiter": ","})
    _pipeline(client, source, "table", "enabled", data={"enabled": "1"})
    _upload_data(client, source)
    client.post("/load/sources/%s/api" % source["id"], data={"enabled": "1"}, follow_redirects=False)
    resp = TestClient(app, base_url="https://testserver").get("/data/acme-push")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/json")
