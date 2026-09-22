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

from providerharvest_service.admin_auth import require_admin_key
from providerharvest_service.db import get_session
from providerharvest_service.models import FtpAccount, FtpAllowedIp

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


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _generate_password() -> str:
    return secrets.token_urlsafe(24)


class FtpAccountCreate(BaseModel):
    username: str
    home_directory: str
    protocol: Protocol = "ftps"

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
    allowed_ips: list[AllowedIpOut]

    model_config = {"from_attributes": True}


class FtpAccountCreated(FtpAccountOut):
    password: str  # shown exactly once, at creation -- never returned again


class PasswordRotated(BaseModel):
    password: str


def _get_account_or_404(session: Session, account_id: str) -> FtpAccount:
    account = session.get(FtpAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such account")
    return account


@router.post("", response_model=FtpAccountCreated, status_code=status.HTTP_201_CREATED)
def create_ftp_account(
    payload: FtpAccountCreate, session: Session = Depends(get_session)
) -> FtpAccountCreated:
    if session.scalar(select(FtpAccount).where(FtpAccount.username == payload.username)):
        raise HTTPException(status.HTTP_409_CONFLICT, "username already in use")

    password = _generate_password()
    account = FtpAccount(
        username=payload.username,
        password_hash=_hash_password(password),
        home_directory=payload.home_directory,
        protocol=payload.protocol,
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


@router.post("/{account_id}/rotate-password", response_model=PasswordRotated)
def rotate_password(account_id: str, session: Session = Depends(get_session)) -> PasswordRotated:
    account = _get_account_or_404(session, account_id)
    password = _generate_password()
    account.password_hash = _hash_password(password)
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
