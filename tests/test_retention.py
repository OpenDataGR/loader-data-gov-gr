"""How long each source's data is kept: the choices, the admin page (with its confirmation before
anything is deleted), the purge itself, and what the FTP box is told."""

import datetime
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from providerharvest_service import harvest_db, provider_sources, retention
from providerharvest_service.app import app
from providerharvest_service.harvest_models import HarvestBase, HarvestedFile, HarvestRun
from providerharvest_service.models import FtpAccount
from tests.test_ui_admin_ftp import (  # noqa: F401  (fixtures shared with the admin page tests)
    _admin_session_env, _isolated_db, _master_key, _upload_root, client,
)
from tests.test_box_actions import _sync_key  # noqa: F401

NOW = datetime.datetime(2026, 10, 9, 12, 0, 0, tzinfo=datetime.timezone.utc)
SYNC_HEADERS = {"X-Ftp-Sync-Key": "test-ftp-sync-key"}


@pytest.fixture
def harvest(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///%s" % (tmp_path / "harvest.db"))
    HarvestBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(harvest_db, "get_session_factory", lambda: factory)

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[harvest_db.get_session] = _override
    yield factory
    app.dependency_overrides.pop(harvest_db.get_session, None)


def _source(session_factory, name="attica", method="ftp", transport="push", **extra):
    with session_factory() as s:
        form = {"name": name, "title": name, "transport_type": transport, "frequency": "MANUAL", **extra}
        if transport == "push":
            form["push_method"] = method
        else:
            form.update(endpoint_url="https://provider.example.gr/api", auth_type="api_key", api_key="k", frequency="DAILY")
        source = provider_sources.register(s, form)
        return source.id


def _file(hfactory, source_id, days_old, size=1000, name="Detector.xml", source_name="attica"):
    with hfactory() as h:
        run = HarvestRun(source_id=source_id, source_name=source_name, status="success", records_count=1)
        h.add(run)
        h.flush()
        at = NOW - datetime.timedelta(days=days_old)
        f = HarvestedFile(run_id=run.id, filename=name, content_type="application/xml", size=size, content=b"x" * size, fetched_at=at)
        h.add(f)
        h.commit()
        return run.id, f.id


def _config(session_factory, source_id):
    with session_factory() as s:
        return json.loads(s.get(provider_sources.ProviderSource, source_id).config)


def _contents(hfactory, file_id):
    with hfactory() as h:
        return h.get(HarvestedFile, file_id).content


# -- the choices ------------------------------------------------------------------------------

def test_the_choices_and_defaults():
    assert retention.LOADER_OPTIONS == (7, 30, 90, 180, 365, 0) and retention.BOX_OPTIONS == (1, 7, 14, 30, 90, 0)
    assert (retention.LOADER_DEFAULT, retention.BOX_DEFAULT) == (90, 30)
    assert [retention.label(d) for d in (0, 1, 7)] == ["forever", "1 day", "7 days"]


def test_an_unset_source_uses_the_defaults_and_junk_in_the_config_falls_back_to_them(_isolated_db):
    source_id = _source(_isolated_db)
    with _isolated_db() as s:
        source = s.get(provider_sources.ProviderSource, source_id)
        assert (retention.loader_days(source), retention.box_days(source)) == (90, 30)
        for junk in ("30", 45, -1, True, None, [30]):
            source.config = json.dumps({**json.loads(source.config), "retention_loader_days": junk, "retention_box_days": junk})
            assert (retention.loader_days(source), retention.box_days(source)) == (90, 30), junk
        source.config = "not json"
        assert retention.loader_days(source) == 90


@pytest.mark.parametrize("raw", ["45", "abc", "", None, "-7", "7.5", "9999"])
def test_a_period_that_is_not_one_of_the_offered_ones_is_refused(raw):
    with pytest.raises(retention.RetentionError):
        retention.parse_choice(raw, retention.LOADER_OPTIONS, "files")


def test_forever_is_zero_and_accepted():
    assert retention.parse_choice("0", retention.LOADER_OPTIONS, "x") == 0


# -- the admin page -----------------------------------------------------------------------------

def test_the_page_offers_the_choices_marks_the_defaults_and_shows_what_is_held(client, _isolated_db, harvest):
    source_id = _source(_isolated_db)
    _file(harvest, source_id, 3, size=2000)
    _file(harvest, source_id, 40, size=3000)
    page = client.get("/admin/sources/%s/retention" % source_id).text
    for text in ("7 days", "30 days", "90 days (default)", "180 days", "365 days", "forever", "1 day"):
        assert text in page
    assert "30 days (default)" in page  # the box's default
    assert "<strong>2</strong> files, 5.0 kB" in page


def test_an_http_source_has_no_box_setting(client, _isolated_db, harvest):
    source_id = _source(_isolated_db, name="demo", transport="http")
    page = client.get("/admin/sources/%s/retention" % source_id).text
    assert 'name="loader"' in page and '<select id="box"' not in page and "does not use an FTP box" in page


def test_saving_a_longer_period_just_saves(client, _isolated_db, harvest):
    source_id = _source(_isolated_db)
    resp = client.post("/admin/sources/%s/retention" % source_id, data={"loader": "180", "box": "90"}, follow_redirects=False)
    assert resp.status_code == 303 and "flash_kind=error" not in resp.headers["location"]
    config = _config(_isolated_db, source_id)
    assert (config["retention_loader_days"], config["retention_box_days"]) == (180, 90)


def test_shortening_with_files_that_would_go_asks_first_and_saves_nothing(client, _isolated_db, harvest):
    source_id = _source(_isolated_db)
    _file(harvest, source_id, 40, size=1_500_000)
    _file(harvest, source_id, 50, size=500_000)
    _file(harvest, source_id, 5)  # newer: stays
    resp = client.post("/admin/sources/%s/retention" % source_id, data={"loader": "30", "box": "30"})
    assert resp.status_code == 200
    assert "cannot be brought back" in resp.text and "<strong>2</strong> stored files" in resp.text and "2.0 MB" in resp.text
    assert "retention_loader_days" not in _config(_isolated_db, source_id)  # nothing was saved


def test_confirming_saves_it(client, _isolated_db, harvest):
    source_id = _source(_isolated_db)
    _file(harvest, source_id, 40)
    resp = client.post("/admin/sources/%s/retention" % source_id, data={"loader": "30", "box": "30", "confirmed": "1"}, follow_redirects=False)
    assert resp.status_code == 303 and _config(_isolated_db, source_id)["retention_loader_days"] == 30


def test_shortening_when_nothing_would_be_removed_needs_no_confirmation(client, _isolated_db, harvest):
    source_id = _source(_isolated_db)
    _file(harvest, source_id, 3)
    resp = client.post("/admin/sources/%s/retention" % source_id, data={"loader": "30", "box": "30"}, follow_redirects=False)
    assert resp.status_code == 303 and _config(_isolated_db, source_id)["retention_loader_days"] == 30


def test_going_from_forever_to_a_period_is_a_shortening(client, _isolated_db, harvest):
    source_id = _source(_isolated_db)
    client.post("/admin/sources/%s/retention" % source_id, data={"loader": "0", "box": "30"})
    _file(harvest, source_id, 100)
    resp = client.post("/admin/sources/%s/retention" % source_id, data={"loader": "365", "box": "30"})
    assert resp.status_code == 303 or "cannot be brought back" not in resp.text  # 100 days old: nothing past 365
    resp = client.post("/admin/sources/%s/retention" % source_id, data={"loader": "90", "box": "30"})
    assert "cannot be brought back" in resp.text and "<strong>1</strong> stored file" in resp.text


def test_a_shorter_box_period_is_confirmed_too(client, _isolated_db, harvest):
    source_id = _source(_isolated_db)
    resp = client.post("/admin/sources/%s/retention" % source_id, data={"loader": "90", "box": "7"})
    assert resp.status_code == 200 and "On the FTP box" in resp.text and "7 days" in resp.text
    assert "retention_box_days" not in _config(_isolated_db, source_id)
    done = client.post("/admin/sources/%s/retention" % source_id, data={"loader": "90", "box": "7", "confirmed": "1"}, follow_redirects=False)
    assert done.status_code == 303 and _config(_isolated_db, source_id)["retention_box_days"] == 7


def test_when_the_stored_data_cannot_be_read_a_shortening_is_refused_not_done_blind(client, _isolated_db, monkeypatch):
    source_id = _source(_isolated_db)

    def boom():
        raise RuntimeError("no harvest database")

    monkeypatch.setattr(harvest_db, "get_session_factory", boom)
    resp = client.post("/admin/sources/%s/retention" % source_id, data={"loader": "7", "box": "30"}, follow_redirects=False)
    assert "flash_kind=error" in resp.headers["location"] and "retention_loader_days" not in _config(_isolated_db, source_id)
    page = client.get("/admin/sources/%s/retention" % source_id)
    assert page.status_code == 200 and "could not be read just now" in page.text  # the page itself still works


@pytest.mark.parametrize("loader, box", [("45", "30"), ("abc", "30"), ("", "30"), ("90", "3"), ("90", "")])
def test_values_that_are_not_offered_are_refused_and_nothing_changes(client, _isolated_db, harvest, loader, box):
    source_id = _source(_isolated_db)
    resp = client.post("/admin/sources/%s/retention" % source_id, data={"loader": loader, "box": box}, follow_redirects=False)
    assert "flash_kind=error" in resp.headers["location"]
    assert "retention_loader_days" not in _config(_isolated_db, source_id)


def test_a_source_that_does_not_exist_is_handled(client, harvest):
    assert "flash_kind=error" in client.get("/admin/sources/nope/retention", follow_redirects=False).headers["location"]
    assert "flash_kind=error" in client.post("/admin/sources/nope/retention", data={"loader": "90", "box": "30"}, follow_redirects=False).headers["location"]


def test_the_page_needs_the_admin_login(_isolated_db, harvest, client):
    source_id = _source(_isolated_db)
    anonymous = TestClient(app, base_url="https://testserver")
    for method in (anonymous.get, anonymous.post):
        resp = method("/admin/sources/%s/retention" % source_id, follow_redirects=False)
        assert resp.headers.get("location", "/admin/login").startswith("/admin/login")
    assert "retention_loader_days" not in _config(_isolated_db, source_id)


def test_the_admin_dashboard_links_to_it_and_the_providers_own_list_does_not(client, _isolated_db, harvest):
    source_id = _source(_isolated_db)
    assert "/admin/sources/%s/retention" % source_id in client.get("/admin/sources").text
    assert "/retention" not in client.get("/provider/sources").text


# -- an edit keeps it ----------------------------------------------------------------------------

def test_editing_a_source_does_not_forget_its_retention(_isolated_db):
    source_id = _source(_isolated_db, name="demo", transport="http")
    with _isolated_db() as s:
        retention.set_retention(s, s.get(provider_sources.ProviderSource, source_id), loader=30, box=7)
        form = provider_sources.config_to_form_fields(s.get(provider_sources.ProviderSource, source_id))
        form.update(api_key="k", title="Renamed")
        provider_sources.update(s, source_id, form)
        config = json.loads(s.get(provider_sources.ProviderSource, source_id).config)
    assert (config["retention_loader_days"], config["retention_box_days"]) == (30, 7)


# -- the purge --------------------------------------------------------------------------------------

def test_files_past_the_period_lose_their_contents_and_nothing_else(harvest, _isolated_db):
    source_id = _source(_isolated_db)
    _, old = _file(harvest, source_id, 40, size=1234)
    _, new = _file(harvest, source_id, 10)
    with harvest() as h:
        assert retention.purge_source(h, source_id, 30, now=NOW) == (1, 1234)
    assert _contents(harvest, old) == b"" and len(_contents(harvest, new)) == 1000
    with harvest() as h:
        kept = h.get(HarvestedFile, old)
        assert (kept.filename, kept.size, kept.content_type) == ("Detector.xml", 1234, "application/xml")  # the record stays
        assert retention.is_purged(kept) and not retention.is_purged(h.get(HarvestedFile, new))
        assert h.scalar(select(HarvestRun).where(HarvestRun.id == kept.run_id)).status == "success"  # and so does the run


def test_purging_again_removes_nothing_more(harvest, _isolated_db):
    source_id = _source(_isolated_db)
    _file(harvest, source_id, 40)
    with harvest() as h:
        assert retention.purge_source(h, source_id, 30, now=NOW)[0] == 1
        assert retention.purge_source(h, source_id, 30, now=NOW) == (0, 0)


def test_forever_removes_nothing_however_old(harvest, _isolated_db):
    source_id = _source(_isolated_db)
    _, f = _file(harvest, source_id, 4000)
    with harvest() as h:
        assert retention.purge_source(h, source_id, 0, now=NOW) == (0, 0)
    assert len(_contents(harvest, f)) == 1000


def test_the_period_is_counted_from_when_the_file_arrived_to_the_exact_moment(harvest, _isolated_db):
    source_id = _source(_isolated_db)
    _, just_inside = _file(harvest, source_id, 29.99)
    _, just_outside = _file(harvest, source_id, 30.01)
    with harvest() as h:
        retention.purge_source(h, source_id, 30, now=NOW)
    assert _contents(harvest, just_inside) and not _contents(harvest, just_outside)


def test_another_sources_files_are_never_touched(harvest, _isolated_db):
    mine, other = _source(_isolated_db, name="mine"), _source(_isolated_db, name="other")
    _, others_file = _file(harvest, other, 400, source_name="other")
    _, my_file = _file(harvest, mine, 400, source_name="mine")
    with harvest() as h:
        retention.purge_source(h, mine, 30, now=NOW)
    assert _contents(harvest, others_file) and not _contents(harvest, my_file)


def test_a_genuinely_empty_file_is_not_mistaken_for_a_removed_one(harvest, _isolated_db):
    source_id = _source(_isolated_db)
    _, empty = _file(harvest, source_id, 400, size=0)
    with harvest() as h:
        assert retention.purge_source(h, source_id, 30, now=NOW) == (0, 0)
        assert not retention.is_purged(h.get(HarvestedFile, empty))


def test_each_source_is_purged_by_its_own_period_and_an_unset_one_by_the_default(harvest, _isolated_db):
    short, unset, forever = (_source(_isolated_db, name=n) for n in ("short", "unset", "forever"))
    with _isolated_db() as s:
        retention.set_retention(s, s.get(provider_sources.ProviderSource, short), loader=7)
        retention.set_retention(s, s.get(provider_sources.ProviderSource, forever), loader=0)
    ids = {}
    for name, sid in (("short", short), ("unset", unset), ("forever", forever)):
        ids[name] = (_file(harvest, sid, 20, source_name=name)[1], _file(harvest, sid, 100, source_name=name)[1])
    with _isolated_db() as s, harvest() as h:
        done = retention.purge_all(s, h, now=NOW)
    assert sorted(n for n, _, _ in done) == ["short", "unset"]
    assert [bool(_contents(harvest, f)) for f in ids["short"]] == [False, False]  # both older than 7 days
    assert [bool(_contents(harvest, f)) for f in ids["unset"]] == [True, False]  # 20 days kept, 100 past the 90 default
    assert [bool(_contents(harvest, f)) for f in ids["forever"]] == [True, True]


def test_the_background_purger_does_a_pass_and_stops_when_asked(harvest, _isolated_db, monkeypatch):
    monkeypatch.delenv("LOADER_RETENTION", raising=False)
    source_id = _source(_isolated_db)
    _, old = _file(harvest, source_id, 400)  # 400 days before NOW; "now" in the thread is the real clock
    thread = retention.start_purger(_isolated_db, lambda: harvest, interval_s=3600, first_after_s=0)
    try:
        deadline = time.time() + 10
        while time.time() < deadline and _contents(harvest, old):
            time.sleep(0.05)
    finally:
        thread.stop_event.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and not _contents(harvest, old)


def test_the_purger_can_be_switched_off(monkeypatch, _isolated_db, harvest):
    monkeypatch.setenv("LOADER_RETENTION", "0")
    assert retention.start_purger(_isolated_db, lambda: harvest) is None


# -- what the box is told ---------------------------------------------------------------------------------

def test_the_account_sync_carries_each_accounts_archive_days(client, _isolated_db):
    source_id = _source(_isolated_db)
    with _isolated_db() as s:
        retention.set_retention(s, s.get(provider_sources.ProviderSource, source_id), box=14)
        provider_sources.activate(s, source_id)  # (a provider's account is only listed for the box once its source is active)
        s.add(FtpAccount(username="by-hand", password_hash="$2a$x", home_directory="/srv/ftp/by-hand", protocol="ftp", enabled=True))
        s.commit()
    got = {a["username"]: a["archive_days"] for a in client.get("/admin/ftp-accounts-sync", headers=SYNC_HEADERS).json()}
    assert 14 in got.values()  # the provider's own account: its source's period
    assert got["by-hand"] == 30  # an account with no source: the default


# -- a removed file on the Runs pages --------------------------------------------------------------------

def test_a_removed_file_is_marked_on_the_run_page_and_cannot_be_downloaded_while_a_kept_one_can(client, _isolated_db, harvest):
    source_id = _source(_isolated_db)
    old_run, old_file = _file(harvest, source_id, 400, size=2048, name="old.xml")
    new_run, new_file = _file(harvest, source_id, 1, size=2048, name="new.xml")
    with harvest() as h:
        retention.purge_source(h, source_id, 90)
    page = client.get("/provider/sources/%s/runs/%s" % (source_id, old_run)).text
    assert "old.xml" in page and "removed (retention)" in page and "/files/%s" % old_file not in page
    gone = client.get("/provider/sources/%s/runs/%s/files/%s" % (source_id, old_run, old_file))
    assert gone.status_code == 410 and "retention" in gone.text
    kept_page = client.get("/provider/sources/%s/runs/%s" % (source_id, new_run)).text
    assert "removed (retention)" not in kept_page and "/files/%s" % new_file in kept_page
    ok = client.get("/provider/sources/%s/runs/%s/files/%s" % (source_id, new_run, new_file))
    assert ok.status_code == 200 and ok.content == b"x" * 2048
