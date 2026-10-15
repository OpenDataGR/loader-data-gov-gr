"""Login with the data.gov.gr Keycloak (OpenID Connect, authorization-code flow with PKCE).

A visitor without a session is sent to Keycloak; Keycloak sends them back to ``/auth/callback`` with a one-time
code; the loader trades the code for tokens (with its client secret, server to server), checks the tokens'
signatures against the realm's published keys, requires the configured realm role, and then starts the *same*
signed session cookie the shared-password login always started -- so the rest of the site (the gate, "who did
this" logging) needs no change, it just now sees the person's own Keycloak user name.

Off until configured: ``enabled()`` is true only when the issuer, the client id and the client secret are all set
(see ``deploy/README.md``). Nothing here is tied to one Keycloak: the endpoints come from the realm's discovery
document.

What is checked, and why:
* ``state`` (a random value kept in a short-lived signed cookie and echoed back by Keycloak) -- stops a login
  response that this browser did not ask for;
* PKCE (``code_verifier`` kept in the same cookie) and ``nonce`` -- bind the code and the ID token to this browser;
* the ID token's signature, issuer, audience (this client), expiry and nonce;
* the access token's signature, issuer, expiry and authorized party -- it is where Keycloak puts the realm roles;
* the required role (default ``loader-admin``): a person Keycloak knows but who was never given it signs in
  successfully there and is refused here, with the reason shown and the attempt recorded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import jwt
import requests
from jwt import PyJWKClient

from providerharvest_service import admin_session, ckan_access

logger = logging.getLogger(__name__)

TX_COOKIE = "loader_oidc_tx"
ID_COOKIE = "loader_oidc_id"  # the ID token, kept so that logging out can tell Keycloak which session to end
TX_TTL_S = 600
DEFAULT_ROLE = "loader-admin"
_ASYMMETRIC = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
_HTTP_TIMEOUT_S = 15
_DISCOVERY_TTL_S = 3600


class OidcError(Exception):
    """A login that cannot go on, with a message fit to show the person."""


class NotAllowed(OidcError):
    """Keycloak knows the person, but this account may not use the loader."""

    def __init__(self, username: str, reason: str):
        super().__init__("You signed in as %s, but this account is not allowed to use the loader: %s" % (username, reason))
        self.username = username
        self.reason = reason


# -- configuration -----------------------------------------------------------------------------------------------------------

def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def issuer() -> str:
    return _env("LOADER_OIDC_ISSUER").rstrip("/")


def client_id() -> str:
    return _env("LOADER_OIDC_CLIENT_ID")


def required_role() -> str:
    return _env("LOADER_OIDC_REQUIRED_ROLE") or DEFAULT_ROLE


def superadmin_role() -> str:
    """With the ``role`` rule, the realm role that makes someone a super admin (Data boxes and Security box)."""
    return _env("LOADER_OIDC_SUPERADMIN_ROLE") or "loader-superadmin"


def access_rule() -> str:
    """Who is let in once Keycloak has said who they are: ``role`` (the realm role, the default) or
    ``ckan-org-admin`` (a CKAN sysadmin or an administrator of at least one CKAN organization -- see ckan_access.py)."""
    return "ckan-org-admin" if _env("LOADER_OIDC_ACCESS").lower() in ("ckan-org-admin", "ckan") else "role"


def enabled() -> bool:
    return bool(issuer() and client_id() and _env("LOADER_OIDC_CLIENT_SECRET"))


def password_login_allowed() -> bool:
    """The shared username/password stays usable until ``LOADER_ADMIN_PASSWORD_LOGIN=off`` is set."""
    return _env("LOADER_ADMIN_PASSWORD_LOGIN").lower() not in ("off", "0", "no", "false")


def redirect_uri(request_base_url: str) -> str:
    explicit = _env("LOADER_OIDC_REDIRECT_URI")
    if explicit:
        return explicit
    # Behind the gateway the request itself says "http" (see ui_provider._push_upload_url), so a configured
    # public address wins over what the request looks like.
    return (_env("LOADER_PUBLIC_BASE_URL") or request_base_url).rstrip("/") + "/auth/callback"


# -- talking to Keycloak (both replaceable in tests) ---------------------------------------------------------------------------

_http = requests
_discovery_cache: dict[str, tuple[float, dict]] = {}


def discovery() -> dict:
    base = issuer()
    cached = _discovery_cache.get(base)
    if cached and time.time() - cached[0] < _DISCOVERY_TTL_S:
        return cached[1]
    try:
        resp = _http.get(base + "/.well-known/openid-configuration", timeout=_HTTP_TIMEOUT_S)
        document = resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError) as exc:
        raise OidcError("The login server could not be reached (%s). Try again in a moment." % exc) from exc
    if not isinstance(document, dict) or document.get("issuer", "").rstrip("/") != base:
        raise OidcError("The login server answered with something unexpected.")
    _discovery_cache[base] = (time.time(), document)
    return document


_jwk_clients: dict[str, PyJWKClient] = {}


def _signing_key(token: str, jwks_uri: str):
    client = _jwk_clients.setdefault(jwks_uri, PyJWKClient(jwks_uri))
    return client.get_signing_key_from_jwt(token).key


# -- the short-lived cookie that ties a callback to the browser that started the login -------------------------------------------

def _seal(data: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
    signature = hmac.new(admin_session._secret().encode("utf-8"), b"oidc-tx:" + payload.encode("ascii"), hashlib.sha256).hexdigest()
    return "%s.%s" % (payload, signature)


def _unseal(value: str) -> dict | None:
    try:
        payload, signature = value.rsplit(".", 1)
        expected = hmac.new(admin_session._secret().encode("utf-8"), b"oidc-tx:" + payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, admin_session.SessionConfigError, UnicodeError):
        return None
    return data if isinstance(data, dict) and time.time() <= data.get("exp", 0) else None


# -- the flow ------------------------------------------------------------------------------------------------------------------------

def start(request_base_url: str, next_url: str) -> tuple[str, str]:
    """(the Keycloak address to send the browser to, the value of the short-lived cookie to set)."""
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    url = discovery()["authorization_endpoint"] + "?" + urlencode({
        "client_id": client_id(), "response_type": "code", "scope": "openid profile email",
        "redirect_uri": redirect_uri(request_base_url), "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    cookie = _seal({"state": state, "nonce": nonce, "verifier": verifier, "next": next_url, "exp": int(time.time()) + TX_TTL_S})
    return url, cookie


def _decode(token: str, jwks_uri: str, *, audience: str | None = None) -> dict:
    """Verifies the signature, issuer and expiry; the audience only when one is given (an access token's audience
    is Keycloak's own, not this client's)."""
    try:
        return jwt.decode(token, _signing_key(token, jwks_uri), algorithms=_ASYMMETRIC, issuer=issuer(), audience=audience,
                          options={"require": ["exp", "iss"], "verify_aud": audience is not None})
    except jwt.PyJWTError as exc:
        raise OidcError("The login could not be verified (%s)." % exc) from exc


def _roles(*claim_sets: dict) -> set[str]:
    roles: set[str] = set()
    for claims in claim_sets:
        found = (claims.get("realm_access") or {}).get("roles")
        if isinstance(found, list):
            roles.update(r for r in found if isinstance(r, str))
    return roles


def finish(request_base_url: str, tx_cookie: str, params: dict) -> tuple[str, str, str, dict]:
    """Completes a login: (the user name to start a session for, where to send them afterwards, the ID token, what to show about them).
    Raises ``OidcError`` (``NotAllowed`` for a person who lacks the role)."""
    if params.get("error"):
        raise OidcError("The login server declined the sign-in (%s)." % str(params.get("error_description") or params["error"])[:200])
    tx = _unseal(tx_cookie or "")
    if tx is None:
        raise OidcError("This sign-in has expired or did not start here. Please start again.")
    if not hmac.compare_digest(str(params.get("state", "")), str(tx.get("state", ""))) or not params.get("code"):
        raise OidcError("This sign-in response does not belong to this browser. Please start again.")

    doc = discovery()
    try:
        resp = _http.post(
            doc["token_endpoint"], timeout=_HTTP_TIMEOUT_S, auth=(client_id(), _env("LOADER_OIDC_CLIENT_SECRET")),
            data={"grant_type": "authorization_code", "code": params["code"], "redirect_uri": redirect_uri(request_base_url), "code_verifier": tx["verifier"]},
        )
        tokens = resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError) as exc:
        raise OidcError("The login server could not be reached (%s). Try again in a moment." % exc) from exc
    if not isinstance(tokens, dict) or not tokens.get("id_token") or not tokens.get("access_token"):
        logger.warning("oidc: the token endpoint refused the code (HTTP %s)", getattr(resp, "status_code", "?"))
        raise OidcError("The login server did not accept the sign-in. Please start again.")

    jwks_uri = doc["jwks_uri"]
    ident = _decode(tokens["id_token"], jwks_uri, audience=client_id())
    if not hmac.compare_digest(str(ident.get("nonce", "")), str(tx.get("nonce", ""))):
        raise OidcError("This sign-in response does not belong to this browser. Please start again.")
    access = _decode(tokens["access_token"], jwks_uri)
    if access.get("azp") not in (None, client_id()):
        raise OidcError("This sign-in was issued to a different application.")
    if access.get("sub") != ident.get("sub"):
        raise OidcError("The sign-in tokens do not belong to the same person.")

    username = str(ident.get("preferred_username") or access.get("preferred_username") or "").strip()
    if not username or len(username) > 100 or any(ord(c) < 32 for c in username):
        raise OidcError("The login server did not say who you are (no user name).")
    full_name = str(ident.get("name") or " ".join(filter(None, [ident.get("given_name"), ident.get("family_name")]))).strip()
    profile = {"u": username, "n": full_name[:100], "e": str(ident.get("email") or "")[:120], "via": "Keycloak (data.gov.gr)", "sub": str(ident.get("sub") or "")[:80]}
    if access_rule() == "ckan-org-admin":
        try:
            verdict = ckan_access.check(str(ident.get("sub") or ""))
        except ckan_access.CkanAccessError as exc:
            raise OidcError(str(exc)) from exc  # fails closed: CKAN could not be asked, so nobody gets in this way
        if not verdict.allowed:
            raise NotAllowed(username, verdict.reason)
        profile.update({"sys": verdict.sysadmin, "orgs": list(verdict.organizations[:4]), "norg": len(verdict.organizations),
                        "super": verdict.sysadmin})
    elif required_role() not in _roles(access, ident):
        raise NotAllowed(username, "it needs the role %s. Ask an administrator." % required_role())
    else:
        profile["role"] = required_role()
        profile["super"] = superadmin_role() in _roles(access, ident)
    return username, str(tx.get("next") or ""), tokens["id_token"], profile


def logout_url(request_base_url: str, id_token_hint: str = "") -> str | None:
    """Where to send the browser to end the Keycloak session too, or None when it cannot be worked out."""
    try:
        endpoint = discovery().get("end_session_endpoint")
    except OidcError:
        return None
    if not endpoint:
        return None
    query = {"client_id": client_id(), "post_logout_redirect_uri": (_env("LOADER_PUBLIC_BASE_URL") or request_base_url).rstrip("/") + "/admin/login"}
    if id_token_hint:
        query["id_token_hint"] = id_token_hint  # with it Keycloak ends the session without asking "are you sure?"
    return endpoint + "?" + urlencode(query)
