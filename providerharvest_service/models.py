"""ORM models for loader-data-gov-gr's own persistence.

FTP-push accounts and their IP whitelists only, for now -- this is the
data an admin-panel-driven FTP receiving server needs, per the design
discussion this came out of. Syncing this into a running ProFTPD (and
nftables for the IP whitelist) is a separate, not-yet-built piece; this
module only defines the data those will eventually read from. See
README.md's roadmap for what else this will grow to hold (provider-
source config, approval status, field-mapping profiles) once that
piece is built.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String
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


class FtpAllowedIp(Base):
    __tablename__ = "ftp_allowed_ip"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    ftp_account_id: Mapped[str] = mapped_column(
        ForeignKey("ftp_account.id"), nullable=False
    )
    cidr: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    account: Mapped["FtpAccount"] = relationship(back_populates="allowed_ips")
