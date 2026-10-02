"""Local health signals about this box's own FTP/SFTP stack --
gathered so loader (which can never reach this box directly, see
README.md's "pull-based and outbound-only") can still show whether
ProFTPD and the file-relay are actually alive: this box reports on
itself during each account-sync cycle, loader just remembers the
latest report (see ``admin_ftp.report_ftp_box_status`` on that side).

``run`` is accepted purely so tests can inject a fake rather than
needing a real systemd/``ss`` on the test machine -- this box's own
CI already proves the real thing works on real Linux (see
``.github/workflows/ftp-forwarder-tests.yml``), not unit-tested
directly here.
"""

from __future__ import annotations

import ipaddress
import subprocess
from typing import Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _default_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def is_unit_active(unit: str, *, run: Runner = _default_run) -> bool:
    result = run(["systemctl", "is-active", "--quiet", unit])
    return result.returncode == 0


def is_port_listening(port: int, *, run: Runner = _default_run) -> bool:
    # `ss -tln` columns: State Recv-Q Send-Q "Local Address:Port" Peer...
    # Matched on the local-address column specifically (not a raw
    # substring search) so port 21 isn't also matched by a line for
    # port 210 or 2100.
    result = run(["ss", "-tln"])
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) < 4:
            continue
        local_address = columns[3]
        if local_address.rsplit(":", 1)[-1] == str(port):
            return True
    return False


def read_banned(jail: str = "loader-ftp", *, run: Runner = _default_run) -> list[str] | None:
    """The addresses fail2ban currently has banned in ``jail``, or None if
    it couldn't be asked (not installed, not running, no such jail) --
    which is not the same as "nobody is banned"."""
    result = run(["fail2ban-client", "status", jail])
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if "Banned IP list:" not in line:
            continue
        addresses = []
        for token in line.split("Banned IP list:", 1)[1].split():
            try:
                addresses.append(str(ipaddress.ip_address(token)))
            except ValueError:
                continue
        return sorted(addresses)
    return None


def unban(address: str, jail: str = "loader-ftp", *, run: Runner = _default_run) -> bool:
    """Lets ``address`` back in. The value comes from loader's reply, and
    this runs as root, so it must be an IP address and nothing else --
    checked here rather than trusted, and passed as a single argument (no
    shell involved) so there is nothing to inject into either way."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return run(["fail2ban-client", "set", jail, "unbanip", str(parsed)]).returncode == 0


DEFAULT_JAILS = ("loader-ftp", "loader-ftp-slow")


def read_banned_in(jails: tuple[str, ...] | list[str], *, run: Runner = _default_run) -> list[str] | None:
    """Everyone banned in any of ``jails`` (a fast burst jail and a slow-scanner
    one, see provision-ftp-box.yml), as one list. A jail that can't be asked
    -- e.g. the box was provisioned before it existed -- is skipped; None only
    when none of them could be, so a missing jail doesn't hide the others."""
    answers = [read_banned(jail, run=run) for jail in jails]
    answered = [a for a in answers if a is not None]
    if not answered:
        return None
    return sorted({address for bans in answered for address in bans})


def unban_everywhere(address: str, jails: tuple[str, ...] | list[str], *, run: Runner = _default_run) -> bool:
    """Lets ``address`` back in whichever jail holds it. True if at least one
    jail accepted it (see ``unban`` for why the address is validated)."""
    return any([unban(address, jail, run=run) for jail in jails])


def collect_status(
    *,
    proftpd_unit: str = "proftpd",
    relay_unit: str = "loader-ftp-forwarder",
    fail2ban_unit: str = "fail2ban",
    ftp_port: int = 21,
    sftp_port: int = 2222,
    run: Runner = _default_run,
) -> dict:
    return {
        "proftpd_active": is_unit_active(proftpd_unit, run=run),
        "ftp_port_listening": is_port_listening(ftp_port, run=run),
        "sftp_port_listening": is_port_listening(sftp_port, run=run),
        "relay_service_active": is_unit_active(relay_unit, run=run),
        "fail2ban_active": is_unit_active(fail2ban_unit, run=run),
    }
