"""Several outputs at once: select rows to download as one zip or delete, and delete every failed output in one click."""

import datetime
import io
import zipfile

from sqlalchemy import select

from providerharvest_service.harvest_models import TransformedFile
from tests.test_pipelines_ui import _add, _pipeline, _setup
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _go, _isolated_db, _isolated_harvest_db, _master_key, _upload_data, client, source,
)


def _default_pipeline_id(_isolated_db, source):
    from providerharvest_service import transform_runner

    with _isolated_db() as s:
        return transform_runner.get_transform(s, source["id"]).id


def _output(_isolated_harvest_db, source_id, transform_id, run, *, status="ok", content=b"data", filename="a.json", minute=0):
    with _isolated_harvest_db() as h:
        row = TransformedFile(run_id="run-%s" % run, source_id=source_id, transform_id=transform_id, status=status, filename=filename if status == "ok" else "",
                              content=content if status == "ok" else b"", size=len(content) if status == "ok" else 0,
                              error_message="The file is not valid XML" if status == "error" else None,
                              created_at=datetime.datetime(2026, 10, 12, 5, minute, tzinfo=datetime.timezone.utc))
        h.add(row)
        h.commit()
        return row.id


def _ids(_isolated_harvest_db, source_id):
    with _isolated_harvest_db() as h:
        return {r.id for r in h.scalars(select(TransformedFile).where(TransformedFile.source_id == source_id)).all()}


def _bulk(client, source, ids, action, pipeline=None):
    path = "/transform/sources/%s%s/outputs/bulk" % (source["id"], "" if pipeline in (None, "default") else "/pipelines/%s" % pipeline)
    return client.post(path, data={"output_id": ids, "action": action}, follow_redirects=False)


def test_selected_outputs_are_deleted_and_the_others_stay(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    t = _default_pipeline_id(_isolated_db, source)
    a, b, c = (_output(_isolated_harvest_db, source["id"], t, n, minute=n) for n in (1, 2, 3))
    resp = _bulk(client, source, [a, c], "delete")
    assert "2 outputs deleted" in _flash(resp)
    location = resp.headers["location"]
    assert location.endswith("#outputs") and location.index("?flash=") < location.index("#outputs")  # the message before the anchor, or it is lost
    assert _ids(_isolated_harvest_db, source["id"]) == {b}


def test_a_bulk_delete_only_touches_this_source_and_this_pipeline(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    _add(client, source, "other")
    with _isolated_db() as s:
        from providerharvest_service import transform_runner
        other_id = transform_runner.get_pipeline(s, source["id"], "other").id
    t = _default_pipeline_id(_isolated_db, source)
    mine = _output(_isolated_harvest_db, source["id"], t, 1)
    theirs = _output(_isolated_harvest_db, source["id"], other_id, 2)
    foreign = _output(_isolated_harvest_db, "another-source", t, 3)
    assert "1 output deleted" in _flash(_bulk(client, source, [mine, theirs, foreign], "delete"))  # only the default pipeline's own
    assert _ids(_isolated_harvest_db, source["id"]) == {theirs} and _ids(_isolated_harvest_db, "another-source") == {foreign}
    assert "None of the selected outputs exist" in _flash(_bulk(client, source, [foreign], "delete"))


def test_selected_outputs_download_as_one_zip_without_the_ones_that_have_nothing(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    t = _default_pipeline_id(_isolated_db, source)
    first = _output(_isolated_harvest_db, source["id"], t, 1, content=b"one", filename="Detector.json", minute=1)
    second = _output(_isolated_harvest_db, source["id"], t, 2, content=b"two", filename="Detector.json", minute=2)  # the same file name, made later
    failed = _output(_isolated_harvest_db, source["id"], t, 3, status="error", minute=3)
    resp = _bulk(client, source, [second, failed, first], "download")
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == 'attachment; filename="acme-push-default-outputs.zip"'
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert archive.namelist() == ["20261012-050100_Detector.json", "20261012-050200_Detector.json"]  # oldest first, told apart by the time
        assert [archive.read(n) for n in archive.namelist()] == [b"one", b"two"]
    assert "Nothing to download" in _flash(_bulk(client, source, [failed], "download"))


def test_nothing_selected_or_an_unknown_action_is_explained(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    t = _default_pipeline_id(_isolated_db, source)
    a = _output(_isolated_harvest_db, source["id"], t, 1)
    assert "Select at least one" in _flash(_bulk(client, source, [], "delete"))
    assert "Choose what to do" in _flash(_bulk(client, source, [a], "explode"))
    assert "Select at most" in _flash(_bulk(client, source, ["x%d" % n for n in range(501)], "delete"))
    assert _ids(_isolated_harvest_db, source["id"]) == {a}


def test_all_failed_outputs_of_the_pipeline_are_deleted_in_one_click_not_only_the_visible_ones(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    t = _default_pipeline_id(_isolated_db, source)
    good = _output(_isolated_harvest_db, source["id"], t, 0)
    for n in range(1, 16):  # more than the ten the page shows
        _output(_isolated_harvest_db, source["id"], t, n, status="error", minute=n)
    other_pipeline = _output(_isolated_harvest_db, source["id"], "another-pipeline-id", 99, status="error")
    resp = client.post("/transform/sources/%s/outputs/delete-failed" % source["id"], follow_redirects=False)
    assert "15 failed outputs deleted" in _flash(resp)
    assert _ids(_isolated_harvest_db, source["id"]) == {good, other_pipeline}  # good ones and other pipelines' are untouched
    assert "There were no failed outputs" in _flash(client.post("/transform/sources/%s/outputs/delete-failed" % source["id"], follow_redirects=False))


def test_the_pipeline_pages_work_the_same_way_under_their_own_address(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    _add(client, source, "other")
    with _isolated_db() as s:
        from providerharvest_service import transform_runner
        other_id = transform_runner.get_pipeline(s, source["id"], "other").id
    keep = _output(_isolated_harvest_db, source["id"], _default_pipeline_id(_isolated_db, source), 1)
    gone = _output(_isolated_harvest_db, source["id"], other_id, 2, status="error")
    assert "1 failed output deleted" in _flash(client.post("/transform/sources/%s/pipelines/other/outputs/delete-failed" % source["id"], follow_redirects=False))
    assert _ids(_isolated_harvest_db, source["id"]) == {keep} and gone not in _ids(_isolated_harvest_db, source["id"])


def test_the_table_has_checkboxes_a_toolbar_and_a_delete_all_failed_button(client, source, _isolated_db, _isolated_harvest_db):
    _setup(client, source)
    t = _default_pipeline_id(_isolated_db, source)
    ok = _output(_isolated_harvest_db, source["id"], t, 1)
    page = client.get("/transform/sources/%s" % source["id"]).text
    assert 'id="bulk-form" method="post" action="/transform/sources/%s/outputs/bulk"' % source["id"] in page
    assert 'class="out-check" name="output_id" value="%s" form="bulk-form"' % ok in page and 'id="out-all"' in page
    assert "Download selected" in page and "Delete selected" in page and "failed</button>" not in page  # no failures yet: no such button
    _output(_isolated_harvest_db, source["id"], t, 2, status="error")
    page = client.get("/transform/sources/%s" % source["id"]).text
    assert 'formaction="/transform/sources/%s/outputs/delete-failed"' % source["id"] in page and "Delete all 1 failed" in page
    assert "Show the failed ones" in page
