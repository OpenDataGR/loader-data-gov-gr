"""The readiness check of a push source: plain ticks and crosses from what the loader knows about the source, its login account and what
the FTP box last reported -- the "test connection" for a source that sends to us."""

import datetime
import re

import pytest
from sqlalchemy import select

from providerharvest_service import provider_sources, push_check
from providerharvest_service.models import FtpAccount, FtpBoxStatus
from tests.test_ui_admin_ftp import (  # noqa: F401  (fixtures and helpers shared with the FTP admin page tests)
    _admin_session_env, _isolated_db, _master_key, _register_ftp_push_source, _upload_root, client,
)

NOW = datetime.datetime(2026, 10, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _box(session_factory, *, base=NOW, seconds_ago=20, proftpd=True, ftp=True, sftp=True, relay=True, accounts=1, sync_ok=True, sync_message=None, hostname="box-1"):
    with session_factory() as session:
        session.merge(FtpBoxStatus(
            hostname=hostname, reported_at=(base - datetime.timedelta(seconds=seconds_ago)).replace(tzinfo=None), proftpd_active=proftpd,
            ftp_port_listening=ftp, sftp_port_listening=sftp, relay_service_active=relay, account_count=accounts, sync_ok=sync_ok, sync_message=sync_message,
        ))
        session.commit()


def _levels(session_factory, source_id):
    with session_factory() as session:
        source = provider_sources.get_or_404(session, source_id)
        return {c.label: c for c in push_check.check(session, source, NOW)}


def _find(checks, fragment):
    matches = [c for label, c in checks.items() if fragment in label]
    assert len(matches) == 1, (fragment, list(checks))
    return matches[0]


def _healthy_ftp(_isolated_db, name="acme-push"):
    source_id = _register_ftp_push_source(_isolated_db, name, activate=True)
    _box(_isolated_db)
    return source_id


def test_a_healthy_ftp_push_source_is_ready_and_waiting_for_its_first_file(_isolated_db):
    source_id = _healthy_ftp(_isolated_db)
    checks = _levels(_isolated_db, source_id)
    assert all(c.level == "ok" for label, c in checks.items() if "No file" not in label)
    assert _find(checks, "No file has arrived").level == "warn" and "ask the provider" in _find(checks, "No file has arrived").detail
    with _isolated_db() as session:
        assert push_check.summary(list(push_check.check(session, provider_sources.get_or_404(session, source_id), NOW))) == ("ok", "Ready -- waiting for the first file.")


def test_once_a_file_has_arrived_it_says_when_and_from_where(_isolated_db):
    source_id = _healthy_ftp(_isolated_db)
    with _isolated_db() as session:
        source = provider_sources.get_or_404(session, source_id)
        source.last_received_at = (NOW - datetime.timedelta(minutes=7)).replace(tzinfo=None)
        source.last_sender = "203.0.113.9"
        session.commit()
        checks = push_check.check(session, source, NOW)
        arrived = [c for c in checks if c.label.startswith("A file arrived")][0]
        assert arrived.level == "ok" and "7 minutes ago" in arrived.label and "203.0.113.9" in arrived.detail
        assert push_check.summary(checks) == ("ok", "Ready and working.")


def test_a_source_that_is_not_active_and_its_disabled_account_are_problems(_isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, "pending-one", activate=False)
    _box(_isolated_db)
    checks = _levels(_isolated_db, source_id)
    assert _find(checks, "The source is pending").level == "fail" and _find(checks, "login account is disabled").level == "fail"
    with _isolated_db() as session:
        assert push_check.summary(push_check.check(session, provider_sources.get_or_404(session, source_id), NOW))[0] == "fail"
        assert push_check.summary(push_check.check(session, provider_sources.get_or_404(session, source_id), NOW))[1] == "Not ready: The source is pending."


def test_an_account_that_does_not_forward_here_or_is_missing_is_a_problem(_isolated_db):
    source_id = _healthy_ftp(_isolated_db)
    with _isolated_db() as session:
        account = session.scalar(select(FtpAccount).where(FtpAccount.managed_source_id == source_id))
        account.push_target_source_name = "somewhere-else"
        session.commit()
    assert _find(_levels(_isolated_db, source_id), "does not forward").level == "fail"
    with _isolated_db() as session:
        session.delete(session.scalar(select(FtpAccount).where(FtpAccount.managed_source_id == source_id)))
        session.commit()
    assert _find(_levels(_isolated_db, source_id), "no login account").level == "fail"


def test_a_box_that_never_reported_or_has_gone_quiet_is_a_problem_and_hides_the_rest_of_the_box_checks(_isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, activate=True)
    checks = _levels(_isolated_db, source_id)
    assert _find(checks, "No FTP box has reported").level == "fail" and not [l for l in checks if "relay" in l]
    _box(_isolated_db, seconds_ago=10 * 60)
    checks = _levels(_isolated_db, source_id)
    quiet = _find(checks, "gone quiet")
    assert quiet.level == "fail" and "10 minutes ago" in quiet.detail and not [l for l in checks if "relay" in l or "ProFTPD" in l]


@pytest.mark.parametrize("box,fragment,level", [
    ({"proftpd": False}, "ProFTPD) is not running", "fail"),
    ({"ftp": False, "sftp": False}, "neither port", "fail"),
    ({"sftp": False}, "not listening on SFTP (2222)", "warn"),
    ({"ftp": False}, "not listening on FTP (21)", "warn"),
    ({"relay": False}, "relay service is not running", "fail"),
    ({"sync_ok": False, "sync_message": "401 from loader"}, "could not fetch the account list", "fail"),
])
def test_what_the_box_reports_decides_the_box_checks(_isolated_db, box, fragment, level):
    source_id = _register_ftp_push_source(_isolated_db, activate=True)
    _box(_isolated_db, **box)
    found = _find(_levels(_isolated_db, source_id), fragment)
    assert found.level == level
    if "sync_message" in box:
        assert "401 from loader" in found.detail


def test_a_box_that_has_fewer_accounts_than_the_loader_is_a_warning_to_check_again(_isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, activate=True)
    _box(_isolated_db, accounts=0)
    found = _find(_levels(_isolated_db, source_id), "does not have all the accounts yet")
    assert found.level == "warn" and "holds 0, the loader has 1" in found.detail


def test_the_sender_list_is_reported_either_way(_isolated_db):
    source_id = _healthy_ftp(_isolated_db)
    assert _find(_levels(_isolated_db, source_id), "any address").level == "ok"
    with _isolated_db() as session:
        source = provider_sources.get_or_404(session, source_id)
        import json
        config = json.loads(source.config)
        config["allowed_senders"] = ["203.0.113.0/24", "198.51.100.7"]
        source.config = json.dumps(config)
        session.commit()
    found = _find(_levels(_isolated_db, source_id), "only from 2 addresses or ranges")
    assert found.level == "ok" and "address changes" in found.detail


def test_an_https_push_source_checks_its_key_and_has_no_box_checks(_isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, {"name": "web-push", "title": "web-push", "transport_type": "push", "push_method": "https"})
        provider_sources.activate(session, source.id)
        source_id = source.id
    checks = _levels(_isolated_db, source_id)
    assert _find(checks, "An API key is set").level == "ok" and not [l for l in checks if "FTP" in l or "relay" in l or "login account" in l]
    with _isolated_db() as session:
        import json
        source = provider_sources.get_or_404(session, source_id)
        config = json.loads(source.config)
        config.pop("push_api_key_hash")
        source.config = json.dumps(config)
        session.commit()
    assert _find(_levels(_isolated_db, source_id), "No API key is set").level == "fail"


def test_a_source_that_is_not_a_push_source_has_no_readiness_check(_isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, {"name": "pull-one", "title": "pull-one", "transport_type": "http", "endpoint_url": "https://x.example.gr/",
                                                     "auth_type": "api_key", "api_key": "k"})
        with pytest.raises(ValueError):
            push_check.check(session, source, NOW)


def test_the_summary_names_the_worst_problem_first():
    ok, warn, fail = push_check.Check("ok", "fine"), push_check.Check("warn", "Odd thing"), push_check.Check("fail", "Broken thing")
    waiting = push_check.Check("warn", "No file has arrived yet")
    assert push_check.summary([ok, warn, fail]) == ("fail", "Not ready: Broken thing.")
    assert push_check.summary([ok, warn, waiting]) == ("warn", "Ready, with a warning: Odd thing.")
    assert push_check.summary([ok, waiting]) == ("ok", "Ready -- waiting for the first file.")
    assert push_check.summary([ok]) == ("ok", "Ready and working.")


# -- the page ---------------------------------------------------------------------------------------------------------------------------------

def _push_source_id(client_):
    page = client_.get("/provider/sources").text
    return re.search(r"/provider/sources/([^/\"]+)/readiness", page).group(1)


def test_the_menu_offers_readiness_for_a_push_source_and_test_connection_for_the_others(client, _isolated_db):
    _register_ftp_push_source(_isolated_db, "push-one", activate=True)
    with _isolated_db() as session:
        provider_sources.register(session, {"name": "pull-one", "title": "pull-one", "transport_type": "http", "endpoint_url": "https://x.example.gr/",
                                            "auth_type": "api_key", "api_key": "k"})
    page = client.get("/provider/sources").text
    assert len(re.findall(r"/readiness\"", page)) == 1 and len(re.findall(r"/check-connection\"", page)) == 1


def test_the_page_lists_the_checks_with_a_summary_and_can_be_run_again(client, _isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, "push-one", activate=True)
    _box(_isolated_db, base=datetime.datetime.now(datetime.timezone.utc), seconds_ago=5)  # the page uses the real clock
    page = client.get("/provider/sources/%s/readiness" % source_id).text
    assert "Readiness -- push-one" in page and "FTP / SFTP push" in page and "readiness-summary" in page
    assert "The source is active" in page and "The relay is running" in page and "No file has arrived yet" in page
    assert 'class="check-mark ok"' in page and 'class="check-mark warn"' in page and "Check again" in page
    assert "Ready -- waiting for the first file." in page


def test_a_problem_shows_as_a_cross_and_the_summary_says_not_ready(client, _isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, "push-one", activate=True)  # no box has ever reported
    page = client.get("/provider/sources/%s/readiness" % source_id).text
    assert 'class="check-mark fail"' in page and "No FTP box has reported yet" in page and "Not ready: No FTP box has reported yet." in page
    assert 'class="readiness-summary fail"' in page


def test_an_unknown_source_and_a_pull_source_are_turned_away_with_a_message(client, _isolated_db):
    assert "No such source" in client.get("/provider/sources/nope/readiness", follow_redirects=False).headers["location"].replace("+", " ")
    with _isolated_db() as session:
        pull_id = provider_sources.register(session, {"name": "pull-one", "title": "pull-one", "transport_type": "http", "endpoint_url": "https://x.example.gr/",
                                                      "auth_type": "api_key", "api_key": "k"}).id
    resp = client.get("/provider/sources/%s/readiness" % pull_id, follow_redirects=False)
    assert resp.status_code == 303 and "Only+a+push+source" in resp.headers["location"]


def test_the_page_needs_the_login():
    from fastapi.testclient import TestClient

    from providerharvest_service.app import app

    anonymous = TestClient(app, base_url="https://testserver")
    assert anonymous.get("/provider/sources/x/readiness", follow_redirects=False).headers["location"].startswith("/admin/login")


# -- the result is the source's health ------------------------------------------------------------------------------------------------------------

def _health(_isolated_db, source_id):
    with _isolated_db() as session:
        source = provider_sources.get_or_404(session, source_id)
        return source.last_check_status, source.last_check_message, source.last_check_at


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _arrived(_isolated_db, source_id):
    with _isolated_db() as session:
        source = provider_sources.get_or_404(session, source_id)
        source.last_received_at = (_now() - datetime.timedelta(minutes=3)).replace(tzinfo=None)
        source.last_sender = "203.0.113.9"
        session.commit()


def test_all_green_makes_the_source_reachable_with_the_date_of_the_check(client, _isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, "push-one", activate=True)
    _box(_isolated_db, base=_now(), seconds_ago=5)
    _arrived(_isolated_db, source_id)
    assert _health(_isolated_db, source_id)[0] is None  # not checked yet
    before = _now()
    client.get("/provider/sources/%s/readiness" % source_id)
    status, message, at = _health(_isolated_db, source_id)
    assert status == "ok" and message == "Ready and working." and before - datetime.timedelta(seconds=2) <= at.replace(tzinfo=datetime.timezone.utc) <= _now() + datetime.timedelta(seconds=2)
    row = re.search(r"<tr>(?:(?!</tr>).)*?>push-one<.*?</tr>", client.get("/provider/sources").text, re.S).group(0)
    assert "reachable" in row and "unreachable" not in row and "%s UTC" % at.strftime("%Y-%m-%d %H:%M") in row


def test_a_problem_makes_it_unreachable_and_the_tooltip_names_the_problem(client, _isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, "push-one", activate=True)  # no box has ever reported
    client.get("/provider/sources/%s/readiness" % source_id)
    status, message, at = _health(_isolated_db, source_id)
    assert status == "error" and message == "Not ready: No FTP box has reported yet." and at is not None
    row = re.search(r"<tr>(?:(?!</tr>).)*?>push-one<.*?</tr>", client.get("/provider/sources").text, re.S).group(0)
    assert "unreachable" in row and "No FTP box has reported yet" in row


def test_only_warnings_give_a_warning_not_reachable_even_when_waiting_for_the_first_file(client, _isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, "push-one", activate=True)
    _box(_isolated_db, base=_now(), seconds_ago=5)  # healthy, but no file has ever arrived
    client.get("/provider/sources/%s/readiness" % source_id)
    status, message, _ = _health(_isolated_db, source_id)
    assert status == "warn" and message == "Ready -- waiting for the first file."
    row = re.search(r"<tr>(?:(?!</tr>).)*?>push-one<.*?</tr>", client.get("/provider/sources").text, re.S).group(0)
    assert "<span>warning</span>" in row and "reachable" not in row
    _arrived(_isolated_db, source_id)
    client.get("/provider/sources/%s/readiness" % source_id)  # a file has arrived: now every check is green
    assert _health(_isolated_db, source_id)[0] == "ok"


def test_checking_again_updates_the_result_when_things_change(client, _isolated_db):
    source_id = _register_ftp_push_source(_isolated_db, "push-one", activate=True)
    _box(_isolated_db, base=_now(), seconds_ago=5)
    _arrived(_isolated_db, source_id)
    client.get("/provider/sources/%s/readiness" % source_id)
    assert _health(_isolated_db, source_id)[0] == "ok"
    _box(_isolated_db, base=_now(), seconds_ago=5, relay=False)
    client.get("/provider/sources/%s/readiness" % source_id)
    status, message, _ = _health(_isolated_db, source_id)
    assert status == "error" and "relay service is not running" in message


def test_looking_at_an_unknown_or_pull_source_changes_no_health(client, _isolated_db):
    with _isolated_db() as session:
        pull_id = provider_sources.register(session, {"name": "pull-one", "title": "pull-one", "transport_type": "http", "endpoint_url": "https://x.example.gr/",
                                                      "auth_type": "api_key", "api_key": "k"}).id
    client.get("/provider/sources/%s/readiness" % pull_id)
    assert _health(_isolated_db, pull_id) == (None, None, None)
