"""Actions an admin can ask this box to run from loader's FTP box page.

Loader can never connect here, so a request waits on loader until the next heartbeat
collects it (the reply's ``commands``); the result goes back in the following
heartbeat (``command_results``). What may be run is decided **here**, from a fixed
list: loader sends only an action *name*, never a command line, so even a compromised
loader (or a forged reply) can do nothing beyond this list. Every command is an
argument list, never a shell, and the output is cut to a size that is safe to send.

The list is deliberately small -- restart the services, reload fail2ban, take a
read-only diagnostic snapshot. There is no way to run an arbitrary command, read an
arbitrary file or open a shell (that stays on SSH, which the box already has).

``run`` is injectable so tests need no systemd or fail2ban.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
from typing import Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess]

MAX_OUTPUT = 12000
LOG_OUTPUT = 40000  # a log view is meant to be read, so it gets more room than the other actions
LOG_LINES = (50, 100, 200, 500)
_LOG_ACTION = re.compile(r"^logs-([a-z0-9]{2,12})-(\d{2,3})$")
SECTION_OUTPUT = 2000  # per section of a diagnostics snapshot, so no one log can push the others out
_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _default_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


@dataclasses.dataclass(frozen=True)
class Result:
    id: str
    ok: bool
    output: str

    def as_payload(self) -> dict:
        return {"id": self.id, "ok": self.ok, "output": self.output}


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text.replace("\x00", "")
    if len(text) <= limit:
        return text
    return "...(cut)...\n" + text[-limit:]


class Executor:
    def __init__(
        self,
        *,
        proftpd_unit: str = "proftpd",
        relay_unit: str = "loader-ftp-forwarder",
        sync_unit: str = "loader-ftp-forwarder-sync",
        fail2ban_unit: str = "fail2ban",
        ssh_unit: str = "ssh",
        run: Runner = _default_run,
    ):
        self._run = run
        self._units = (proftpd_unit, relay_unit, sync_unit, fail2ban_unit)
        self._proftpd, self._relay, self._sync, self._fail2ban = self._units
        # What a log request may name: a short word -> the journal unit it reads. Fixed here.
        self._log_units = {"proftpd": self._proftpd, "sync": self._sync, "relay": self._relay,
                           "fail2ban": self._fail2ban, "sshd": ssh_unit}
        self._actions: dict[str, Callable[[], tuple[bool, str]]] = {
            "restart-proftpd": lambda: self._restart(self._proftpd),
            "restart-relay": lambda: self._restart(self._relay),
            "reload-fail2ban": self._reload_fail2ban,
            "diagnostics": self._diagnostics,
        }

    @property
    def actions(self) -> tuple[str, ...]:
        return tuple(self._actions)

    @property
    def log_services(self) -> tuple[str, ...]:
        return tuple(self._log_units)

    def _logs(self, service: str, lines: int) -> tuple[bool, str]:
        """The last ``lines`` lines of one service's journal. Read-only; the unit and the count come
        from this box's own tables, never from the request text."""
        unit = self._log_units[service]
        done = self._do(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short"])
        text = (done.stdout or done.stderr or "").strip()
        if done.returncode != 0:
            return False, "journalctl for %s failed: %s" % (unit, text or "no output")
        return True, "== last %d lines of %s\n%s" % (lines, unit, text or "(nothing logged)")

    # -- the actions ---------------------------------------------------------------

    def _do(self, cmd: list[str]) -> subprocess.CompletedProcess:
        try:
            return self._run(cmd)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", "timed out")
        except (OSError, subprocess.SubprocessError) as exc:
            return subprocess.CompletedProcess(cmd, 127, "", str(exc))

    def _restart(self, unit: str) -> tuple[bool, str]:
        done = self._do(["systemctl", "restart", unit])
        if done.returncode != 0:
            return False, "systemctl restart %s failed: %s" % (unit, (done.stderr or done.stdout or "").strip())
        active = self._do(["systemctl", "is-active", unit]).stdout.strip() or "unknown"
        return active == "active", "restarted %s; it is now %s" % (unit, active)

    def _reload_fail2ban(self) -> tuple[bool, str]:
        done = self._do(["fail2ban-client", "reload"])
        text = (done.stdout or done.stderr or "").strip()
        return done.returncode == 0, "fail2ban-client reload: %s" % (text or "ok")

    def _diagnostics(self) -> tuple[bool, str]:
        """A read-only snapshot: what an admin would look at first when logging in."""
        sections = [("uptime", ["uptime"]), ("memory", ["free", "-m"]), ("disk", ["df", "-h", "/", "/srv/ftp"])]
        sections += [("unit " + u, ["systemctl", "is-active", u]) for u in self._units]
        sections += [("fail2ban", ["fail2ban-client", "status"])]
        sections += [("log " + u, ["journalctl", "-u", u, "-n", "15", "--no-pager", "-o", "short"]) for u in (self._proftpd, self._sync, self._relay)]
        out = []
        for title, cmd in sections:
            done = self._do(cmd)
            out.append("== %s\n%s" % (title, _clip((done.stdout or done.stderr or "").strip(), SECTION_OUTPUT)))
        return True, "\n\n".join(out)

    # -- entry point -----------------------------------------------------------------

    def execute(self, command: object) -> Result | None:
        """Run one command from loader's reply. None for something not even shaped like
        one (nothing to report back); a failed Result for an action not on the list."""
        if not isinstance(command, dict):
            return None
        command_id, action = command.get("id"), command.get("action")
        if not isinstance(command_id, str) or not _ID.match(command_id):
            return None
        handler = self._actions.get(action) if isinstance(action, str) else None
        match = _LOG_ACTION.match(action) if isinstance(action, str) else None
        if match and match.group(1) in self._log_units and int(match.group(2)) in LOG_LINES:
            service, lines = match.group(1), int(match.group(2))
            handler = lambda: self._logs(service, lines)  # noqa: E731
            limit = LOG_OUTPUT
        else:
            limit = MAX_OUTPUT
        if handler is None:
            return Result(command_id, False, "this box does not run %r" % (action if isinstance(action, str) else "?"))
        try:
            ok, output = handler()
        except Exception as exc:  # never let one action take the sync loop down
            return Result(command_id, False, "failed: %s" % exc)
        return Result(command_id, ok, _clip(output, limit))
