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

from providerharvest_service.engine.secrets.base import SecretBundle
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


def _parse_positive_int(raw, default: int, field_name: str, errors: dict) -> int:
    """A blank/missing value falls back to `default`; anything else must
    parse as a positive integer -- reject clearly rather than crash.
    (Port fields used to call bare ``int(...)`` with no guard at all,
    which raised an unhandled ValueError -- a 500, not a validation
    error -- on a non-numeric value; folded into this same helper
    rather than leaving that path unguarded next to a new field that
    needs the identical check.)"""
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors[field_name] = "must be a positive whole number"
        return default
    if value <= 0:
        errors[field_name] = "must be a positive whole number"
        return default
    return value


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


def credentials(session: Session, source_id: str) -> SecretBundle:
    """Decrypts and returns a source's stored credentials -- for
    connection_check.py's dry-run health check, and later the actual
    scheduled-harvest run. Never cache the result beyond the current
    call (same rule engine/secrets/base.py's SecretBundle itself
    documents)."""
    return _secrets_backend(session).get(source_id)


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
        "max_requests_per_minute": _parse_positive_int(
            form.get("max_requests_per_minute"), 60, "max_requests_per_minute", errors
        ),
        # A distinct budget from the one above: that one caps this
        # service's own total outbound call rate to the provider,
        # shared across every caller; this one caps how much of that
        # shared budget any single calling IP can consume through
        # proxy.py, so one abusive consumer can't starve every other
        # caller of a source's proxy.
        "proxy_rate_limit_per_ip": _parse_positive_int(
            form.get("proxy_rate_limit_per_ip"), 30, "proxy_rate_limit_per_ip", errors
        ),
        # Every one of these has a real, previously-hardcoded default
        # in engine/transport/direct_https.py (DEFAULT_TIMEOUT_S,
        # DEFAULT_MAX_PAGES, DEFAULT_MAX_RETRIES) -- exposed here so a
        # provider whose API is slow, paginates deeply, or is flaky in
        # a way that needs more/fewer retries isn't stuck with values
        # tuned for nothing in particular.
        "request_timeout_s": _parse_positive_int(
            form.get("request_timeout_s"), 30, "request_timeout_s", errors
        ),
        "max_pages": _parse_positive_int(form.get("max_pages"), 1000, "max_pages", errors),
        "max_retries": _parse_positive_int(form.get("max_retries"), 5, "max_retries", errors),
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
        "port": _parse_positive_int(form.get("sftp_port"), 22, "sftp_port", errors),
        "remote_path": (form.get("sftp_remote_path") or "").strip() or "/",
        "glob_pattern": (form.get("sftp_glob_pattern") or "").strip() or "*",
        "host_key_fingerprint": fingerprint,
        "connect_timeout_s": _parse_positive_int(
            form.get("sftp_connect_timeout_s"), 30, "sftp_connect_timeout_s", errors
        ),
        # Same default as harvest_runner.DEFAULT_MAX_FILE_SIZE_BYTES --
        # not imported from there to keep this module framework/engine-
        # agnostic (see its own docstring); harvest_runner falls back to
        # its own constant if a source's config predates this field.
        "max_file_size_bytes": _parse_positive_int(
            form.get("sftp_max_file_size_bytes"), 20 * 1024 * 1024, "sftp_max_file_size_bytes", errors
        ),
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
        "port": _parse_positive_int(form.get("ftp_port"), 21, "ftp_port", errors),
        "remote_path": (form.get("ftp_remote_path") or "").strip() or "/",
        "glob_pattern": (form.get("ftp_glob_pattern") or "").strip() or "*",
        "use_tls": use_tls,
        "plain_ftp_acknowledged": plain_ftp_acknowledged,
        "connect_timeout_s": _parse_positive_int(
            form.get("ftp_connect_timeout_s"), 30, "ftp_connect_timeout_s", errors
        ),
        "max_file_size_bytes": _parse_positive_int(
            form.get("ftp_max_file_size_bytes"), 20 * 1024 * 1024, "ftp_max_file_size_bytes", errors
        ),
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
    # No default=True fallback here, same reasoning as ftp_use_tls
    # above: an unchecked HTML checkbox is simply absent from the
    # submitted form, indistinguishable at this layer from "never
    # mentioned" -- so this only actually behaves as "opt-out, not
    # opt-in" because the registration form pre-checks this box by
    # default (see ui_provider._form_template_vars); a provider who
    # never touches it submits an explicit "on". Defaulting the missing
    # case to True here instead would make unchecking the box a no-op.
    # Only meaningful for transport_type="http" (proxy.py only ever
    # proxies HTTP sources anyway), harmless to store for the others.
    proxy_enabled = _parse_bool(form.get("proxy_enabled"), default=False)

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
        proxy_enabled=proxy_enabled,
    )
    session.add(row)
    session.flush()  # makes `row` persistent/gettable before the secrets backend looks it up
    _secrets_backend(session).put(harvest_source_id=row.id, fields=credential_fields)
    session.refresh(row)
    return row


def config_to_form_fields(source: ProviderSource) -> dict:
    """Reconstructs the non-secret form field values source_form.html
    needs to pre-fill an edit, from a source's own row + its config
    JSON blob. Deliberately never includes anything from the encrypted
    secret -- credential fields are always left blank for the provider
    to either leave alone (keeps the existing secret, see `update`) or
    overwrite outright, never round-tripped through the browser."""
    config = json.loads(source.config or "{}")
    data = {
        "name": source.name,
        "title": source.title,
        "endpoint_url": source.endpoint_url,
        "transport_type": source.transport_type,
        "frequency": source.frequency,
        "notification_email": source.notification_email or "",
        "proxy_enabled": "on" if source.proxy_enabled is not False else "",
    }
    if source.transport_type == "http":
        data["auth_type"] = source.auth_type
        auth_opts = config.get("auth_opts") or {}
        data["api_key_location"] = auth_opts.get("location", "header")
        data["api_key_name"] = auth_opts.get("name", "X-API-Key")
        data["oauth2_token_url"] = auth_opts.get("token_url", "")
        data["oauth2_scope"] = auth_opts.get("scope") or ""
        pagination = config.get("pagination") or {}
        data["items_path"] = pagination.get("items_path", "results")
        data["max_requests_per_minute"] = str(config.get("max_requests_per_minute", 60))
        data["proxy_rate_limit_per_ip"] = str(config.get("proxy_rate_limit_per_ip", 30))
        data["request_timeout_s"] = str(config.get("request_timeout_s", 30))
        data["max_pages"] = str(config.get("max_pages", 1000))
        data["max_retries"] = str(config.get("max_retries", 5))
    elif source.transport_type in SSH_TRANSPORT_TYPES:
        data["sftp_port"] = str(config.get("port", 22))
        data["sftp_remote_path"] = config.get("remote_path", "/")
        data["sftp_glob_pattern"] = config.get("glob_pattern", "*")
        data["host_key_fingerprint"] = config.get("host_key_fingerprint", "")
        data["sftp_connect_timeout_s"] = str(config.get("connect_timeout_s", 30))
        data["sftp_max_file_size_bytes"] = str(config.get("max_file_size_bytes", 20 * 1024 * 1024))
    elif source.transport_type == "ftp":
        data["ftp_port"] = str(config.get("port", 21))
        data["ftp_remote_path"] = config.get("remote_path", "/")
        data["ftp_glob_pattern"] = config.get("glob_pattern", "*")
        data["ftp_use_tls"] = "on" if config.get("use_tls", True) else ""
        data["ftp_connect_timeout_s"] = str(config.get("connect_timeout_s", 30))
        data["ftp_max_file_size_bytes"] = str(config.get("max_file_size_bytes", 20 * 1024 * 1024))
    return data


def _merge_existing_credentials(form: dict, transport_type: str, auth_type: str, existing: SecretBundle) -> dict:
    """Edit-mode only: any raw credential-shaped field left blank in the
    submitted form is filled in from the source's current stored
    secret, so _http_fields/_ssh_fields/_ftp_fields's own "required"
    validation passes unchanged and the existing credential stays in
    effect -- reused server-side, never decrypted back to the browser.
    A field the provider actually typed a new value into always wins.
    Only called when transport_type/auth_type match what the source
    already had (see `update`) -- across an actual change, the old
    secret's shape doesn't apply and a fresh one must be entered."""
    merged = dict(form)
    fields = existing.fields

    def _keep(form_key: str, secret_key: str) -> None:
        if not (merged.get(form_key) or "").strip():
            merged[form_key] = fields.get(secret_key, "")

    if transport_type == "http":
        if auth_type == "api_key":
            _keep("api_key", "api_key")
        elif auth_type == "basic_auth":
            _keep("basic_username", "username")
            _keep("basic_password", "password")
        elif auth_type == "oauth2_client_credentials":
            _keep("oauth2_client_id", "client_id")
            _keep("oauth2_client_secret", "client_secret")
        elif auth_type == "mtls":
            _keep("mtls_client_cert", "client_cert_pem")
            _keep("mtls_client_key", "client_key_pem")
    elif transport_type in SSH_TRANSPORT_TYPES:
        _keep("sftp_username", "username")
        if not (merged.get("sftp_password") or "").strip() and not (merged.get("sftp_private_key") or "").strip():
            if "private_key_pem" in fields:
                merged["sftp_private_key"] = fields["private_key_pem"]
                merged["sftp_private_key_passphrase"] = fields.get("private_key_passphrase", "")
            else:
                merged["sftp_password"] = fields.get("password", "")
    elif transport_type == "ftp":
        _keep("ftp_username", "username")
        _keep("ftp_password", "password")
    return merged


def update(session: Session, source_id: str, form: dict) -> ProviderSource:
    """Edits an existing source. ``name`` is immutable once
    registered -- proxy URLs and bookmarks reference it directly, and
    the form doesn't even offer to change it. Credential fields left
    entirely blank keep the existing encrypted secret; typing a new
    value rotates it. That "leave blank to keep" behavior only applies
    when the submitted transport_type (and, for HTTP, auth_type) match
    what the source already had -- switching either means the old
    secret's shape no longer applies, so the new one must be fully
    re-entered, same validation as a fresh registration. Does not touch
    ``status`` -- editing an active source doesn't force it back
    through re-approval (this whole service still runs as a single
    stand-in user; revisit once real per-user auth exists)."""
    source = get_or_404(session, source_id)
    errors: dict[str, str] = {}

    title = (form.get("title") or "").strip()
    if not title:
        errors["title"] = "required"

    endpoint_url = (form.get("endpoint_url") or "").strip()
    if not endpoint_url:
        errors["endpoint_url"] = "required"

    transport_type = form.get("transport_type") or source.transport_type
    if transport_type not in TRANSPORT_TYPES:
        errors["transport_type"] = "must be one of %s" % (TRANSPORT_TYPES,)
        transport_type = source.transport_type

    frequency = form.get("frequency") or "DAILY"
    if frequency not in FREQUENCIES:
        errors["frequency"] = "must be one of %s" % (FREQUENCIES,)
        frequency = "DAILY"

    notification_email = (form.get("notification_email") or "").strip() or None
    proxy_enabled = _parse_bool(form.get("proxy_enabled"), default=False)

    submitted_auth_type = form.get("auth_type") or source.auth_type
    keep_existing_shape = transport_type == source.transport_type and (
        transport_type != "http" or submitted_auth_type == source.auth_type
    )
    working_form = form
    if keep_existing_shape:
        existing_secret = credentials(session, source.id)
        working_form = _merge_existing_credentials(form, transport_type, submitted_auth_type, existing_secret)

    if transport_type in SSH_TRANSPORT_TYPES:
        credential_fields, config = _ssh_fields(working_form, endpoint_url, errors)
        auth_type = "ssh_credentials"
    elif transport_type == "ftp":
        credential_fields, config = _ftp_fields(working_form, endpoint_url, errors)
        auth_type = "ftp_credentials"
    else:
        auth_type, credential_fields, config = _http_fields(working_form, errors)

    if errors:
        raise ValidationError(errors)

    source.title = title
    source.endpoint_url = endpoint_url
    source.transport_type = transport_type
    source.frequency = frequency
    source.auth_type = auth_type
    source.config = json.dumps(config)
    source.notification_email = notification_email
    source.proxy_enabled = proxy_enabled
    session.flush()
    _secrets_backend(session).rotate(source.id, credential_fields)
    session.refresh(source)
    return source


def delete(session: Session, source_id: str) -> None:
    """Permanently removes a source and its encrypted credential (the
    secret lives on this same row, not a separate table -- deleting the
    row deletes it too, no cascade needed). Allowed regardless of
    status -- no special-casing "active" sources; this whole service
    still runs as a single stand-in user, so there's no ownership check
    to enforce here yet either."""
    source = get_or_404(session, source_id)
    session.delete(source)
    session.commit()


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


def status_counts(session: Session) -> dict[str, int]:
    """Counts per status, for the dashboard stat tiles -- always includes
    every status key (0 for statuses with no rows yet) so templates
    don't need a Jinja `default(0)` on every lookup."""
    counts = {status: 0 for status in STATUSES_ADMIN_MANAGES}
    for row in list_all(session):
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts


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
