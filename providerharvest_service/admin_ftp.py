"""Admin API for managing FTP-push accounts and their IP whitelists --
the parametric, admin-panel-driven config a general "providers can
push files via FTP" capability needs.

Scope of this module, deliberately: it only owns the data (accounts,
password hashes, per-account allowed-IP list). Syncing that data into
a running ProFTPD (and generating nftables rules for the IP whitelist)
is a separate, not-yet-built piece -- getting a real server's expected
password-hash format right needs verifying against ProFTPD's actual
docs rather than guessing, so it's being built as its own slice.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from providerharvest_service.admin_auth import require_admin_key, require_ftp_sync_key
from providerharvest_service.db import get_session
from providerharvest_service.ftp_credentials import (
    USERNAME_RE,
    ftp_upload_root,
    generate_password,
    hash_password,
    validate_home_directory,
)
from providerharvest_service.sender_allowlist import normalize_cidr
from providerharvest_service.models import FtpAccount, FtpAllowedIp, FtpBoxStatus, FtpLoginAttempt, FtpUnbanRequest, ProviderSource


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

router = APIRouter(
    prefix="/admin/ftp-accounts",
    tags=["ftp-accounts"],
    dependencies=[Depends(require_admin_key)],
)

Protocol = Literal["ftp", "ftps", "sftp"]

_USERNAME_RE = USERNAME_RE
_ftp_upload_root = ftp_upload_root
_validate_home_directory = validate_home_directory


class FtpAccountCreate(BaseModel):
    username: str
    home_directory: str
    protocol: Protocol = "ftps"
    # Which push-mode ProviderSource this account's uploads should be
    # forwarded to, if already known -- optional, can also be set (or
    # changed) later via the dedicated forward-target endpoint below.
    push_target_source_name: str | None = None

    @field_validator("username")
    @classmethod
    def _valid_username(cls, value: str) -> str:
        if not _USERNAME_RE.match(value):
            raise ValueError(
                "username must be 3-32 characters, letters/digits/-/_ only"
            )
        return value

    @field_validator("home_directory")
    @classmethod
    def _valid_home_directory(cls, value: str) -> str:
        return _validate_home_directory(value)


class FtpAccountUpdate(BaseModel):
    enabled: bool | None = None
    protocol: Protocol | None = None


class ForwardTargetUpdate(BaseModel):
    # Required, not optional-with-None-meaning-unchanged like the
    # fields on FtpAccountUpdate above: None here is a real, distinct
    # value (explicitly clear the mapping), so it can't share that
    # "absent means leave alone" convention.
    push_target_source_name: str | None


#: An address or range an account may log in from, in canonical form. IPv4 only,
#: deliberately: the box enforces the list with ProFTPD ``Allow`` rules, and an
#: account whose whole list the box could not express would be locked out
#: entirely. Raises ValueError with a message fit to show an admin. (Defined in
#: sender_allowlist, which the push-source registration shares.)
normalize_allowed_cidr = normalize_cidr


class AllowedIpCreate(BaseModel):
    cidr: str

    @field_validator("cidr")
    @classmethod
    def _valid_cidr(cls, value: str) -> str:
        return normalize_allowed_cidr(value)


class AllowedIpOut(BaseModel):
    id: str
    cidr: str

    model_config = {"from_attributes": True}


class FtpAccountOut(BaseModel):
    id: str
    username: str
    home_directory: str
    protocol: str
    enabled: bool
    push_target_source_name: str | None
    allowed_ips: list[AllowedIpOut]

    model_config = {"from_attributes": True}


class FtpAccountCreated(FtpAccountOut):
    password: str  # shown exactly once, at creation -- never returned again


class PasswordRotated(BaseModel):
    password: str


class FtpAccountSyncOut(BaseModel):
    """For a remote FTP-receiving box's own sync job -- includes the
    bcrypt ``password_hash`` (never the plaintext, which was never
    stored anywhere past account creation) so that box's own ProFTPD
    account store can be kept eventually-consistent with whatever's
    configured here, without loader ever needing inbound network
    access from that box beyond this one narrow, separately-keyed
    endpoint (see ``admin_auth.require_ftp_sync_key``)."""

    username: str
    password_hash: str
    home_directory: str
    protocol: str
    enabled: bool
    allowed_ips: list[str]

    model_config = {"from_attributes": True}

    @field_validator("allowed_ips", mode="before")
    @classmethod
    def _cidrs_only(cls, value):
        return [ip.cidr if hasattr(ip, "cidr") else ip for ip in value]


def _get_account_or_404(session: Session, account_id: str) -> FtpAccount:
    account = session.get(FtpAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such account")
    return account


def _refuse_if_managed(account: FtpAccount) -> None:
    """A provider's own account takes its allowed addresses from its push source
    (the single place they are edited); changing them here would be silently
    undone the next time the source is saved."""
    if account.managed_source_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this account belongs to a push source -- set its allowed senders on the source instead",
        )


def validate_push_target(session: Session, source_name: str | None) -> None:
    """Light, config-time check only -- existence and transport_type,
    not ``status``: an admin may reasonably wire up the mapping before
    the push-mode source is actually activated. The real, load-bearing
    check (source exists AND is active) happens at relay-upload time in
    ftp_relay_ingest.py, where a rejection just means the box's next
    retry-with-backoff eventually succeeds once the source goes active,
    not a hard failure here."""
    if source_name is None:
        return
    source = session.scalar(select(ProviderSource).where(ProviderSource.name == source_name))
    if source is None or source.transport_type != "push":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "no push-mode source named %r" % source_name,
        )


@router.post("", response_model=FtpAccountCreated, status_code=status.HTTP_201_CREATED)
def create_ftp_account(
    payload: FtpAccountCreate, session: Session = Depends(get_session)
) -> FtpAccountCreated:
    if session.scalar(select(FtpAccount).where(FtpAccount.username == payload.username)):
        raise HTTPException(status.HTTP_409_CONFLICT, "username already in use")
    validate_push_target(session, payload.push_target_source_name)

    password = generate_password()
    account = FtpAccount(
        username=payload.username,
        password_hash=hash_password(password),
        home_directory=payload.home_directory,
        protocol=payload.protocol,
        push_target_source_name=payload.push_target_source_name,
    )
    session.add(account)
    session.commit()
    session.refresh(account)

    out = FtpAccountOut.model_validate(account)
    return FtpAccountCreated(password=password, **out.model_dump())


@router.get("", response_model=list[FtpAccountOut])
def list_ftp_accounts(session: Session = Depends(get_session)) -> list[FtpAccount]:
    return list(session.scalars(select(FtpAccount)).all())


@router.get("/{account_id}", response_model=FtpAccountOut)
def get_ftp_account(account_id: str, session: Session = Depends(get_session)) -> FtpAccount:
    return _get_account_or_404(session, account_id)


@router.patch("/{account_id}", response_model=FtpAccountOut)
def update_ftp_account(
    account_id: str, payload: FtpAccountUpdate, session: Session = Depends(get_session)
) -> FtpAccount:
    account = _get_account_or_404(session, account_id)
    if payload.enabled is not None:
        account.enabled = payload.enabled
    if payload.protocol is not None:
        account.protocol = payload.protocol
    session.commit()
    session.refresh(account)
    return account


@router.patch("/{account_id}/forward-target", response_model=FtpAccountOut)
def set_forward_target(
    account_id: str, payload: ForwardTargetUpdate, session: Session = Depends(get_session)
) -> FtpAccount:
    account = _get_account_or_404(session, account_id)
    validate_push_target(session, payload.push_target_source_name)
    account.push_target_source_name = payload.push_target_source_name
    session.commit()
    session.refresh(account)
    return account


@router.post("/{account_id}/rotate-password", response_model=PasswordRotated)
def rotate_password(account_id: str, session: Session = Depends(get_session)) -> PasswordRotated:
    account = _get_account_or_404(session, account_id)
    password = generate_password()
    account.password_hash = hash_password(password)
    session.commit()
    return PasswordRotated(password=password)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ftp_account(account_id: str, session: Session = Depends(get_session)) -> None:
    account = _get_account_or_404(session, account_id)
    session.delete(account)
    session.commit()


@router.post(
    "/{account_id}/allowed-ips",
    response_model=AllowedIpOut,
    status_code=status.HTTP_201_CREATED,
)
def add_allowed_ip(
    account_id: str, payload: AllowedIpCreate, session: Session = Depends(get_session)
) -> FtpAllowedIp:
    account = _get_account_or_404(session, account_id)
    _refuse_if_managed(account)
    existing = next((a for a in account.allowed_ips if a.cidr == payload.cidr), None)
    if existing is not None:
        return existing  # already there: adding it twice changes nothing
    allowed = FtpAllowedIp(ftp_account_id=account_id, cidr=payload.cidr)
    session.add(allowed)
    session.commit()
    session.refresh(allowed)
    return allowed


@router.delete("/{account_id}/allowed-ips/{ip_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_allowed_ip(
    account_id: str, ip_id: str, session: Session = Depends(get_session)
) -> None:
    allowed = session.get(FtpAllowedIp, ip_id)
    if allowed is None or allowed.ftp_account_id != account_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such allowed IP")
    _refuse_if_managed(allowed.account)
    session.delete(allowed)
    session.commit()


# A deliberately separate router, not just another route on the one
# above: that router's accounts CRUD is gated by the full admin key
# (every admin-only feature this service has); this single read-only
# endpoint is gated by its own, narrower LOADER_FTP_SYNC_KEY instead
# (see admin_auth.require_ftp_sync_key's own docstring for why), so a
# remote FTP-receiving box only ever needs to hold a credential scoped
# to exactly this. Its own distinct path (not nested under
# /admin/ftp-accounts/) is deliberate too, not just cosmetic: that
# router already has a GET /{account_id}, and Starlette matches routes
# in registration order regardless of specificity -- sharing the
# prefix would make this endpoint's fate depend on which router
# happened to be included first in app.py, a fragile thing to rely on.
sync_router = APIRouter(tags=["ftp-accounts-sync"], dependencies=[Depends(require_ftp_sync_key)])


@sync_router.get("/admin/ftp-accounts-sync", response_model=list[FtpAccountSyncOut])
def sync_ftp_accounts(session: Session = Depends(get_session)) -> list[FtpAccount]:
    """Only enabled accounts -- a disabled one should be removed from
    the remote box's own account store by its sync job (see that job's
    own reconciliation logic), not kept around there with credentials
    that should no longer work."""
    return list(session.scalars(select(FtpAccount).where(FtpAccount.enabled.is_(True))).all())


class _BoxStatusFields(BaseModel):
    hostname: str
    proftpd_active: bool
    ftp_port_listening: bool
    sftp_port_listening: bool
    relay_service_active: bool
    account_count: int
    sync_ok: bool
    sync_message: str | None = None


class ClientSeen(BaseModel):
    """A provider's successful login on the box since its last report --
    the address they connected from (see ftp-forwarder's auth_log.py)."""

    username: str
    ip: str


class FtpBoxStatusIn(_BoxStatusFields):
    clients: list[ClientSeen] = []
    # The box's fail2ban state; both optional so a box on older code still reports.
    banned_ips: list[str] | None = None
    fail2ban_active: bool | None = None


class FtpBoxStatusOut(_BoxStatusFields):
    reported_at: datetime.datetime
    # Addresses an admin asked to be let back in, for the box to release
    # now -- how a request made here reaches a box that loader can't call.
    unban: list[str] = []


# A request the box never acts on (it's down, or fail2ban is) shouldn't sit
# there forever and then fire an unban long after anyone remembers asking.
UNBAN_REQUEST_TTL = datetime.timedelta(minutes=15)


@sync_router.put("/admin/ftp-box-status", response_model=FtpBoxStatusOut)
def report_ftp_box_status(
    payload: FtpBoxStatusIn, session: Session = Depends(get_session)
) -> FtpBoxStatusOut:
    # Same narrow LOADER_FTP_SYNC_KEY as the sync pull above, not a new
    # credential -- this is that same remote box reporting on itself
    # during the same cycle, not a distinct capability that needs its
    # own blast radius.
    row = session.get(FtpBoxStatus, payload.hostname)
    if row is None:
        row = FtpBoxStatus(hostname=payload.hostname)
        session.add(row)
    row.reported_at = _utcnow()
    row.proftpd_active = payload.proftpd_active
    row.ftp_port_listening = payload.ftp_port_listening
    row.sftp_port_listening = payload.sftp_port_listening
    row.relay_service_active = payload.relay_service_active
    row.account_count = payload.account_count
    row.sync_ok = payload.sync_ok
    row.sync_message = payload.sync_message
    if payload.banned_ips is not None:
        addresses = {_valid_address(candidate) for candidate in payload.banned_ips}
        row.banned_ips = json.dumps(sorted(a for a in addresses if a))
    row.fail2ban_active = payload.fail2ban_active
    for client in payload.clients:
        _record_sender(session, client.username, client.ip)
    pending = _collect_unban_requests(session, row)
    session.commit()
    session.refresh(row)
    return FtpBoxStatusOut(
        hostname=row.hostname,
        proftpd_active=row.proftpd_active,
        ftp_port_listening=row.ftp_port_listening,
        sftp_port_listening=row.sftp_port_listening,
        relay_service_active=row.relay_service_active,
        account_count=row.account_count,
        sync_ok=row.sync_ok,
        sync_message=row.sync_message,
        reported_at=row.reported_at,
        unban=pending,
    )


def _valid_address(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _collect_unban_requests(session: Session, box: FtpBoxStatus) -> list[str]:
    """The addresses this box should release now. A request is finished
    once the box reports that address no longer banned (so it is dropped
    here), or has expired; until then it is handed back on every report --
    harmless to repeat, since unbanning an address that isn't banned is a
    no-op, and it means a dropped reply doesn't lose the request."""
    banned = set(box.banned)
    cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - UNBAN_REQUEST_TTL
    pending: set[str] = set()
    for request in session.scalars(select(FtpUnbanRequest).where(FtpUnbanRequest.hostname == box.hostname)).all():
        if request.ip not in banned or request.requested_at < cutoff:
            session.delete(request)
        else:
            pending.add(request.ip)
    return sorted(pending)


def _record_sender(session: Session, username: str, ip: str) -> None:
    """Remember where a provider last connected from, on the push source
    their account forwards to. Best effort and silent: an address that
    isn't one, an unknown account, or one that forwards nowhere is just
    skipped -- a bad entry must never fail the whole heartbeat."""
    address = _valid_address(ip)
    if address is None:
        return
    account = session.scalar(select(FtpAccount).where(FtpAccount.username == username))
    if account is None or not account.push_target_source_name:
        return
    source = session.scalar(
        select(ProviderSource).where(
            ProviderSource.name == account.push_target_source_name,
            ProviderSource.transport_type == "push",
        )
    )
    if source is not None:
        source.last_sender = address


# -- the record of refused logins ---------------------------------------------------

# Long enough to see a slow campaign, short enough that the table stays small.
ATTEMPT_RETENTION = datetime.timedelta(days=90)
# Port 2222 serves SFTP and SCP over one SSH login; a refusal cannot say which.
SFTP_SCP = "SFTP/SCP"
# Ways in the box may name itself, beyond what the port says: sshd (22 and
# 7002) logs the client's port, not its own, so the port alone can't say.
BOX_CHANNELS = frozenset({"SSH"})
MAX_ATTEMPTS_PER_REPORT = 2000
MAX_USERNAME_LENGTH = 100


def clean_username(raw: str) -> str:
    """What a client typed as its username, made safe to store and show:
    control characters (including anything that could fake a new log line
    or hide text) are dropped and the length is capped. It is still
    untrusted -- the report page escapes it and the CSV export defuses
    spreadsheet formulas -- this just keeps the worst of it out."""
    printable = "".join(ch for ch in raw if ch.isprintable())
    return printable[:MAX_USERNAME_LENGTH]


class LoginAttemptIn(BaseModel):
    at: int  # epoch seconds
    ip: str
    port: int
    username: str = ""
    code: int
    channel: str | None = None


class LoginAttemptsIn(BaseModel):
    hostname: str
    attempts: list[LoginAttemptIn] = Field(max_length=MAX_ATTEMPTS_PER_REPORT)


@sync_router.post("/admin/ftp-login-attempts")
def report_login_attempts(payload: LoginAttemptsIn, session: Session = Depends(get_session)) -> dict:
    """A batch of refused logins from a box. Entries that don't make sense
    (a bad address, a port that isn't one, a time outside the retention
    window or in the future) are counted and skipped, never a reason to
    refuse the whole batch -- the box would otherwise resend it forever."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    oldest, newest = now - ATTEMPT_RETENTION, now + datetime.timedelta(days=1)
    stored = rejected = 0
    for attempt in payload.attempts:
        address = _valid_address(attempt.ip)
        try:
            when = datetime.datetime.fromtimestamp(attempt.at, tz=datetime.timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            when = None
        if address is None or when is None or not (1 <= attempt.port <= 65535) or not (oldest <= when <= newest):
            rejected += 1
            continue
        session.add(FtpLoginAttempt(
            hostname=payload.hostname, at=when, ip=address, port=attempt.port,
            username=clean_username(attempt.username), code=attempt.code,
            channel=(
                attempt.channel if attempt.channel in BOX_CHANNELS
                else {21: "FTP", 2222: SFTP_SCP}.get(attempt.port, "FTP/SFTP port %d" % attempt.port)
            ),
        ))
        stored += 1
    session.execute(delete(FtpLoginAttempt).where(FtpLoginAttempt.at < oldest))
    session.commit()
    return {"stored": stored, "rejected": rejected}
