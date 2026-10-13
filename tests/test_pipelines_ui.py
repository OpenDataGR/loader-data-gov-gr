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


def _names(_isolated_db, source):
    with _isolated_db() as s:
        return [t.name for t in transform_runner.list_pipelines(s, source["id"])]


def test_a_pipeline_is_added_as_a_switched_off_copy_of_the_one_you_are_on(client, source, _isolated_db):
    _setup(client, source)
    _go(client, source, "output", data={"format": "csv", "delimiter": ";"})
    resp = _go(client, source, "pipelines", data={"name": "Lookup", "from": "default"})
    assert resp.headers["location"].startswith("/transform/sources/%s/pipelines/lookup?" % source["id"]) and "copy of default" in _flash(resp)
    assert _names(_isolated_db, source) == ["default", "lookup"]
    with _isolated_db() as s:
        copy, original = transform_runner.get_pipeline(s, source["id"], "lookup"), transform_runner.get_transform(s, source["id"])
        assert copy.steps == original.steps and copy.output_format == "csv" and copy.csv_delimiter == ";" and not copy.enabled and copy.id != original.id


def test_a_pipelines_own_page_works_on_that_pipeline_only(client, source, _isolated_db):
    _setup(client, source)
    _go(client, source, "pipelines", data={"name": "lookup", "from": "default"})
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
    _go(client, source, "pipelines", data={"name": "lookup", "from": "default"})
    base = "/transform/sources/%s/pipelines/lookup" % source["id"]
    for sub in ("steps/input", "steps/output", "steps/1/edit"):
        page = client.get("%s/%s" % (base, sub)).text
        assert base in page and 'href="/transform/sources/%s/steps' % source["id"] not in page
    edited = client.post(base + "/steps/1/edit", data=READER_FORM, follow_redirects=False)
    assert edited.headers["location"].startswith(base + "?")


def test_the_switcher_lists_the_pipelines_and_the_default_page_is_unchanged(client, source):
    _setup(client, source)
    _go(client, source, "pipelines", data={"name": "lookup", "from": "default"})
    page = client.get("/transform/sources/%s" % source["id"]).text
    assert ">default</a>" in page and ">lookup</a>" in page and "Remove this pipeline" not in page and "+ Add a pipeline" in page
    assert 'href="/transform/sources/%s/pipelines/lookup"' % source["id"] in page


@pytest.mark.parametrize("name,problem", [("", "Name the pipeline"), ("1abc", "Name the pipeline"), ("a b", "Name the pipeline"), ("a_b", "Name the pipeline"),
                                           ("x" * 31, "Name the pipeline"), ("default", "already a pipeline called")])
def test_bad_or_taken_names_are_refused(client, source, _isolated_db, name, problem):
    _setup(client, source)
    assert problem in _flash(_go(client, source, "pipelines", data={"name": name, "from": "default"}))
    assert _names(_isolated_db, source) == ["default"]


def test_a_pipeline_needs_a_first_transform_to_copy(client, source, _isolated_db):
    assert "Create the first transform" in _flash(_go(client, source, "pipelines", data={"name": "lookup"}))
    assert _names(_isolated_db, source) == []


def test_a_pipeline_is_removed_with_its_outputs_but_the_default_stays(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    _go(client, source, "pipelines", data={"name": "lookup", "from": "default"})
    _pipeline(client, source, "lookup", "enabled", data={"enabled": "1"})
    _pipeline(client, source, "lookup", "reprocess", data={"limit": "5"})
    with _isolated_harvest_db() as h:
        before = len(transform_runner.recent_outputs(h, source["id"]))
    assert "removed" in _flash(_pipeline(client, source, "lookup", "delete"))
    assert _names(_isolated_db, source) == ["default"]
    with _isolated_harvest_db() as h:
        assert len(transform_runner.recent_outputs(h, source["id"])) <= before
    assert "cannot be removed" in _flash(_pipeline(client, source, "default", "delete"))
    assert "No such pipeline" in _flash(_pipeline(client, source, "nope", "delete"))
    assert _names(_isolated_db, source) == ["default"]


def test_outputs_shown_and_deleted_are_the_pipelines_own(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    _go(client, source, "pipelines", data={"name": "lookup", "from": "default"})
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
