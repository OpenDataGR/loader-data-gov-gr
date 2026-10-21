"""ORM models for loader-data-gov-gr's own persistence.

FTP-push accounts and their IP whitelists (the data an admin-panel-
driven FTP receiving server needs -- syncing it into a running ProFTPD
is a separate piece, see admin_ftp.py), the per-user CKAN token store,
and now ``ProviderSource`` -- the provider registration/approval
lifecycle, still independent of CKAN (see that model's own docstring).
Field-mapping profiles are not modeled yet; still on README.md's
roadmap.
"""

from __future__ import annotations

import datetime
import json
import uuid

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class FtpAccount(Base):
    __tablename__ = "ftp_account"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    home_directory: Mapped[str] = mapped_column(String, nullable=False)
    # "ftp" | "ftps" | "sftp" -- validated at the API layer, not here.
    protocol: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    # Which push-mode ProviderSource.name this account's uploaded files
    # get forwarded to by the remote relay box (see
    # ftp_relay_ingest.py) -- nullable, since an account can exist
    # before its forwarding target is decided. Set via the admin panel
    # only; the relay box itself never needs to know or hold a
    # per-source credential for this, it authenticates as itself (the
    # same LOADER_FTP_SYNC_KEY it already has) and this mapping is
    # looked up server-side.
    push_target_source_name: Mapped[str | None] = mapped_column(String, nullable=True)

    # Set only for an account a provider created for themselves while
    # registering a push source (provider_sources.register) -- that
    # source's lifecycle then drives this account's: enabled only while
    # the source is active, removed when the source is deleted. NULL for
    # an admin-created account, which stays independently managed.
    managed_source_id: Mapped[str | None] = mapped_column(String, nullable=True)

    allowed_ips: Mapped[list["FtpAllowedIp"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class FtpGroup(Base):
    """Deliberately empty and never touched by this application's own
    code. ProFTPD's mod_sql always issues a lookup against a table
    named "groups" (columns groupname/gid/members) during auth --
    unconditionally, even with no SQLGroupInfo directive configured
    and no group-membership features in use; SQLDefaultUID/SQLDefaultGID
    only supply fallback *values*, they don't suppress the lookup
    itself. Without this table, every real FTP login fails with
    ProFTPD's own "unrecoverable backend error: no such table: groups"
    (confirmed against ProFTPD's sample mod_sql.conf and a matching
    real-world Debian bug report, not guessed). It exists purely so
    that query doesn't hard-fail against a missing table -- an empty
    result set is fine, a missing table is not.
    """

    __tablename__ = "groups"

    groupname: Mapped[str] = mapped_column(String, primary_key=True)
    gid: Mapped[int] = mapped_column(Integer, nullable=False)
    members: Mapped[str | None] = mapped_column(String, nullable=True)


class CkanUserToken(Base):
    """One row per person, storing their personal CKAN API token
    envelope-encrypted (see engine/secrets/envelope.py -- the exact
    same AES-256-GCM scheme already built for provider credentials,
    reused here rather than inventing a second secret store).

    Per the "per-user token does both" design: this token is minted
    once (via the standing sysadmin credential, at first actual need,
    not preemptively) and then used for BOTH the org-membership
    authorization check and every subsequent write -- so CKAN's own
    permission system enforces org membership for real on every call,
    rather than this service's own logic being the only thing
    standing between a caller and writing to the wrong org.
    """

    __tablename__ = "ckan_user_token"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    data_key_wrapped: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    algorithm: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)


class ProviderSource(Base):
    """A provider-registered data source, self-service registered and
    admin-approved -- this service's own record of it, independent of
    CKAN. Mirrors the shape ``ckanext-providerharvest``'s
    ``ProviderSource``/``HarvestSource.config`` split held, collapsed
    into columns this service owns outright rather than living partly
    on a CKAN package.

    ``config`` holds whatever's specific to ``transport_type`` (port,
    remote_path, glob_pattern, pagination, host_key_fingerprint, ...) as
    a JSON blob -- these vary enough per transport that dedicated
    columns for all of them would mostly be NULL for any given row.
    Never holds a credential; those are envelope-encrypted in the
    ``secret_*`` columns below (see engine/secrets/envelope.py -- the
    same AES-256-GCM scheme already used for ``CkanUserToken``), keyed
    by this row's own id (see provider_sources.py's
    ``ProviderSourceSecretRepository``).

    ``status`` is the approval/run state machine: "pending" (just
    registered, awaiting admin approval) -> "active" (approved, would
    run on schedule once a scheduler exists) or "rejected" (admin
    denied it, see ``rejection_reason``); "active" and "paused" toggle
    back and forth via self-service pause/resume. Approving a source
    does NOT yet provision anything in CKAN -- that's a later slice,
    once a harvest run actually needs the package/resource/DataStore to
    exist (see README's roadmap).
    """

    __tablename__ = "provider_source"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    # "http" | "sftp" | "scp" | "ftp" -- matches engine/transport/'s own types.
    transport_type: Mapped[str] = mapped_column(String, nullable=False)
    endpoint_url: Mapped[str] = mapped_column(String, nullable=False)
    # "MANUAL" | "DAILY" | "WEEKLY" | "MONTHLY"
    frequency: Mapped[str] = mapped_column(String, nullable=False, default="DAILY")
    # matches engine/auth_strategies/'s AUTH_STRATEGIES keys for "http", or
    # "ssh_credentials"/"ftp_credentials" (inert placeholders) otherwise.
    auth_type: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[str] = mapped_column(String, nullable=False, default="{}")
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_data_key_wrapped: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_algorithm: Mapped[str] = mapped_column(String, nullable=False)
    # "pending" | "active" | "rejected" | "paused"
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    notification_email: Mapped[str | None] = mapped_column(String, nullable=True)
    # The CKAN organization the source belongs to: who sees it on the Overview (an administrator of that organization, or a super admin). Empty for a
    # source from before this existed, or registered by a super admin who did not choose one; only super admins see those.
    organization_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    organization_title: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    # Connection health: set by connection_check.check_connection, a real
    # dry-run reachability/auth check against the source's actual
    # endpoint -- independent of `status` above (a pending source can
    # still be checked; a checked source can still be rejected). Never
    # fetches or stores any harvested data itself -- see README's
    # roadmap for the scheduled-harvest slice that will.
    last_check_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_status: Mapped[str | None] = mapped_column(String, nullable=True)  # "ok" | "warn" | "error"
    last_check_message: Mapped[str | None] = mapped_column(String, nullable=True)

    # Whether an active transport_type="http" source is also reachable
    # live via the old GET /proxy/{name} -- opt-out, not opt-in. No longer used: publishing (the Load Box) is the switch; publishing.migrate_legacy_proxy carried it over once
    # (defaults True for a new registration; see the registration form's
    # own checkbox). Deliberately `nullable=True` despite having a
    # Python-side default: db._sync_schema adds this column to an
    # already-existing table via a plain ALTER TABLE ... ADD COLUMN with
    # no SQL-level DEFAULT, so every row that predates this column gets
    # SQL NULL, not the Python default -- a NOT NULL column here would
    # make that ALTER TABLE itself fail on startup (SQLite refuses a
    # NOT NULL column with no default against a table with existing
    # rows), which is a strictly worse failure than the "no such column"
    # incident this same file's own docstring already describes.
    # Application code must therefore treat NULL as "enabled" (the
    # backward-compatible default for every source registered before
    # this column existed) -- always compare with
    # ``is False`` / ``!= False``, never truthiness alone.
    proxy_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)

    # Push sources only: when the last file was stored, and where the
    # data last came from. For an HTTPS push, the sender's address
    # (client_addr.sender_address); for an FTP/SFTP push, the provider's
    # address as last reported by the FTP box (it, not loader, is what
    # the provider connects to). Both NULL until something arrives.
    last_received_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_sender: Mapped[str | None] = mapped_column(String, nullable=True)

    @property
    def kind(self) -> str:
        """"push" (the provider sends data to us as it arrives) or "pull"
        (we fetch it, on a schedule) -- derived from transport_type, not
        stored: the two can't drift apart."""
        return "push" if self.transport_type == "push" else "pull"

    @property
    def proxy_method(self) -> str:
        """How this source's endpoint is called (when it is asked on demand): "GET" (the default, and what every older source does)
        or "POST" (a JSON body is passed through). Only meaningful for an HTTP source."""
        try:
            config = json.loads(self.config or "{}")
        except ValueError:
            return "GET"
        return "POST" if isinstance(config, dict) and config.get("proxy_method") == "POST" else "GET"

    @property
    def test_request(self) -> str:
        """The request an admin gave for testing this API: the JSON body for a POST API, the query string for a GET one. Used by "Test
        connection" (a real call) and by "Fetch a sample". Empty when none was given."""
        try:
            config = json.loads(self.config or "{}")
        except ValueError:
            return ""
        value = config.get("test_request") if isinstance(config, dict) else ""
        return value if isinstance(value, str) else ""

    @property
    def proxy_only(self) -> bool:
        """An HTTP source whose API is called with POST: it can only be asked on demand, through its published address -- there is nothing to
        pull on a schedule (each call asks one question, and may create something at the provider)."""
        return self.transport_type == "http" and self.proxy_method == "POST"

    @property
    def push_method(self) -> str | None:
        """For a push source, how it delivers: "https" (API key) or "ftp"
        (FTP/SFTP login). None for a pull source. Sources registered
        before this choice existed were all HTTPS."""
        if self.transport_type != "push":
            return None
        return json.loads(self.config or "{}").get("push_method", "https")


class FtpBoxStatus(Base):
    """One row per remote FTP/SFTP-receiving box (see
    ftp-forwarder/README.md), upserted by that box's own account-sync
    job every cycle. Loader can never reach that box directly --
    deliberately pull-based/outbound-only, so the box calling loader is
    the only network path that exists -- so this is the only way
    loader ever learns whether it's alive: the box reports on itself,
    this just remembers the latest report."""

    __tablename__ = "ftp_box_status"

    hostname: Mapped[str] = mapped_column(String, primary_key=True)
    reported_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    proftpd_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ftp_port_listening: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sftp_port_listening: Mapped[bool] = mapped_column(Boolean, nullable=False)
    relay_service_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    account_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sync_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sync_message: Mapped[str | None] = mapped_column(String, nullable=True)
    # fail2ban's current ban list for the box's login jail, as a JSON list
    # of addresses (NULL until the box has reported one), and whether
    # fail2ban itself is running (NULL: a box that doesn't report it).
    banned_ips: Mapped[str | None] = mapped_column(String, nullable=True)
    fail2ban_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # The address this box's reports come from (its public IP, as loader sees it):
    # what the access rules must never block, or the box could not reach loader.
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    # Which version of the loader's whitelist/blacklist (see access_control.py) the
    # box says it has applied; compared with the current one to show "applied" or
    # "waiting". NULL: a box that doesn't report it.
    access_version: Mapped[str | None] = mapped_column(String, nullable=True)
    # The box's own load / memory / disk / sessions figures from its last report, as JSON
    # (see ftp-forwarder's box_resources.py). NULL: a box that doesn't report them.
    resources: Mapped[str | None] = mapped_column(String, nullable=True)

    @property
    def resource_figures(self) -> dict:
        try:
            data = json.loads(self.resources) if self.resources else {}
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def banned(self) -> list[str]:
        return json.loads(self.banned_ips) if self.banned_ips else []


class Transform(Base):
    """What to do with one source's input before it goes anywhere: an ordered list of steps (see
    transform_engine.py) and the output format. One per source. The original file is never changed;
    the output is stored beside it (``TransformedFile`` in the harvest database).

    A source (the input) can have several of these -- *pipelines*, each with its own ``name`` (unique within the source), steps and
    output format. The first one, and every one that existed before pipelines did, is named ``default``. ``name`` is nullable only
    because a column added to a table that already has rows has to be (see db._sync_schema); db._migrate_pipelines fills it in."""

    __tablename__ = "transform"
    __table_args__ = (Index("ix_transform_source_name", "source_id", "name", unique=True),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    source_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True, default="default")
    steps: Mapped[str] = mapped_column(String, nullable=False, default="[]")  # JSON list of step objects
    output_format: Mapped[str] = mapped_column(String, nullable=False, default="json")
    root_name: Mapped[str] = mapped_column(String, nullable=False, default="records")  # XML output only
    record_name: Mapped[str] = mapped_column(String, nullable=False, default="record")  # XML output only
    # CSV output only. NULL (a transform made before these existed) means the defaults: comma, with a byte-order mark.
    csv_delimiter: Mapped[str | None] = mapped_column(String, nullable=True)
    csv_bom: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # JSON output only: when the steps leave exactly one record, return that object instead of a one-element list (a lookup).
    output_single: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # The request side of an API source's published address: callers use the OTHER method than the provider (provider POST -> callers GET, provider GET -> callers
    # POST), and this template turns their call into the provider's (see request_template.py). Off (NULL/False) = same method both ways.
    request_swap: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    request_template: Mapped[str | None] = mapped_column(String, nullable=True)
    # One answer of the provider, pasted by an admin, to build and try the steps on -- for a source whose answers are not stored
    # (a live proxy keeps nothing). Never used to answer anyone.
    sample: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Published (the Load Box's switch): this pipeline answers at /data/<source>[/<pipeline>]. The ``default`` one also answers at
    # /data/<source>. Empty means not published (a column added to a table with rows is nullable -- see db._sync_schema).
    published: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_by: Mapped[str] = mapped_column(String, nullable=False, default="")


class LookupTable(Base):
    """An index table an admin uploaded (CSV, JSON or Excel) to a source: rows that a "lookup" step of that source's pipelines matches
    against, to add columns to records (for example a station id -> its name and location). A name is unique within its source, so two
    sources can each have a ``stations``. ``source_id`` is empty only for a table that no source used when tables moved to sources (see
    db._migrate_lookup_tables): it is listed in the Transform Box until it is bound to a source or deleted."""

    __tablename__ = "lookup_table"
    __table_args__ = (Index("ix_lookup_table_source_name", "source_id", "name", unique=True),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False, default="")
    columns: Mapped[str] = mapped_column(String, nullable=False, default="[]")  # JSON list of column names
    rows: Mapped[str] = mapped_column(String, nullable=False, default="[]")  # JSON list of {column: value}
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    uploaded_by: Mapped[str] = mapped_column(String, nullable=False, default="")


class BoxSample(Base):
    """One reading of a box's figures, kept for the small charts on the Data boxes page (see
    box_history.py): at most one per box per five-minute bucket, kept seven days."""

    __tablename__ = "box_sample"
    __table_args__ = (Index("ix_box_sample_host_bucket", "hostname", "bucket", unique=True),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    bucket: Mapped[int] = mapped_column(Integer, nullable=False)  # unix time // 300
    at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    load_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # one-minute load as % of the CPUs
    mem_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    rx_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tx_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sessions: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FtpBoxCommand(Base):
    """An action an admin asked an FTP box to run (restart a service, take a diagnostic
    snapshot -- see box_commands.py). Loader can't reach the box, so it waits here until the
    box's next heartbeat collects it ("sent"), and the box's answer arrives in a later one."""

    __tablename__ = "ftp_box_command"
    __table_args__ = (Index("ix_ftp_box_command_host_at", "hostname", "requested_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    requested_by: Mapped[str] = mapped_column(String, nullable=False, default="")
    requested_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    # pending (waiting for the box) | sent (handed over, no answer yet) | ok | failed | expired
    state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    output: Mapped[str] = mapped_column(String, nullable=False, default="")


class AccessLog(Base):
    """A record of a change to the whitelist/blacklist: who added, removed, moved or
    changed an entry, and the automatic bans loader made. Shown as "Recent changes" in the
    Security box, so removals and the system's own actions are not invisible. An entry
    simply expiring is not logged (nothing happened that anyone did)."""

    __tablename__ = "access_log"
    __table_args__ = (Index("ix_access_log_at", "at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    who: Mapped[str] = mapped_column(String, nullable=False, default="")  # the admin, or "auto-ban"
    action: Mapped[str] = mapped_column(String, nullable=False)  # added | removed | moved | changed | converted | banned | lifted
    kind: Mapped[str] = mapped_column(String, nullable=False)  # the list it concerns: "allow" | "block"
    cidr: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str] = mapped_column(String, nullable=False, default="")


class AccessRule(Base):
    """One entry of the Security box's whitelist ("allow") or blacklist ("block"):
    an IPv4 or IPv6 address or range. Enforced by loader itself on every HTTP request and,
    through the box's heartbeat reply, by the FTP box (see access_control.py). A
    rule may carry an expiry (a temporary block); expired rules are ignored."""

    __tablename__ = "access_rule"
    __table_args__ = (Index("ix_access_rule_kind_cidr", "kind", "cidr", unique=True),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "allow" | "block"
    cidr: Mapped[str] = mapped_column(String, nullable=False)  # canonical IPv4 network
    note: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class FtpUnbanRequest(Base):
    """An admin asked for an address to be let back in on a box. Loader
    can't reach the box, so the request waits here until that box's next
    heartbeat collects it (the reply to ``PUT /admin/ftp-box-status``
    lists it); it is dropped once the box reports the address no longer
    banned, or after a timeout if the box never does."""

    __tablename__ = "ftp_unban_request"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    ip: Mapped[str] = mapped_column(String, nullable=False)
    requested_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)


class FtpLoginAttempt(Base):
    """One refused login or credential check, on any way in -- for the report
    of who has been trying the door (see ui_admin_failed_logins.py). FTP/SFTP
    ones are shipped in batches by the box's sync service, HTTP ones are
    recorded by loader itself (failed_auth.py); kept for a limited time only (see
    ``admin_ftp.ATTEMPT_RETENTION``). ``username`` is whatever the client
    typed, so it is hostile input: cleaned on the way in and treated as
    untrusted wherever it is shown or exported."""

    __tablename__ = "ftp_login_attempt"
    __table_args__ = (
        Index("ix_ftp_login_attempt_at", "at"),
        Index("ix_ftp_login_attempt_ip", "ip"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)  # UTC
    ip: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)  # the server port it hit: 21 FTP, 2222 SFTP
    username: Mapped[str] = mapped_column(String, nullable=False, default="")
    code: Mapped[int] = mapped_column(Integer, nullable=False)  # the refusal, e.g. 530 (FTP) or 401 (HTTP)
    # How they came in: "FTP", "SFTP" (from the box), or an HTTP credential
    # ("Web login", "Push API key", ...; see failed_auth.py). NULL on rows
    # that predate the column -- those are FTP/SFTP, told apart by port.
    channel: Mapped[str | None] = mapped_column(String, nullable=True)


class FtpAllowedIp(Base):
    __tablename__ = "ftp_allowed_ip"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    ftp_account_id: Mapped[str] = mapped_column(
        ForeignKey("ftp_account.id"), nullable=False
    )
    cidr: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    account: Mapped["FtpAccount"] = relationship(back_populates="allowed_ips")


class LoadTarget(Base):
    """The Load Box's settings for one source: where its transformed output goes after the Transform Box.
    Today one destination, the published API endpoint (``GET /data/{source name}``, the newest good output),
    off until an admin turns it on. One row per source; the row exists only once something was set."""

    __tablename__ = "load_target"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    source_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    api_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_by: Mapped[str] = mapped_column(String, nullable=False, default="")


class CkanTarget(Base):
    """One load of a pipeline into CKAN (the Load Box's CKAN destination, see ``ckan_load``): a pipeline may have several, each its own row and
    each exactly one way. ``mode`` is ``link`` (the resource points at the published address), ``upload`` (the newest output is uploaded into
    CKAN), ``datastore`` (its rows are written into a DataStore table) or ``service`` (a data-service entry). ``ckan_user`` is the CKAN user
    the loader acts as for it -- the person who connected it -- so CKAN's own permissions apply to every later change too."""

    __tablename__ = "ckan_target"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    source_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    pipeline_id: Mapped[str] = mapped_column(String, nullable=False, index=True)  # (a pipeline may have several loads, each one way)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="link")
    organization_id: Mapped[str | None] = mapped_column(String, nullable=True)
    organization_title: Mapped[str | None] = mapped_column(String, nullable=True)
    dataset_id: Mapped[str] = mapped_column(String, nullable=False)
    dataset_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    dataset_title: Mapped[str] = mapped_column(String, nullable=False, default="")
    resource_id: Mapped[str | None] = mapped_column(String, nullable=True)  # the id of the thing in CKAN (the resource; for a data service the entry itself); empty while it is not there
    service_meta: Mapped[str | None] = mapped_column(String, nullable=True)  # a data service's own details (licence, themes, contact ...), as JSON
    datastore_config: Mapped[str | None] = mapped_column(String, nullable=True)  # a DataStore table's settings (refresh, keys, the columns it was made with), as JSON
    resource_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    description: Mapped[str] = mapped_column(String, nullable=False, default="")
    ckan_user: Mapped[str] = mapped_column(String, nullable=False, default="")
    connected_by: Mapped[str] = mapped_column(String, nullable=False, default="")
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)  # ok | error
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    last_pushed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_output_id: Mapped[str | None] = mapped_column(String, nullable=True)  # the output an upload last sent (so the same one is not sent twice)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)


class CkanServiceDefaults(Base):
    """What was last chosen for the details of a data service in one CKAN organization (licence, themes, legislation, contact, publisher ...),
    so the next service of the same organization starts from the same values."""

    __tablename__ = "ckan_service_defaults"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    organization_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    values: Mapped[str] = mapped_column(String, nullable=False, default="{}")
    updated_by: Mapped[str] = mapped_column(String, nullable=False, default="")
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)


class Alert(Base):
    """Something that needs attention, found by the alert monitor (``alerts.py``): a source that has gone silent, a transform or a CKAN push that keeps
    failing, an FTP box that does not report. One row per problem (``key`` says which), opened when it is found, kept up to date while it lasts and
    resolved when it is gone; a person can acknowledge it. ``changed_at`` moves only when the alert appears, is resolved, comes back or is
    acknowledged -- it is what the open pages ask about, so a page hears of a change within seconds and a still-open alert costs nothing."""

    __tablename__ = "alert"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    key: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # silent | failing | ckan | box | health
    severity: Mapped[str] = mapped_column(String, nullable=False, default="warning")  # warning | error
    source_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str] = mapped_column(String, nullable=False, default="")
    link: Mapped[str] = mapped_column(String, nullable=False, default="")
    first_seen: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    changed_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String, nullable=True)
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
