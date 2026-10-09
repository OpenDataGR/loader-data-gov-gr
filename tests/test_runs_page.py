"""The Runs page: paging through a source's whole history, filtering it, and the File column."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from providerharvest_service import harvest_db, harvest_runner, provider_sources, retention
from providerharvest_service.app import app
from providerharvest_service.harvest_models import HarvestBase, HarvestedFile, HarvestRun
from tests.test_ui_admin_ftp import (  # noqa: F401  (fixtures shared with the admin page tests)
    _admin_session_env, _isolated_db, _master_key, _upload_root, client,
)

BASE = datetime.datetime(2026, 10, 9, 12, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def harvest(tmp_path):
    engine = create_engine("sqlite:///%s" % (tmp_path / "harvest.db"))
    HarvestBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[harvest_db.get_session] = _override
    yield factory
    app.dependency_overrides.pop(harvest_db.get_session, None)


@pytest.fixture
def source_id(_isolated_db):
    with _isolated_db() as s:
        return provider_sources.register(s, {"name": "attica", "title": "Attica", "transport_type": "push", "push_method": "ftp", "frequency": "MANUAL"}).id


def _runs(harvest, source_id, count, *, status="success", start=BASE, step=datetime.timedelta(minutes=30), file=True, size=420_000):
    ids = []
    with harvest() as h:
        for i in range(count):
            run = HarvestRun(source_id=source_id, source_name="attica", status=status, records_count=1, started_at=start - step * i,
                             error_message="boom" if status == "error" else None)
            h.add(run)
            h.flush()
            if file:
                h.add(HarvestedFile(run_id=run.id, filename="Detector.xml", content_type="application/xml", size=size, content=b"x" * 10,
                                    fetched_at=start - step * i))
            ids.append(run.id)
        h.commit()
    return ids


def _page(client, source_id, query=""):
    return client.get("/provider/sources/%s/runs%s" % (source_id, query))


# -- the query ------------------------------------------------------------------------------------------

def test_query_runs_pages_newest_first_and_reports_the_total(harvest, source_id):
    ids = _runs(harvest, source_id, 60)
    with harvest() as h:
        first, total = harvest_runner.query_runs(h, source_id, limit=25, offset=0)
        third, _ = harvest_runner.query_runs(h, source_id, limit=25, offset=50)
    assert total == 60 and [r.id for r in first] == ids[:25] and [r.id for r in third] == ids[50:]


def test_the_status_and_date_filters_are_inclusive_and_combine(harvest, source_id):
    _runs(harvest, source_id, 3, status="error")
    _runs(harvest, source_id, 4, start=BASE - datetime.timedelta(days=3))
    _runs(harvest, source_id, 5, start=BASE - datetime.timedelta(days=10))
    with harvest() as h:
        assert harvest_runner.query_runs(h, source_id, status="error")[1] == 3
        day = (BASE - datetime.timedelta(days=3)).date()
        assert harvest_runner.query_runs(h, source_id, since=day, until=day)[1] == 4  # the whole of that day
        assert harvest_runner.query_runs(h, source_id, since=day)[1] == 7  # that day and later
        assert harvest_runner.query_runs(h, source_id, until=day)[1] == 9  # that day and earlier
        assert harvest_runner.query_runs(h, source_id, status="success", until=day)[1] == 9
        assert harvest_runner.query_runs(h, source_id, status="nonsense")[1] == 12  # not a status: no filter


def test_another_sources_runs_never_appear(harvest, source_id):
    _runs(harvest, source_id, 3)
    with harvest() as h:
        h.add(HarvestRun(source_id="someone-else", source_name="other", status="success"))
        h.commit()
        assert harvest_runner.query_runs(h, source_id)[1] == 3


# -- the page ---------------------------------------------------------------------------------------------

def test_the_page_says_how_many_there_are_and_shows_one_page_of_them(client, harvest, source_id):
    _runs(harvest, source_id, 60)
    page = _page(client, source_id).text
    assert "Showing 1&ndash;25 of <strong>60</strong> runs, newest first." in page
    assert page.count("View details") == 25 and 'rel="next"' in page and 'rel="prev"' not in page


def test_later_pages_and_the_page_size(client, harvest, source_id):
    _runs(harvest, source_id, 60)
    assert "Showing 26&ndash;50 of <strong>60</strong>" in _page(client, source_id, "?page=2").text
    last = _page(client, source_id, "?page=3").text
    assert "Showing 51&ndash;60 of <strong>60</strong>" in last and last.count("View details") == 10 and 'rel="next"' not in last
    fifty = _page(client, source_id, "?per=50").text
    assert "Showing 1&ndash;50 of <strong>60</strong>" in fifty and fifty.count("View details") == 50


@pytest.mark.parametrize("query", ["?page=0", "?page=-3", "?page=abc", "?page=", "?per=7", "?per=abc", "?per=100000", "?from=garbage", "?to=2026-13-45", "?status=%27%3B", "?page=1;2"])
def test_junk_in_the_address_falls_back_to_the_defaults_and_is_never_echoed(client, harvest, source_id, query):
    _runs(harvest, source_id, 30)
    resp = _page(client, source_id, query)
    assert resp.status_code == 200 and "Showing 1&ndash;25 of <strong>30</strong>" in resp.text
    assert "garbage" not in resp.text and "13-45" not in resp.text


def test_a_page_past_the_end_shows_the_last_page(client, harvest, source_id):
    _runs(harvest, source_id, 30)
    page = _page(client, source_id, "?page=99").text
    assert "Showing 26&ndash;30 of <strong>30</strong>" in page and page.count("View details") == 5


def test_filters_narrow_the_list_and_say_so_and_the_links_keep_them(client, harvest, source_id):
    _runs(harvest, source_id, 30, status="error")
    _runs(harvest, source_id, 4, start=BASE - datetime.timedelta(days=2))
    page = _page(client, source_id, "?status=error").text
    assert "of <strong>30</strong> matching runs" in page and "Clear filters" in page
    assert "status=error" in page and "page=2" in page  # the next page keeps the filter
    day = (BASE - datetime.timedelta(days=2)).date().isoformat()
    ranged = _page(client, source_id, "?from=%s&to=%s" % (day, day)).text
    assert "of <strong>4</strong> matching runs" in ranged and ranged.count("View details") == 4


def test_a_reversed_date_range_is_swapped_not_empty(client, harvest, source_id):
    _runs(harvest, source_id, 3)
    day = BASE.date().isoformat()
    early = (BASE - datetime.timedelta(days=5)).date().isoformat()
    assert "of <strong>3</strong>" in _page(client, source_id, "?from=%s&to=%s" % (day, early)).text


def test_no_match_says_so_and_keeps_the_filters_to_change(client, harvest, source_id):
    _runs(harvest, source_id, 3)
    page = _page(client, source_id, "?status=error").text
    assert "No runs match these filters." in page and "Clear filters" in page and 'id="status"' in page
    assert "No harvest runs yet." not in page


def test_a_source_with_no_runs_at_all_keeps_the_plain_empty_message(client, harvest, source_id):
    assert "No harvest runs yet." in _page(client, source_id).text


def test_the_file_column_shows_the_name_and_size(client, harvest, source_id):
    _runs(harvest, source_id, 1, size=420_000)
    _runs(harvest, source_id, 1, size=800, start=BASE - datetime.timedelta(days=1))
    _runs(harvest, source_id, 1, file=False, start=BASE - datetime.timedelta(days=2))  # a run with no file: an empty cell
    page = _page(client, source_id).text
    assert page.count("Detector.xml") == 2 and "420 kB" in page and "800 B" in page and "Removed when it passed" not in page


def test_a_removed_file_is_marked_on_its_own_row_only(client, harvest, source_id):
    kept = _runs(harvest, source_id, 1, start=BASE)[0]
    gone = _runs(harvest, source_id, 1, start=BASE - datetime.timedelta(days=200))[0]
    with harvest() as h:
        retention.purge_source(h, source_id, 90, now=BASE)
    rows = {run_id: row for run_id in (kept, gone) for row in _page(client, source_id).text.split("<tr>") if run_id in row}
    assert "Removed when it passed its retention period" in rows[gone]
    assert "Removed when it passed its retention period" not in rows[kept] and "Detector.xml" in rows[kept]


def test_the_pager_links_are_the_pages_own_address_with_the_filters(client, harvest, source_id):
    _runs(harvest, source_id, 200)
    page = _page(client, source_id, "?page=4&per=25&status=success").text
    for expected in ("page=3&amp;status=success", "page=5&amp;status=success"):
        assert expected in page or expected.replace("&amp;", "&") in page


def test_each_row_still_links_to_its_run_in_the_shape_other_pages_rely_on(client, harvest, source_id):
    ids = _runs(harvest, source_id, 3)
    page = _page(client, source_id).text
    for run_id in ids:
        assert '/provider/sources/%s/runs/%s"' % (source_id, run_id) in page
