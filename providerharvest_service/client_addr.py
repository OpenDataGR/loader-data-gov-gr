"""Working out who actually sent a request, behind the proxies in front
of this app.

Production's chain is Azure Application Gateway -> nginx -> this app.
The gateway sets ``X-Forwarded-For`` to the real client address *with its
port* (appending to whatever the client itself sent), and nginx
(``proxy_add_x_forwarded_for``) then appends the address it saw -- the
gateway's -- so the header ends ``..., <client>:<port>, <gateway>``. The
client-supplied part at the front is untrustworthy; the real client is
the entry just before the last one. (``request.client.host`` is no help:
uvicorn resolves it to the gateway's private address.)

That "one trusted hop" assumption is deliberate and fixed: nginx isn't
reachable except through the gateway, so nothing else can prepend a
forged trusted entry. With no ``X-Forwarded-For`` at all (local dev,
tests) it falls back to the socket's peer address.
"""

from __future__ import annotations

import ipaddress

from starlette.requests import Request


def _strip_port(entry: str) -> str:
    entry = entry.strip()
    if entry.startswith("["):  # [2001:db8::1]:443
        return entry[1:].split("]", 1)[0]
    if entry.count(":") == 1:  # 203.0.113.9:51234
        return entry.split(":", 1)[0]
    return entry  # bare IPv4, or bare IPv6


def _valid(address: str | None) -> str | None:
    if not address:
        return None
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        return None


def sender_address(request: Request) -> str | None:
    """The sender's IP address, or None when it can't be determined --
    never an unvalidated string, since this ends up stored and displayed."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        entries = [e for e in (part.strip() for part in forwarded.split(",")) if e]
        if entries:
            trusted = entries[-2] if len(entries) >= 2 else entries[-1]
            return _valid(_strip_port(trusted))
    return _valid(request.client.host if request.client else None)
