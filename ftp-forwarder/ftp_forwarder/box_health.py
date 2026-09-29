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


def collect_status(
    *,
    proftpd_unit: str = "proftpd",
    relay_unit: str = "loader-ftp-forwarder",
    ftp_port: int = 21,
    sftp_port: int = 2222,
    run: Runner = _default_run,
) -> dict:
    return {
        "proftpd_active": is_unit_active(proftpd_unit, run=run),
        "ftp_port_listening": is_port_listening(ftp_port, run=run),
        "sftp_port_listening": is_port_listening(sftp_port, run=run),
        "relay_service_active": is_unit_active(relay_unit, run=run),
    }
