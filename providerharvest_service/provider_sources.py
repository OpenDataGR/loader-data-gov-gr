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
import posixpath
import re
import secrets
from urllib.parse import urlparse

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.secrets.envelope import EnvelopeSecretsBackend
from providerharvest_service.ftp_credentials import (
    MIN_CUSTOM_PASSWORD_LENGTH,
    USERNAME_RE,
    ftp_upload_root,
    generate_password,
    hash_password,
    password_problem,
)
from providerharvest_service import retention, sender_allowlist
from providerharvest_service.models import CkanTarget, FtpAccount, FtpAllowedIp, LoadTarget, LookupTable, ProviderSource, Transform, _new_id

TRANSPORT_TYPES = ("http", "sftp", "scp", "ftp", "push")
PROXY_METHODS = ("GET", "POST")
TEST_REQUEST_MAX_BYTES = 64 * 1024
_KEY_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,19}$")  # the word before an API key: Token, Bearer, ...
SSH_TRANSPORT_TYPES = ("sftp", "scp")
# How a push-mode source receives its data: "https" (the provider calls
# push_ingest.py with a generated API key) or "ftp" (the provider logs
# into the external FTP/SFTP box with a username/password created for
# them at registration, see ftp_relay_ingest.py).
PUSH_METHODS = ("https", "ftp")
MIN_CUSTOM_FTP_PASSWORD_LENGTH = MIN_CUSTOM_PASSWORD_LENGTH  # (kept under its old name)
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
        prefix = (form.get("api_key_prefix") or "").strip()
        if prefix == "__custom__":  # "Custom..." chosen in the dropdown: the word is typed in its own box
            prefix = (form.get("api_key_prefix_custom") or "").strip()
            if not prefix:
                errors["api_key_prefix"] = "type the word, or choose another option"
        if prefix:
            if not _KEY_PREFIX.match(prefix):
                errors["api_key_prefix"] = "a single word of letters, digits, '-' or '_' (e.g. Token or Bearer)"
            else:
                auth_opts["prefix"] = prefix

    proxy_method = (form.get("proxy_method") or "GET").strip().upper()
    if proxy_method not in PROXY_METHODS:
        errors["proxy_method"] = "must be one of %s" % (PROXY_METHODS,)
        proxy_method = "GET"

    test_request = (form.get("test_request") or "").strip()
    if test_request:
        if proxy_method == "POST":
            if len(test_request.encode("utf-8")) > TEST_REQUEST_MAX_BYTES:
                errors["test_request"] = "larger than %d kB" % (TEST_REQUEST_MAX_BYTES // 1000)
            else:
                try:
                    json.loads(test_request)
                except ValueError:
                    errors["test_request"] = "must be valid JSON -- the body this API expects"
        elif len(test_request) > 2000 or any(ord(c) < 32 for c in test_request):
            errors["test_request"] = "must be a query string on one line (name=value&name2=value2), at most 2000 characters"

    config_extra = {
        "proxy_method": proxy_method,
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
    if test_request and "test_request" not in errors:
        config_extra["test_request"] = test_request
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
    }
    return credential_fields, config_extra


def _parse_allowed_senders(form: dict, errors: dict) -> list[str]:
    """The addresses a push source accepts data from (see sender_allowlist.py);
    empty means anyone. Hostnames typed here are looked up now."""
    try:
        return sender_allowlist.parse(form.get("allowed_senders"))
    except sender_allowlist.AllowlistError as exc:
        errors["allowed_senders"] = str(exc)
        return []


def _push_fields(session: Session, form: dict, source_name: str, errors: dict) -> tuple[dict, dict, dict]:
    """Returns (credential_fields, config_extra, reveal) for a push-mode
    source -- one that never connects anywhere; the provider sends us
    data as it arrives instead, either by calling this service's own
    upload endpoint (``push_method="https"``, see ``push_ingest.py``) or
    by logging into the FTP/SFTP box (``push_method="ftp"``, see
    ``ftp_relay_ingest.py``). ``reveal`` carries whatever must be shown
    to the provider exactly once and never stored in recoverable form:
    ``{"api_key": ...}`` for https, ``{"ftp": {...}}`` for ftp.

    https: we generate the key right now, stored as a one-way bcrypt
    hash in ``config`` (verified the same way ``push_ingest.py`` checks
    it: hash-and-compare, never decrypted) rather than through the
    envelope-encryption secret path every other transport's *outbound*
    credentials use -- deliberately: this key only ever needs to be
    *verified* against what a caller presents, never read back out and
    used by this service itself, so a one-way hash is strictly safer (a
    compromised master key or DB dump exposes nothing usable). Matches
    the same "never re-shown, never recoverable" choice already made for
    FTP-push account passwords (``ftp_credentials.hash_password``).

    ftp: the provider may pick a username (defaults to the source name)
    and a password (leave blank to have one generated); the actual
    account is created in ``register``, after validation passes.
    ``credential_fields`` is returned empty only to keep the same
    shape every ``_x_fields`` function has; nothing is ever stored via
    the secrets backend for a push-mode source."""
    config_extra: dict = {}
    method = form.get("push_method") or "https"
    if method not in PUSH_METHODS:
        errors["push_method"] = "must be one of %s" % (PUSH_METHODS,)
        method = "https"
    config_extra["push_method"] = method
    config_extra["allowed_senders"] = _parse_allowed_senders(form, errors)

    if method == "ftp":
        typed_username = (form.get("push_ftp_username") or "").strip()
        username = typed_username or source_name
        if typed_username or "name" not in errors:
            if not USERNAME_RE.match(username):
                errors["push_ftp_username"] = (
                    "3-32 characters, letters/digits/-/_ only"
                    + ("" if typed_username else " -- the source name doesn't fit, so enter a username")
                )
            elif session.scalar(select(FtpAccount).where(FtpAccount.username == username)):
                errors["push_ftp_username"] = "already in use -- pick another"

        chosen_password = form.get("push_ftp_password") or ""
        problem = password_problem(chosen_password) if chosen_password else None
        if problem:
            errors["push_ftp_password"] = "%s (or leave blank to generate one)" % problem
        return {}, config_extra, {
            "ftp": {
                "username": username,
                "password": chosen_password or generate_password(),
                "generated": not chosen_password,
            }
        }

    plaintext_key = secrets.token_urlsafe(32)
    config_extra["push_api_key_hash"] = bcrypt.hashpw(
        plaintext_key.encode("utf-8"), bcrypt.gensalt()
    ).decode("ascii")
    return {}, config_extra, {"api_key": plaintext_key}


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

    transport_type = form.get("transport_type") or "http"
    if transport_type not in TRANSPORT_TYPES:
        errors["transport_type"] = "must be one of %s" % (TRANSPORT_TYPES,)
        transport_type = "http"

    # A push-mode source has no endpoint of its own to describe -- we
    # never connect anywhere for one, the provider calls us instead
    # (see push_ingest.py) -- so there's nothing meaningful to require
    # here.
    endpoint_url = (form.get("endpoint_url") or "").strip()
    if not endpoint_url and transport_type != "push":
        errors["endpoint_url"] = "required"

    frequency = form.get("frequency") or "DAILY"
    if frequency not in FREQUENCIES:
        errors["frequency"] = "must be one of %s" % (FREQUENCIES,)
        frequency = "DAILY"
    if transport_type == "push":
        # A push source has no schedule -- data is received as it
        # arrives, never fetched -- so a frequency doesn't apply.
        frequency = "MANUAL"

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

    push_reveal: dict = {}
    if transport_type in SSH_TRANSPORT_TYPES:
        credential_fields, config = _ssh_fields(form, endpoint_url, errors)
        auth_type = "ssh_credentials"  # inert for sftp/scp, matches base_generic's own dispatch
    elif transport_type == "ftp":
        credential_fields, config = _ftp_fields(form, endpoint_url, errors)
        auth_type = "ftp_credentials"  # inert for ftp
    elif transport_type == "push":
        credential_fields, config, push_reveal = _push_fields(session, form, name, errors)
        # inert for push: push_ingest.py checks the key itself, and FTP
        # push authenticates at ProFTPD -- neither goes through auth_strategies
        auth_type = "push_ftp_account" if config.get("push_method") == "ftp" else "push_api_key"
    else:
        auth_type, credential_fields, config = _http_fields(form, errors)
        if config.get("proxy_method") == "POST":
            # A POST API is only reachable through the live proxy: it is never pulled on a schedule.
            frequency = "MANUAL"  # nothing to pull on a schedule; whether it is exposed is the checkbox's own choice

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
    if "api_key" in push_reveal:
        # Shown exactly once, on the registration confirmation page --
        # only the bcrypt hash above is ever persisted, and bcrypt is
        # one-way, so this plaintext is genuinely never recoverable
        # again after this response. A plain instance attribute, not a
        # mapped column: never written to the database, gone once this
        # object is garbage collected. Same "generate once, never show
        # again" rule this codebase already applies to FTP-push account
        # passwords (admin_ftp.py) and CKAN tokens.
        row.generated_api_key = push_reveal["api_key"]
    elif "ftp" in push_reveal:
        spec = push_reveal["ftp"]
        home_directory = posixpath.join(ftp_upload_root(), spec["username"])
        # Created disabled: anyone can reach the registration form (no
        # login wall on the provider portal yet), so this account must
        # not be able to log in -- or even be synced to the FTP box --
        # until an admin approves the source. activate()/pause()/etc.
        # flip it via _sync_managed_ftp_accounts.
        account = FtpAccount(
            username=spec["username"],
            password_hash=hash_password(spec["password"]),
            home_directory=home_directory,
            protocol="sftp",  # informational only -- ProFTPD serves the same account on both ports
            enabled=False,
            push_target_source_name=row.name,
            managed_source_id=row.id,
        )
        session.add(account)
        session.flush()
        _sync_managed_allowed_ips(session, row)  # the box enforces the source's sender list on this login
        session.commit()
        # Same one-time, never-persisted-in-plaintext rule as the API
        # key above. `password` stays None when the provider chose their
        # own -- it's never echoed back.
        row.generated_ftp = {
            "username": spec["username"],
            "password": spec["password"] if spec["generated"] else None,
            "home_directory": home_directory,
        }
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
        data["api_key_prefix"] = auth_opts.get("prefix") or ""
        data["oauth2_token_url"] = auth_opts.get("token_url", "")
        data["oauth2_scope"] = auth_opts.get("scope") or ""
        pagination = config.get("pagination") or {}
        data["items_path"] = pagination.get("items_path", "results")
        data["max_requests_per_minute"] = str(config.get("max_requests_per_minute", 60))
        data["proxy_rate_limit_per_ip"] = str(config.get("proxy_rate_limit_per_ip", 30))
        data["request_timeout_s"] = str(config.get("request_timeout_s", 30))
        data["proxy_method"] = "POST" if config.get("proxy_method") == "POST" else "GET"
        data["test_request"] = config.get("test_request") or ""
        data["max_pages"] = str(config.get("max_pages", 1000))
        data["max_retries"] = str(config.get("max_retries", 5))
    elif source.transport_type in SSH_TRANSPORT_TYPES:
        data["sftp_port"] = str(config.get("port", 22))
        data["sftp_remote_path"] = config.get("remote_path", "/")
        data["sftp_glob_pattern"] = config.get("glob_pattern", "*")
        data["host_key_fingerprint"] = config.get("host_key_fingerprint", "")
        data["sftp_connect_timeout_s"] = str(config.get("connect_timeout_s", 30))
    elif source.transport_type == "ftp":
        data["ftp_port"] = str(config.get("port", 21))
        data["ftp_remote_path"] = config.get("remote_path", "/")
        data["ftp_glob_pattern"] = config.get("glob_pattern", "*")
        data["ftp_use_tls"] = "on" if config.get("use_tls", True) else ""
        data["ftp_connect_timeout_s"] = str(config.get("connect_timeout_s", 30))
    elif source.transport_type == "push":
        data["push_method"] = config.get("push_method", "https")
        data["allowed_senders"] = "\n".join(config.get("allowed_senders") or [])
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
    re-entered, same validation as a fresh registration (sftp <-> scp
    doesn't count: same credentials). Does not touch
    ``status`` -- editing an active source doesn't force it back
    through re-approval (this whole service still runs as a single
    stand-in user; revisit once real per-user auth exists)."""
    source = get_or_404(session, source_id)
    errors: dict[str, str] = {}

    title = (form.get("title") or "").strip()
    if not title:
        errors["title"] = "required"

    transport_type = form.get("transport_type") or source.transport_type
    if transport_type not in TRANSPORT_TYPES:
        errors["transport_type"] = "must be one of %s" % (TRANSPORT_TYPES,)
        transport_type = source.transport_type

    endpoint_url = (form.get("endpoint_url") or "").strip()
    if not endpoint_url and transport_type != "push":
        errors["endpoint_url"] = "required"

    frequency = form.get("frequency") or "DAILY"
    if frequency not in FREQUENCIES:
        errors["frequency"] = "must be one of %s" % (FREQUENCIES,)
        frequency = "DAILY"
    if transport_type == "push":
        frequency = "MANUAL"  # no schedule for a source that only receives data

    notification_email = (form.get("notification_email") or "").strip() or None
    proxy_enabled = _parse_bool(form.get("proxy_enabled"), default=False)

    submitted_auth_type = form.get("auth_type") or source.auth_type
    # sftp and scp take identical credentials (username + password or
    # key), so switching between them keeps the stored secret usable.
    same_ssh_family = transport_type in SSH_TRANSPORT_TYPES and source.transport_type in SSH_TRANSPORT_TYPES
    keep_existing_shape = (transport_type == source.transport_type or same_ssh_family) and (
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
    elif transport_type == "push":
        # Never rotates the key (or touches the FTP account) through
        # this generic edit path -- there is no form field for it to even
        # submit, and regenerating credentials on every unrelated edit
        # (e.g. just renaming the title) would silently break whatever
        # the provider already configured with the old ones. Only the
        # nothing about the credentials is editable here; the existing
        # config (push_method, push_api_key_hash) is carried over as-is.
        # Only supported when the source was already push-mode:
        # switching an existing source *into* push mode via edit isn't
        # supported -- there's no one-time-reveal page in this flow to
        # show freshly generated credentials on, unlike `register`.
        if source.transport_type != "push":
            errors["transport_type"] = (
                "switching to push mode isn't supported via edit yet -- "
                "delete and re-register the source as push instead, so its credentials can be shown once"
            )
            existing_config: dict = {}
        else:
            existing_config = json.loads(source.config or "{}")
        # A per-source size from before the cap became system-wide is dropped.
        credential_fields = {}
        config = {k: v for k, v in existing_config.items() if k != "max_file_size_bytes"}
        if "allowed_senders" in form:
            # The one thing the edit form does change for a push source. Present-but-blank
            # means "accept from anywhere" (clear the list); absent (a caller that never
            # mentions it) leaves it alone.
            config["allowed_senders"] = _parse_allowed_senders(form, errors)
        auth_type = source.auth_type if source.transport_type == "push" else "push_api_key"
    else:
        auth_type, credential_fields, config = _http_fields(working_form, errors)
        if config.get("proxy_method") == "POST":
            frequency = "MANUAL"  # nothing to pull on a schedule; whether it is exposed is the checkbox's own choice

    if errors:
        raise ValidationError(errors)

    source.title = title
    source.endpoint_url = endpoint_url
    source.transport_type = transport_type
    source.frequency = frequency
    source.auth_type = auth_type
    source.config = json.dumps(retention.carry_over(source.config, config))  # (an edit must not forget the retention)
    source.notification_email = notification_email
    source.proxy_enabled = proxy_enabled
    session.flush()
    _sync_managed_allowed_ips(session, source)
    _secrets_backend(session).rotate(source.id, credential_fields)
    session.refresh(source)
    return source


class NotRotatable(Exception):
    """The source has no API key to rotate (it is not an HTTPS push source)."""


def rotate_api_key(session: Session, source_id: str) -> tuple[ProviderSource, str]:
    """Issue a new API key for an HTTPS push source, replacing the old one at
    once: the old key stops working the moment this commits. Returns the source
    and the new key in plain text -- shown once by the caller and never stored
    (only its bcrypt hash is, exactly as at registration), so there is nothing
    to look it up from later. Lost keys used to mean deleting and re-registering
    the source."""
    source = get_or_404(session, source_id)
    if source.transport_type != "push" or source.push_method != "https":
        raise NotRotatable("only an HTTPS push source has an API key")
    config = json.loads(source.config or "{}")
    plaintext = secrets.token_urlsafe(32)
    config["push_api_key_hash"] = bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    source.config = json.dumps(config)
    session.commit()
    return source, plaintext


def delete(session: Session, source_id: str) -> None:
    """Permanently removes a source and its encrypted credential (the
    secret lives on this same row, not a separate table -- deleting the
    row deletes it too, no cascade needed). Allowed regardless of
    status -- no special-casing "active" sources; this whole service
    still runs as a single stand-in user, so there's no ownership check
    to enforce here yet either."""
    source = get_or_404(session, source_id)
    removed = {"accounts": 0, "pipelines": 0, "tables": 0, "published": 0}
    for account in _managed_ftp_accounts(session, source):
        session.delete(account)
        removed["accounts"] += 1
    # what belongs to the source goes with it: its pipelines, its index tables and its publish setting (nothing can use them afterwards)
    for model, key in ((Transform, "pipelines"), (LookupTable, "tables"), (LoadTarget, None), (CkanTarget, None)):  # (the old switch and the CKAN connections are only cleaned up)
        for row in session.scalars(select(model).where(model.source_id == source_id)).all():
            if key:
                removed[key] += 1
            if model is Transform and row.published:
                removed["published"] += 1
            session.delete(row)
    session.delete(source)
    session.commit()
    return removed


def deletion_summary(removed: dict[str, int], stored: dict[str, int] | None = None) -> str:
    """What deleting a source took with it, in words, for the message that follows."""
    def n(count: int, what: str) -> str:
        return "%d %s%s" % (count, what, "" if count == 1 else "s")

    parts = []
    if removed.get("pipelines"):
        parts.append(n(removed["pipelines"], "pipeline"))
    if removed.get("tables"):
        parts.append(n(removed["tables"], "index table"))
    if stored:
        if stored.get("runs"):
            parts.append("%s with %s" % (n(stored["runs"], "stored run"), n(stored["files"], "file")))
        if stored.get("outputs"):
            parts.append(n(stored["outputs"], "output"))
    text = "Source deleted."
    if parts:
        text += " Removed with it: %s." % ", ".join(parts)
    if removed.get("published"):
        text += " Its published address stopped answering."
    return text


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


def _managed_ftp_accounts(session: Session, source: ProviderSource) -> list[FtpAccount]:
    return list(session.scalars(select(FtpAccount).where(FtpAccount.managed_source_id == source.id)).all())


def managed_ftp_account(session: Session, source: ProviderSource) -> FtpAccount | None:
    """The FTP/SFTP login created for this source at registration (an
    ``push_method="ftp"`` source has exactly one), or None."""
    accounts = _managed_ftp_accounts(session, source)
    return accounts[0] if accounts else None


def allowed_senders(source: ProviderSource) -> list[str]:
    """The addresses this (push) source accepts data from; empty means anyone."""
    return list(json.loads(source.config or "{}").get("allowed_senders") or [])


def _sync_managed_allowed_ips(session: Session, source: ProviderSource) -> None:
    """Makes each FTP account this source manages accept logins from exactly the
    source's sender list (the box turns ``FtpAllowedIp`` rows into ProFTPD
    rules). The source is the single place the list is edited -- the FTP box
    page shows a managed account's list read-only -- so this overwrites. An
    admin-made account (no ``managed_source_id``) is never touched."""
    wanted = set(allowed_senders(source)) if source.transport_type == "push" else set()
    for account in _managed_ftp_accounts(session, source):
        have = {a.cidr: a for a in account.allowed_ips}
        for cidr, row in have.items():
            if cidr not in wanted:
                account.allowed_ips.remove(row)
        for cidr in sorted(wanted - set(have)):
            account.allowed_ips.append(FtpAllowedIp(cidr=cidr))


def _sync_managed_ftp_accounts(session: Session, source: ProviderSource) -> None:
    """A self-service FTP account (created by ``register`` for an
    ``push_method="ftp"`` source) can log in only while its source is
    active -- so an unapproved, paused, or rejected source's account is
    never even synced to the FTP box (see admin_ftp.sync_ftp_accounts,
    which only returns enabled accounts). Admin-created accounts
    (``managed_source_id`` NULL) are never touched here."""
    for account in _managed_ftp_accounts(session, source):
        account.enabled = source.status == "active"


def _transition(session: Session, source_id: str, *, from_status: str, to_status: str) -> ProviderSource:
    row = get_or_404(session, source_id)
    if row.status != from_status:
        raise InvalidTransition(
            "only a %r source can move to %r (current status: %r)"
            % (from_status, to_status, row.status)
        )
    row.status = to_status
    _sync_managed_ftp_accounts(session, row)
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
    _sync_managed_ftp_accounts(session, row)
    session.commit()
    return row


def pause(session: Session, source_id: str) -> ProviderSource:
    return _transition(session, source_id, from_status="active", to_status="paused")


def resume(session: Session, source_id: str) -> ProviderSource:
    return _transition(session, source_id, from_status="paused", to_status="active")
