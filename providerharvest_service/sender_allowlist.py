"""Which addresses a push source accepts data from.

A provider can say, when registering a push source, "only my machine(s) may
send": an IPv4 or IPv6 address, a range (CIDR), or a hostname / URL of the sending
machine. Everything else is refused. The list lives in the source's config
(``allowed_senders``, canonical CIDR strings) and is enforced where the data
arrives:

* HTTPS push -- ``push_ingest`` checks the sender address before it even looks
  at the API key;
* FTP/SFTP push -- the list is copied onto the source's FTP account
  (``FtpAllowedIp``), which the FTP box turns into ProFTPD login rules
  (ftp-forwarder ``ip_limits.py``).

No list means no restriction, as before.

A hostname is looked up **once, when the form is saved**, and the addresses it
points to are what is stored -- not the name. (Checking a name per request would
put DNS on the upload path and let whoever controls that DNS record decide who
may upload.) The cost is that a sender whose address changes must update the
source; the form says so.

IPv6 works the same as IPv4 (proven against ProFTPD with ``UseIPv6 on``, FTP and
SFTP alike, 2026-10-07). An IPv4-mapped IPv6 address (``::ffff:1.2.3.4``) is refused:
write the IPv4 address instead. The FTP box must understand IPv6 entries too -- an
entry it could not express would silently become "no restriction" for that part of
the list -- so it refuses the whole list (and says so) rather than guess.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit

MAX_ENTRIES = 50
_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
MAX_ADDRESSES_PER_HOSTNAME = 16

# A dotted hostname. At least one dot on purpose: a bare word is far more likely a
# typo ("any", "all", "localhost") than the name of a sender.
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)

Resolver = Callable[[str], Iterable[str]]


class AllowlistError(ValueError):
    """Carries a message fit to show the person filling in the form."""


def normalize_cidr(value: str) -> str:
    """An address or range in canonical form ("203.0.113.9" -> "203.0.113.9/32").
    IPv4 or IPv6. Raises ValueError."""
    try:
        network = ipaddress.ip_network((value or "").strip(), strict=False)
    except ValueError as exc:
        raise ValueError("not a valid IP address or CIDR range: %r" % value) from exc
    if network.version == 6 and network.overlaps(_MAPPED):
        raise ValueError("%r is an IPv4 address written as IPv6 -- list the IPv4 address instead" % value)
    return str(network)


def _resolve(hostname: str) -> list[str]:
    """Every IPv4 and IPv6 address the name points to (a dual-stack sender may connect over either)."""
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise AllowlistError("could not look up %r: %s" % (hostname, exc.strerror or exc)) from exc
    return sorted({info[4][0].split("%")[0] for info in infos})  # (no zone ids: "fe80::1%eth0")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _host_of(token: str) -> str | None:
    """The hostname inside a URL-ish token ("https://sender.example.gr:8443/x",
    "sender.example.gr:21", "sender.example.gr/path"), or None."""
    candidate = token if "://" in token else "//" + token
    try:
        return urlsplit(candidate).hostname
    except ValueError:
        return None


def parse(text: str | None, *, resolve: Resolver | None = None) -> list[str]:
    """The canonical CIDR list for what was typed: entries separated by
    commas, semicolons, spaces or new lines, each an address, a range, or a
    hostname / URL (looked up now, see the module docstring). Empty input is an
    empty list (no restriction). Raises ``AllowlistError`` on the first bad
    entry. ``resolve`` is injectable so tests need no DNS."""
    resolve = resolve or _resolve  # looked up per call, so a test can replace the DNS lookup
    tokens = [t for t in re.split(r"[,;\s]+", text or "") if t]
    if len(tokens) > MAX_ENTRIES:
        raise AllowlistError("at most %d entries" % MAX_ENTRIES)

    networks: set[str] = set()
    for token in tokens:
        try:
            networks.add(normalize_cidr(token))
            continue
        except ValueError as ip_error:
            host = _host_of(token)
            if not host:
                raise AllowlistError(str(ip_error)) from None
            if "://" in token:
                try:
                    networks.add(normalize_cidr(host))  # a URL around an IP literal: "http://203.0.113.9:8080/x"
                    continue
                except ValueError:
                    pass
            # (Without a scheme, "203.0.113.0/99" is a bad range -- not an address with a path.)
            if not _HOSTNAME.match(host) or _is_ip_literal(host):
                # Not an address, and not something that looks like a hostname either:
                # say what is wrong with it as an address (the likelier intent).
                raise AllowlistError(str(ip_error)) from None
        try:
            addresses = list(resolve(host))
        except AllowlistError:
            raise
        except OSError as exc:
            raise AllowlistError("could not look up %r: %s" % (host, exc)) from exc
        if not addresses:
            raise AllowlistError("%r has no IP address" % host)
        if len(addresses) > MAX_ADDRESSES_PER_HOSTNAME:
            raise AllowlistError("%r points to %d addresses -- list the range instead" % (host, len(addresses)))
        for address in addresses:
            networks.add(normalize_cidr(address))

    if len(networks) > MAX_ENTRIES:
        raise AllowlistError("at most %d addresses or ranges in all" % MAX_ENTRIES)
    return sorted(networks, key=lambda n: (ipaddress.ip_network(n).version, ipaddress.ip_network(n).network_address, n))


def permits(allowed: list[str] | None, address: str | None) -> bool:
    """Whether ``address`` may send. No list: anyone. A list: only addresses in
    it -- and an address that could not be determined is not in it."""
    if not allowed:
        return True
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # a dual-stack listener reports an IPv4 sender this way
    return any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in allowed)
