"""The short history of each box's figures: what is kept, one sample per five minutes, honest gaps,
and the charts on the Data boxes page."""

import datetime
import time

import pytest
from sqlalchemy import select

from providerharvest_service import box_history, http_box
from providerharvest_service.models import BoxSample
from tests.test_box_actions import HOST, RESOURCES, _beat  # noqa: F401
from tests.test_ui_admin_ftp import (  # noqa: F401  (fixtures shared with the Data boxes page tests)
    _admin_session_env, _isolated_db, _master_key, _upload_root, client,
)
from tests.test_box_actions import _sync_key  # noqa: F401


def _at(minutes_ago=0, base=None):
    base = base or datetime.datetime(2026, 10, 8, 12, 0, 0)
    return base - datetime.timedelta(minutes=minutes_ago)


FIGURES = {
    "load": [1.0, 0.8, 0.6], "cpus": 2, "mem_total_mb": 4000, "mem_available_mb": 3000,
    "disks": [{"path": "/srv", "total_mb": 1000, "used_mb": 100}, {"path": "/", "total_mb": 10000, "used_mb": 2500}],
    "ftp_sessions": 3, "sftp_sessions": 1, "net": {"iface": "eth0", "rx_bps": 1500, "tx_bps": 2500},
}


@pytest.fixture
def session(_isolated_db):
    with _isolated_db() as s:
        yield s


def _rows(session):
    return list(session.scalars(select(BoxSample).order_by(BoxSample.at)).all())


# -- what is kept ---------------------------------------------------------------------------

def test_a_sample_keeps_the_few_numbers_that_matter():
    assert box_history.summarize(FIGURES) == {
        "load_pct": 50.0, "mem_pct": 25.0, "disk_pct": 25.0, "rx_bps": 1500, "tx_bps": 2500, "sessions": 4,
    }


def test_the_root_disk_is_the_one_charted_and_missing_figures_are_none_not_zero():
    got = box_history.summarize({"disks": [{"path": "/srv", "total_mb": 1000, "used_mb": 500}]})
    assert got["disk_pct"] == 50.0  # no "/" reported: the first disk
    empty = box_history.summarize({})
    assert set(empty.values()) == {None}


def test_the_http_box_counts_its_connections_as_sessions():
    assert box_history.summarize({"http_connections": 7})["sessions"] == 7


# -- one per five minutes ---------------------------------------------------------------------

def test_only_one_sample_per_box_per_five_minute_bucket(session):
    base = datetime.datetime(2026, 10, 8, 12, 0, 30)
    assert box_history.record(session, "b", FIGURES, at=base) is True
    assert box_history.record(session, "b", FIGURES, at=base + datetime.timedelta(minutes=2)) is False  # same bucket
    assert box_history.record(session, "b", FIGURES, at=base + datetime.timedelta(minutes=6)) is True
    assert box_history.record(session, "other", FIGURES, at=base) is True  # another box has its own
    assert len(_rows(session)) == 3


def test_nothing_is_recorded_without_figures(session):
    assert box_history.record(session, "b", None) is False and box_history.record(session, "b", {}) is False
    assert _rows(session) == []


def test_recording_never_raises_into_the_heartbeat(session, monkeypatch):
    monkeypatch.setattr(box_history, "summarize", lambda f: 1 / 0)
    assert box_history.record(session, "b", FIGURES) is False


def test_old_samples_are_dropped_after_seven_days(session):
    box_history._last_prune = 0.0
    now = datetime.datetime(2026, 10, 8, 12, 0, 0)
    box_history.record(session, "b", FIGURES, at=now - datetime.timedelta(days=8))
    box_history._last_prune = 0.0
    box_history.record(session, "b", FIGURES, at=now - datetime.timedelta(days=6))
    box_history._last_prune = 0.0
    box_history.record(session, "b", FIGURES, at=now)
    assert [r.at for r in _rows(session)] == [now - datetime.timedelta(days=6), now]


def test_the_window_selects_only_recent_samples(session):
    now = datetime.datetime(2026, 10, 8, 12, 0, 0)
    for hours in (30, 20, 5, 1):
        box_history.record(session, "b", FIGURES, at=now - datetime.timedelta(hours=hours))
    assert len(box_history.samples(session, "b", 24, now=now)) == 3
    assert len(box_history.samples(session, "b", 6, now=now)) == 2


def test_a_heartbeat_with_figures_records_one_sample_however_often_it_arrives(client, _isolated_db):
    _beat(client, resources=RESOURCES)
    _beat(client, resources=RESOURCES)
    _beat(client)  # a box on older code: no figures, no sample
    with _isolated_db() as s:
        rows = [r for r in _rows(s) if r.hostname == HOST]
    assert len(rows) == 1 and rows[0].disk_pct == 95.0 and rows[0].sessions == 4


# -- the charts -------------------------------------------------------------------------------

def _rows_at(minutes, **values):
    return [BoxSample(hostname="b", bucket=i, at=_at(m), **values) for i, m in enumerate(minutes)]


def test_a_quiet_stretch_is_a_gap_in_the_line_not_a_line_over_nothing():
    rows = _rows_at([60, 55, 50, 20, 15, 10], mem_pct=40.0)  # nothing between minute 50 and 20 ago
    c = box_history.chart(rows, title="t", unit="%", series=[("m", box_history.BLUE, lambda r: r.mem_pct)],
                          start=_at(120), end=_at(0), fixed_max=100)
    assert c["svg"].count('d="M') == 1 and c["svg"].count(" M") == 1  # two separate runs in one path
    assert not c["empty"]


def test_a_chart_with_no_readings_says_so_and_draws_no_line():
    c = box_history.chart([], title="t", unit="%", series=[("m", box_history.BLUE, lambda r: r.mem_pct)],
                          start=_at(120), end=_at(0), fixed_max=100)
    assert c["empty"] and "<path" not in c["svg"] and c["now"]["m"] == "--"


def test_a_lone_reading_is_still_drawn():
    rows = _rows_at([30], mem_pct=40.0)
    c = box_history.chart(rows, title="t", unit="%", series=[("m", box_history.BLUE, lambda r: r.mem_pct)],
                          start=_at(120), end=_at(0), fixed_max=100)
    assert "<path" in c["svg"]


def test_a_load_over_100_percent_still_fits_the_chart():
    rows = _rows_at([20, 15, 10], load_pct=250.0)
    c = box_history.chart(rows, title="t", unit="%", series=[("l", box_history.BLUE, lambda r: r.load_pct)],
                          start=_at(60), end=_at(0), fixed_max=100)
    assert c["peak"] == "250" and c["scale"] == "250"


def test_network_is_one_chart_with_two_lines_on_one_axis_in_two_validated_colours():
    rows = _rows_at([20, 15, 10], rx_bps=100_000, tx_bps=2_500_000)
    network = next(c for c in box_history.charts(rows, 1, now=_at(0)) if c["title"] == "Network")
    assert [name for name, _ in network["legend"]] == ["in", "out"]
    assert [colour for _, colour in network["legend"]] == ["#2a78d6", "#eb6834"]
    assert network["now"] == {"in": "100.0 kB/s", "out": "2.5 MB/s"}


def test_the_table_lists_the_latest_readings_newest_first_as_text():
    rows = _rows_at([20, 15, 10], load_pct=12.0, mem_pct=30.0, disk_pct=44.0, rx_bps=2000, tx_bps=None, sessions=5)
    first = box_history.table(rows, limit=2)[0]
    assert first == {"at": "10-08 11:50", "load": "12%", "mem": "30%", "disk": "44%", "rx": "2.0 kB/s", "tx": "--", "sessions": "5"}
    assert len(box_history.table(rows, limit=2)) == 2


# -- the page ---------------------------------------------------------------------------------

def test_the_page_draws_the_charts_with_a_table_alternative_and_window_links(client, _isolated_db):
    _beat(client, resources=RESOURCES)
    with _isolated_db() as s:
        box_history._last_prune = 0.0
        for minutes in (30, 25, 20, 15):
            box_history.record(s, HOST, RESOURCES, at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(minutes=minutes))
    page = client.get("/admin/ftp").text
    assert page.count('class="spark-card"') >= 5 and "Show the latest readings as a table" in page
    for name in ("Load (% of CPUs busy)", "Memory in use", "Disk / used", "Network", "Open connections"):
        assert name in page
    assert 'href="?window=6h"' in page and 'href="?window=7d"' in page and 'aria-current="true"' in page


@pytest.mark.parametrize("window", ["6h", "24h", "7d"])
def test_each_offered_window_works(client, window):
    _beat(client, resources=RESOURCES)
    page = client.get("/admin/ftp?window=" + window).text
    assert 'class="current" aria-current="true">' in page


@pytest.mark.parametrize("window", ["", "1y", "../x", "6h; drop", "\"><b>x"])
def test_any_other_window_falls_back_to_the_default_and_is_never_echoed(client, window):
    page = client.get("/admin/ftp", params={"window": window}).text
    assert page.count('aria-current="true"') == 1 and "24 hours" in page
    assert not window or window not in page  # a window we do not offer is never written back into the page


# -- the HTTP box's own sampler --------------------------------------------------------------

def test_the_sampler_can_be_switched_off(monkeypatch, _isolated_db):
    monkeypatch.setenv("LOADER_BOX_HISTORY", "0")
    assert box_history.start_sampler(_isolated_db) is None


def test_the_sampler_records_the_http_box_once_per_bucket_and_stops_when_asked(monkeypatch, _isolated_db):
    monkeypatch.delenv("LOADER_BOX_HISTORY", raising=False)
    calls = []
    monkeypatch.setattr(http_box, "collect_resources", lambda **kw: calls.append(1) or {**FIGURES, "http_connections": 2})
    thread = box_history.start_sampler(_isolated_db, interval_s=0.05)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not calls:
            time.sleep(0.05)
        time.sleep(0.3)  # several more ticks: they must not collect again within the bucket
    finally:
        thread.stop_event.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    with _isolated_db() as s:
        rows = _rows(s)
    assert [r.hostname for r in rows] == ["loader"] and rows[0].sessions == 2
    assert len(calls) == 1  # (it checks for this bucket's sample before collecting anything)
