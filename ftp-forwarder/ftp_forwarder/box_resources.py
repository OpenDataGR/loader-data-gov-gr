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

import os
import shutil
import subprocess
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


def collect(
    *,
    disks: Iterable[str] = ("/",),
    ftp_port: int = 21,
    sftp_port: int = 2222,
    proc: str = "/proc",
    run: Runner = _default_run,
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
    }
