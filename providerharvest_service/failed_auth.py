"""Records failed authentication on loader's own HTTP side, into the same
table as the FTP/SFTP refusals (``FtpLoginAttempt``), so the failed-login
report covers every way in.

Two sources:

* the web login form (``record`` called from ``ui_admin_login``, which knows
  the username that was typed), and
* a middleware that notices any ``401`` response -- a wrong or missing API
  key, sync key or bearer token -- and files it under the credential that
  path uses. Catching it on the way out means no auth dependency has to
  remember to report, and a new one is covered automatically.

Never records the secret that was presented, only that one was refused and
from where. Recording is best-effort and bounded: it must never turn a 401
into a 500, and a flood of bad requests from one address must not turn into
a flood of rows (see ``MAX_PER_ADDRESS_PER_MINUTE``).
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import delete, func, select
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from providerharvest_service import db
from providerharvest_service.admin_ftp import ATTEMPT_RETENTION, clean_username
from providerharvest_service.client_addr import sender_address
from providerharvest_service.models import FtpLoginAttempt

logger = logging.getLogger(__name__)

HTTPS_PORT = 443
MAX_PER_ADDRESS_PER_MINUTE = 30

WEB_LOGIN = "Web login"
PUSH_KEY = "Push API key"
ADMIN_KEY = "Admin API key"
BOX_KEY = "FTP box key"
BEARER = "Bearer token"
OTHER_HTTP = "HTTP"

# Longest prefix wins, so the box's endpoints are not taken for the admin API
# whose path they share the start of.
_BOX_PREFIXES = ("/admin/ftp-accounts-sync", "/admin/ftp-box-status", "/admin/ftp-login-attempts", "/admin/ftp-relay")
_ADMIN_PREFIXES = ("/admin/ftp-accounts", "/admin/harvest-db")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def channel_for_path(path: str) -> str:
    if any(_under(path, p) for p in _BOX_PREFIXES):
        return BOX_KEY
    if any(_under(path, p) for p in _ADMIN_PREFIXES):
        return ADMIN_KEY
    if _under(path, "/push"):
        return PUSH_KEY
    if _under(path, "/me"):
        return BEARER
    return OTHER_HTTP


def _record(address: str, channel: str, username: str, code: int) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    with db.SessionLocal() as session:
        recent = session.scalar(
            select(func.count()).select_from(FtpLoginAttempt).where(
                FtpLoginAttempt.ip == address,
                FtpLoginAttempt.channel == channel,
                FtpLoginAttempt.at >= now - datetime.timedelta(minutes=1),
            )
        )
        if recent >= MAX_PER_ADDRESS_PER_MINUTE:
            return
        session.add(FtpLoginAttempt(
            hostname="loader", at=now, ip=address, port=HTTPS_PORT,
            username=clean_username(username), code=code, channel=channel,
        ))
        session.execute(delete(FtpLoginAttempt).where(FtpLoginAttempt.at < now - ATTEMPT_RETENTION))
        session.commit()


def record(request: Request, channel: str, username: str = "", code: int = 401) -> None:
    """Best-effort: an address we cannot determine, or a database hiccup,
    means no row -- never an error for the request being refused."""
    address = sender_address(request)
    if address is None:
        return
    try:
        _record(address, channel, username, code)
    except Exception:  # noqa: BLE001 -- see docstring
        logger.exception("could not record a failed authentication from %s", address)


class FailedAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if response.status_code == 401:
            path = request.url.path
            channel = channel_for_path(path)
            # A push source's name is in the path and is public (it is in the
            # URL the provider was given): worth showing, unlike the key.
            parts = path.split("/")
            username = parts[2] if channel == PUSH_KEY and len(parts) > 2 else ""
            await run_in_threadpool(record, request, channel, username, 401)
        return response
