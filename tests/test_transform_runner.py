"""A source's transform running when a file arrives, being re-run over older files, and previewed -- and always
leaving the original file exactly as received."""

import datetime
import json

import pytest
from sqlalchemy import select

from providerharvest_service import retention, transform_runner
from providerharvest_service import transform_engine as te
from providerharvest_service.harvest_models import HarvestedFile, HarvestRun, TransformedFile
from providerharvest_service.models import LookupTable, Transform
from tests.test_push_ingest import (  # noqa: F401  (fixtures and helpers shared with the push ingest tests)
    _isolated_db, _isolated_harvest_db, _master_key, _register_and_activate_push_source, client,
)
from tests.test_transform_engine import SOAP, STATIONS, XML_STEP

LOOKUP_STEP = {"type": "lookup", "table": "stations", "key": "id", "table_key": "id", "add": [{"column": "name"}, {"column": "lat"}]}


@pytest.fixture
def source(client, _isolated_db):
    source_id, api_key = _register_and_activate_push_source(client, _isolated_db)
    return {"id": source_id, "key": api_key, "name": "acme-push"}


def _add_table(session_factory, name="stations", columns=("id", "name", "lat", "lon"), rows=None):
    rows = rows or [{"id": "MS812", "name": "Kifisias", "lat": "38.03", "lon": "23.80"}, {"id": "MS810", "name": "Mesogeion", "lat": "38.00", "lon": "23.80"}]
    with session_factory() as s:
        table = LookupTable(name=name, columns=json.dumps(list(columns)), rows=json.dumps(rows), row_count=len(rows))
        s.add(table)
        s.commit()
        return table.id


def _transform(session_factory, source_id, steps=None, enabled=True, output="json"):
    with session_factory() as s:
        t = Transform(source_id=source_id, steps=json.dumps(steps if steps is not None else [XML_STEP, LOOKUP_STEP]), output_format=output, enabled=enabled)
        s.add(t)
        s.commit()
        return t.id


def _upload(client, source, content=SOAP.encode("utf-8"), name="Detector.xml"):
    return client.post("/push/%s/upload" % source["name"], files={"file": (name, content, "application/xml")}, headers={"X-Api-Key": source["key"]})


def _outputs(hfactory):
    with hfactory() as h:
        return list(h.scalars(select(TransformedFile).order_by(TransformedFile.created_at)).all())


# -- on arrival ----------------------------------------------------------------------------------------

def test_a_pushed_file_is_transformed_and_the_original_is_stored_untouched(client, source, _isolated_db, _isolated_harvest_db):
    _add_table(_isolated_db)
    _transform(_isolated_db, source["id"])
    resp = _upload(client, source)
    assert resp.status_code == 200 and resp.json()["status"] == "success"
    (out,) = _outputs(_isolated_harvest_db)
    rows = json.loads(out.content)
    assert out.status == "ok" and out.filename == "Detector.json" and out.content_type == "application/json"
    assert (out.records_in, out.records_out) == (3, 3) and rows[0]["name"] == "Kifisias" and rows[1]["name"] == "Mesogeion"
    with _isolated_harvest_db() as h:  # the original is exactly what was sent
        original = h.scalar(select(HarvestedFile.content).where(HarvestedFile.run_id == out.run_id))
    assert original == SOAP.encode("utf-8")


def test_the_output_format_and_name_follow_the_transform(client, source, _isolated_db, _isolated_harvest_db):
    _add_table(_isolated_db)
    _transform(_isolated_db, source["id"], output="csv")
    _upload(client, source, name="export.2026.xml")
    (out,) = _outputs(_isolated_harvest_db)
    assert out.filename == "export.2026.csv" and out.content.decode("utf-8-sig").startswith("id,time,state,count,occ,speed,vehicle,name,lat")


def test_nothing_runs_for_a_source_without_a_transform_or_with_a_disabled_one(client, source, _isolated_db, _isolated_harvest_db):
    assert _upload(client, source).status_code == 200 and _outputs(_isolated_harvest_db) == []
    _transform(_isolated_db, source["id"], enabled=False)
    assert _upload(client, source).status_code == 200 and _outputs(_isolated_harvest_db) == []


def test_a_file_the_transform_cannot_read_still_arrives_and_leaves_an_error_to_read(client, source, _isolated_db, _isolated_harvest_db):
    _add_table(_isolated_db)
    _transform(_isolated_db, source["id"])
    resp = _upload(client, source, content=b"this is not xml at all")
    assert resp.status_code == 200 and resp.json()["status"] == "success"  # it arrived
    (out,) = _outputs(_isolated_harvest_db)
    assert out.status == "error" and "not valid XML" in out.error_message and out.size == 0 and out.content == b""
    with _isolated_harvest_db() as h:
        assert h.scalar(select(HarvestedFile.content).where(HarvestedFile.run_id == out.run_id)) == b"this is not xml at all"


def test_an_index_table_that_was_deleted_is_an_error_row_not_a_failed_upload(client, source, _isolated_db, _isolated_harvest_db):
    _transform(_isolated_db, source["id"])  # uses the table "stations", which was never uploaded
    assert _upload(client, source).status_code == 200
    (out,) = _outputs(_isolated_harvest_db)
    assert out.status == "error" and "does not exist" in out.error_message


def test_even_an_unexpected_bug_in_a_transform_cannot_fail_the_upload(client, source, _isolated_db, _isolated_harvest_db, monkeypatch):
    _add_table(_isolated_db)
    _transform(_isolated_db, source["id"])

    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(transform_runner, "apply", boom)
    resp = _upload(client, source)
    assert resp.status_code == 200 and resp.json()["status"] == "success" and _outputs(_isolated_harvest_db) == []
    monkeypatch.undo()


def test_each_run_has_one_output_and_a_rerun_replaces_it(client, source, _isolated_db, _isolated_harvest_db):
    _add_table(_isolated_db)
    _transform(_isolated_db, source["id"])
    _upload(client, source)
    with _isolated_db() as s, _isolated_harvest_db() as h:
        run_id = h.scalar(select(HarvestRun.id))
        first_id = transform_runner.process_run(s, h, source["id"], run_id, "Detector.xml", SOAP.encode("utf-8")).id
        again = transform_runner.process_run(s, h, source["id"], run_id, "Detector.xml", SOAP.encode("utf-8"))
        assert first_id != again.id and h.scalar(select(TransformedFile.id).where(TransformedFile.run_id == run_id)) == again.id
        assert len(list(h.scalars(select(TransformedFile)).all())) == 1


# -- re-running over older files ----------------------------------------------------------------------------

def _runs(client, source, n=4):
    for _ in range(n):
        assert _upload(client, source).status_code == 200


def test_reprocess_runs_the_transform_over_earlier_files_whether_or_not_it_was_on_then(client, source, _isolated_db, _isolated_harvest_db):
    _runs(client, source, 4)  # arrived before any transform existed
    assert _outputs(_isolated_harvest_db) == []
    _add_table(_isolated_db)
    _transform(_isolated_db, source["id"], enabled=False)
    with _isolated_db() as s, _isolated_harvest_db() as h:
        summary = transform_runner.reprocess(s, h, source["id"], limit=3)
    assert summary == {"done": 3, "ok": 3, "failed": 0, "skipped_removed": 0, "stopped_early": False}
    assert len(_outputs(_isolated_harvest_db)) == 3


def test_reprocess_skips_files_whose_contents_retention_removed_and_says_so(client, source, _isolated_db, _isolated_harvest_db):
    _runs(client, source, 3)
    _add_table(_isolated_db)
    _transform(_isolated_db, source["id"])
    with _isolated_harvest_db() as h:
        oldest = h.scalar(select(HarvestedFile).order_by(HarvestedFile.fetched_at).limit(1))
        oldest.content = b""
        h.commit()
    with _isolated_db() as s, _isolated_harvest_db() as h:
        summary = transform_runner.reprocess(s, h, source["id"], limit=10)
    assert summary["skipped_removed"] == 1 and summary["done"] == 2


def test_reprocess_stops_at_its_time_budget_and_reports_it(client, source, _isolated_db, _isolated_harvest_db):
    _runs(client, source, 4)
    _add_table(_isolated_db)
    _transform(_isolated_db, source["id"])
    with _isolated_db() as s, _isolated_harvest_db() as h:
        summary = transform_runner.reprocess(s, h, source["id"], limit=10, budget_s=-1)
    assert summary["stopped_early"] is True and summary["done"] == 0


def test_reprocess_counts_failures_and_does_nothing_without_a_transform(client, source, _isolated_db, _isolated_harvest_db):
    _runs(client, source, 2)
    with _isolated_db() as s, _isolated_harvest_db() as h:
        assert transform_runner.reprocess(s, h, source["id"], limit=5)["done"] == 0
    _transform(_isolated_db, source["id"])  # needs "stations", which does not exist
    with _isolated_db() as s, _isolated_harvest_db() as h:
        summary = transform_runner.reprocess(s, h, source["id"], limit=5)
    assert summary["done"] == 2 and summary["failed"] == 2 and summary["ok"] == 0


# -- previewing -----------------------------------------------------------------------------------------------

def test_preview_uses_the_newest_stored_file_and_stores_nothing(client, source, _isolated_db, _isolated_harvest_db):
    _upload(client, source)
    _add_table(_isolated_db)
    tid = _transform(_isolated_db, source["id"], enabled=False)
    with _isolated_db() as s, _isolated_harvest_db() as h:
        sample, result, error = transform_runner.preview(s, h, s.get(Transform, tid))
    assert error is None and sample.filename == "Detector.xml" and result.records_out == 3 and result.preview[0]["name"] == "Kifisias"
    assert _outputs(_isolated_harvest_db) == []


def test_preview_says_so_when_there_is_nothing_to_try_it_on_and_explains_a_failure(client, source, _isolated_db, _isolated_harvest_db):
    tid = _transform(_isolated_db, source["id"])
    with _isolated_db() as s, _isolated_harvest_db() as h:
        sample, result, error = transform_runner.preview(s, h, s.get(Transform, tid))
        assert sample is None and "no stored file" in error
    _upload(client, source)
    with _isolated_db() as s, _isolated_harvest_db() as h:
        _, result, error = transform_runner.preview(s, h, s.get(Transform, tid))
    assert result is None and "does not exist" in error  # the index table is missing


def test_preview_can_use_a_chosen_run(client, source, _isolated_db, _isolated_harvest_db):
    _upload(client, source, content=SOAP.replace("MS812", "MS000").encode("utf-8"))
    _upload(client, source)
    _add_table(_isolated_db)
    tid = _transform(_isolated_db, source["id"], steps=[XML_STEP])
    with _isolated_harvest_db() as h:
        first_run = h.scalar(select(HarvestRun.id).order_by(HarvestRun.started_at).limit(1))
    with _isolated_db() as s, _isolated_harvest_db() as h:
        sample, result, _ = transform_runner.preview(s, h, s.get(Transform, tid), first_run)
    assert sample.run_id == first_run and result.preview[0]["id"] == "MS000"


# -- index tables ---------------------------------------------------------------------------------------------

def test_which_transforms_use_an_index_table(client, source, _isolated_db):
    _transform(_isolated_db, source["id"])
    with _isolated_db() as s:
        assert transform_runner.lookups_in_use(s, "stations") == [source["id"]] and transform_runner.lookups_in_use(s, "other") == []


def test_a_replaced_index_table_is_used_for_the_next_file_not_a_stale_copy(client, source, _isolated_db, _isolated_harvest_db):
    table_id = _add_table(_isolated_db)
    _transform(_isolated_db, source["id"])
    _upload(client, source)
    with _isolated_db() as s:
        table = s.get(LookupTable, table_id)
        table.rows = json.dumps([{"id": "MS812", "name": "RENAMED", "lat": "1", "lon": "2"}])
        table.uploaded_at = table.uploaded_at + datetime.timedelta(seconds=5)  # an upload is a new version
        s.commit()
    _upload(client, source)
    first, second = _outputs(_isolated_harvest_db)
    assert json.loads(first.content)[0]["name"] == "Kifisias" and json.loads(second.content)[0]["name"] == "RENAMED"


# -- retention ------------------------------------------------------------------------------------------------

def test_a_transform_output_goes_when_the_file_it_came_from_does(client, source, _isolated_db, _isolated_harvest_db):
    _add_table(_isolated_db)
    _transform(_isolated_db, source["id"])
    _upload(client, source)
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=100)
    with _isolated_harvest_db() as h:
        assert retention.purge_source(h, source["id"], 90, now=later)[0] == 1
        out = h.scalar(select(TransformedFile))
        assert out.content == b"" and out.size > 0 and out.records_out == 3 and out.status == "ok"  # contents gone, the record stays
        assert retention.purge_source(h, source["id"], 90, now=later) == (0, 0)


def test_the_outputs_are_kept_when_the_period_has_not_passed(client, source, _isolated_db, _isolated_harvest_db):
    _add_table(_isolated_db)
    _transform(_isolated_db, source["id"])
    _upload(client, source)
    with _isolated_harvest_db() as h:
        retention.purge_source(h, source["id"], 90)
        assert h.scalar(select(TransformedFile)).content != b""


# -- the CSV settings ---------------------------------------------------------------------------------------------------------

def _csv_transform(session_factory, source_id, **settings):
    with session_factory() as s:
        t = Transform(source_id=source_id, steps=json.dumps([XML_STEP]), output_format="csv", enabled=True, **settings)
        s.add(t)
        s.commit()


def test_a_transform_made_before_the_csv_settings_existed_gets_excel_friendly_output(client, source, _isolated_db, _isolated_harvest_db):
    _csv_transform(_isolated_db, source["id"])  # csv_delimiter and csv_bom are NULL, as in an existing row
    _upload(client, source)
    (out,) = _outputs(_isolated_harvest_db)
    assert out.content[:3] == b"\xef\xbb\xbf" and out.content.decode("utf-8-sig").split("\n")[0].startswith("id,time,state,")


def test_the_chosen_separator_and_mark_are_used(client, source, _isolated_db, _isolated_harvest_db):
    _csv_transform(_isolated_db, source["id"], csv_delimiter=";", csv_bom=False)
    _upload(client, source)
    (out,) = _outputs(_isolated_harvest_db)
    assert out.content[:3] != b"\xef\xbb\xbf" and out.content.decode("utf-8").split("\n")[0].startswith("id;time;state;")
