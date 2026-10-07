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

MAX_OUTPUT = 6000
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


def _clip(text: str) -> str:
    text = text.replace("\x00", "")
    if len(text) <= MAX_OUTPUT:
        return text
    return "...(cut)...\n" + text[-MAX_OUTPUT:]


class Executor:
    def __init__(
        self,
        *,
        proftpd_unit: str = "proftpd",
        relay_unit: str = "loader-ftp-forwarder",
        sync_unit: str = "loader-ftp-forwarder-sync",
        fail2ban_unit: str = "fail2ban",
        run: Runner = _default_run,
    ):
        self._run = run
        self._units = (proftpd_unit, relay_unit, sync_unit, fail2ban_unit)
        self._proftpd, self._relay, self._sync, self._fail2ban = self._units
        self._actions: dict[str, Callable[[], tuple[bool, str]]] = {
            "restart-proftpd": lambda: self._restart(self._proftpd),
            "restart-relay": lambda: self._restart(self._relay),
            "reload-fail2ban": self._reload_fail2ban,
            "diagnostics": self._diagnostics,
        }

    @property
    def actions(self) -> tuple[str, ...]:
        return tuple(self._actions)

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
        sections += [("log " + u, ["journalctl", "-u", u, "-n", "25", "--no-pager", "-o", "short"]) for u in (self._proftpd, self._sync, self._relay)]
        out = []
        for title, cmd in sections:
            done = self._do(cmd)
            out.append("== %s\n%s" % (title, (done.stdout or done.stderr or "").strip()))
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
        if handler is None:
            return Result(command_id, False, "this box does not run %r" % (action if isinstance(action, str) else "?"))
        try:
            ok, output = handler()
        except Exception as exc:  # never let one action take the sync loop down
            return Result(command_id, False, "failed: %s" % exc)
        return Result(command_id, ok, _clip(output))
