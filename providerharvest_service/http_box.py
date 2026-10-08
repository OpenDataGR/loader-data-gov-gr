"""The HTTP box: loader itself, as a box on the "Data boxes" page, next to the FTP/SFTP boxes.

Same idea as an FTP box -- see how loaded it is, run a few actions -- but this one is the
process serving the page, so it is local rather than a heartbeat away:

* **resources** are read live from ``/proc`` (the VM is shared with other projects, so load,
  memory and disk are the VM's), plus loader's own: workers and open connections to nginx (port 80: TLS ends at the Azure gateway);
* **diagnostics** runs here, now, and its snapshot is stored like an FTP box's;
* **reload workers** sends gunicorn's master ``SIGHUP``: new workers start, the old ones finish
  their requests and leave. Nothing is unavailable, nothing is dropped;
* **restart service** sends the master ``SIGTERM`` -- what a deploy does -- and systemd
  (``Restart=always``) starts loader again. The *whole* of loader is unavailable for about
  15 seconds: the web pages, the API, provider pushes, the FTP boxes' heartbeats and any
  harvest in progress. It asks for confirmation, and is refused twice within two minutes.

The signal goes out only after the response is on its way, so the page that asked is not the
casualty, and only to a gunicorn master that is this process's own parent and runs as the same
user -- never to a pid taken from anywhere else. The outcome is recorded in the same
history as the FTP boxes' actions (``FtpBoxCommand``, host name ``loader``).
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import subprocess
import time
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import box_commands
from providerharvest_service.box_commands import LOG_OUTPUT, MAX_OUTPUT, Action, CommandError
from providerharvest_service.models import FtpBoxCommand

NAME = "loader"
SIGHUP, SIGTERM = 1, 15  # Linux's numbers, spelled out so this module imports (and tests run) anywhere
SECTION_OUTPUT = 2000
RESTART_COOLDOWN = datetime.timedelta(minutes=2)

ACTIONS: dict[str, Action] = {a.key: a for a in (
    Action("diagnostics", "Run diagnostics", "A read-only snapshot: uptime, memory, disk, service states and recent log lines."),
    Action("reload-workers", "Reload workers",
           "Replaces loader's worker processes one by one. No downtime, nothing dropped.",
           "Reload loader's workers? Nothing is interrupted."),
    Action("restart-service", "Restart the whole service",
           "Restarts ALL of loader: web pages, API, pushes and harvests are unavailable for about 15 seconds.",
           "Restart ALL of loader? The web pages, the API, provider pushes and the FTP boxes' check-ins are "
           "unavailable for about 15 seconds, and a harvest in progress is cut. Use Reload workers instead unless you must."),
)}

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _default_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def _service_unit() -> str:
    return os.environ.get("LOADER_SERVICE_UNIT", "loader-data-gov-gr")


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


# -- the gunicorn master ---------------------------------------------------------------------

def gunicorn_master(*, pid: int | None = None, proc: str = "/proc", uid: int | None = None) -> int | None:
    """The pid of the gunicorn master that started this worker, or None when this process is not
    one (a development server, a test): then there is nothing to signal. Checked rather than
    assumed because it is what a signal is about to be sent to."""
    pid = os.getppid() if pid is None else pid
    uid = (os.getuid() if hasattr(os, "getuid") else None) if uid is None else uid
    cmdline = _read("%s/%d/cmdline" % (proc, pid))
    if not cmdline or "gunicorn" not in cmdline or "providerharvest_service.app" not in cmdline:
        return None
    try:
        if uid is not None and os.stat("%s/%d" % (proc, pid)).st_uid != uid:
            return None
    except OSError:
        return None
    return pid


# -- network -----------------------------------------------------------------------------------

def _default_interface(proc: str) -> str | None:
    for line in (_read(proc + "/net/route") or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            return fields[0]
    return None


def _counters(iface: str, proc: str) -> tuple[int, int] | None:
    for line in (_read(proc + "/net/dev") or "").splitlines():
        name, _, rest = line.partition(":")
        if name.strip() == iface:
            fields = rest.split()
            try:
                return int(fields[0]), int(fields[8])
            except (ValueError, IndexError):
                return None
    return None


_last_sample: tuple[float, int, int] | None = None


def network(*, proc: str = "/proc", sleep=time.sleep, clock=time.monotonic) -> dict | None:
    """Traffic on the main interface: totals since boot and bytes per second. Each worker keeps its
    previous reading; without a recent one (the first look, or after a quiet while) a second one is
    taken half a second later, so the page always has a rate to show."""
    global _last_sample
    iface = _default_interface(proc)
    counters = _counters(iface, proc) if iface else None
    if counters is None:
        return None
    now = clock()
    previous = _last_sample
    if previous is None or now - previous[0] > 120 or now <= previous[0]:
        previous = (now, *counters)
        sleep(0.5)
        counters = _counters(iface, proc) or counters
        now = clock()
    _last_sample = (now, *counters)
    seconds = now - previous[0]
    rx_bps = tx_bps = None
    if seconds > 0 and counters[0] >= previous[1] and counters[1] >= previous[2]:
        rx_bps, tx_bps = int((counters[0] - previous[1]) / seconds), int((counters[1] - previous[2]) / seconds)
    return {"iface": iface, "rx_bytes": counters[0], "tx_bytes": counters[1], "rx_bps": rx_bps, "tx_bps": tx_bps}


def own_addresses(*, run=None, iface: str | None = None) -> list[str]:
    """The machine's global addresses on the main interface (not the Docker bridges)."""
    run = run or _default_run
    try:
        out = run(["ip", "-o", "addr", "show", "scope", "global"] + (["dev", iface] if iface else []))
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[str] = []
    for line in out.stdout.splitlines() if out.returncode == 0 else []:
        fields = line.split()
        if len(fields) > 3 and fields[2] in ("inet", "inet6"):
            try:
                address = str(ipaddress.ip_interface(fields[3]).ip)
            except ValueError:
                continue
            if address not in found:
                found.append(address)
    return found[:8]


# -- resources ---------------------------------------------------------------------------------

def _meminfo(proc: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (_read(proc + "/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) // 1024
    return out


def collect_resources(*, disks=("/",), proc: str = "/proc", run: Runner = _default_run, master: int | None = None) -> dict:
    """The same figures an FTP box reports, plus loader's workers and open connections to nginx (port 80: TLS ends at the Azure gateway)."""
    import shutil

    load = None
    try:
        load = [float(x) for x in (_read(proc + "/loadavg") or "").split()[:3]] or None
    except ValueError:
        load = None
    mem = _meminfo(proc)
    swap_total, swap_free = mem.get("SwapTotal"), mem.get("SwapFree")
    uptime = None
    try:
        uptime = int(float((_read(proc + "/uptime") or "").split()[0]))
    except (ValueError, IndexError):
        pass
    seen: list[dict] = []
    for path in disks:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if path not in [d["path"] for d in seen]:
            seen.append({"path": path, "total_mb": usage.total // 1048576, "used_mb": usage.used // 1048576})

    http_conns = workers = None
    try:
        out = run(["ss", "-Htn", "state", "established"])
        if out.returncode == 0:
            http_conns = sum(1 for line in out.stdout.splitlines() if line.split()[2:3] and line.split()[2].rsplit(":", 1)[-1] == "80")
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    master = gunicorn_master(proc=proc) if master is None else master
    since = None
    if master:
        try:
            out = run(["pgrep", "-P", str(master), "-c"])
            workers = int(out.stdout.strip()) if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        try:
            since = int(time.time() - os.stat("%s/%d" % (proc, master)).st_ctime)
        except OSError:
            pass
    return {
        "load": load, "cpus": os.cpu_count(), "mem_total_mb": mem.get("MemTotal"), "mem_available_mb": mem.get("MemAvailable"),
        "swap_used_mb": (swap_total - swap_free) if swap_total is not None and swap_free is not None else None,
        "uptime_s": uptime, "disks": seen, "http_connections": http_conns, "workers": workers, "service_up_s": since,
        "addresses": own_addresses(run=run, iface=_default_interface(proc)), "net": network(proc=proc),
    }


# -- actions -----------------------------------------------------------------------------------

def _clip(text: str, limit: int) -> str:
    text = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())
    return text if len(text) <= limit else "...(cut)...\n" + text[-limit:]


def log_units() -> dict[str, str]:
    """What the log viewer may read: a short word -> the journal unit. Fixed here."""
    return {"loader": _service_unit(), "nginx": "nginx"}


def read_logs(service: str, lines: int, *, run: Runner = _default_run) -> tuple[bool, str]:
    unit = log_units()[service]
    try:
        done = run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short"])
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "could not read the journal: %s" % exc
    text = (done.stdout or done.stderr or "").strip()
    if done.returncode != 0:
        return False, "journalctl for %s failed: %s" % (unit, text or "no output")
    return True, "== last %d lines of %s" % (lines, unit) + chr(10) + (text or "(nothing logged)")


def diagnostics(*, run: Runner = _default_run) -> str:
    unit = _service_unit()
    sections = [("uptime", ["uptime"]), ("memory", ["free", "-m"]), ("disk", ["df", "-h", "/"]),
                ("unit " + unit, ["systemctl", "is-active", unit]), ("unit nginx", ["systemctl", "is-active", "nginx"]),
                ("log " + unit, ["journalctl", "-u", unit, "-n", "15", "--no-pager", "-o", "short"])]
    out = []
    for title, cmd in sections:
        try:
            done = run(cmd)
            text = (done.stdout or done.stderr or "").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            text = "could not run: %s" % exc
        out.append("== %s\n%s" % (title, _clip(text, SECTION_OUTPUT)))
    return "\n\n".join(out)


def perform(session: Session, action: str, who: str, *, run: Runner | None = None,
            master: int | None = None, now: datetime.datetime | None = None) -> tuple[FtpBoxCommand, Callable[[], None] | None]:
    """Do ``action`` for the HTTP box. Returns the recorded history row and, for the two that
    signal gunicorn, the function that sends the signal -- to be called once the response has
    gone out. Raises ``CommandError`` for an unknown action, when there is no gunicorn master to
    signal, or when a restart was already asked for in the last two minutes."""
    run = run or _default_run  # looked up now, so a test can replace it
    parsed = box_commands.parse_log_action(action)
    if action not in ACTIONS and not (parsed and parsed[0] in log_units()):
        raise CommandError("Unknown action.")
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    row = FtpBoxCommand(hostname=NAME, action=action, requested_by=(who or "")[:100], requested_at=now,
                        sent_at=now, finished_at=now, state="ok")
    send = None
    if parsed:
        ok, text = read_logs(parsed[0], parsed[1], run=run)
        row.state, row.output = ("ok" if ok else "failed"), _clip(text, LOG_OUTPUT)
    elif action == "diagnostics":
        row.output = _clip(diagnostics(run=run), MAX_OUTPUT)
    else:
        pid = gunicorn_master() if master is None else master
        if not pid:
            raise CommandError("loader is not running under gunicorn here, so there is nothing to signal.")
        if action == "restart-service":
            recent = session.scalar(select(FtpBoxCommand).where(
                FtpBoxCommand.hostname == NAME, FtpBoxCommand.action == action,
                FtpBoxCommand.requested_at > now - RESTART_COOLDOWN,
            ))
            if recent is not None:
                raise CommandError("loader was just restarted -- give it two minutes.")
            sig, row.output = SIGTERM, "Told the service to stop; systemd starts it again within seconds."
        else:
            sig, row.output = SIGHUP, "Told gunicorn to replace its workers; each finishes its requests first."
        send = lambda: os.kill(pid, sig)  # noqa: E731
    session.add(row)
    session.commit()
    return row, send
