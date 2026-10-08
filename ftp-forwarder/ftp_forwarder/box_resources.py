"""How loaded this box is, for loader's FTP box page: load, memory, disk, uptime and
how many FTP/SFTP sessions are open right now.

Read straight from ``/proc`` and ``shutil.disk_usage`` where possible (no commands, nothing to
inject into); only the session count asks ``ss``. Every figure is best effort: one
that cannot be read is ``None`` and the rest still report, because a monitoring
number must never be the reason the heartbeat (which also carries the access rules
and the account sync's result) fails. ``run`` and ``proc`` are injectable so tests need
no real box.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import time
from typing import Callable, Iterable

Runner = Callable[[list[str]], subprocess.CompletedProcess]

_MB = 1024 * 1024


def _default_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _load(proc: str) -> list[float] | None:
    text = _read(os.path.join(proc, "loadavg"))
    try:
        return [float(x) for x in text.split()[:3]] if text else None
    except ValueError:
        return None


def _meminfo(proc: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (_read(os.path.join(proc, "meminfo")) or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) // 1024  # kB -> MB
    return out


def _uptime(proc: str) -> int | None:
    text = _read(os.path.join(proc, "uptime"))
    try:
        return int(float(text.split()[0])) if text else None
    except (ValueError, IndexError):
        return None


def _disk(path: str) -> dict | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {"path": path, "total_mb": usage.total // _MB, "used_mb": usage.used // _MB}


def _net(proc: str, rate: NetRate | None) -> dict | None:
    iface = default_interface(proc)
    counters = net_counters(iface, proc) if iface else None
    if counters is None:
        return None
    speed = rate.update(*counters) if rate is not None else None
    return {"iface": iface, "rx_bytes": counters[0], "tx_bytes": counters[1],
            "rx_bps": speed[0] if speed else None, "tx_bps": speed[1] if speed else None}


def count_sessions(ports: Iterable[int], *, run: Runner = _default_run) -> dict[int, int] | None:
    """Established TCP connections to each of ``ports`` (a session is one connection)."""
    try:
        result = run(["ss", "-Htn", "state", "established"])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    counts = {int(p): 0 for p in ports}
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 3:
            continue
        local = columns[2] if len(columns) >= 4 else columns[0]  # Recv-Q Send-Q Local Peer
        try:
            port = int(local.rsplit(":", 1)[-1])
        except ValueError:
            continue
        if port in counts:
            counts[port] += 1
    return counts


def default_interface(proc: str = "/proc") -> str | None:
    """The interface the default route leaves by: the one whose traffic is the box's."""
    for line in (_read(os.path.join(proc, "net", "route")) or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            return fields[0]
    return None


def net_counters(iface: str, proc: str = "/proc") -> tuple[int, int] | None:
    """(bytes received, bytes sent) by ``iface`` since boot."""
    for line in (_read(os.path.join(proc, "net", "dev")) or "").splitlines():
        name, _, rest = line.partition(":")
        if name.strip() == iface:
            fields = rest.split()
            try:
                return int(fields[0]), int(fields[8])
            except (ValueError, IndexError):
                return None
    return None


def addresses(*, run: Runner = _default_run, iface: str | None = None) -> list[str]:
    """This machine's own global (non-link-local, non-loopback) addresses, IPv4 and IPv6 -- on ``iface``
    (the main one) when given, so container bridges do not clutter the list."""
    try:
        result = run(["ip", "-o", "addr", "show", "scope", "global"] + (["dev", iface] if iface else []))
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[str] = []
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        fields = line.split()
        if len(fields) > 3 and fields[2] in ("inet", "inet6"):
            try:
                address = str(ipaddress.ip_interface(fields[3]).ip)
            except ValueError:
                continue
            if address not in found:
                found.append(address)
    return found[:8]


class NetRate:
    """Bytes per second between two readings. Counters only ever grow, so the first reading has no
    rate (None), and one that went backwards (a reboot, a counter reset) restarts the series."""

    def __init__(self):
        self._last: tuple[float, int, int] | None = None

    def update(self, rx: int, tx: int, now: float | None = None) -> tuple[int, int] | None:
        now = time.monotonic() if now is None else now
        last, self._last = self._last, (now, rx, tx)
        if last is None or now <= last[0] or rx < last[1] or tx < last[2]:
            return None
        seconds = now - last[0]
        return int((rx - last[1]) / seconds), int((tx - last[2]) / seconds)


def collect(
    *,
    disks: Iterable[str] = ("/",),
    ftp_port: int = 21,
    sftp_port: int = 2222,
    proc: str = "/proc",
    run: Runner = _default_run,
    rate: NetRate | None = None,
) -> dict:
    mem = _meminfo(proc)
    swap_total, swap_free = mem.get("SwapTotal"), mem.get("SwapFree")
    sessions = count_sessions((ftp_port, sftp_port), run=run)
    seen = []
    for path in disks:
        info = _disk(path) if path else None
        if info and info["path"] not in [d["path"] for d in seen]:
            seen.append(info)
    return {
        "load": _load(proc),
        "cpus": os.cpu_count(),
        "mem_total_mb": mem.get("MemTotal"),
        "mem_available_mb": mem.get("MemAvailable"),
        "swap_used_mb": (swap_total - swap_free) if swap_total is not None and swap_free is not None else None,
        "uptime_s": _uptime(proc),
        "disks": seen,
        "ftp_sessions": sessions.get(ftp_port) if sessions else None,
        "sftp_sessions": sessions.get(sftp_port) if sessions else None,
        "addresses": addresses(run=run, iface=default_interface(proc)),
        "net": _net(proc, rate),
    }
