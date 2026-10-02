"""Refused SSH logins on this box, for loader's failed-login report. sshd
runs on 22 and 7002 here (separately from ProFTPD's SFTP on 2222) and logs
through journald; a refused password looks like::

    Failed password for root from 202.51.214.98 port 48604 ssh2
    Failed password for invalid user vpn from 193.46.255.86 port 37952 ssh2

(confirmed against this box's real journal, not assumed). The port in the
line is the *client's*; sshd does not log which of its ports was hit, so the
report cannot say 22 or 7002 -- it is shown as "SSH".

Only ``Failed password`` lines count, one per password tried. The other
lines scanners leave ("Invalid user", "Connection closed by authenticating
user", pam messages) describe the same attempt or never got as far as a
password, and counting them would multiply every attempt.

Same delivery rule as ``attempts_log.py``: the read position (a journal
cursor) only moves once loader has accepted the batch built from it, so
nothing is lost or repeated. ``run`` is injectable so tests need no journald.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Callable

from ftp_forwarder.attempts_log import BATCH_LIMIT, Attempt

logger = logging.getLogger(__name__)

CHANNEL = "SSH"
SSH_PORT = 22  # stands for "sshd": the real port (22 or 7002) is not in the log
REFUSED_CODE = 0  # sshd has no reply code; loader just stores what it is given
FIRST_RUN_WINDOW = "-1 day"  # no saved position yet: pick up the last day
RECOVERY_WINDOW = "-1 hour"  # saved position unusable (journal vacuumed): bounded replay

_FAILED_PASSWORD = re.compile(
    r"^Failed password for (?:invalid user )?(?P<user>.+) from (?P<ip>[0-9a-fA-F:.]+) port \d+(?: ssh2)?$"
)

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _default_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)


def parse_entry(entry: dict) -> Attempt | None:
    message = entry.get("MESSAGE")
    if not isinstance(message, str):  # journald gives binary messages as a list of numbers
        return None
    match = _FAILED_PASSWORD.match(message)
    if not match:
        return None
    try:
        at = int(entry["__REALTIME_TIMESTAMP"]) // 1_000_000
    except (KeyError, ValueError, TypeError):
        return None
    return Attempt(
        at=at, ip=match["ip"], port=SSH_PORT, username=match["user"], code=REFUSED_CODE, channel=CHANNEL,
    )


class JournalSshTail:
    def __init__(self, state_path: str, *, unit: str = "ssh", run: Runner = _default_run):
        self._state_path = state_path
        self._unit = unit
        self._run = run

    def _load(self) -> str | None:
        try:
            with open(self._state_path, encoding="utf-8") as fh:
                cursor = json.load(fh).get("cursor")
            return cursor if isinstance(cursor, str) and cursor else None
        except (OSError, ValueError, AttributeError):
            return None

    def _journal(self, *window: str) -> subprocess.CompletedProcess:
        return self._run(["journalctl", "-u", self._unit, "-o", "json", "--no-pager", *window])

    def poll(self, limit: int = BATCH_LIMIT) -> tuple[list[Attempt], str | None]:
        """Refused logins since the last committed position (at most ``limit``),
        and the cursor to ``commit`` once they have been delivered."""
        cursor = self._load()
        result = self._journal("--after-cursor", cursor) if cursor else self._journal("--since", FIRST_RUN_WINDOW)
        if result.returncode != 0 and cursor:
            logger.error("journal cursor no longer usable (%s); replaying the last hour", (result.stderr or "").strip()[:200])
            result = self._journal("--since", RECOVERY_WINDOW)
        if result.returncode != 0:
            logger.error("could not read the sshd journal: %s", (result.stderr or "").strip()[:200])
            return [], None

        attempts: list[Attempt] = []
        last_cursor: str | None = None
        for line in (result.stdout or "").splitlines():
            if len(attempts) >= limit:
                break
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("__CURSOR"), str):
                last_cursor = entry["__CURSOR"]
            attempt = parse_entry(entry)
            if attempt:
                attempts.append(attempt)
        return attempts, last_cursor

    def commit(self, cursor: str | None) -> None:
        if cursor is None:
            return
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cursor": cursor}, fh)
        os.replace(tmp, self._state_path)
