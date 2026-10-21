"""The alert monitor and what shows it: a source that has gone silent, a transform / pull / CKAN output that keeps failing, an FTP box that does not report;
the bell and the feed the open pages ask every few seconds; the Alerts page."""

import datetime

import pytest

from providerharvest_service import alerts, provider_sources
from providerharvest_service.harvest_models import HarvestRun, TransformedFile
from providerharvest_service.models import Alert, CkanTarget, FtpBoxStatus, ProviderSource, Transform
from tests.test_runs_page import harvest  # noqa: F401  (a harvest database for the page tests)
from tests.test_ui_admin_ftp import (  # noqa: F401  (fixtures shared with the admin page tests)
    _admin_session_env, _isolated_db, _master_key, _upload_root, client,
)

NOW = datetime.datetime(2026, 10, 21, 12, 0, 0)


def _source(session, name="emy-stations", *, push=True):
    source = provider_sources.register(session, {"name": name, "title": name.title(), "transport_type": "push" if push else "http", "push_method": "ftp",
                                                  "frequency": "MANUAL", "endpoint_url": "https://x.example.gr/", "auth_type": "api_key", "api_key": "k"} if not push else
                                       {"name": name, "title": name.title(), "transport_type": "push", "push_method": "ftp", "frequency": "MANUAL"})
    provider_sources.activate(session, source.id)
    return source


def _runs(harvest, source, minutes_ago, *, status="success", step=5, count=12):
    """``count`` runs ending ``minutes_ago`` minutes before NOW, ``step`` minutes apart."""
    with harvest() as h:
        for i in range(count):
            h.add(HarvestRun(source_id=source.id, source_name=source.name, status=status, started_at=(NOW - datetime.timedelta(minutes=minutes_ago + i * step)).replace(tzinfo=datetime.timezone.utc),
                             error_message="boom" if status == "error" else None))
        h.commit()


def _open(session):
    return {a.key: a for a in session.query(Alert).filter(Alert.resolved_at.is_(None)).all()}


def test_a_source_that_sends_every_five_minutes_and_has_been_quiet_for_an_hour_is_silent(_isolated_db, harvest):
    with _isolated_db() as session, harvest() as h:
        source = _source(session)
        _runs(harvest, source, 60)
        done = alerts.run_once(session, h, NOW)
        assert done == {"opened": 1, "resolved": 0}
        (alert,) = _open(session).values()
        assert alert.kind == "silent" and alert.title.startswith("No file from Emy-Stations for 1 h") and "usually sends about every 5 min" in alert.detail
        assert alert.link == "/provider/sources/%s/runs" % source.id


def test_a_quiet_spell_within_the_usual_rhythm_is_not_news(_isolated_db, harvest):
    with _isolated_db() as session, harvest() as h:
        source = _source(session)
        _runs(harvest, source, 12)  # (twelve minutes: less than 15, less than four gaps)
        assert alerts.run_once(session, h, NOW) == {"opened": 0, "resolved": 0}
        _runs(harvest, _source(session, "slow-one"), 90, step=60, count=8)  # (hourly: 90 minutes is fine, five hours would not be)
        assert alerts.run_once(session, h, NOW) == {"opened": 0, "resolved": 0}


def test_a_source_without_a_rhythm_yet_and_a_long_dead_one_are_left_alone(_isolated_db, harvest):
    with _isolated_db() as session, harvest() as h:
        _runs(harvest, _source(session, "new-one"), 600, count=3)  # (three files: no rhythm to judge by)
        _runs(harvest, _source(session, "dead-one"), 60 * 24 * 40)  # (nothing for forty days)
        assert alerts.run_once(session, h, NOW) == {"opened": 0, "resolved": 0}


def test_the_alert_is_refreshed_not_repeated_and_resolved_when_the_files_come_back(_isolated_db, harvest):
    with _isolated_db() as session, harvest() as h:
        source = _source(session)
        _runs(harvest, source, 60)
        alerts.run_once(session, h, NOW)
        (first,) = _open(session).values()
        changed = first.changed_at
        later = NOW + datetime.timedelta(minutes=10)
        assert alerts.run_once(session, h, later) == {"opened": 0, "resolved": 0}  # (still silent: the same alert, not a new one)
        assert session.query(Alert).count() == 1 and _open(session)[first.key].changed_at == changed and "1 h 10 min" in _open(session)[first.key].title
        _runs(harvest, source, 0, count=2)  # (a file arrives)
        assert alerts.run_once(session, h, later) == {"opened": 0, "resolved": 1}
        assert not _open(session) and session.get(Alert, first.id).resolved_at is not None
        # it goes quiet again: the same alert comes back, no longer acknowledged
        first.acknowledged_by = "someone"
        session.commit()
        assert alerts.run_once(session, h, later + datetime.timedelta(hours=3)) == {"opened": 1, "resolved": 0}
        assert session.query(Alert).count() == 1 and session.get(Alert, first.id).acknowledged_by is None


def test_a_pull_source_whose_last_runs_all_failed_and_a_transform_that_keeps_failing(_isolated_db, harvest):
    with _isolated_db() as session, harvest() as h:
        pull = _source(session, "registry", push=False)
        _runs(harvest, pull, 3, status="error", count=3)
        push = _source(session)
        pipeline = Transform(source_id=push.id, name="default", steps="[]", enabled=True)
        session.add(pipeline)
        session.commit()
        def output(minutes_ago, status):
            run = HarvestRun(source_id=push.id, source_name=push.name, status="success", started_at=(NOW - datetime.timedelta(minutes=minutes_ago)).replace(tzinfo=datetime.timezone.utc))
            h.add(run)
            h.flush()
            h.add(TransformedFile(run_id=run.id, source_id=push.id, transform_id=pipeline.id, status=status, error_message="bad column" if status == "error" else None,
                                  created_at=NOW - datetime.timedelta(minutes=minutes_ago)))
            h.commit()

        for i in (2, 1, 0):
            output(i, "error")
        alerts.run_once(session, h, NOW)
        found = {a.key.split(":")[0]: a for a in _open(session).values()}
        assert found["pull"].severity == "error" and "last 3 runs failed" in found["pull"].title and found["pull"].detail == "boom"
        assert found["transform"].title.startswith("The transform of Emy-Stations failed on its last 3 files") and found["transform"].detail == "bad column"
        output(-1, "ok")  # (one good output, newer than the failures, ends it)
        assert alerts.run_once(session, h, NOW)["resolved"] == 1


def test_a_failed_ckan_output_and_the_ftp_box_that_stopped_reporting(_isolated_db, harvest):
    with _isolated_db() as session, harvest() as h:
        source = _source(session)
        session.add(CkanTarget(source_id=source.id, pipeline_id="p1", mode="upload", dataset_id="ds1", dataset_title="Attica traffic", last_status="error", last_error="CKAN refused it"))
        session.add(FtpBoxStatus(hostname="box1", reported_at=NOW - datetime.timedelta(minutes=20), proftpd_active=True, ftp_port_listening=True, sftp_port_listening=True,
                                 relay_service_active=True, account_count=3, sync_ok=True))
        session.add(FtpBoxStatus(hostname="box2", reported_at=NOW, proftpd_active=True, ftp_port_listening=True, sftp_port_listening=False, relay_service_active=True,
                                 account_count=3, sync_ok=True))
        session.commit()
        alerts.run_once(session, h, NOW)
        found = _open(session)
        ckan = next(a for k, a in found.items() if k.startswith("ckan:"))
        assert ckan.title == "CKAN: Attica traffic could not be updated" and ckan.detail == "CKAN refused it" and "?load=" in ckan.link
        assert "has not reported for 20 min" in found["box:box1"].title and found["box:box1"].source_id is None
        assert "the SFTP port is down" in found["box:box2"].title


def test_a_connection_check_that_says_error_is_an_alert(_isolated_db, harvest):
    with _isolated_db() as session, harvest() as h:
        source = _source(session)
        source.last_check_status, source.last_check_message = "error", "timed out"
        session.commit()
        alerts.run_once(session, h, NOW)
        assert _open(session)["health:%s" % source.id].detail == "timed out"
        source.last_check_status = "warn"  # (a warning is not an alert: a source that has sent nothing yet always has one)
        session.commit()
        assert alerts.run_once(session, h, NOW)["resolved"] == 1


# -- the pages -----------------------------------------------------------------------------------------------------------------------------------------

def _add(_isolated_db, key="silent:x", severity="warning", title="No file from X for 1 h", acknowledged=False, resolved=False, source_id=None):
    with _isolated_db() as s:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        alert = Alert(key=key, kind="silent", severity=severity, title=title, detail="detail", link="/provider/sources", source_id=source_id,
                      resolved_at=now if resolved else None, acknowledged_by="kostas" if acknowledged else None, first_seen=now, changed_at=now)
        s.add(alert)
        s.commit()
        return alert.id


def test_the_feed_counts_the_open_alerts_and_tells_a_page_only_what_changed_since_it_last_asked(client, _isolated_db):
    first = client.get("/alerts/feed").json()
    assert first["open"] == 0 and first["unacknowledged"] == 0 and first["changes"] == []
    _add(_isolated_db)
    _add(_isolated_db, key="silent:y", title="Another", acknowledged=True)
    _add(_isolated_db, key="silent:old", title="Gone", resolved=True)
    quiet = client.get("/alerts/feed").json()  # (a page just opened asks without a cursor: no shouting about old news)
    assert (quiet["open"], quiet["unacknowledged"], quiet["changes"]) == (2, 1, [])
    changes = client.get("/alerts/feed", params={"since": first["now"]}).json()["changes"]
    assert sorted((c["title"], c["state"]) for c in changes) == [("Another", "acknowledged"), ("Gone", "resolved"), ("No file from X for 1 h", "new")]
    assert client.get("/alerts/feed", params={"since": quiet["now"]}).json()["changes"] == []
    assert client.get("/alerts/feed", params={"since": "garbage"}).json()["changes"] == []


def test_the_alerts_page_lists_them_and_an_alert_can_be_acknowledged(client, _isolated_db):
    assert "Nothing needs attention." in client.get("/alerts").text
    alert_id = _add(_isolated_db, severity="error", title="The transform of X failed")
    _add(_isolated_db, key="silent:old", title="Was quiet", resolved=True)
    page = client.get("/alerts").text
    assert "The transform of X failed" in page and ">problem<" in page and "Acknowledge" in page and "Gone in the last 24 hours" in page and "Was quiet" in page
    resp = client.post("/alerts/%s/ack" % alert_id, follow_redirects=False)
    assert resp.status_code == 303 and "Acknowledged" in client.get(resp.headers["location"]).text
    page = client.get("/alerts").text
    assert "acknowledged by" in page and 'action="/alerts/%s/ack"' % alert_id not in page
    assert client.get("/alerts/feed").json()["unacknowledged"] == 0
    assert "No such alert" in client.get(client.post("/alerts/nope/ack", follow_redirects=False).headers["location"]).text


def test_everything_can_be_acknowledged_at_once(client, _isolated_db):
    _add(_isolated_db)
    _add(_isolated_db, key="silent:y")
    resp = client.post("/alerts/ack-all", follow_redirects=False)
    assert "2 alerts acknowledged." in client.get(resp.headers["location"]).text and client.get("/alerts/feed").json()["unacknowledged"] == 0


def test_somebody_who_cannot_see_a_source_does_not_hear_about_its_alerts(client, _isolated_db, monkeypatch):
    from providerharvest_service import overview, ui_provider

    with _isolated_db() as session:
        mine = _source(session, "mine")
        theirs = _source(session, "theirs")
        ids = (mine.id, theirs.id)
    _add(_isolated_db, key="silent:mine", title="Mine is quiet", source_id=ids[0])
    _add(_isolated_db, key="silent:theirs", title="Theirs is quiet", source_id=ids[1])
    _add(_isolated_db, key="box:b", title="The box is down")
    assert client.get("/alerts/feed").json()["open"] == 3  # (a super admin hears of everything)
    monkeypatch.setattr(ui_provider, "_signed_in", lambda request: (False, "user_abc"))
    monkeypatch.setattr(overview, "visible_sources", lambda session, is_super, ckan_user: overview.Scope([s for s in session.query(ProviderSource).all() if s.name == "mine"], False, [], ""))
    assert client.get("/alerts/feed").json()["open"] == 1
    page = client.get("/alerts").text
    assert "Mine is quiet" in page and "Theirs is quiet" not in page and "The box is down" not in page


def test_every_page_has_the_bell_and_the_script_that_asks_for_news(client):
    page = client.get("/alerts").text
    assert 'id="alert-bell"' in page and 'id="alert-toasts"' in page and "/alerts/feed" in page and "setInterval(tick, 15000)" in page and "document.hidden" in page


def test_the_monitor_can_be_switched_off_and_runs_the_passes_it_is_given(monkeypatch):
    monkeypatch.setenv("LOADER_ALERTS", "0")
    assert alerts.start_monitor(lambda: None, lambda: None) is None
