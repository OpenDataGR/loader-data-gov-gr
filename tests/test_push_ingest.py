import base64
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from providerharvest_service import db, harvest_db, harvest_runner, provider_sources
from providerharvest_service.app import app
from providerharvest_service.harvest_models import HarvestBase


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "test.db"))
    session_factory = db.make_session_factory(engine)

    def _override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db.get_session] = _override
    yield session_factory
    app.dependency_overrides.pop(db.get_session, None)


@pytest.fixture(autouse=True)
def _isolated_harvest_db(tmp_path):
    engine = create_engine("sqlite:///%s" % (tmp_path / "harvest-test.db"))
    HarvestBase.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def _override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[harvest_db.get_session] = _override
    yield session_factory
    app.dependency_overrides.pop(harvest_db.get_session, None)


@pytest.fixture
def client(logged_in_client):
    return logged_in_client


def _register_and_activate_push_source(client, config_session_factory, **overrides):
    form = {"name": "acme-push", "title": "Acme push", "transport_type": "push", "frequency": "MANUAL"}
    form.update(overrides)
    resp = client.post("/provider/sources/new", data=form, follow_redirects=False)
    assert resp.status_code == 200  # the one-time push-credentials page, not a redirect
    api_key = _extract_api_key(resp.text)

    with config_session_factory() as session:
        source = provider_sources.list_all(session)[0]
        source_id = source.id
        provider_sources.activate(session, source_id)
    return source_id, api_key


def _extract_api_key(html: str) -> str:
    import re
    m = re.search(r'value="([A-Za-z0-9_-]{20,})"', html)
    assert m, "no API key found in the push-credentials page"
    return m.group(1)


def test_registering_a_push_source_shows_credentials_once(client):
    resp = client.post(
        "/provider/sources/new",
        data={"name": "acme-push", "title": "Acme push", "transport_type": "push", "frequency": "MANUAL"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Push credentials" in resp.text
    assert "/push/acme-push/upload" in resp.text


def test_upload_url_uses_the_configured_public_base_when_set(client, monkeypatch):
    monkeypatch.setenv("LOADER_PUBLIC_BASE_URL", "https://loader.data.gov.gr")
    resp = client.post(
        "/provider/sources/new",
        data={"name": "acme-push", "title": "Acme push", "transport_type": "push", "frequency": "MANUAL"},
        follow_redirects=False,
    )
    assert "https://loader.data.gov.gr/push/acme-push/upload" in resp.text
    # Not the test client's own default host -- proves the configured
    # base URL actually won, not that it happened to match anyway.
    assert "http://testserver" not in resp.text


def test_upload_stores_the_file_for_an_active_push_source(client, _isolated_db):
    source_id, api_key = _register_and_activate_push_source(client, _isolated_db)

    resp = client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        headers={"X-Api-Key": api_key},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    runs_page = client.get("/provider/sources/%s/runs" % source_id)
    assert "success" in runs_page.text

    import re
    run_id = re.search(r"/runs/([^\"]+)\"", runs_page.text).group(1)
    detail = client.get("/provider/sources/%s/runs/%s" % (source_id, run_id))
    assert "data.csv" in detail.text


def test_upload_rejects_a_wrong_api_key(client, _isolated_db):
    _register_and_activate_push_source(client, _isolated_db)

    resp = client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers={"X-Api-Key": "totally-wrong-key"},
    )
    assert resp.status_code == 401


def test_upload_404s_for_an_unknown_source_name(client):
    resp = client.post(
        "/push/no-such-source/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers={"X-Api-Key": "whatever"},
    )
    assert resp.status_code == 404


def test_upload_404s_for_a_still_pending_push_source(client, _isolated_db):
    resp = client.post(
        "/provider/sources/new",
        data={"name": "acme-push", "title": "Acme push", "transport_type": "push", "frequency": "MANUAL"},
        follow_redirects=False,
    )
    api_key = _extract_api_key(resp.text)
    # Deliberately never activated -- still "pending".

    resp = client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers={"X-Api-Key": api_key},
    )
    assert resp.status_code == 404


def test_upload_404s_for_an_http_source_even_with_a_valid_looking_name(client, _isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, {
            "name": "acme-http", "title": "t", "endpoint_url": "https://example.gr/api",
            "transport_type": "http", "auth_type": "api_key", "api_key": "k",
        })
        provider_sources.activate(session, source.id)

    resp = client.post(
        "/push/acme-http/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers={"X-Api-Key": "whatever"},
    )
    assert resp.status_code == 404


def test_upload_413s_for_a_file_over_the_system_wide_max_size(client, _isolated_db, monkeypatch):
    monkeypatch.setattr(harvest_runner, "MAX_FILE_SIZE_BYTES", 5)
    source_id, api_key = _register_and_activate_push_source(client, _isolated_db)

    resp = client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"x" * 6, "text/csv")},
        headers={"X-Api-Key": api_key},
    )
    assert resp.status_code == 413

    runs_page = client.get("/provider/sources/%s/runs" % source_id)
    assert "No harvest runs yet" in runs_page.text  # rejected before any run was even created


def _source(_isolated_db):
    with _isolated_db() as session:
        return provider_sources.list_all(session)[0]


def test_an_upload_records_when_it_arrived_and_who_sent_it(client, _isolated_db):
    _, api_key = _register_and_activate_push_source(client, _isolated_db)
    before = _source(_isolated_db)
    assert before.last_received_at is None and before.last_sender is None

    resp = client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"a,b\n", "text/csv")},
        headers={"X-Api-Key": api_key, "X-Forwarded-For": "203.0.113.9:51234, 10.0.0.4"},
    )
    assert resp.status_code == 200

    after = _source(_isolated_db)
    assert after.last_sender == "203.0.113.9"
    assert after.last_received_at is not None


def test_a_forged_forwarded_for_cannot_pick_the_recorded_sender(client, _isolated_db):
    _, api_key = _register_and_activate_push_source(client, _isolated_db)
    client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"a,b\n", "text/csv")},
        headers={"X-Api-Key": api_key, "X-Forwarded-For": "1.1.1.1, 203.0.113.9:51234, 10.0.0.4"},
    )
    assert _source(_isolated_db).last_sender == "203.0.113.9"


def test_an_upload_from_an_unknown_address_still_records_arrival(client, _isolated_db):
    _, api_key = _register_and_activate_push_source(client, _isolated_db)
    client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"a,b\n", "text/csv")},
        headers={"X-Api-Key": api_key},  # no proxy header, and the test client peer is not an IP
    )
    source = _source(_isolated_db)
    assert source.last_received_at is not None
    assert source.last_sender is None


def test_a_rejected_upload_records_nothing(client, _isolated_db):
    _register_and_activate_push_source(client, _isolated_db)
    client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"x", "text/csv")},
        headers={"X-Api-Key": "wrong", "X-Forwarded-For": "203.0.113.9:1, 10.0.0.4"},
    )
    source = _source(_isolated_db)
    assert source.last_received_at is None and source.last_sender is None


# -- allowed sender addresses -----------------------------------------------------------------

def _from(address):
    # Behind the gateway the real client is the second-to-last entry.
    return {"X-Forwarded-For": "%s:5555, 10.0.0.4" % address}


def _upload(client, api_key, **headers):
    return client.post(
        "/push/acme-push/upload",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        headers={"X-Api-Key": api_key, **headers},
    )


def _config(_isolated_db):
    import json

    return json.loads(_source(_isolated_db).config)


def test_a_source_with_no_sender_list_accepts_from_anywhere(client, _isolated_db):
    _, api_key = _register_and_activate_push_source(client, _isolated_db)
    assert _config(_isolated_db)["allowed_senders"] == []
    assert _upload(client, api_key, **_from("198.51.100.1")).status_code == 200


def test_registering_with_senders_stores_them_canonical_and_shows_them(client, _isolated_db):
    resp = client.post(
        "/provider/sources/new",
        data={"name": "acme-push", "title": "t", "transport_type": "push", "allowed_senders": "203.0.113.9, 198.51.100.0/24"},
    )
    assert resp.status_code == 200
    assert _config(_isolated_db)["allowed_senders"] == ["198.51.100.0/24", "203.0.113.9/32"]
    assert "Accepted only from" in resp.text and "<code>203.0.113.9/32</code>" in resp.text


def test_an_upload_from_a_listed_address_works(client, _isolated_db):
    _, api_key = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9\n198.51.100.0/24")
    assert _upload(client, api_key, **_from("203.0.113.9")).status_code == 200
    assert _upload(client, api_key, **_from("198.51.100.200")).status_code == 200


def test_an_upload_from_any_other_address_is_refused_even_with_the_right_key(client, _isolated_db):
    _, api_key = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9")
    resp = _upload(client, api_key, **_from("8.8.8.8"))
    assert resp.status_code == 403
    assert _source(_isolated_db).last_received_at is None  # nothing was received


def test_an_address_that_cannot_be_determined_is_refused_when_there_is_a_list(client, _isolated_db):
    _, api_key = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9")
    assert _upload(client, api_key).status_code == 403  # no proxy header, peer is not an IP


def test_a_forged_forwarded_for_cannot_get_a_foreign_sender_in(client, _isolated_db):
    _, api_key = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9")
    # The attacker prepends the allowed address; the trusted entry is still the real one.
    resp = _upload(client, api_key, **{"X-Forwarded-For": "203.0.113.9, 8.8.8.8:5555, 10.0.0.4"})
    assert resp.status_code == 403


def test_the_address_is_checked_before_the_key(client, _isolated_db):
    _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9")
    # A wrong key from a wrong address is a 403 (not the 401 that would say the key is wrong).
    assert _upload(client, "wrong-key", **_from("8.8.8.8")).status_code == 403
    # A wrong key from a right address is the ordinary 401.
    assert _upload(client, "wrong-key", **_from("203.0.113.9")).status_code == 401


def test_a_refused_address_is_recorded_in_the_security_report(client, _isolated_db):
    from providerharvest_service import db
    from providerharvest_service.models import FtpLoginAttempt

    _, api_key = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9")
    _upload(client, api_key, **_from("8.8.8.8"))
    with db.SessionLocal() as session:  # patched to a throwaway database by conftest
        rows = [(r.channel, r.ip, r.username, r.code) for r in session.query(FtpLoginAttempt).all()]
    assert rows == [("Push: address not allowed", "8.8.8.8", "acme-push", 403)]


def test_an_invalid_sender_list_is_a_form_error_and_registers_nothing(client, _isolated_db):
    resp = client.post(
        "/provider/sources/new",
        data={"name": "acme-push", "title": "t", "transport_type": "push", "allowed_senders": "not-an-address, 2001:db8::1"},
    )
    assert resp.status_code == 400
    assert "Allowed sender addresses" in resp.text
    with _isolated_db() as session:
        assert provider_sources.list_all(session) == []


def test_a_hostname_is_resolved_when_registering_and_its_addresses_stored(client, _isolated_db, monkeypatch):
    from providerharvest_service import sender_allowlist

    monkeypatch.setattr(sender_allowlist, "_resolve", lambda host: ["203.0.113.9"] if host == "sender.example.gr" else [])
    _, api_key = _register_and_activate_push_source(client, _isolated_db, allowed_senders="https://sender.example.gr/upload")
    assert _config(_isolated_db)["allowed_senders"] == ["203.0.113.9/32"]
    assert _upload(client, api_key, **_from("203.0.113.9")).status_code == 200
    assert _upload(client, api_key, **_from("203.0.113.10")).status_code == 403


def _edit(client, source_id, **fields):
    data = {"title": "Acme push", "transport_type": "push"}
    data.update(fields)
    return client.post("/provider/sources/%s/edit" % source_id, data=data, follow_redirects=False)


def test_editing_changes_the_list_and_blank_clears_it(client, _isolated_db):
    source_id, api_key = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9")
    assert _upload(client, api_key, **_from("8.8.8.8")).status_code == 403

    assert _edit(client, source_id, allowed_senders="8.8.8.8").status_code == 303
    assert _upload(client, api_key, **_from("8.8.8.8")).status_code == 200
    assert _upload(client, api_key, **_from("203.0.113.9")).status_code == 403

    assert _edit(client, source_id, allowed_senders="").status_code == 303  # present but blank: anywhere
    assert _config(_isolated_db)["allowed_senders"] == []
    assert _upload(client, api_key, **_from("203.0.113.9")).status_code == 200


def test_an_edit_that_does_not_mention_the_list_leaves_it_alone(client, _isolated_db):
    source_id, _ = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9")
    _edit(client, source_id, title="Renamed")
    assert _config(_isolated_db)["allowed_senders"] == ["203.0.113.9/32"]


def test_the_edit_form_shows_the_current_list(client, _isolated_db):
    source_id, _ = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9\n198.51.100.0/24")
    text = client.get("/provider/sources/%s/edit" % source_id).text
    assert "198.51.100.0/24\n203.0.113.9/32" in text


def test_a_bad_list_on_edit_is_refused_and_the_old_one_stays(client, _isolated_db):
    source_id, _ = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9")
    assert _edit(client, source_id, allowed_senders="nonsense").status_code == 400
    assert _config(_isolated_db)["allowed_senders"] == ["203.0.113.9/32"]


# -- rotating the API key ------------------------------------------------------------------

def _rotate(client, source_id):
    return client.post("/provider/sources/%s/rotate-key" % source_id, follow_redirects=False)


def test_rotating_issues_a_new_key_and_the_old_one_stops_working_at_once(client, _isolated_db):
    source_id, old_key = _register_and_activate_push_source(client, _isolated_db)
    assert _upload(client, old_key).status_code == 200

    resp = _rotate(client, source_id)
    assert resp.status_code == 200
    new_key = _extract_api_key(resp.text)
    assert new_key != old_key
    assert _upload(client, old_key).status_code == 401  # gone, immediately
    assert _upload(client, new_key).status_code == 200


def test_the_new_key_is_shown_once_and_stored_only_as_a_hash(client, _isolated_db):
    import json

    import bcrypt

    source_id, _ = _register_and_activate_push_source(client, _isolated_db)
    resp = _rotate(client, source_id)
    new_key = _extract_api_key(resp.text)
    assert "no longer works" in resp.text and "awaiting admin approval" not in resp.text  # says it is a replacement
    config = _config(_isolated_db)
    assert new_key not in json.dumps(config)  # nothing recoverable is kept
    assert bcrypt.checkpw(new_key.encode(), config["push_api_key_hash"].encode())
    assert new_key not in client.get("/provider/sources").text  # and it is not on any later page


def test_rotating_twice_invalidates_the_first_rotated_key_too(client, _isolated_db):
    source_id, _ = _register_and_activate_push_source(client, _isolated_db)
    first = _extract_api_key(_rotate(client, source_id).text)
    second = _extract_api_key(_rotate(client, source_id).text)
    assert _upload(client, first).status_code == 401 and _upload(client, second).status_code == 200


def test_rotating_keeps_everything_else_about_the_source(client, _isolated_db):
    source_id, _ = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9")
    _rotate(client, source_id)
    config = _config(_isolated_db)
    assert config["allowed_senders"] == ["203.0.113.9/32"] and config["push_method"] == "https"
    assert _source(_isolated_db).status == "active"


def test_only_an_https_push_source_has_a_key_to_rotate(client, _isolated_db):
    _register_ftp(client)
    with _isolated_db() as session:
        ftp_source = provider_sources.list_all(session)[0].id
        before = provider_sources.list_all(session)[0].config
    resp = _rotate(client, ftp_source)
    assert resp.status_code == 303 and "flash_kind=error" in resp.headers["location"]
    with _isolated_db() as session:
        assert provider_sources.list_all(session)[0].config == before

    pull = client.post("/provider/sources/new", data={
        "name": "a-pull-source", "title": "p", "transport_type": "http", "endpoint_url": "https://x.example.gr/a",
        "frequency": "DAILY", "auth_type": "api_key", "api_key": "k"})
    with _isolated_db() as session:
        pull_id = [s.id for s in provider_sources.list_all(session) if s.name == "a-pull-source"][0]
    assert "flash_kind=error" in _rotate(client, pull_id).headers["location"]


def test_rotating_an_unknown_source_or_without_the_login_is_refused(client, _isolated_db):
    from fastapi.testclient import TestClient

    from providerharvest_service.app import app

    assert "flash_kind=error" in _rotate(client, "no-such-source").headers["location"]
    anonymous = TestClient(app, base_url="https://testserver")
    resp = anonymous.post("/provider/sources/x/rotate-key", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login")


def test_the_sources_menu_links_to_credentials_for_push_sources_only_and_holds_no_copy_of_its_controls(client, _isolated_db):
    https_id, _ = _register_and_activate_push_source(client, _isolated_db)
    assert client.post("/provider/sources/new", data={"name": "ftp-one", "title": "t", "transport_type": "push", "push_method": "ftp"}).status_code == 200
    client.post("/provider/sources/new", data={
        "name": "a-pull-source", "title": "p", "transport_type": "http", "endpoint_url": "https://x.example.gr/a",
        "frequency": "DAILY", "auth_type": "api_key", "api_key": "k"})
    page = client.get("/provider/sources").text
    assert page.count("/credentials") == 2 and 'href="/provider/sources/%s/credentials"' % https_id in page  # both push sources, not the pull one
    assert "/rotate-key" not in page and "rotate-password" not in page  # one home: the Credentials page
    admin_page = client.get("/provider/sources").text
    assert admin_page.count("/credentials") == 2 and "/rotate-key" not in admin_page


def test_the_credentials_page_of_an_https_source_has_the_url_rotate_and_senders(client, _isolated_db):
    source_id, _ = _register_and_activate_push_source(client, _isolated_db, allowed_senders="203.0.113.9")
    page = client.get("/provider/sources/%s/credentials" % source_id).text
    assert "/push/acme-push/upload" in page and "stored as a hash" in page
    rotate = page.split('action="/provider/sources/%s/rotate-key"' % source_id, 1)[1].split("</form>", 1)[0]
    assert "return confirm(" in rotate and "stops working at once" in rotate
    assert "<code>203.0.113.9/32</code>" in page and "Edit allowed senders" in page
    assert "set-password" not in page  # an FTP-only control


def test_the_credentials_page_of_an_ftp_source_has_the_login_set_rotate_and_senders(client, _isolated_db):
    _register_ftp(client, allowed_senders="203.0.113.9")
    with _isolated_db() as session:
        source = provider_sources.list_all(session)[0]
        source_id = source.id
    from sqlalchemy import select

    from providerharvest_service.models import FtpAccount

    with _isolated_db() as session:
        account = session.scalar(select(FtpAccount))
        account_id, username = account.id, account.username
    page = client.get("/provider/sources/%s/credentials" % source_id).text
    assert "<code>%s</code>" % username in page and 'href="/admin/ftp/%s/set-password"' % account_id in page
    rotate = page.split('action="/admin/ftp/%s/rotate-password"' % account_id, 1)[1].split("</form>", 1)[0]
    assert "return confirm(" in rotate and "A password they chose themselves is replaced too" in rotate
    assert "cannot be shown" in page and "<code>203.0.113.9/32</code>" in page
    assert "rotate-key" not in page  # an HTTPS-only control


def test_the_credentials_page_says_the_blacklist_still_applies_and_links_to_it(client, _isolated_db):
    source_id, _ = _register_and_activate_push_source(client, _isolated_db)
    page = client.get("/provider/sources/%s/credentials" % source_id).text
    assert "blacklist and whitelist" in page and "/admin/security/failed-logins#address-control" in page


def test_the_credentials_page_shows_activity_and_works_for_a_pending_source(client, _isolated_db):
    client.post("/provider/sources/new", data={"name": "acme-push", "title": "t", "transport_type": "push"})
    with _isolated_db() as session:
        source_id = provider_sources.list_all(session)[0].id
    resp = client.get("/provider/sources/%s/credentials" % source_id)  # still pending: the page works before approval
    assert resp.status_code == 200 and "nothing received yet" in resp.text and "pending" in resp.text.lower()


def test_credentials_for_a_pull_source_an_unknown_source_or_without_the_login_go_nowhere(client, _isolated_db):
    from fastapi.testclient import TestClient

    from providerharvest_service.app import app

    client.post("/provider/sources/new", data={
        "name": "a-pull-source", "title": "p", "transport_type": "http", "endpoint_url": "https://x.example.gr/a",
        "frequency": "DAILY", "auth_type": "api_key", "api_key": "k"})
    with _isolated_db() as session:
        pull_id = provider_sources.list_all(session)[0].id
    assert "flash_kind=error" in client.get("/provider/sources/%s/credentials" % pull_id, follow_redirects=False).headers["location"]
    assert "flash_kind=error" in client.get("/provider/sources/no-such/credentials", follow_redirects=False).headers["location"]
    anonymous = TestClient(app, base_url="https://testserver")
    resp = anonymous.get("/provider/sources/%s/credentials" % pull_id, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login")


def test_the_page_never_shows_a_secret(client, _isolated_db):
    source_id, api_key = _register_and_activate_push_source(client, _isolated_db)
    page = client.get("/provider/sources/%s/credentials" % source_id).text
    assert api_key not in page
    config = _config(_isolated_db)
    assert config["push_api_key_hash"] not in page


# -- FTP/SFTP push: the list becomes the account's login limit --------------------------------

def _ftp_account(_isolated_db):
    from sqlalchemy import select

    from providerharvest_service.models import FtpAccount

    with _isolated_db() as session:
        account = session.scalar(select(FtpAccount))
        return account.username, sorted(a.cidr for a in account.allowed_ips), account.managed_source_id


def _register_ftp(client, **fields):
    data = {"name": "acme-push", "title": "t", "transport_type": "push", "push_method": "ftp"}
    data.update(fields)
    return client.post("/provider/sources/new", data=data)


def test_an_ftp_push_source_limits_its_account_to_the_listed_senders(client, _isolated_db):
    resp = _register_ftp(client, allowed_senders="203.0.113.9, 198.51.100.0/24")
    assert resp.status_code == 200 and "Logins accepted only from" in resp.text
    assert _ftp_account(_isolated_db)[1] == ["198.51.100.0/24", "203.0.113.9/32"]


def test_a_chosen_ftp_password_follows_the_same_rules_as_a_set_one(client, _isolated_db):
    for password, message in (("tiny", "at least 6"), ("x" * 80, "at most 72"), ("é" * 40, "at most 72")):
        resp = _register_ftp(client, push_ftp_password=password)
        assert resp.status_code == 400 and message in resp.text and password not in resp.text
    assert _register_ftp(client, push_ftp_password="a perfectly fine password").status_code == 200


def test_an_ftp_push_source_with_no_list_leaves_its_account_unrestricted(client, _isolated_db):
    _register_ftp(client)
    assert _ftp_account(_isolated_db)[1] == []


def test_editing_the_source_rewrites_its_accounts_limit(client, _isolated_db):
    _register_ftp(client, allowed_senders="203.0.113.9")
    with _isolated_db() as session:
        source_id = provider_sources.list_all(session)[0].id

    _edit(client, source_id, allowed_senders="198.51.100.7, 192.0.2.0/24")
    assert _ftp_account(_isolated_db)[1] == ["192.0.2.0/24", "198.51.100.7/32"]  # old one gone, new ones in
    _edit(client, source_id, allowed_senders="")
    assert _ftp_account(_isolated_db)[1] == []


def test_the_box_receives_the_limit_via_the_sync_endpoint(client, _isolated_db, monkeypatch):
    monkeypatch.setenv("LOADER_FTP_SYNC_KEY", "box-key")
    _register_ftp(client, allowed_senders="203.0.113.9")
    with _isolated_db() as session:
        provider_sources.activate(session, provider_sources.list_all(session)[0].id)  # accounts sync only once approved
    synced = client.get("/admin/ftp-accounts-sync", headers={"X-Ftp-Sync-Key": "box-key"}).json()
    assert [(a["username"], a["allowed_ips"]) for a in synced] == [("acme-push", ["203.0.113.9/32"])]


def test_the_ftp_box_page_shows_a_provider_accounts_list_read_only_and_refuses_edits(client, _isolated_db):
    _register_ftp(client, allowed_senders="203.0.113.9")
    username, _, managed = _ftp_account(_isolated_db)
    assert managed  # a provider's own account
    page = client.get("/admin/ftp").text
    assert "<code>203.0.113.9/32</code>" in page and "set on the source" in page
    assert "/allowed-ips" not in page  # no form to change it here

    from sqlalchemy import select

    from providerharvest_service.models import FtpAccount

    with _isolated_db() as session:
        account_id = session.scalar(select(FtpAccount.id))
    resp = client.post("/admin/ftp/%s/allowed-ips" % account_id, data={"cidr": "8.8.8.8"}, follow_redirects=False)
    assert "flash_kind=error" in resp.headers["location"]
    assert _ftp_account(_isolated_db)[1] == ["203.0.113.9/32"]


def test_deleting_the_source_takes_its_limit_with_it(client, _isolated_db):
    _register_ftp(client, allowed_senders="203.0.113.9")
    with _isolated_db() as session:
        provider_sources.delete(session, provider_sources.list_all(session)[0].id)
    from sqlalchemy import select

    from providerharvest_service.models import FtpAllowedIp

    with _isolated_db() as session:
        assert session.scalar(select(FtpAllowedIp)) is None
