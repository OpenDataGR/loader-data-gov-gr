"""Shared-secret gate for the admin API.

This is deliberately a placeholder, not the long-term answer: the real
design (from the architecture discussion this project came out of) is
per-user authorization via Keycloak + a per-user CKAN API token, so
CKAN's own permission system enforces who can do what. That needs the
token-provisioning flow this service doesn't have yet. A single shared
admin key unblocks building admin-managed features (like the FTP
account API) now, without waiting on that -- swap this dependency out
once the real thing exists; every admin route already goes through
this one function.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status


def require_admin_key(x_admin_key: str = Header(default="")) -> None:
    expected = os.environ.get("LOADER_ADMIN_API_KEY", "")
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "LOADER_ADMIN_API_KEY is not configured on this deployment",
        )
    if not secrets.compare_digest(x_admin_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin key")


def require_ftp_sync_key(x_ftp_sync_key: str = Header(default="")) -> None:
    """A separate, narrower credential from the admin key above --
    deliberately: this gates the one endpoint that hands back FTP
    account *bcrypt hashes* (never plaintext passwords) to a remote
    FTP-receiving box's own sync job, so that box only ever holds a
    credential scoped to "read account hashes to sync," not the full
    admin key's much wider blast radius (every admin-only feature this
    service has) -- if that external box is ever compromised, this is
    what limits the damage. Bcrypt hashes are safe to expose to a
    caller that's meant to hold them anyway (that's the whole point of
    a one-way hash); the plaintext password is never involved here at
    all, matching how it's never stored anywhere after creation
    either."""
    expected = os.environ.get("LOADER_FTP_SYNC_KEY", "")
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "LOADER_FTP_SYNC_KEY is not configured on this deployment",
        )
    if not secrets.compare_digest(x_ftp_sync_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid ftp sync key")
