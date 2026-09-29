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
import os
import posixpath
import re
import secrets
from typing import Literal

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service.admin_auth import require_admin_key, require_ftp_sync_key
from providerharvest_service.db import get_session
from providerharvest_service.models import FtpAccount, FtpAllowedIp, FtpBoxStatus, ProviderSource


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

router = APIRouter(
    prefix="/admin/ftp-accounts",
    tags=["ftp-accounts"],
    dependencies=[Depends(require_admin_key)],
)

Protocol = Literal["ftp", "ftps", "sftp"]

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")


def _ftp_upload_root() -> str:
    # posixpath, not pathlib/os.path: this validates a path meant for
    # the Linux deployment target, regardless of what OS runs the code
    # (including this test suite, which may run on Windows).
    return posixpath.normpath(os.environ.get("LOADER_FTP_UPLOAD_ROOT", "/srv/ftp-uploads"))


def _validate_home_directory(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError("home_directory must be an absolute path")
    normalized = posixpath.normpath(value)
    root = _ftp_upload_root()
    if normalized != root and not normalized.startswith(root + "/"):
        raise ValueError("home_directory must be under %s" % root)
    return normalized


def hash_password(password: str) -> str:
    # prefix=b"2a", not bcrypt's own $2b$ default: confirmed for real
    # against a live ProFTPD 1.3.8b + mod_sql_passwd instance that a
    # $2b$ hash is silently rejected (clean "530 Login incorrect", no
    # error logged) while the identical password re-hashed as $2a$
    # authenticates correctly. Not a guess -- the $2b$ hash was tested
    # first and failed; only $2a$ was confirmed working.
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(prefix=b"2a")
    ).decode("ascii")


def generate_password() -> str:
    return secrets.token_urlsafe(24)


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


class AllowedIpCreate(BaseModel):
    cidr: str

    @field_validator("cidr")
    @classmethod
    def _valid_cidr(cls, value: str) -> str:
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError("not a valid IP address or CIDR range: %r" % value) from exc
        return value


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
    _get_account_or_404(session, account_id)
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


class FtpBoxStatusIn(BaseModel):
    hostname: str
    proftpd_active: bool
    ftp_port_listening: bool
    sftp_port_listening: bool
    relay_service_active: bool
    account_count: int
    sync_ok: bool
    sync_message: str | None = None


class FtpBoxStatusOut(FtpBoxStatusIn):
    reported_at: datetime.datetime

    model_config = {"from_attributes": True}


@sync_router.put("/admin/ftp-box-status", response_model=FtpBoxStatusOut)
def report_ftp_box_status(
    payload: FtpBoxStatusIn, session: Session = Depends(get_session)
) -> FtpBoxStatus:
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
    session.commit()
    session.refresh(row)
    return row
