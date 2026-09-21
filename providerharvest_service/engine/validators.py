"""Network-target validation -- the SSRF-class defense for this extension.

A provider-supplied endpoint (HTTP URL, or an SFTP/SCP/FTP host:port) is a
target the harvester will connect to on a schedule with a real credential
attached. Without this check, a malicious or compromised registration could
point the harvester at cloud metadata services, loopback, or other internal
network targets reachable from the harvester host.

This must be called BOTH at registration time and again immediately before
every scheduled connection attempt (DNS can rebind between the two -- this
is the classic TOCTOU/DNS-rebinding flavour of SSRF).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_HTTP_SCHEMES = ("http", "https")

# Networks that should never be a valid provider target, even when the
# deployment otherwise allows private-range endpoints (see
# ``allow_private_ranges``): loopback and link-local (which covers the
# 169.254.169.254 cloud-metadata address on AWS/Azure/GCP) are unsafe
# regardless of topology.
_ALWAYS_BLOCKED_PREDICATES = (
    "is_loopback",
    "is_link_local",
    "is_multicast",
    "is_unspecified",
    "is_reserved",
)


class UnsafeNetworkTargetError(ValueError):
    """Raised when a provider-supplied endpoint resolves to a disallowed target."""


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
                    allow_private_ranges: bool) -> bool:
    for attr in _ALWAYS_BLOCKED_PREDICATES:
        if getattr(ip, attr):
            return True
    if not allow_private_ranges and ip.is_private:
        return True
    return False


def resolve_and_check_host(hostname: str, *, allow_private_ranges: bool = False,
                            resolver=socket.getaddrinfo) -> list[str]:
    """Resolve ``hostname`` and reject it if ANY resolved address is unsafe.

    Rejecting on any unsafe address (rather than just the first) matters
    because a host can resolve to multiple A/AAAA records; an attacker only
    needs one of them reachable.

    Returns the list of resolved IP strings on success, for callers that
    want to pin/log what was actually validated.
    """
    try:
        infos = resolver(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeNetworkTargetError(
            "Could not resolve host %r: %s" % (hostname, exc)
        ) from exc

    resolved_ips = sorted({info[4][0] for info in infos})
    if not resolved_ips:
        raise UnsafeNetworkTargetError("Host %r did not resolve to any address" % hostname)

    for ip_str in resolved_ips:
        ip = ipaddress.ip_address(ip_str)
        if _ip_is_blocked(ip, allow_private_ranges):
            raise UnsafeNetworkTargetError(
                "Host %r resolves to disallowed address %s" % (hostname, ip_str)
            )
    return resolved_ips


def assert_safe_http_url(url: str, *, allow_private_ranges: bool = False,
                          resolver=socket.getaddrinfo) -> list[str]:
    """Validate an HTTP(S) endpoint URL. Returns the resolved IPs.

    Rejects: non-http(s) schemes, URLs with embedded credentials
    (``user:pass@host``), and any host resolving to a blocked address range.
    """
    parts = urlsplit(url)

    if parts.scheme not in ALLOWED_HTTP_SCHEMES:
        raise UnsafeNetworkTargetError(
            "Scheme %r is not allowed (must be http or https)" % parts.scheme
        )
    if parts.username or parts.password:
        raise UnsafeNetworkTargetError("URLs with embedded credentials are not allowed")
    if not parts.hostname:
        raise UnsafeNetworkTargetError("URL has no host: %r" % url)

    return resolve_and_check_host(
        parts.hostname, allow_private_ranges=allow_private_ranges, resolver=resolver
    )


def assert_safe_network_target(host: str, port: int, *, allow_private_ranges: bool = False,
                                resolver=socket.getaddrinfo) -> list[str]:
    """Validate a bare host:port target (SFTP/SCP/FTP). Returns resolved IPs."""
    if not host:
        raise UnsafeNetworkTargetError("Empty host")
    if not (0 < port <= 65535):
        raise UnsafeNetworkTargetError("Port %r out of range" % port)
    return resolve_and_check_host(
        host, allow_private_ranges=allow_private_ranges, resolver=resolver
    )
