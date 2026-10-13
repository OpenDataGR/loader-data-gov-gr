import base64
import json
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


def test_register_defaults_rate_limits_when_not_specified(session):
    import json
    source = provider_sources.register(session, _http_form())
    config = json.loads(source.config)
    assert config["max_requests_per_minute"] == 60
    assert config["proxy_rate_limit_per_ip"] == 30


def test_register_respects_custom_rate_limits(session):
    import json
    source = provider_sources.register(session, _http_form(
        max_requests_per_minute="120", proxy_rate_limit_per_ip="10",
    ))
    config = json.loads(source.config)
    assert config["max_requests_per_minute"] == 120
    assert config["proxy_rate_limit_per_ip"] == 10


def test_register_rejects_non_numeric_rate_limit(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _http_form(max_requests_per_minute="lots"))
    assert "max_requests_per_minute" in exc_info.value.errors


def test_register_rejects_zero_or_negative_rate_limit(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _http_form(proxy_rate_limit_per_ip="0"))
    assert "proxy_rate_limit_per_ip" in exc_info.value.errors


def test_register_rejects_non_numeric_sftp_port_without_crashing(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _sftp_form(sftp_port="not-a-port"))
    assert "sftp_port" in exc_info.value.errors


def test_register_rejects_non_numeric_ftp_port_without_crashing(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _ftp_form(ftp_port="not-a-port"))
    assert "ftp_port" in exc_info.value.errors


def test_register_defaults_http_engine_limits_when_not_specified(session):
    import json
    source = provider_sources.register(session, _http_form())
    config = json.loads(source.config)
    assert config["request_timeout_s"] == 30
    assert config["max_pages"] == 1000
    assert config["max_retries"] == 5


def test_register_respects_custom_http_engine_limits(session):
    import json
    source = provider_sources.register(session, _http_form(
        request_timeout_s="10", max_pages="50", max_retries="2",
    ))
    config = json.loads(source.config)
    assert config["request_timeout_s"] == 10
    assert config["max_pages"] == 50
    assert config["max_retries"] == 2


@pytest.mark.parametrize("field", ["request_timeout_s", "max_pages", "max_retries"])
def test_register_rejects_non_numeric_http_engine_limits(session, field):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _http_form(**{field: "lots"}))
    assert field in exc_info.value.errors


def test_register_defaults_ssh_connect_timeout(session):
    import json
    source = provider_sources.register(session, _sftp_form())
    assert json.loads(source.config)["connect_timeout_s"] == 30


def test_register_respects_custom_ssh_connect_timeout(session):
    import json
    source = provider_sources.register(session, _sftp_form(sftp_connect_timeout_s="5"))
    assert json.loads(source.config)["connect_timeout_s"] == 5


def test_register_rejects_non_numeric_ssh_connect_timeout(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _sftp_form(sftp_connect_timeout_s="lots"))
    assert "sftp_connect_timeout_s" in exc_info.value.errors


def test_register_defaults_ftp_connect_timeout(session):
    import json
    source = provider_sources.register(session, _ftp_form(ftp_use_tls="on"))
    assert json.loads(source.config)["connect_timeout_s"] == 30


def test_register_respects_custom_ftp_connect_timeout(session):
    import json
    source = provider_sources.register(session, _ftp_form(ftp_use_tls="on", ftp_connect_timeout_s="5"))
    assert json.loads(source.config)["connect_timeout_s"] == 5


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


def test_update_changes_title_without_touching_credentials(session):
    source = provider_sources.register(session, _http_form())
    original_message = provider_sources._secrets_backend(session).get(source.id).fields

    edit_form = provider_sources.config_to_form_fields(source)
    edit_form["title"] = "Renamed title"
    # Credential fields stay blank -- config_to_form_fields never
    # includes them.
    updated = provider_sources.update(session, source.id, edit_form)

    assert updated.title == "Renamed title"
    assert updated.name == source.name  # immutable
    kept = provider_sources._secrets_backend(session).get(source.id).fields
    assert kept == original_message


def test_update_rotates_credential_when_a_new_value_is_typed(session):
    source = provider_sources.register(session, _http_form(api_key="old-key"))

    edit_form = provider_sources.config_to_form_fields(source)
    edit_form["api_key"] = "new-key"
    provider_sources.update(session, source.id, edit_form)

    bundle = provider_sources._secrets_backend(session).get(source.id)
    assert bundle.fields == {"api_key": "new-key"}


def test_update_preserves_non_secret_ssh_config(session):
    source = provider_sources.register(session, _sftp_form())
    edit_form = provider_sources.config_to_form_fields(source)
    edit_form["sftp_remote_path"] = "/new-path"
    updated = provider_sources.update(session, source.id, edit_form)

    import json
    config = json.loads(updated.config)
    assert config["remote_path"] == "/new-path"
    # Credential (username/password) untouched.
    bundle = provider_sources._secrets_backend(session).get(source.id)
    assert bundle.fields["username"] == "acme"
    assert bundle.fields["password"] == "hunter2"


def test_update_switching_auth_type_requires_full_new_credential(session):
    source = provider_sources.register(session, _http_form(auth_type="api_key", api_key="k"))
    edit_form = provider_sources.config_to_form_fields(source)
    edit_form["auth_type"] = "basic_auth"
    # No basic_username/basic_password supplied -- the old api_key
    # secret's shape doesn't apply to basic_auth, so this must fail
    # validation rather than silently reusing the old value somehow.
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.update(session, source.id, edit_form)
    assert "credential_fields" in exc_info.value.errors


def test_update_rejects_missing_title(session):
    source = provider_sources.register(session, _http_form())
    edit_form = provider_sources.config_to_form_fields(source)
    edit_form["title"] = ""
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.update(session, source.id, edit_form)
    assert "title" in exc_info.value.errors


def test_update_unknown_source_raises_not_found(session):
    with pytest.raises(provider_sources.NotFound):
        provider_sources.update(session, "no-such-id", _http_form())


def test_delete_removes_the_source(session):
    source = provider_sources.register(session, _http_form())
    provider_sources.delete(session, source.id)
    with pytest.raises(provider_sources.NotFound):
        provider_sources.get_or_404(session, source.id)
    assert provider_sources.list_all(session) == []


def test_delete_unknown_source_raises_not_found(session):
    with pytest.raises(provider_sources.NotFound):
        provider_sources.delete(session, "no-such-id")


def test_delete_works_regardless_of_status(session):
    source = provider_sources.register(session, _http_form())
    provider_sources.activate(session, source.id)
    provider_sources.delete(session, source.id)
    assert provider_sources.list_all(session) == []


# -- push mode --------------------------------------------------------


def _push_form(**overrides):
    form = {"name": "acme-push", "title": "Acme push", "transport_type": "push", "frequency": "MANUAL"}
    form.update(overrides)
    return form


def test_register_push_source_needs_no_endpoint_url(session):
    source = provider_sources.register(session, _push_form())
    assert source.transport_type == "push"
    assert source.endpoint_url == ""


def test_register_push_source_exposes_the_plaintext_key_once(session):
    source = provider_sources.register(session, _push_form())
    assert isinstance(source.generated_api_key, str)
    assert len(source.generated_api_key) > 20


def test_register_push_source_never_persists_the_plaintext_key(session):
    import bcrypt

    source = provider_sources.register(session, _push_form())
    config = json.loads(source.config)
    assert "push_api_key_hash" in config
    assert config["push_api_key_hash"] != source.generated_api_key
    assert bcrypt.checkpw(source.generated_api_key.encode("utf-8"), config["push_api_key_hash"].encode("ascii"))

    # And the envelope-encrypted secret (used by every other transport
    # for its own outbound credentials) is genuinely empty for push --
    # there's nothing for this service to decrypt-and-use, the key is
    # only ever verified, never read back.
    assert provider_sources.credentials(session, source.id).fields == {}


def test_the_max_file_size_is_system_wide_not_stored_per_source(session):
    # A per-source value would only be a promise the upload path in front
    # of the app (nginx, the WAF) can't keep -- so it isn't accepted or stored.
    for form in (_push_form(push_max_file_size_bytes="1000"), _sftp_form(sftp_max_file_size_bytes="1000")):
        source = provider_sources.register(session, form)
        assert "max_file_size_bytes" not in json.loads(source.config)


def test_config_to_form_fields_never_leaks_the_key_hash(session):
    source = provider_sources.register(session, _push_form())
    data = provider_sources.config_to_form_fields(source)
    assert "push_api_key_hash" not in data
    assert "push_max_file_size_bytes" not in data


def test_a_non_push_source_never_gets_a_generated_api_key(session):
    source = provider_sources.register(session, _http_form())
    assert getattr(source, "generated_api_key", None) is None


def test_editing_a_push_source_keeps_the_existing_key_hash(session):
    source = provider_sources.register(session, _push_form())
    original_hash = json.loads(source.config)["push_api_key_hash"]

    edit_form = provider_sources.config_to_form_fields(source)
    edit_form["title"] = "Renamed"
    updated = provider_sources.update(session, source.id, edit_form)

    assert updated.title == "Renamed"
    assert json.loads(updated.config)["push_api_key_hash"] == original_hash


def test_switching_an_existing_source_to_push_via_edit_is_rejected(session):
    source = provider_sources.register(session, _http_form())
    edit_form = provider_sources.config_to_form_fields(source)
    edit_form["transport_type"] = "push"
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.update(session, source.id, edit_form)
    assert "transport_type" in exc_info.value.errors


# -- push via FTP/SFTP credentials (push_method="ftp") ---------------------


def _ftp_push_form(**overrides):
    return _push_form(push_method="ftp", **overrides)


def _ftp_account(session, username):
    from sqlalchemy import select

    from providerharvest_service.models import FtpAccount

    return session.scalar(select(FtpAccount).where(FtpAccount.username == username))


@pytest.fixture(autouse=True)
def _upload_root(monkeypatch):
    monkeypatch.setenv("LOADER_FTP_UPLOAD_ROOT", "/srv/ftp")


def test_register_ftp_push_source_creates_a_disabled_managed_account(session):
    source = provider_sources.register(session, _ftp_push_form())

    account = _ftp_account(session, "acme-push")
    assert account is not None
    assert account.enabled is False  # nothing may log in before an admin approves the source
    assert account.managed_source_id == source.id
    assert account.push_target_source_name == "acme-push"
    assert account.home_directory == "/srv/ftp/acme-push"


def test_register_ftp_push_source_has_no_frequency_and_no_api_key(session):
    source = provider_sources.register(session, _ftp_push_form(frequency="DAILY"))
    assert source.frequency == "MANUAL"  # a push source has no schedule
    assert getattr(source, "generated_api_key", None) is None
    assert json.loads(source.config)["push_method"] == "ftp"
    assert "push_api_key_hash" not in json.loads(source.config)


def test_register_ftp_push_source_generates_a_password_and_shows_it_once(session):
    import bcrypt

    source = provider_sources.register(session, _ftp_push_form())
    revealed = source.generated_ftp
    assert revealed["username"] == "acme-push"
    assert len(revealed["password"]) >= 20

    account = _ftp_account(session, "acme-push")
    assert account.password_hash != revealed["password"]
    assert bcrypt.checkpw(revealed["password"].encode("utf-8"), account.password_hash.encode("ascii"))
    # ProFTPD's mod_sql_passwd only accepts $2a$ (see ftp_credentials.hash_password).
    assert account.password_hash.startswith("$2a$")


def test_register_ftp_push_source_accepts_a_chosen_username_and_password(session):
    import bcrypt

    source = provider_sources.register(
        session, _ftp_push_form(push_ftp_username="acme_ingest", push_ftp_password="a-long-enough-secret")
    )
    account = _ftp_account(session, "acme_ingest")
    assert account.home_directory == "/srv/ftp/acme_ingest"
    assert bcrypt.checkpw(b"a-long-enough-secret", account.password_hash.encode("ascii"))
    # A password the provider chose is never echoed back.
    assert source.generated_ftp["password"] is None
    assert source.generated_ftp["username"] == "acme_ingest"


def test_register_ftp_push_source_rejects_a_short_chosen_password(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _ftp_push_form(push_ftp_password="short"))
    assert "push_ftp_password" in exc_info.value.errors
    assert _ftp_account(session, "acme-push") is None  # nothing was created


def test_register_ftp_push_source_rejects_a_username_already_in_use(session):
    provider_sources.register(session, _ftp_push_form())
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _ftp_push_form(name="another-source", push_ftp_username="acme-push"))
    assert "push_ftp_username" in exc_info.value.errors


def test_register_ftp_push_source_rejects_an_invalid_username(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _ftp_push_form(push_ftp_username="bad name!"))
    assert "push_ftp_username" in exc_info.value.errors


def test_register_ftp_push_source_asks_for_a_username_when_the_name_is_too_long(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _ftp_push_form(name="a-source-name-that-is-far-too-long-for-a-username"))
    assert "push_ftp_username" in exc_info.value.errors


def test_register_ftp_push_source_rejects_an_unknown_push_method(session):
    with pytest.raises(provider_sources.ValidationError) as exc_info:
        provider_sources.register(session, _push_form(push_method="carrier-pigeon"))
    assert "push_method" in exc_info.value.errors


def test_activating_the_source_enables_its_account(session):
    source = provider_sources.register(session, _ftp_push_form())
    provider_sources.activate(session, source.id)
    assert _ftp_account(session, "acme-push").enabled is True


def test_pausing_and_resuming_the_source_disables_and_enables_its_account(session):
    source = provider_sources.register(session, _ftp_push_form())
    provider_sources.activate(session, source.id)

    provider_sources.pause(session, source.id)
    assert _ftp_account(session, "acme-push").enabled is False

    provider_sources.resume(session, source.id)
    assert _ftp_account(session, "acme-push").enabled is True


def test_rejecting_the_source_leaves_its_account_disabled(session):
    source = provider_sources.register(session, _ftp_push_form())
    provider_sources.reject(session, source.id, "not a real provider")
    assert _ftp_account(session, "acme-push").enabled is False


def test_deleting_the_source_deletes_its_account(session):
    source = provider_sources.register(session, _ftp_push_form())
    provider_sources.delete(session, source.id)
    assert _ftp_account(session, "acme-push") is None


def test_an_admin_created_account_is_not_touched_by_source_lifecycle(session):
    from providerharvest_service.models import FtpAccount

    source = provider_sources.register(session, _push_form())  # https push, no managed account
    session.add(FtpAccount(
        username="admin-made", password_hash="$2a$x", home_directory="/srv/ftp/admin-made",
        protocol="ftp", enabled=False, push_target_source_name=source.name,
    ))
    session.commit()

    provider_sources.activate(session, source.id)
    assert _ftp_account(session, "admin-made").enabled is False  # managed_source_id is NULL

    provider_sources.delete(session, source.id)
    assert _ftp_account(session, "admin-made") is not None


def test_editing_an_ftp_push_source_keeps_its_method_and_only_changes_what_was_edited(session):
    source = provider_sources.register(session, _ftp_push_form())

    edit_form = provider_sources.config_to_form_fields(source)
    assert edit_form["push_method"] == "ftp"
    edit_form["title"] = "Renamed"
    updated = provider_sources.update(session, source.id, edit_form)

    config = json.loads(updated.config)
    assert updated.title == "Renamed"
    assert config["push_method"] == "ftp"
    assert _ftp_account(session, "acme-push") is not None  # the account was not touched by the edit


def test_switching_between_sftp_and_scp_on_edit_keeps_the_stored_credentials(session):
    # Same credentials, same settings -- only the transport differs, so
    # flipping the protocol must not force the provider to retype a secret.
    source = provider_sources.register(session, _sftp_form())
    edit_form = provider_sources.config_to_form_fields(source)
    edit_form["transport_type"] = "scp"

    updated = provider_sources.update(session, source.id, edit_form)

    assert updated.transport_type == "scp"
    fields = provider_sources.credentials(session, source.id).fields
    assert fields["username"] == "acme"
    assert fields["password"] == "hunter2"


def test_editing_drops_a_per_source_size_left_over_from_before_the_cap_was_system_wide(session):
    source = provider_sources.register(session, _push_form())
    legacy = json.loads(source.config)
    legacy["max_file_size_bytes"] = 1234  # what an older registration stored
    source.config = json.dumps(legacy)
    session.commit()

    updated = provider_sources.update(session, source.id, provider_sources.config_to_form_fields(source))

    assert "max_file_size_bytes" not in json.loads(updated.config)
    assert "push_api_key_hash" in json.loads(updated.config)  # the key is untouched


def test_kind_and_push_method_are_derived_from_the_source(session):
    pull = provider_sources.register(session, _http_form())
    https_push = provider_sources.register(session, _push_form(name="https-push"))
    ftp_push = provider_sources.register(session, _ftp_push_form(name="ftp-push"))

    assert (pull.kind, pull.push_method) == ("pull", None)
    assert (https_push.kind, https_push.push_method) == ("push", "https")
    assert (ftp_push.kind, ftp_push.push_method) == ("push", "ftp")


def test_a_push_source_from_before_the_method_choice_counts_as_https(session):
    source = provider_sources.register(session, _push_form())
    source.config = json.dumps({"push_api_key_hash": "x"})  # what an older registration stored
    assert source.push_method == "https"
