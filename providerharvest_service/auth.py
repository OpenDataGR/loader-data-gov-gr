"""Identifies the caller of a request via a Keycloak-issued JWT bearer
token, validated against the realm's own signing keys (JWKS). This is
the "who is making this request" piece ckan_tokens.py needs but has
no way to answer on its own.

Deliberately built and fully tested against a self-signed test
keypair, independent of the real Keycloak client actually being
registered yet -- KEYCLOAK_JWKS_URL / KEYCLOAK_ISSUER /
KEYCLOAK_AUDIENCE are the only things that need filling in once that
registration happens; the validation logic itself doesn't change.
The key-lookup step is a narrow, injectable ``kid -> PEM string``
callable (not a hard dependency on any one JWKS-fetching library's
own network/caching behavior), so tests exercise the real signature-
verification code path with zero network calls -- a forged/expired/
wrong-issuer/wrong-audience/tampered token is rejected by the same
logic that will run in production, not a mocked-away approximation
of it.
"""

from __future__ import annotations

import os
from typing import Callable

import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient

KeyResolver = Callable[[str], str]


class AuthConfigError(RuntimeError):
    pass


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise AuthConfigError("%s is not configured on this deployment" % name)
    return value


_default_jwk_client: PyJWKClient | None = None


def _default_key_resolver(kid: str) -> str:
    global _default_jwk_client
    if _default_jwk_client is None:
        _default_jwk_client = PyJWKClient(_required_env("KEYCLOAK_JWKS_URL"))
    return _default_jwk_client.get_signing_key(kid).key


def _extract_kid(raw_token: str) -> str:
    try:
        header = jwt.get_unverified_header(raw_token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed token") from exc
    kid = header.get("kid")
    if not kid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token header has no kid")
    return kid


def decode_bearer_token(
    authorization: str,
    *,
    key_resolver: KeyResolver | None = None,
    issuer: str | None = None,
    audience: str | None = None,
) -> dict:
    """Validates a raw ``Authorization`` header value (``Bearer <jwt>``)
    and returns the token's decoded claims. Raises HTTPException(401)
    for anything wrong: missing/malformed header, bad signature,
    expired, wrong issuer, or wrong audience.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    raw_token = authorization[len("Bearer "):]

    kid = _extract_kid(raw_token)
    resolver = key_resolver or _default_key_resolver
    public_key = resolver(kid)

    try:
        return jwt.decode(
            raw_token,
            public_key,
            algorithms=["RS256"],
            issuer=issuer or _required_env("KEYCLOAK_ISSUER"),
            audience=audience or _required_env("KEYCLOAK_AUDIENCE"),
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token: %s" % exc) from exc


def require_current_username(authorization: str = Header(default="")) -> str:
    """FastAPI dependency: the validated caller's CKAN/Keycloak username
    (Keycloak's own ``preferred_username`` claim), for endpoints that
    need to know who's calling -- e.g. to call
    ``ckan_tokens.get_or_provision_ckan_client`` on their behalf.
    """
    claims = decode_bearer_token(authorization)
    username = claims.get("preferred_username")
    if not username:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "token has no preferred_username claim"
        )
    return username
