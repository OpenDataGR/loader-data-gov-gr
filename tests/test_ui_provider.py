import base64
import io
import os
import re

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from providerharvest_service import connection_check, db, harvest_db
from providerharvest_service.app import app
from providerharvest_service.engine.transport.base import Entry, Page, Transport
from providerharvest_service.harvest_models import HarvestBase

ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


@pytest.fixture(autouse=True)
def _admin_session_env(monkeypatch):
    # Several tests here activate a source via the admin route
    # (/admin/sources/{id}/activate) as setup, not because they're
    # testing the admin UI itself -- now that route sits behind the
    # session gate too, so it needs a real logged-in session the same
    # as any other admin route.
    monkeypatch.setenv("LOADER_ADMIN_UI_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv(
        "LOADER_ADMIN_UI_PASSWORD_HASH",
        bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode(),
    )
    monkeypatch.setenv("LOADER_ADMIN_SESSION_SECRET", "test-session-secret")


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
def client(_admin_session_env):
    c = TestClient(app, base_url="https://testserver")
    c.post("/admin/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/admin/sources"})
    return c


def _valid_form(**overrides):
    form = {
        "name": "acme-open-data",
        "title": "Acme open data",
        "endpoint_url": "https://provider.example.gr/api/records",
        "transport_type": "http",
        "frequency": "DAILY",
        "auth_type": "api_key",
        "api_key": "secret-key-123",
    }
    form.update(overrides)
    return form


def test_new_source_form_renders(client):
    resp = client.get("/provider/sources/new")
    assert resp.status_code == 200
    assert "Register a new source" in resp.text


def test_registering_a_source_redirects_to_the_list(client):
    resp = client.post("/provider/sources/new", data=_valid_form(), follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/provider/sources?flash=")

    listing = client.get(resp.headers["location"])
    assert "acme-open-data" in listing.text
    assert "pending" in listing.text


def test_registering_with_missing_required_field_reshows_the_form_with_errors(client):
    resp = client.post("/provider/sources/new", data=_valid_form(api_key=""))
    assert resp.status_code == 400
    assert "Please fix the errors below" in resp.text
    # The rest of what was typed must be preserved, not lost.
    assert "acme-open-data" in resp.text


def test_pause_and_resume_flow(client, _isolated_db):
    session_factory = _isolated_db
    client.post("/provider/sources/new", data=_valid_form())

    with session_factory() as session:
        from providerharvest_service import provider_sources
        source = provider_sources.list_all(session)[0]
        provider_sources.activate(session, source.id)
        source_id = source.id

    resp = client.post("/provider/sources/%s/pause" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    listing = client.get("/provider/sources")
    assert "paused" in listing.text

    client.post("/provider/sources/%s/resume" % source_id)
    listing = client.get("/provider/sources")
    assert "active" in listing.text


class _FakeTransport(Transport):
    def __init__(self, entries=None, error=None, files=None):
        self._entries = entries or []
        self._error = error
        self._files = files or {}  # ref -> bytes, for file-transport tests

    def connect(self):
        if self._error:
            raise self._error

    def list_entries(self, cursor):
        return Page(entries=self._entries, next_cursor=None)

    def open_entry(self, entry):
        if entry.ref not in self._files:
            raise NotImplementedError
        return io.BytesIO(self._files[entry.ref])

    def close(self):
        pass


def test_check_connection_shows_reachable_health_badge(client, monkeypatch):
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda source, secret: _FakeTransport(entries=[Entry(ref="r1")]),
    )
    client.post("/provider/sources/new", data=_valid_form())
    listing = client.get("/provider/sources")
    source_id = re.search(r"/provider/sources/([^/]+)/check-connection", listing.text).group(1)

    resp = client.post("/provider/sources/%s/check-connection" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash_kind=success" in resp.headers["location"]

    listing = client.get("/provider/sources")
    assert "reachable" in listing.text


def test_check_connection_shows_unreachable_health_badge_on_failure(client, monkeypatch):
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda source, secret: _FakeTransport(error=ConnectionRefusedError("refused")),
    )
    client.post("/provider/sources/new", data=_valid_form())
    listing = client.get("/provider/sources")
    source_id = re.search(r"/provider/sources/([^/]+)/check-connection", listing.text).group(1)

    resp = client.post("/provider/sources/%s/check-connection" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]

    listing = client.get("/provider/sources")
    assert "unreachable" in listing.text


def test_flash_message_with_special_characters_does_not_break_redirect(client):
    resp = client.post("/provider/sources/%s/pause" % "no-such-id", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/provider/sources?flash=")
    followed = client.get(location)
    assert followed.status_code == 200
    assert "No such source." in followed.text


def _register_and_get_id(client):
    client.post("/provider/sources/new", data=_valid_form())
    listing = client.get("/provider/sources")
    return re.search(r"/provider/sources/([^/]+)/check-connection", listing.text).group(1)


def test_edit_form_renders_prefilled_without_the_secret(client):
    source_id = _register_and_get_id(client)
    resp = client.get("/provider/sources/%s/edit" % source_id)
    assert resp.status_code == 200
    assert "Edit source" in resp.text
    assert "acme-open-data" in resp.text
    # The stored API key must never come back to the browser.
    assert "secret-key-123" not in resp.text


def test_editing_updates_title_and_keeps_working(client, monkeypatch):
    source_id = _register_and_get_id(client)

    resp = client.post(
        "/provider/sources/%s/edit" % source_id,
        data={
            "title": "Renamed",
            "endpoint_url": "https://provider.example.gr/api/records",
            "transport_type": "http",
            "frequency": "DAILY",
            "auth_type": "api_key",
            "api_key": "",  # left blank -- keep the existing key
            "api_key_location": "header",
            "api_key_name": "X-API-Key",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "flash=Source+updated" in resp.headers["location"]

    listing = client.get("/provider/sources")
    assert "Renamed" in listing.text

    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda source, secret: _FakeTransport(entries=[Entry(ref="r1")]),
    )
    check = client.post("/provider/sources/%s/check-connection" % source_id, follow_redirects=False)
    assert "flash_kind=success" in check.headers["location"]


def test_editing_with_validation_error_reshows_form(client):
    source_id = _register_and_get_id(client)
    resp = client.post(
        "/provider/sources/%s/edit" % source_id,
        data={"title": "", "endpoint_url": "https://x", "transport_type": "http", "auth_type": "api_key"},
    )
    assert resp.status_code == 400
    assert "Edit source" in resp.text


def test_editing_unknown_source_redirects_with_error(client):
    resp = client.get("/provider/sources/no-such-id/edit", follow_redirects=False)
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]


def test_delete_removes_source_from_list(client):
    source_id = _register_and_get_id(client)
    resp = client.post("/provider/sources/%s/delete" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash=Source+deleted" in resp.headers["location"]

    listing = client.get("/provider/sources")
    assert "acme-open-data" not in listing.text


def test_delete_unknown_source_shows_not_found(client):
    resp = client.post("/provider/sources/no-such-id/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]


def _register_activate_and_get_id(client):
    source_id = _register_and_get_id(client)
    client.post("/admin/sources/%s/activate" % source_id)
    return source_id


def test_harvest_now_stores_records_and_shows_them(client, monkeypatch):
    source_id = _register_activate_and_get_id(client)
    entries = [Entry(ref="r1", inline_data=b'{"id": 1}')]
    monkeypatch.setattr(connection_check, "_build_transport", lambda src, secret: _FakeTransport(entries=entries))

    resp = client.post("/provider/sources/%s/harvest-now" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash=Harvest+complete" in resp.headers["location"]

    runs_page = client.get("/provider/sources/%s/runs" % source_id)
    assert resp.status_code == 303
    assert "success" in runs_page.text

    run_id = re.search(r"/runs/([^\"]+)\"", runs_page.text).group(1)
    detail = client.get("/provider/sources/%s/runs/%s" % (source_id, run_id))
    assert '"id": 1' in detail.text


def test_harvest_now_records_failure_without_500(client, monkeypatch):
    source_id = _register_activate_and_get_id(client)
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda src, secret: _FakeTransport(error=ConnectionRefusedError("refused")),
    )

    resp = client.post("/provider/sources/%s/harvest-now" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]

    runs_page = client.get("/provider/sources/%s/runs" % source_id)
    assert "error" in runs_page.text


def test_harvest_now_refuses_a_pending_source(client):
    source_id = _register_and_get_id(client)  # never activated
    resp = client.post("/provider/sources/%s/harvest-now" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]


def test_runs_page_empty_state(client):
    source_id = _register_activate_and_get_id(client)
    resp = client.get("/provider/sources/%s/runs" % source_id)
    assert resp.status_code == 200
    assert "No harvest runs yet" in resp.text


def _valid_sftp_form(**overrides):
    form = {
        "name": "acme-sftp", "title": "Acme SFTP", "endpoint_url": "sftp.example.gr",
        "transport_type": "sftp", "frequency": "DAILY",
        "sftp_username": "u", "sftp_password": "p", "host_key_fingerprint": "SHA256:abcdef",
    }
    form.update(overrides)
    return form


def _register_activate_sftp_and_get_id(client):
    client.post("/provider/sources/new", data=_valid_sftp_form())
    listing = client.get("/provider/sources")
    source_id = re.search(r"/provider/sources/([^/]+)/check-connection", listing.text).group(1)
    client.post("/admin/sources/%s/activate" % source_id)
    return source_id


def test_harvest_now_stores_and_serves_whole_files_for_an_sftp_source(client, monkeypatch):
    source_id = _register_activate_sftp_and_get_id(client)
    entries = [Entry(ref="/incoming/data.csv", metadata={"size": 12})]
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda src, secret: _FakeTransport(entries=entries, files={"/incoming/data.csv": b"a,b\n1,2\n"}),
    )

    resp = client.post("/provider/sources/%s/harvest-now" % source_id, follow_redirects=False)
    assert resp.status_code == 303
    assert "flash=Harvest+complete" in resp.headers["location"]

    runs_page = client.get("/provider/sources/%s/runs" % source_id)
    assert "success" in runs_page.text
    run_id = re.search(r"/runs/([^\"]+)\"", runs_page.text).group(1)

    detail = client.get("/provider/sources/%s/runs/%s" % (source_id, run_id))
    assert "data.csv" in detail.text
    assert "Harvested files" in detail.text

    file_id = re.search(r"/runs/%s/files/([^\"]+)\"" % re.escape(run_id), detail.text).group(1)
    download = client.get("/provider/sources/%s/runs/%s/files/%s" % (source_id, run_id, file_id))
    assert download.status_code == 200
    assert download.content == b"a,b\n1,2\n"
    assert download.headers["content-type"].startswith("text/csv")
    assert 'filename="data.csv"' in download.headers["content-disposition"]


def test_file_download_404s_for_a_file_from_a_different_run(client, monkeypatch):
    source_id = _register_activate_sftp_and_get_id(client)
    monkeypatch.setattr(
        connection_check, "_build_transport",
        lambda src, secret: _FakeTransport(entries=[Entry(ref="/f.txt")], files={"/f.txt": b"hi"}),
    )
    client.post("/provider/sources/%s/harvest-now" % source_id)
    runs_page = client.get("/provider/sources/%s/runs" % source_id)
    run_id = re.search(r"/runs/([^\"]+)\"", runs_page.text).group(1)
    detail = client.get("/provider/sources/%s/runs/%s" % (source_id, run_id))
    file_id = re.search(r"/runs/%s/files/([^\"]+)\"" % re.escape(run_id), detail.text).group(1)

    resp = client.get("/provider/sources/%s/runs/not-the-real-run/files/%s" % (source_id, file_id))
    assert resp.status_code == 404


def test_file_download_404s_for_an_unknown_file_id(client):
    source_id = _register_activate_sftp_and_get_id(client)
    resp = client.get("/provider/sources/%s/runs/some-run/files/no-such-file" % source_id)
    assert resp.status_code == 404


# -- pull vs push, and push via FTP/SFTP credentials -----------------------


def _ftp_push_form(**overrides):
    form = {"name": "acme-push", "title": "Acme push", "transport_type": "push", "push_method": "ftp"}
    form.update(overrides)
    return form


def _ftp_account(session_factory, username):
    from sqlalchemy import select

    from providerharvest_service.models import FtpAccount

    with session_factory() as session:
        return session.scalar(select(FtpAccount).where(FtpAccount.username == username))


def test_new_source_form_offers_pull_and_push_and_both_push_methods(client):
    text = client.get("/provider/sources/new").text
    assert 'id="kind-pull"' in text and 'id="kind-push"' in text
    assert 'id="push-method-https"' in text and 'id="push-method-ftp"' in text
    # Push is a kind, not one more connector card next to http/sftp/scp/ftp.
    assert 'name="transport_type" value="push" onchange' not in text


def test_registering_an_ftp_push_source_shows_the_login_and_server_once(client, _isolated_db, monkeypatch):
    monkeypatch.setenv("LOADER_FTP_PUBLIC_HOST", "ftp.example.gr")
    resp = client.post("/provider/sources/new", data=_ftp_push_form())
    assert resp.status_code == 200
    assert "ftp.example.gr" in resp.text
    assert 'value="acme-push"' in resp.text  # the username
    assert "sftp -P 2222 acme-push@ftp.example.gr" in resp.text

    # The generated password is on this page and nowhere else.
    import re
    password = re.search(r'<label>Password</label>\s*<input[^>]*value="([^"]+)"', resp.text).group(1)
    assert len(password) >= 20
    assert password not in client.get("/provider/sources").text
    assert password not in client.get("/admin/ftp").text

    account = _ftp_account(_isolated_db, "acme-push")
    assert account.enabled is False
    assert password not in account.password_hash


def test_ftp_push_credentials_page_says_so_when_the_server_address_is_not_configured(client, monkeypatch):
    monkeypatch.delenv("LOADER_FTP_PUBLIC_HOST", raising=False)
    resp = client.post("/provider/sources/new", data=_ftp_push_form())
    assert resp.status_code == 200
    assert "isn't configured yet" in resp.text
    assert "sftp -P" not in resp.text  # no example built around a missing host


def test_a_password_the_provider_chose_is_never_echoed_back(client):
    resp = client.post(
        "/provider/sources/new", data=_ftp_push_form(push_ftp_password="a-long-enough-secret")
    )
    assert resp.status_code == 200
    assert "a-long-enough-secret" not in resp.text
    assert "You chose your own password" in resp.text


def test_a_rejected_ftp_push_registration_never_echoes_the_typed_password(client):
    resp = client.post("/provider/sources/new", data=_ftp_push_form(push_ftp_password="short"))
    assert resp.status_code == 400
    assert "at least 12 characters" in resp.text
    assert 'value="short"' not in resp.text
    assert "acme-push" in resp.text  # everything else that was typed is kept


def test_approving_the_source_enables_the_ftp_login(client, _isolated_db):
    client.post("/provider/sources/new", data=_ftp_push_form())
    assert _ftp_account(_isolated_db, "acme-push").enabled is False

    with _isolated_db() as session:
        from providerharvest_service import provider_sources
        source_id = provider_sources.list_all(session)[0].id

    client.post("/admin/sources/%s/activate" % source_id)
    assert _ftp_account(_isolated_db, "acme-push").enabled is True


def test_push_source_ignores_a_submitted_frequency(client, _isolated_db):
    client.post("/provider/sources/new", data=_ftp_push_form(frequency="DAILY"))
    with _isolated_db() as session:
        from providerharvest_service import provider_sources
        assert provider_sources.list_all(session)[0].frequency == "MANUAL"


def test_editing_a_push_source_locks_its_kind_and_method(client, _isolated_db):
    client.post("/provider/sources/new", data=_ftp_push_form())
    with _isolated_db() as session:
        from providerharvest_service import provider_sources
        source_id = provider_sources.list_all(session)[0].id

    text = client.get("/provider/sources/%s/edit" % source_id).text
    assert "This can't be changed once registered" in text
    assert "can't be changed here -- ask an admin to issue a new password" in text
    assert 'name="push_ftp_password"' not in text  # no password field to edit


def test_sftp_and_scp_share_one_connector_card_with_a_protocol_switch(client):
    text = client.get("/provider/sources/new").text
    assert text.count('name="connector" value=') == 3  # HTTP, SFTP / SCP, FTP
    assert 'name="connector" value="ssh"' in text
    assert 'name="connector" value="scp"' not in text
    assert 'id="ssh-protocol-sftp"' in text and 'id="ssh-protocol-scp"' in text


def test_a_rejected_scp_registration_reshows_the_form_with_scp_still_selected(client):
    resp = client.post("/provider/sources/new", data={
        "name": "acme-scp", "title": "Acme SCP", "endpoint_url": "scp.example.gr",
        "transport_type": "scp", "sftp_username": "",  # missing -> validation error
    })
    assert resp.status_code == 400
    import re
    assert re.search(r'id="transport-type-pull"[^>]*value="scp"', resp.text)
    assert re.search(r'id="ssh-protocol-scp"[^>]*checked', resp.text, re.S)
    assert re.search(r'name="connector" value="ssh"[^>]*checked', resp.text, re.S)


def test_registering_scp_through_the_form_stores_scp(client, _isolated_db):
    resp = client.post("/provider/sources/new", data={
        "name": "acme-scp", "title": "Acme SCP", "endpoint_url": "scp.example.gr",
        "transport_type": "scp", "sftp_username": "acme", "sftp_password": "hunter2",
        "host_key_fingerprint": "SHA256:abcdef",
    }, follow_redirects=False)
    assert resp.status_code == 303
    with _isolated_db() as session:
        from providerharvest_service import provider_sources
        assert provider_sources.list_all(session)[0].transport_type == "scp"


def test_forms_show_the_system_wide_size_limit_as_information_not_an_input(client):
    text = client.get("/provider/sources/new").text
    assert text.count("Maximum file size: <strong>100 MB</strong>") == 3  # sftp/scp, ftp and push
    assert "max_file_size_bytes" not in text  # nothing editable is submitted


def test_human_size_formats_the_limit_the_way_it_is_quoted():
    from providerharvest_service.ui_provider import _human_size

    assert _human_size(100 * 1000 * 1000) == "100 MB"
    assert _human_size(20 * 1000 * 1000) == "20 MB"
    assert _human_size(1500) == "1.5 KB"
    assert _human_size(999) == "999 B"
