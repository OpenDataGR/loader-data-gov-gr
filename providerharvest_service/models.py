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

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String
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
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    # Connection health: set by connection_check.check_connection, a real
    # dry-run reachability/auth check against the source's actual
    # endpoint -- independent of `status` above (a pending source can
    # still be checked; a checked source can still be rejected). Never
    # fetches or stores any harvested data itself -- see README's
    # roadmap for the scheduled-harvest slice that will.
    last_check_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_status: Mapped[str | None] = mapped_column(String, nullable=True)  # "ok" | "error"
    last_check_message: Mapped[str | None] = mapped_column(String, nullable=True)

    # Whether an active transport_type="http" source is also reachable
    # live via proxy.py's GET /proxy/{name} -- opt-out, not opt-in
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

    @property
    def banned(self) -> list[str]:
        return json.loads(self.banned_ips) if self.banned_ips else []


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
    of who has been trying the door (see ui_admin_ftp_attempts.py). FTP/SFTP
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
