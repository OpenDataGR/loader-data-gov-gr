"""Refused logins, for loader's failed-login report. Reads the second
login log ProFTPD writes (``ExtendedLog ... AUTH loader_attempt``, see
``.github/workflows/provision-ftp-box.yml``) -- a separate file from
``auth_log.py``'s, because the report needs the server port and the
username as typed, and the fail2ban filter reads that other file and
must not change shape.

One line per attempt, in ``LogFormat "%{%s}t %a %p %m %s <token> %U"``::

    1790764941 188.166.1.1 2222 PASS 530 3f9c2a7e5b1d4c68 admin

epoch seconds, client address, the server port it hit (21 FTP / 2222
SFTP), command, reply code, a secret token, and the username as typed --
last, because it is the one field a stranger controls and may contain spaces.

The token exists because ProFTPD writes the typed username into the log
unescaped (its own docs warn of it; confirmed on this box 2026-10-02): an SFTP
username containing a newline split one log line into two, the second one
entirely the attacker's -- any address, any username -- and would have shown up
as a forged row in the failed-login report. A line is only believed if it
carries the token, which is generated at provisioning, never leaves the box and
so cannot appear in anything a stranger sends. (The fail2ban log is not
affected: it records the authenticated user, not the typed one.)

Unlike the last-login tail this must not lose or repeat anything: each
refused login is one row in loader's report. So the read position lives in
a state file and only advances (``commit``) once loader has accepted the
batch that came from it. A crash or a failed report re-sends from the last
committed position; nothing is dropped.
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import os

BATCH_LIMIT = 2000  # loader refuses more than this in one report


@dataclasses.dataclass(frozen=True)
class Attempt:
    at: int
    ip: str
    port: int
    username: str
    code: int
    channel: str | None = None  # set for anything that isn't FTP/SFTP-by-port, e.g. "SSH"

    def as_payload(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


def parse_line(line: str, token: str) -> Attempt | None:
    if not token:
        return None  # nothing to vouch for a line with: believe nothing
    parts = line.split(None, 6)
    if len(parts) < 6:
        return None
    epoch, ip, port, command, code, presented = parts[:6]
    if not hmac.compare_digest(presented, token):
        return None  # not written by ProFTPD: a forgery, or a line from before the token existed
    username = parts[6].strip() if len(parts) == 7 else ""
    if command != "PASS" or code != "530":
        return None
    if username == "-":
        username = ""
    try:
        return Attempt(at=int(epoch), ip=ip, port=int(port), username=username, code=int(code))
    except ValueError:
        return None


@dataclasses.dataclass(frozen=True)
class Position:
    inode: int
    offset: int


class AttemptsLogTail:
    def __init__(self, log_path: str, state_path: str, token: str):
        self._log_path = log_path
        self._state_path = state_path
        self._token = token

    def _load(self) -> Position | None:
        try:
            with open(self._state_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return Position(inode=int(data["inode"]), offset=int(data["offset"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def poll(self, limit: int = BATCH_LIMIT) -> tuple[list[Attempt], Position | None]:
        """Refused logins since the last committed position (at most ``limit``),
        and the position to ``commit`` once they have been delivered."""
        try:
            stat = os.stat(self._log_path)
        except FileNotFoundError:
            return [], None

        saved = self._load()
        if saved is None or saved.inode != stat.st_ino or stat.st_size < saved.offset:
            # No state yet (a log that is new: everything in it is wanted) or the
            # file was rotated / truncated: start at the top of this one.
            start = 0
        else:
            start = saved.offset

        with open(self._log_path, "rb") as fh:
            fh.seek(start)
            raw = fh.read()
        # Only whole lines: one still being written is picked up next time.
        raw = raw[: raw.rfind(b"\n") + 1]

        attempts: list[Attempt] = []
        consumed = 0
        for line in raw.splitlines(keepends=True):
            if len(attempts) >= limit:
                break
            consumed += len(line)
            attempt = parse_line(line.decode("utf-8", errors="replace"), self._token)
            if attempt:
                attempts.append(attempt)
        return attempts, Position(inode=stat.st_ino, offset=start + consumed)

    def commit(self, position: Position | None) -> None:
        if position is None:
            return
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"inode": position.inode, "offset": position.offset}, fh)
        os.replace(tmp, self._state_path)
