"""Refuses every request from a blacklisted address (see access_control.py),
before anything else runs -- before the login gate, before any route. It is the
outermost middleware.

The address is the trusted client address (``client_addr.sender_address``: the
entry the gateway added, never one the client claims). A request whose address
cannot be determined is not blocked -- there is nothing to match it against --
and a whitelisted address is never blocked, even inside a blacklisted range.

The answer is a bare 403 with no explanation, and the refusal is recorded in the
Security box (method "Blocked address"; the recorder caps how many rows one
address can add per minute, so a scanner hammering a block cannot flood the table).
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from providerharvest_service import access_control, failed_auth
from providerharvest_service.client_addr import sender_address


class AccessControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        address = sender_address(request)
        if address and access_control.cached_snapshot().verdict(address) == access_control.BLOCK:
            await run_in_threadpool(failed_auth.record, request, failed_auth.BLOCKED, "", 403)
            return PlainTextResponse("Forbidden", status_code=403)
        return await call_next(request)
