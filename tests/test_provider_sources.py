import base64
import os

import pytest

from providerharvest_service import db, provider_sources


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


@pytest.fixture
def session(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "sources-test.db"))
    return db.make_session_factory(engine)()


def _http_form(**overrides):
    form = {
        "name": "acme-open-data",
        "title": "Acme open data",
        "endpoint_url": "https://provider.example.gr/api/records",
        "transport_type": "http",
        "frequency": "DAILY",
        "auth_type": "api_key",
        "api_key": "secret-key-123",
        "notification_email": "ops@acme.example.gr",
    }
    form.update(overrides)
    return form


def _sftp_form(**overrides):
    form = {
        "name": "acme-sftp",
        "title": "Acme SFTP drop",
        "endpoint_url": "sftp.example.gr",
        "transport_type": "sftp",
        "frequency": "DAILY",
        "sftp_username": "acme",
        "sftp_password": "hunter2",
        "host_key_fingerprint": "SHA256:abcdef",
    }
    form.update(overrides)
    return form


def _ftp_form(**overrides):
    form = {
        "name": "acme-ftp",
        "title": "Acme FTP drop",
        "endpoint_url": "ftp.example.gr",
        "transport_type": "ftp",
        "frequency": "DAILY",
        "ftp_username": "acme",
        "ftp_password": "hunter2",
    }
    form.update(overrides)
    return form


def test_register_http_source_is_pending_with_encrypted_credentials(session):
    source = provider_sources.register(session, _http_form())

    assert source.status == "pending"
    assert source.transport_type == "http"
    assert source.auth_type == "api_key"
    # The raw API key must never land in a plaintext column.
    assert b"secret-key-123" not in source.secret_ciphertext
    assert source.secret_ciphertext != b""

    backend = provider_sources._secrets_backend(session)
    bundle = backend.get(source.id)
    assert bundle.fields == {"api_key": "secret-key-123"}


def test_register_rejects_duplicate_name(session):
    provider_sources.register(session, _http_form())
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _http_form(title="Different title"))
    assert "name" in exc_info.value.errors


def test_register_rejects_invalid_name(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _http_form(name="Not A Valid Name!"))
    assert "name" in exc_info.value.errors


def test_register_http_requires_api_key_when_auth_type_is_api_key(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _http_form(api_key=""))
    assert "credential_fields" in exc_info.value.errors


def test_register_sftp_requires_host_key_fingerprint(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _sftp_form(host_key_fingerprint=""))
    assert "host_key_fingerprint" in exc_info.value.errors


def test_register_sftp_accepts_private_key_instead_of_password(session):
    source = provider_sources.register(
        session,
        _sftp_form(sftp_password="", sftp_private_key="-----BEGIN KEY-----"),
    )
    bundle = provider_sources._secrets_backend(session).get(source.id)
    assert bundle.fields["private_key_pem"] == "-----BEGIN KEY-----"
    assert "password" not in bundle.fields


def test_register_ftp_with_tls_checked_needs_no_acknowledgment(session):
    # Matches the real browser flow: the form pre-checks "Use FTPS", so
    # a user who submits without touching it sends ftp_use_tls=on.
    source = provider_sources.register(session, _ftp_form(ftp_use_tls="on"))
    assert source.status == "pending"


def test_register_ftp_with_use_tls_omitted_requires_explicit_acknowledgment(session):
    # An unchecked checkbox (or a raw API caller that omits the field
    # entirely) is indistinguishable from "not confirmed as TLS" -- must
    # fail safe into the acknowledgment gate, not silently assume TLS.
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _ftp_form())
    assert "plain_ftp_acknowledged" in exc_info.value.errors


def test_register_plain_ftp_requires_explicit_acknowledgment(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _ftp_form(ftp_plain_ftp_acknowledged=""))
    assert "plain_ftp_acknowledged" in exc_info.value.errors

    # Acknowledged explicitly -> allowed.
    source = provider_sources.register(
        session, _ftp_form(name="acme-ftp-plain", ftp_plain_ftp_acknowledged="on")
    )
    assert source.status == "pending"


def test_activate_flips_pending_to_active(session):
    source = provider_sources.register(session, _http_form())
    activated = provider_sources.activate(session, source.id)
    assert activated.status == "active"


def test_activate_twice_raises_invalid_transition(session):
    source = provider_sources.register(session, _http_form())
    provider_sources.activate(session, source.id)
    with pytest.raises(provider_sources.InvalidTransition):
        provider_sources.activate(session, source.id)


def test_reject_records_reason(session):
    source = provider_sources.register(session, _http_form())
    rejected = provider_sources.reject(session, source.id, "endpoint unreachable")
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "endpoint unreachable"


def test_pause_and_resume_round_trip(session):
    source = provider_sources.register(session, _http_form())
    provider_sources.activate(session, source.id)

    paused = provider_sources.pause(session, source.id)
    assert paused.status == "paused"

    resumed = provider_sources.resume(session, source.id)
    assert resumed.status == "active"


def test_pause_requires_active_status(session):
    source = provider_sources.register(session, _http_form())
    with pytest.raises(provider_sources.InvalidTransition):
        provider_sources.pause(session, source.id)


def test_list_pending_only_returns_pending_sources(session):
    a = provider_sources.register(session, _http_form())
    b = provider_sources.register(session, _http_form(name="acme-open-data-2"))
    provider_sources.activate(session, b.id)

    pending = provider_sources.list_pending(session)
    assert [s.id for s in pending] == [a.id]


def test_get_or_404_raises_not_found_for_unknown_id(session):
    with pytest.raises(provider_sources.NotFound):
        provider_sources.get_or_404(session, "no-such-id")
