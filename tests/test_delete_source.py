"""Deleting a source takes everything that belongs to it along: pipelines, index tables, publish setting, stored runs / files / records and
outputs -- and leaves every other source alone."""

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from providerharvest_service.app import app
from providerharvest_service.harvest_models import HarvestedFile, HarvestedRecord, HarvestRun, TransformedFile
from providerharvest_service.models import LoadTarget, LookupTable, ProviderSource, Transform
from tests.test_pipelines_ui import _add, _setup
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _go, _isolated_db, _isolated_harvest_db, _master_key, _upload_data, _upload_table, client, source,
)


def _other_sources_data(_isolated_db, _isolated_harvest_db):
    """A second source's rows in both databases, to prove they are not touched."""
    with _isolated_db() as s:
        s.add_all([Transform(source_id="other", steps="[]"), LookupTable(source_id="other", name="stations", columns='["id"]', rows="[]", row_count=0),
                   LoadTarget(source_id="other", api_enabled=True)])
        s.commit()
    with _isolated_harvest_db() as h:
        run = HarvestRun(source_id="other", source_name="other", status="success")
        h.add(run)
        h.flush()
        h.add_all([HarvestedFile(run_id=run.id, filename="a.xml", size=1, content=b"x"), HarvestedRecord(run_id=run.id, ref="r", payload={}),
                   TransformedFile(run_id=run.id, source_id="other", transform_id="t", filename="a.json", size=1, content=b"x")])
        h.commit()


def _counts(_isolated_db, _isolated_harvest_db, source_id):
    with _isolated_db() as s:
        config = [s.scalar(select(func.count()).select_from(m).where(m.source_id == source_id)) for m in (Transform, LookupTable, LoadTarget)]
    with _isolated_harvest_db() as h:
        stored = [h.scalar(select(func.count()).select_from(HarvestRun).where(HarvestRun.source_id == source_id)),
                  h.scalar(select(func.count()).select_from(TransformedFile).where(TransformedFile.source_id == source_id)),
                  h.scalar(select(func.count()).select_from(HarvestedFile).join(HarvestRun, HarvestRun.id == HarvestedFile.run_id).where(HarvestRun.source_id == source_id))]
    return config + stored


def test_deleting_a_source_removes_what_belongs_to_it_and_nothing_else(client, source, _isolated_db, _isolated_harvest_db):
    _upload_table(client, source)
    _setup(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _add(client, source, "lookup")
    _upload_data(client, source)
    client.post("/load/sources/%s/api" % source["id"], data={"enabled": "1"}, follow_redirects=False)
    _other_sources_data(_isolated_db, _isolated_harvest_db)
    before = _counts(_isolated_db, _isolated_harvest_db, source["id"])
    assert before[0] == 2 and before[1] == 1 and before[2] == 1 and all(n >= 1 for n in before[3:])
    caller = TestClient(app, base_url="https://testserver")
    assert caller.get("/data/acme-push").status_code == 200

    resp = client.post("/provider/sources/%s/delete" % source["id"], follow_redirects=False)

    message = _flash(resp)
    assert message.startswith("Source deleted. Removed with it: 2 pipelines, 1 index table, ") and "stored run" in message and "output" in message
    assert "published address stopped" in message
    assert _counts(_isolated_db, _isolated_harvest_db, source["id"]) == [0, 0, 0, 0, 0, 0]
    assert caller.get("/data/acme-push").status_code == 404
    assert _counts(_isolated_db, _isolated_harvest_db, "other") == [1, 1, 1, 1, 1, 1]  # the other source is untouched
    with _isolated_harvest_db() as h:
        assert h.scalar(select(func.count()).select_from(HarvestedRecord)) == 1


def test_deleting_a_source_with_nothing_stored_says_just_that(client, source, _isolated_db):
    assert _flash(client.post("/provider/sources/%s/delete" % source["id"], follow_redirects=False)) == "Source deleted."
    with _isolated_db() as s:
        assert s.get(ProviderSource, source["id"]) is None


def test_deleting_an_unknown_source_changes_nothing(client, source, _isolated_db, _isolated_harvest_db):
    _other_sources_data(_isolated_db, _isolated_harvest_db)
    assert "No such source" in _flash(client.post("/provider/sources/nope/delete", follow_redirects=False))
    assert _counts(_isolated_db, _isolated_harvest_db, "other") == [1, 1, 1, 1, 1, 1]


def test_the_menu_warns_what_a_delete_takes_with_it(client, source):
    page = client.get("/provider/sources").text
    assert "and everything stored for it -- its pipelines, index tables, published address, stored files and outputs? This cannot be undone." in page
