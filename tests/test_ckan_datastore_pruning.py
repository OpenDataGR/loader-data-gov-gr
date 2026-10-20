"""A DataStore table that keeps a limited history: rows older than N days leave it a day at a time, each day saved as a CSV file in the dataset first --
against a fake CKAN."""

import datetime
import json

import pytest

from providerharvest_service import ckan_client, ckan_datastore, ckan_load
from providerharvest_service.models import CkanTarget
from tests.test_ckan_datastore import DatastoreCkan, _datastore, _output, ckan  # noqa: F401
from tests.test_ckan_load import _pipeline, _source, _target
from tests.test_ckan_ui import _connect, _page, _ready, _targets
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _isolated_db, _isolated_harvest_db, _master_key, client, source,
)

NOW = datetime.datetime(2026, 10, 20, 9, 0)


def _readings(*days):
    return json.dumps([{"id": "MS%d" % i, "at": "%sT10:00:00.000Z" % day, "cars": i} for i, day in enumerate(days)]).encode()


def _kept(refresh="upsert", days=7, archive=True):
    cfg = {"refresh": refresh, "keys": ["id", "at"] if refresh == "upsert" else [], "retention_days": days, "archive": archive, "time_column": "at"}
    return _target("datastore", datastore_config=json.dumps(cfg))


@pytest.fixture
def now(monkeypatch):
    monkeypatch.setattr(ckan_load, "_now", lambda: NOW)


def _loaded(session, ckan, *days, **kept):
    target = _kept(**kept)
    session.add(target)
    session.commit()
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(_readings(*days)))
    session.commit()
    return target


def test_pruning_has_to_be_chosen_properly():
    assert ckan_datastore.check_config("append", [], 30, False, "at") == {"refresh": "append", "keys": [], "retention_days": 30, "archive": False, "time_column": "at"}
    for args, message in ((("replace", [], 30, True, "at"), "keeps history"), (("append", [], 5, True, "at"), "from the list"), (("append", [], 30, True, ""), "holds the time")):
        with pytest.raises(ckan_datastore.DatastoreError, match=message):
            ckan_datastore.check_config(*args)
    assert ckan_datastore.day_of("2026-10-20T08:18:00.000Z") == "2026-10-20" and ckan_datastore.day_of("n/a") is None and ckan_datastore.day_of(None) is None


def test_with_a_retention_each_row_carries_its_day_and_rows_already_too_old_are_not_loaded(now):
    ckan, target = DatastoreCkan(), _kept()
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(_readings("2026-10-20", "2026-10-15", "2026-08-30")))
    rows = ckan.tables["res1"]
    assert sorted(r["loader_day"] for r in rows) == ["2026-10-15", "2026-10-20"]  # (30 August is older than 7 days: not loaded, so not pruned again and again)
    assert {"id": "loader_day", "type": "text"} in ckan.fields["res1"]


def test_a_day_that_has_aged_out_is_saved_as_a_csv_and_then_removed(now, _isolated_db):
    ckan = DatastoreCkan()
    with _isolated_db() as session:
        target = _loaded(session, ckan, "2026-10-20", "2026-10-15")
        later = NOW + datetime.timedelta(days=4)  # (the 15th is now 9 days old; the 20th is not)
        assert ckan_load.prune_target(session, target, now=later, client=ckan) == "Pruned 2026-10-15."
        assert [r["loader_day"] for r in ckan.tables["res1"]] == ["2026-10-20"]
        archive = [r for r in ckan.resources.values() if r["url"] == ""]
        assert len(archive) == 1 and archive[0]["name"].endswith("2026-10-15") and archive[0]["format"] == "CSV"
        call = [c for c in ckan.calls if c[0] == "resource_create" and c[2]][0]
        text = call[2]["upload"][1].decode()
        assert call[2]["upload"][0].endswith("2026-10-15.csv") and text.splitlines()[0] == "id,at,cars,loader_day" and "MS1,2026-10-15T10:00:00.000Z,1,2026-10-15" in text
        assert "Pruned 2026-10-15" in ckan_datastore.config_of(target.datastore_config)["prune_note"]


def test_a_day_with_many_rows_is_saved_in_parts(now, _isolated_db, monkeypatch):
    monkeypatch.setattr(ckan_load, "ARCHIVE_PAGE", 2)
    monkeypatch.setattr(ckan_load, "ARCHIVE_PART_ROWS", 4)
    ckan = DatastoreCkan()
    with _isolated_db() as session:
        target = _loaded(session, ckan, *(["2026-10-15"] * 9), "2026-10-20")
        ckan_load.prune_target(session, target, now=NOW + datetime.timedelta(days=4), client=ckan)
        names = sorted(r["name"] for r in ckan.resources.values() if r["url"] == "")
        assert len(names) == 3 and names[0].endswith("(part 1)") and sum(len(c[2]["upload"][1].decode().splitlines()) - 1 for c in ckan.calls if c[0] == "resource_create" and c[2]) == 9


def test_pruning_is_not_repeated_within_hours_and_a_second_worker_does_nothing(now, _isolated_db):
    ckan = DatastoreCkan()
    with _isolated_db() as session:
        target = _loaded(session, ckan, "2026-10-20", "2026-10-15", archive=False)
        later = NOW + datetime.timedelta(days=4)
        assert ckan_load.prune_target(session, target, now=later, client=ckan) == "Pruned 2026-10-15."
        assert not [c for c in ckan.calls if c[0] == "resource_create" and c[2]]  # (not chosen: no file)
        calls = len(ckan.calls)
        assert ckan_load.prune_target(session, target, now=later + datetime.timedelta(hours=1), client=ckan) == "" and len(ckan.calls) == calls
        # another worker took the pass a moment ago: our claim does not land, so nothing is done
        stale = CkanTarget(id=target.id, source_id="s1", pipeline_id="p1", dataset_id="ds1", mode="datastore", resource_id=target.resource_id,
                           datastore_config=json.dumps({"refresh": "upsert", "keys": ["id"], "retention_days": 7, "time_column": "at"}))
        session.expunge(target)
        assert ckan_load.prune_target(session, stale, now=later + datetime.timedelta(days=1), client=ckan) == ""


def test_a_day_is_never_removed_when_saving_it_failed(now, _isolated_db):
    ckan = DatastoreCkan()
    with _isolated_db() as session:
        target = _loaded(session, ckan, "2026-10-20", "2026-10-15")
        ckan.fail["resource_create"] = ckan_client.CKANValidationError("resource_create", "no space")
        note = ckan_load.prune_target(session, target, now=NOW + datetime.timedelta(days=4), client=ckan)
        assert note.startswith("Pruning failed") and len(ckan.tables["res1"]) == 2  # (nothing was deleted)
        assert "Pruning failed" in ckan_datastore.config_of(target.datastore_config)["prune_note"]


def test_only_a_limited_number_of_days_are_pruned_per_pass_and_replace_tables_are_left_alone(now, _isolated_db):
    ckan = DatastoreCkan()
    with _isolated_db() as session:
        target = _loaded(session, ckan, "2026-10-20", "2026-10-19", "2026-10-18", "2026-10-17", archive=False)
        later = NOW + datetime.timedelta(days=12)
        assert ckan_load.prune_target(session, target, now=later, client=ckan) == "Pruned 2026-10-17, 2026-10-18."  # (two a pass, oldest first)
        replace = _datastore("replace")
        replace.resource_id = "res1"
        assert ckan_load.prune_target(session, replace, now=later, client=ckan) == ""


def test_the_page_offers_the_retention_and_keeps_it(client, source, ckan, _isolated_db):
    _ready(client, source)
    page = client.get(_page(source["id"])).text
    assert 'name="ds_retention"' in page and 'name="ds_time"' in page and 'name="ds_archive"' in page
    assert "holds the time of a row" in _flash(_connect(client, source["id"], mode="datastore", ds_refresh="append", ds_retention="30"))
    _connect(client, source["id"], mode="datastore", ds_refresh="append", ds_retention="30", ds_time="at", ds_archive="1")
    (target,) = _targets(_isolated_db)
    cfg = ckan_datastore.config_of(target.datastore_config)
    assert (cfg["refresh"], cfg["retention_days"], cfg["time_column"], cfg["archive"]) == ("append", 30, "at", True)
