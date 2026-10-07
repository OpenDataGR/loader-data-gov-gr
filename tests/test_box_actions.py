"""Resources and actions on the FTP box page: figures the box reports, and the requests an admin
queues that the box collects at its next heartbeat, runs, and answers in the one after."""

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from providerharvest_service import box_commands
from providerharvest_service.app import app
from providerharvest_service.models import FtpBoxCommand
from tests.test_ui_admin_ftp import (  # noqa: F401  (fixtures shared with the FTP box page tests)
    _admin_session_env, _isolated_db, _master_key, _upload_root, client,
)

SYNC_HEADERS = {"X-Ftp-Sync-Key": "test-ftp-sync-key"}
HOST = "snf-899358"

RESOURCES = {
    "load": [0.5, 0.4, 0.3], "cpus": 2, "mem_total_mb": 4000, "mem_available_mb": 1000, "swap_used_mb": 0,
    "uptime_s": 90061, "disks": [{"path": "/", "total_mb": 10000, "used_mb": 9500}, {"path": "/srv/ftp", "total_mb": 50000, "used_mb": 5000}],
    "ftp_sessions": 3, "sftp_sessions": 1,
}


@pytest.fixture(autouse=True)
def _sync_key(monkeypatch):
    monkeypatch.setenv("LOADER_FTP_SYNC_KEY", "test-ftp-sync-key")


def _beat(client, host=HOST, **extra):
    payload = {
        "hostname": host, "proftpd_active": True, "ftp_port_listening": True, "sftp_port_listening": True,
        "relay_service_active": True, "account_count": 1, "sync_ok": True, "sync_message": None, **extra,
    }
    resp = client.put("/admin/ftp-box-status", json=payload, headers=SYNC_HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ask(client, action, host=HOST):
    return client.post("/admin/ftp/boxes/%s/actions/%s" % (host, action), follow_redirects=False)


def _rows(session_factory):
    with session_factory() as session:
        return list(session.scalars(select(FtpBoxCommand).order_by(FtpBoxCommand.requested_at)).all())


# -- resources ------------------------------------------------------------------------

def test_reported_figures_are_shown_with_how_worrying_each_is(client):
    _beat(client, resources=RESOURCES)
    page = client.get("/admin/ftp").text
    assert "0.50  0.40  0.30  (2 CPUs)" in page
    assert "2.9 GB of 3.9 GB in use" in page  # 3000 of 4000 MB
    assert "9.3 GB of 9.8 GB used" in page and "meter bad" in page  # the root disk is 95% full
    assert "4.9 GB of 48.8 GB used" in page
    assert "3 FTP, 1 SFTP" in page and "1d 1h" in page


def test_a_box_that_does_not_report_resources_says_so_and_the_page_still_works(client):
    _beat(client)
    page = client.get("/admin/ftp").text
    assert "does not report its resources yet" in page and HOST in page


def test_junk_resource_figures_are_refused_not_stored(client):
    payload = {"hostname": HOST, "proftpd_active": True, "ftp_port_listening": True, "sftp_port_listening": True,
               "relay_service_active": True, "account_count": 1, "sync_ok": True, "resources": {"load": "high", "disks": "x"}}
    assert client.put("/admin/ftp-box-status", json=payload, headers=SYNC_HEADERS).status_code == 422


# -- asking for an action --------------------------------------------------------------

def test_a_request_waits_then_is_handed_to_the_box_exactly_once_and_answered(client, _isolated_db):
    _beat(client)
    resp = _ask(client, "restart-proftpd")
    assert resp.status_code == 303 and "requested" in resp.headers["location"]
    (row,) = _rows(_isolated_db)
    assert (row.state, row.action, row.requested_by) == ("pending", "restart-proftpd", "test-admin")
    assert "waiting for the box" in client.get("/admin/ftp").text

    first = _beat(client)
    assert first["commands"] == [{"id": row.id, "action": "restart-proftpd"}]
    assert _beat(client)["commands"] == []  # at most once, even though no answer has come
    assert "running" in client.get("/admin/ftp").text

    _beat(client, command_results=[{"id": row.id, "ok": True, "output": "restarted proftpd; it is now active"}])
    (row,) = _rows(_isolated_db)
    assert row.state == "ok" and row.finished_at is not None
    page = client.get("/admin/ftp").text
    assert "done" in page and "restarted proftpd; it is now active" in page
    assert _beat(client)["commands"] == []


def test_a_failed_answer_is_shown_as_failed_with_its_output_open(client, _isolated_db):
    _beat(client)
    _ask(client, "reload-fail2ban")
    command_id = _beat(client)["commands"][0]["id"]
    _beat(client, command_results=[{"id": command_id, "ok": False, "output": "fail2ban-client reload: no jail"}])
    page = client.get("/admin/ftp").text
    assert "failed" in page and "no jail" in page and "<details open>" in page


def test_output_is_escaped_and_cleaned(client, _isolated_db):
    _beat(client)
    _ask(client, "diagnostics")
    command_id = _beat(client)["commands"][0]["id"]
    _beat(client, command_results=[{"id": command_id, "ok": True, "output": "<script>alert(1)</script>\x00\x07 fine\nnext line"}])
    page = client.get("/admin/ftp").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    (row,) = _rows(_isolated_db)
    assert "\x00" not in row.output and "\x07" not in row.output and "fine\nnext line" in row.output


def test_output_is_cut_to_a_size_loader_will_store(client, _isolated_db):
    _beat(client)
    _ask(client, "diagnostics")
    command_id = _beat(client)["commands"][0]["id"]
    _beat(client, command_results=[{"id": command_id, "ok": True, "output": "x" * 100000}])
    (row,) = _rows(_isolated_db)
    assert len(row.output) == box_commands.MAX_OUTPUT


def test_answers_that_do_not_belong_are_ignored(client, _isolated_db):
    _beat(client)
    _beat(client, host="other-box")
    _ask(client, "restart-relay")
    command_id = _beat(client)["commands"][0]["id"]
    _beat(client, host="other-box", command_results=[{"id": command_id, "ok": True, "output": "not mine"}])  # another box's
    _beat(client, command_results=[{"id": "no-such-id", "ok": True, "output": "x"}])  # unknown
    assert _rows(_isolated_db)[0].state == "sent"
    _beat(client, command_results=[{"id": command_id, "ok": True, "output": "first"}])
    _beat(client, command_results=[{"id": command_id, "ok": False, "output": "second"}])  # already answered
    (row,) = _rows(_isolated_db)
    assert (row.state, row.output) == ("ok", "first")


def test_a_command_is_only_handed_to_the_box_it_was_for(client):
    _beat(client)
    _beat(client, host="other-box")
    _ask(client, "restart-proftpd")
    assert _beat(client, host="other-box")["commands"] == []
    assert len(_beat(client)["commands"]) == 1


def test_asking_twice_while_one_is_waiting_is_refused(client, _isolated_db):
    _beat(client)
    _ask(client, "restart-proftpd")
    again = _ask(client, "restart-proftpd")
    assert "already waiting" in again.headers["location"].replace("%20", " ").replace("+", " ")
    assert len(_rows(_isolated_db)) == 1
    _ask(client, "diagnostics")  # a different action is fine
    assert len(_rows(_isolated_db)) == 2


@pytest.mark.parametrize("action", ["reboot", "shell", "restart-sshd", "Restart-Proftpd"])
def test_an_action_not_on_the_list_is_refused(client, _isolated_db, action):
    _beat(client)
    resp = _ask(client, action)
    assert resp.status_code == 303 and "flash_kind=error" in resp.headers["location"]
    assert _rows(_isolated_db) == []


def test_an_unknown_box_is_refused(client, _isolated_db):
    resp = _ask(client, "diagnostics", host="no-such-box")
    assert "flash_kind=error" in resp.headers["location"] and _rows(_isolated_db) == []


def test_it_needs_the_admin_login(_isolated_db, client):
    _beat(client)
    anonymous = TestClient(app, base_url="https://testserver")
    resp = anonymous.post("/admin/ftp/boxes/%s/actions/restart-proftpd" % HOST, follow_redirects=False)
    assert resp.status_code in (303, 401, 403) and resp.headers.get("location", "/admin/login").startswith("/admin/login")
    assert _rows(_isolated_db) == []


def test_disruptive_actions_ask_for_confirmation_and_harmless_ones_do_not(client):
    _beat(client)
    page = client.get("/admin/ftp").text
    assert "Uploads in progress will be interrupted" in page and "Restart the file relay on this box?" in page
    assert "Run diagnostics" in page
    diag = page.split("Run diagnostics")[0].rsplit("<form", 1)[1]
    assert "onsubmit" not in diag


# -- time limits -----------------------------------------------------------------------

def _age(session_factory, **fields):
    with session_factory() as session:
        for row in session.scalars(select(FtpBoxCommand)).all():
            for name, value in fields.items():
                setattr(row, name, value)
        session.commit()


def test_a_request_the_box_never_collects_expires_and_is_not_sent_late(client, _isolated_db):
    _beat(client)
    _ask(client, "restart-proftpd")
    _age(_isolated_db, requested_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(minutes=16))
    assert _beat(client)["commands"] == []  # the box was away; it must not restart ProFTPD now
    (row,) = _rows(_isolated_db)
    assert row.state == "expired" and "did not collect" in row.output
    assert "no answer" in client.get("/admin/ftp").text


def test_a_request_taken_but_never_answered_expires(client, _isolated_db):
    _beat(client)
    _ask(client, "restart-proftpd")
    _beat(client)
    _age(_isolated_db, sent_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(minutes=11))
    client.get("/admin/ftp")
    (row,) = _rows(_isolated_db)
    assert row.state == "expired" and "never reported back" in row.output
    _ask(client, "restart-proftpd")  # and it can be asked again
    assert len(_rows(_isolated_db)) == 2


def test_old_history_is_dropped(client, _isolated_db):
    _beat(client)
    _ask(client, "diagnostics")
    _age(_isolated_db, state="ok", requested_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=31))
    _ask(client, "reload-fail2ban")  # a new request prunes
    assert [r.action for r in _rows(_isolated_db)] == ["reload-fail2ban"]
