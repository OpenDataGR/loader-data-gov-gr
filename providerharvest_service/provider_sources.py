"""Provider-source registration and approval-workflow logic.

The spec for the field shapes and validation rules here is
``ckanext-providerharvest``'s own ``logic/action.py`` and
``blueprints/provider_ui.py`` (see CLAUDE.md's "what to reuse" list) --
ported as *behavior*, not code: that plugin calls
``toolkit.get_action("harvest_source_create")`` etc. as in-process CKAN
calls and provisions a real CKAN package/DataStore resource on
approval. This service does neither (yet) -- everything here lives in
``ProviderSource``, this service's own table, independent of CKAN.
Approving a source just flips its status; provisioning it for real in
CKAN over the Action API is a later slice, added once a harvest run
actually needs the package/resource to exist.

Kept framework-agnostic (no FastAPI/Jinja2 imports) so both the
provider-facing and admin-facing UI route modules can share it without
either depending on the other.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service.engine.secrets.envelope import EnvelopeSecretsBackend
from providerharvest_service.models import ProviderSource, _new_id

TRANSPORT_TYPES = ("http", "sftp", "scp", "ftp")
SSH_TRANSPORT_TYPES = ("sftp", "scp")
FREQUENCIES = ("MANUAL", "DAILY", "WEEKLY", "MONTHLY")
HTTP_AUTH_TYPES = ("api_key", "basic_auth", "oauth2_client_credentials", "mtls")
STATUSES_ADMIN_MANAGES = ("pending", "active", "rejected", "paused")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,99}$")


class ValidationError(Exception):
    """Carries a field -> message dict, like the old blueprint's
    ``toolkit.ValidationError.error_dict`` -- so a form can re-render
    itself with per-field errors instead of one flat message."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join("%s: %s" % kv for kv in errors.items()))
        self.errors = errors


class NotFound(Exception):
    pass


class InvalidTransition(Exception):
    pass


def _parse_bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "on", "yes")


def _parse_host(endpoint_url: str) -> str:
    """SFTP/SCP/FTP sources reuse endpoint_url for the host (no separate
    "host" field) -- same tolerance the old plugin's blueprint had for
    either a bare hostname or a sftp://host[:port]-ish string."""
    if "://" in endpoint_url:
        return urlparse(endpoint_url).hostname or endpoint_url
    return endpoint_url.split("/")[0].split(":")[0]


class ProviderSourceSecretRepository:
    """Implements engine.secrets.envelope.SecretRepository directly
    against ProviderSource's own secret_* columns -- same pattern as
    ckan_tokens.CkanUserTokenRepository, keyed by this row's own id
    rather than a separate secret table (one credential per source, no
    rotation-across-multiple-refs need yet)."""

    def __init__(self, session: Session):
        self._session = session

    def create(self, harvest_source_id: str, ciphertext: bytes,
               data_key_wrapped: bytes, algorithm: str) -> str:
        row = self._session.get(ProviderSource, harvest_source_id)
        if row is None:
            raise LookupError("no such provider source %r" % harvest_source_id)
        row.secret_ciphertext = ciphertext
        row.secret_data_key_wrapped = data_key_wrapped
        row.secret_algorithm = algorithm
        self._session.commit()
        return harvest_source_id

    def get(self, secret_ref: str) -> dict:
        row = self._session.get(ProviderSource, secret_ref)
        if row is None:
            raise LookupError("no such provider source %r" % secret_ref)
        return {
            "ciphertext": row.secret_ciphertext,
            "data_key_wrapped": row.secret_data_key_wrapped,
            "algorithm": row.secret_algorithm,
            "harvest_source_id": row.id,
            "version": 1,
        }

    def update(self, secret_ref: str, ciphertext: bytes, data_key_wrapped: bytes) -> None:
        row = self._session.get(ProviderSource, secret_ref)
        if row is None:
            raise LookupError("no such provider source %r" % secret_ref)
        row.secret_ciphertext = ciphertext
        row.secret_data_key_wrapped = data_key_wrapped
        self._session.commit()

    def delete(self, secret_ref: str) -> None:
        pass  # deleting the ProviderSource row deletes these columns with it


def _secrets_backend(session: Session) -> EnvelopeSecretsBackend:
    return EnvelopeSecretsBackend(repository=ProviderSourceSecretRepository(session))


def _http_fields(form: dict, errors: dict) -> tuple[str, dict, dict]:
    """Returns (auth_type, credential_fields, config_extra) -- config_extra
    holds auth_opts + pagination, both non-secret. Mirrors the old
    blueprint's ``_http_auth_payload``."""
    auth_type = form.get("auth_type") or "api_key"
    if auth_type not in HTTP_AUTH_TYPES:
        errors["auth_type"] = "must be one of %s" % (HTTP_AUTH_TYPES,)
        auth_type = "api_key"

    if auth_type == "basic_auth":
        credential_fields = {
            "username": (form.get("basic_username") or "").strip(),
            "password": (form.get("basic_password") or "").strip(),
        }
        if not credential_fields["username"] or not credential_fields["password"]:
            errors["credential_fields"] = "basic_auth requires both username and password"
        auth_opts = {}
    elif auth_type == "oauth2_client_credentials":
        credential_fields = {
            "client_id": (form.get("oauth2_client_id") or "").strip(),
            "client_secret": (form.get("oauth2_client_secret") or "").strip(),
        }
        token_url = (form.get("oauth2_token_url") or "").strip()
        if not all(credential_fields.values()) or not token_url:
            errors["credential_fields"] = (
                "oauth2_client_credentials requires client_id, client_secret, and a token URL"
            )
        auth_opts = {"token_url": token_url, "scope": (form.get("oauth2_scope") or "").strip() or None}
    elif auth_type == "mtls":
        credential_fields = {
            "client_cert_pem": (form.get("mtls_client_cert") or "").strip(),
            "client_key_pem": (form.get("mtls_client_key") or "").strip(),
        }
        if not all(credential_fields.values()):
            errors["credential_fields"] = "mtls requires both a client certificate and a client key"
        auth_opts = {}
    else:
        credential_fields = {"api_key": (form.get("api_key") or "").strip()}
        if not credential_fields["api_key"]:
            errors["credential_fields"] = "api_key auth requires an API key"
        auth_opts = {
            "location": form.get("api_key_location") or "header",
            "name": (form.get("api_key_name") or "").strip() or "X-API-Key",
        }

    config_extra = {
        "auth_opts": auth_opts,
        "pagination": {
            "style": form.get("pagination_style") or "page_number",
            "items_path": (form.get("items_path") or "").strip() or "results",
            "page_param": (form.get("page_param") or "").strip() or "page",
        },
    }
    return auth_type, credential_fields, config_extra


def _ssh_fields(form: dict, endpoint_url: str, errors: dict) -> tuple[dict, dict]:
    """Returns (credential_fields, config_extra). Mirrors the old
    blueprint's ``_ssh_payload`` + action.py's ``_require_ssh_fields``:
    either a password or a private key, plus a host-key fingerprint the
    provider must already have confirmed out-of-band (this service
    doesn't yet have a fetch_host_key-equivalent dry-run step -- see
    README's roadmap)."""
    username = (form.get("sftp_username") or "").strip()
    if not username:
        errors["sftp_username"] = "required for sftp/scp"

    private_key_pem = (form.get("sftp_private_key") or "").strip()
    credential_fields = {"username": username}
    if private_key_pem:
        credential_fields["private_key_pem"] = private_key_pem
        passphrase = (form.get("sftp_private_key_passphrase") or "").strip()
        if passphrase:
            credential_fields["private_key_passphrase"] = passphrase
    else:
        password = (form.get("sftp_password") or "").strip()
        if not password:
            errors["credential_fields"] = "sftp/scp requires either a password or a private key"
        credential_fields["password"] = password

    fingerprint = (form.get("host_key_fingerprint") or "").strip()
    if not fingerprint:
        errors["host_key_fingerprint"] = (
            "required for sftp/scp -- confirm the server's host-key fingerprint "
            "with the provider out-of-band before registering"
        )

    config_extra = {
        "host": _parse_host(endpoint_url),
        "port": int(form.get("sftp_port") or 22),
        "remote_path": (form.get("sftp_remote_path") or "").strip() or "/",
        "glob_pattern": (form.get("sftp_glob_pattern") or "").strip() or "*",
        "host_key_fingerprint": fingerprint,
    }
    return credential_fields, config_extra


def _ftp_fields(form: dict, endpoint_url: str, errors: dict) -> tuple[dict, dict]:
    """Returns (credential_fields, config_extra). Mirrors the old
    blueprint's ``_ftp_payload`` + action.py's ``_require_ftp_fields``:
    FTPS (use_tls) is the default; plain FTP needs an explicit
    acknowledgment since it sends credentials and file contents
    unencrypted."""
    username = (form.get("ftp_username") or "").strip()
    password = (form.get("ftp_password") or "").strip()
    if not username or not password:
        errors["credential_fields"] = "ftp requires both a username and a password"

    # No default=True fallback here: an HTML checkbox that's unchecked
    # is simply absent from the submitted form, indistinguishable at
    # this layer from "never mentioned" -- so a missing key must mean
    # "not confirmed as TLS" (fails safe into the acknowledgment gate
    # below), not silently assumed secure. The form pre-checks this box
    # by default (see ui_provider._form_template_vars), so a real user
    # who never touches it still submits an explicit "on".
    use_tls = _parse_bool(form.get("ftp_use_tls"), default=False)
    plain_ftp_acknowledged = _parse_bool(form.get("ftp_plain_ftp_acknowledged"), default=False)
    if not use_tls and not plain_ftp_acknowledged:
        errors["plain_ftp_acknowledged"] = (
            "required when FTPS (use_tls) is disabled -- plain FTP sends "
            "credentials and file contents unencrypted. Acknowledge this "
            "explicitly, or leave FTPS enabled instead."
        )

    credential_fields = {"username": username, "password": password}
    config_extra = {
        "host": _parse_host(endpoint_url),
        "port": int(form.get("ftp_port") or 21),
        "remote_path": (form.get("ftp_remote_path") or "").strip() or "/",
        "glob_pattern": (form.get("ftp_glob_pattern") or "").strip() or "*",
        "use_tls": use_tls,
        "plain_ftp_acknowledged": plain_ftp_acknowledged,
    }
    return credential_fields, config_extra


def register(session: Session, form: dict) -> ProviderSource:
    """Validates a registration form (a plain str-keyed dict -- from a
    submitted HTML form, or built directly by a test) and creates a new
    ``ProviderSource`` in "pending" status. Raises ValidationError with
    a field -> message dict on any problem, same shape as the old
    blueprint's error rendering."""
    errors: dict[str, str] = {}

    name = (form.get("name") or "").strip()
    if not _NAME_RE.match(name):
        errors["name"] = (
            "required: lowercase letters, digits, '-' or '_' only, 2-100 characters"
        )
    elif session.scalar(select(ProviderSource).where(ProviderSource.name == name)):
        errors["name"] = "a source named %r is already registered" % name

    title = (form.get("title") or "").strip()
    if not title:
        errors["title"] = "required"

    endpoint_url = (form.get("endpoint_url") or "").strip()
    if not endpoint_url:
        errors["endpoint_url"] = "required"

    transport_type = form.get("transport_type") or "http"
    if transport_type not in TRANSPORT_TYPES:
        errors["transport_type"] = "must be one of %s" % (TRANSPORT_TYPES,)
        transport_type = "http"

    frequency = form.get("frequency") or "DAILY"
    if frequency not in FREQUENCIES:
        errors["frequency"] = "must be one of %s" % (FREQUENCIES,)
        frequency = "DAILY"

    notification_email = (form.get("notification_email") or "").strip() or None

    if transport_type in SSH_TRANSPORT_TYPES:
        credential_fields, config = _ssh_fields(form, endpoint_url, errors)
        auth_type = "ssh_credentials"  # inert for sftp/scp, matches base_generic's own dispatch
    elif transport_type == "ftp":
        credential_fields, config = _ftp_fields(form, endpoint_url, errors)
        auth_type = "ftp_credentials"  # inert for ftp
    else:
        auth_type, credential_fields, config = _http_fields(form, errors)

    if errors:
        raise ValidationError(errors)

    row = ProviderSource(
        id=_new_id(),
        name=name,
        title=title,
        transport_type=transport_type,
        endpoint_url=endpoint_url,
        frequency=frequency,
        auth_type=auth_type,
        config=json.dumps(config),
        secret_ciphertext=b"",
        secret_data_key_wrapped=b"",
        secret_algorithm="",
        status="pending",
        notification_email=notification_email,
    )
    session.add(row)
    session.flush()  # makes `row` persistent/gettable before the secrets backend looks it up
    _secrets_backend(session).put(harvest_source_id=row.id, fields=credential_fields)
    session.refresh(row)
    return row


def get_or_404(session: Session, source_id: str) -> ProviderSource:
    row = session.get(ProviderSource, source_id)
    if row is None:
        raise NotFound(source_id)
    return row


def list_all(session: Session) -> list[ProviderSource]:
    return list(
        session.scalars(
            select(ProviderSource).order_by(ProviderSource.created_at.desc())
        ).all()
    )


def list_pending(session: Session) -> list[ProviderSource]:
    return list(
        session.scalars(
            select(ProviderSource)
            .where(ProviderSource.status == "pending")
            .order_by(ProviderSource.created_at.asc())
        ).all()
    )


def _transition(session: Session, source_id: str, *, from_status: str, to_status: str) -> ProviderSource:
    row = get_or_404(session, source_id)
    if row.status != from_status:
        raise InvalidTransition(
            "only a %r source can move to %r (current status: %r)"
            % (from_status, to_status, row.status)
        )
    row.status = to_status
    session.commit()
    return row


def activate(session: Session, source_id: str) -> ProviderSource:
    """Admin approval -- flips "pending" to "active". Does NOT provision
    anything in CKAN yet (see module docstring)."""
    return _transition(session, source_id, from_status="pending", to_status="active")


def reject(session: Session, source_id: str, reason: str | None) -> ProviderSource:
    row = get_or_404(session, source_id)
    if row.status != "pending":
        raise InvalidTransition(
            "only a pending source can be rejected (current status: %r)" % row.status
        )
    row.status = "rejected"
    row.rejection_reason = (reason or "").strip() or None
    session.commit()
    return row


def pause(session: Session, source_id: str) -> ProviderSource:
    return _transition(session, source_id, from_status="active", to_status="paused")


def resume(session: Session, source_id: str) -> ProviderSource:
    return _transition(session, source_id, from_status="paused", to_status="active")
