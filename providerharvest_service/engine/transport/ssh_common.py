"""Shared SSH connection plumbing for every SSH-based transport
(``SFTPTransport``, ``ScpTransport``) -- same underlying
``paramiko.Transport``, same trust-on-first-use host-key pinning, same
secret-driven auth (password or private key). Per DESIGN.md's Phase 1.5
notes, SCP "reuses the exact same SSH connection and host-key pinning/
verification code as SFTPTransport (same trust model, same security
requirement)" -- this module is that shared code, factored out so there
is exactly one implementation of the security-critical host-key check,
not two copies that could silently drift apart.
"""

from __future__ import annotations

import base64
import hashlib
import io
from typing import Callable, Optional

import paramiko

from providerharvest_service.engine.validators import assert_safe_network_target
from providerharvest_service.engine.secrets.base import SecretBundle

DEFAULT_PORT = 22
DEFAULT_TIMEOUT_S = 30


class HostKeyMismatchError(Exception):
    """The server's current host key doesn't match the one pinned at
    registration time -- either the server changed keys for a real reason
    (in which case a human needs to re-confirm and re-pin, not this code
    silently accepting it), or something is impersonating it."""


def _fingerprint(key: paramiko.PKey) -> str:
    """SHA256 fingerprint, base64-encoded -- matches modern
    ``ssh-keygen -E sha256`` output, which is what a provider would see
    and be asked to confirm against, not paramiko's legacy MD5 default."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def fetch_host_key_fingerprint(
    host: str, port: int = DEFAULT_PORT, *, allow_private_ranges: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    transport_factory: Callable[[tuple], "paramiko.Transport"] = paramiko.Transport,
) -> str:
    """Connect just far enough to read the server's host key, without
    authenticating. Used both by every SSH transport's own pinning check
    and by the ``provider_source_fetch_host_key`` action, so a provider
    can be shown the real fingerprint to confirm before it's pinned."""
    assert_safe_network_target(host, port, allow_private_ranges=allow_private_ranges)
    transport = transport_factory((host, port))
    try:
        transport.start_client(timeout=timeout_s)
        key = transport.get_remote_server_key()
        if key is None:
            raise HostKeyMismatchError("Server did not present a host key")
        return _fingerprint(key)
    finally:
        transport.close()


def connect_and_authenticate(
    host: str, port: int, secret: SecretBundle, *,
    pinned_host_key_fingerprint: Optional[str],
    allow_private_ranges: bool = False,
    connect_timeout_s: int = DEFAULT_TIMEOUT_S,
    transport_factory: Callable[[tuple], "paramiko.Transport"] = paramiko.Transport,
) -> "paramiko.Transport":
    """Validates the network target, connects, verifies the host key
    against ``pinned_host_key_fingerprint`` (raising
    ``HostKeyMismatchError`` on any mismatch, or if nothing was pinned
    yet -- there is deliberately no "skip the check" option), then
    authenticates using the secret's username + password-or-private-key.
    Returns the live, authenticated ``paramiko.Transport`` for the caller
    to layer its own protocol client (``SFTPClient``, ``SCPClient`` --
    the same SSH connection, just a different subsystem) on top of.
    """
    # Re-validated here (not just at registration time) so a DNS rebind
    # between registration and this call is caught -- same requirement
    # as the HTTP transport.
    assert_safe_network_target(host, port, allow_private_ranges=allow_private_ranges)

    transport = transport_factory((host, port))
    transport.start_client(timeout=connect_timeout_s)

    key = transport.get_remote_server_key()
    fingerprint = _fingerprint(key) if key else None
    if pinned_host_key_fingerprint is None:
        transport.close()
        raise HostKeyMismatchError(
            "No host key has been pinned for this source -- register it via "
            "provider_source_fetch_host_key first"
        )
    if fingerprint != pinned_host_key_fingerprint:
        transport.close()
        raise HostKeyMismatchError(
            "Host key fingerprint changed: expected %s, got %s"
            % (pinned_host_key_fingerprint, fingerprint)
        )

    try:
        _authenticate(transport, secret)
    except Exception:
        transport.close()
        raise
    return transport


def _authenticate(transport: "paramiko.Transport", secret: SecretBundle) -> None:
    fields = secret.fields
    username = fields.get("username")
    if not username:
        raise ValueError("SSH secret bundle is missing 'username'")

    if "private_key_pem" in fields:
        pkey = paramiko.RSAKey.from_private_key(
            io.StringIO(fields["private_key_pem"]),
            password=fields.get("private_key_passphrase") or None,
        )
        transport.auth_publickey(username, pkey)
    elif "password" in fields:
        transport.auth_password(username, fields["password"])
    else:
        raise ValueError(
            "SSH secret bundle needs either 'password' or 'private_key_pem'"
        )
