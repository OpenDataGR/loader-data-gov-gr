"""Cookie-based session gate for the whole human-facing loader -- the
provider portal, the admin pages and the API docs -- distinct from
admin_auth.py's shared API keys, which gate the JSON APIs a script or a
remote box calls, not a browser. A real per-user Keycloak login is still the eventual design
(see auth.py's own docstring for why that isn't wired up yet); this
unblocks a real login *today* without needing a Keycloak client
registered, using the same "deliberate placeholder, swap out later"
posture admin_auth.py already documents for its own shared key.

Stateless, signed cookie (HMAC-SHA256, stdlib only -- no new
dependency, no server-side session store to manage): the cookie itself
carries the username and an expiry, and its signature proves it wasn't
forged or extended past what LOADER_ADMIN_SESSION_SECRET authorized.
Rotating that secret invalidates every existing session at once, which
is the correct response to a suspected compromise.

Gating itself is a single middleware (``AdminSessionMiddleware``), not
a per-route dependency repeated on every admin UI route -- the same
"every admin route already goes through this one function" reasoning
``admin_auth.require_admin_key``'s own docstring gives, just enforced
at the ASGI layer instead so a newly added HTML route can't be
accidentally left ungated by forgetting to add a dependency to it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from urllib.parse import quote, urlencode

import bcrypt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

COOKIE_NAME = "loader_admin_session"
DEFAULT_TTL_S = 12 * 60 * 60  # 12h: long enough for a work session, short enough a stolen cookie doesn't work forever

# Deny by default: every path needs a session unless it is on this short
# list of things that are not a person in a browser. (The list used to
# be the other way round -- only the admin pages were gated -- which left
# the provider portal, and the harvested files it serves, open to anyone.
# Deny-by-default also means a route added later is protected without
# anyone having to remember to.)
#
# Exempt, and why -- each of these carries its own credential or is
# public on purpose:
#   /health                     the deploy smoke test and gateway probe
#   /admin/login, /admin/logout the form itself
#   /push/*                     a provider's system, X-Api-Key
#   /admin/ftp-relay/*          the FTP box, X-Ftp-Sync-Key
#   /admin/ftp-accounts-sync,
#   /admin/ftp-box-status,
#   /admin/ftp-login-attempts   the FTP box, X-Ftp-Sync-Key
#   /admin/ftp-accounts*        the JSON admin API, X-Admin-Key
#   /admin/harvest-db/health    ops check, X-Admin-Key
#   /me                         Keycloak bearer token
#   /auth/*                     the Keycloak login itself (start + callback)
#   /data/*                     a published source's newest output (the Load
#                               Box's API) -- public, only once switched on
#                               (a deprecated alias) -- public by design; gating
#                               it would break every consumer of it
# Matched with a "/" boundary, so "/admin/ftp-accounts" does not exempt
# the admin page "/admin/ftp" (or the other way round).
_EXEMPT_PATHS = frozenset({
    "/health", "/me", "/admin/login", "/admin/logout",
    "/admin/ftp-accounts-sync", "/admin/ftp-box-status", "/admin/ftp-login-attempts",
    "/admin/harvest-db/health",
})
_EXEMPT_PREFIXES = ("/push", "/data", "/auth", "/admin/ftp-accounts", "/admin/ftp-relay")


class SessionConfigError(RuntimeError):
    pass


def _is_gated(path: str) -> bool:
    if path in _EXEMPT_PATHS:
        return False
    return not any(path == prefix or path.startswith(prefix + "/") for prefix in _EXEMPT_PREFIXES)


def _secret() -> str:
    value = os.environ.get("LOADER_ADMIN_SESSION_SECRET", "")
    if not value:
        raise SessionConfigError("LOADER_ADMIN_SESSION_SECRET is not configured on this deployment")
    return value


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


PROFILE_COOKIE = "loader_profile"  # who the person is, for showing in the menu: signed, never trusted for access


def seal(purpose: str, data: dict, *, ttl_s: int = DEFAULT_TTL_S) -> str:
    """``data`` as a signed, expiring cookie value (``purpose`` keeps one kind of cookie from standing in for another)."""
    payload = base64.urlsafe_b64encode(json.dumps({**data, "exp": int(time.time()) + ttl_s}).encode("utf-8")).decode("ascii")
    return "%s.%s" % (payload, _sign(("%s:%s" % (purpose, payload)).encode("ascii"), _secret()))


def unseal(purpose: str, value: str) -> dict | None:
    try:
        payload, signature = value.rsplit(".", 1)
        if not hmac.compare_digest(signature, _sign(("%s:%s" % (purpose, payload)).encode("ascii"), _secret())):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, SessionConfigError, UnicodeError, binascii.Error):
        return None
    return data if isinstance(data, dict) and time.time() <= data.get("exp", 0) else None


SUPERADMIN_ONLY_MESSAGE = "Only super admins can open the Data boxes and the Security box."


def needs_superadmin(path: str) -> bool:
    """The pages (and their actions) only a super admin may use: approving, rejecting and the data retention of a source (they are
    on the Extract Box for a super admin), the Data boxes page with its box actions and log viewers, and everything in the Security box. The per-account FTP actions under /admin/ftp/<account>/... are part
    of managing a source and stay open to everyone who is signed in."""
    return (
        path.startswith("/admin/sources/")  # approving or rejecting a source, its data retention, and the other actions of the old admin dashboard
        or path == "/admin/ftp" or path.startswith(("/admin/ftp/http-box", "/admin/ftp/boxes/"))
        or path == "/admin/security" or path.startswith("/admin/security/")
    )


def user_info(username: str, profile_cookie: str) -> dict:
    """What the menu shows about the signed-in person. The profile cookie only adds detail; it counts only when it is
    validly signed and is about the same user name as the session itself."""
    profile = unseal("profile", profile_cookie or "") or {}
    if profile.get("u") != username:
        profile = {}
    if profile.get("sys"):
        access = "CKAN sysadmin"
    elif profile.get("norg"):
        shown = profile.get("orgs") or []
        access = "Administrator of %d organization%s" % (profile["norg"], "" if profile["norg"] == 1 else "s")
        if shown:
            access += " (%s%s)" % (", ".join(shown), ", ..." if profile["norg"] > len(shown) else "")
    elif profile.get("role"):
        access = "Role %s" % profile["role"]
    else:
        access = ""
    # Without a valid profile cookie nobody is a super admin: it is the cookie, signed by this server at login, that says so.
    return {"username": username, "sub": profile.get("sub") or "", "name": profile.get("n") or "", "email": profile.get("e") or "",
            "via": profile.get("via") or "Unknown", "access": access, "super": bool(profile.get("super"))}


def create_session_cookie(username: str, *, ttl_s: int = DEFAULT_TTL_S) -> str:
    expiry = int(time.time()) + ttl_s
    payload = ("%s:%d" % (username, expiry)).encode("utf-8")
    signature = _sign(payload, _secret())
    return "%s.%s" % (base64.urlsafe_b64encode(payload).decode("ascii"), signature)


def verify_session_cookie(token: str) -> str | None:
    try:
        secret = _secret()
    except SessionConfigError:
        return None
    try:
        encoded_payload, signature = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(encoded_payload.encode("ascii"))
    except (ValueError, binascii.Error):
        return None
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return None
    try:
        username, expiry_str = payload.decode("utf-8").rsplit(":", 1)
        expiry = int(expiry_str)
    except (ValueError, UnicodeDecodeError):
        return None
    if time.time() > expiry:
        return None
    return username


def verify_credentials(username: str, password: str) -> bool:
    expected_username = os.environ.get("LOADER_ADMIN_UI_USERNAME", "")
    expected_hash = os.environ.get("LOADER_ADMIN_UI_PASSWORD_HASH", "")
    if not expected_username or not expected_hash:
        raise SessionConfigError(
            "LOADER_ADMIN_UI_USERNAME / LOADER_ADMIN_UI_PASSWORD_HASH are not configured on this deployment"
        )
    # bcrypt.checkpw runs unconditionally, before the username check --
    # not short-circuited by `and` -- so a wrong username can't be
    # distinguished from a wrong password by response timing alone.
    password_ok = bcrypt.checkpw(password.encode("utf-8"), expected_hash.encode("utf-8"))
    username_ok = hmac.compare_digest(username, expected_username)
    return username_ok and password_ok


class AdminSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_gated(request.url.path):
            token = request.cookies.get(COOKIE_NAME, "")
            username = verify_session_cookie(token) if token else None
            if username is not None:
                request.state.user = user_info(username, request.cookies.get(PROFILE_COOKIE, ""))
            if username is not None and needs_superadmin(request.url.path) and not request.state.user["super"]:
                return RedirectResponse("/provider/sources?%s" % urlencode({"flash": SUPERADMIN_ONLY_MESSAGE, "flash_kind": "error"}), status_code=303)
            if not token or username is None:
                next_path = request.url.path
                if request.url.query:
                    next_path += "?" + request.url.query
                return RedirectResponse(
                    "/admin/login?next=%s" % quote(next_path, safe=""), status_code=303
                )
        return await call_next(request)
