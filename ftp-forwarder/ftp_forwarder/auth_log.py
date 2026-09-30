"""Where do providers connect from? Reads the login log ProFTPD writes
for both the FTP and SFTP servers (``ExtendedLog ... AUTH loader_auth``,
see ``.github/workflows/provision-ftp-box.yml``) so the heartbeat can tell
loader the address each account last logged in from.

One line per login attempt, in ``LogFormat "%{%s}t %a %u %m %s"``::

    1790764941 84.205.215.133 zz-dryrun PASS 230

epoch seconds, client address, user, command, reply code. A successful
login is ``PASS`` with ``230`` -- for SFTP too (mod_sftp emulates the
command, confirmed against the live box), where neither ProFTPD's system
log nor its transfer log records one at all. Failed attempts (``530``,
including internet scanners probing the port) are ignored here.

The log is read incrementally from where the last poll stopped, not
re-read whole: at this service's cadence it grows fast. The very first
poll seeds from the tail of the file so a restart doesn't blank the
picture; after that, only new lines. Rotation (a new file, or one that
shrank) restarts from the beginning of the new file.
"""

from __future__ import annotations

import dataclasses
import os


@dataclasses.dataclass(frozen=True)
class Login:
    username: str
    ip: str
    at: int


def parse_line(line: str) -> Login | None:
    parts = line.split()
    if len(parts) != 5:
        return None
    epoch, ip, username, command, code = parts
    if command != "PASS" or code != "230" or username == "-":
        return None
    try:
        return Login(username=username, ip=ip, at=int(epoch))
    except ValueError:
        return None


class AuthLogTail:
    def __init__(self, path: str, *, seed_bytes: int = 256 * 1024):
        self._path = path
        self._seed_bytes = seed_bytes
        self._inode: int | None = None
        self._offset = 0

    def poll(self) -> dict[str, Login]:
        """The latest successful login per user since the previous poll."""
        try:
            stat = os.stat(self._path)
        except FileNotFoundError:
            return {}

        first_poll = self._inode is None
        rotated = not first_poll and (stat.st_ino != self._inode or stat.st_size < self._offset)
        if first_poll:
            start = max(0, stat.st_size - self._seed_bytes)
        elif rotated:
            start = 0
        else:
            start = self._offset
        self._inode = stat.st_ino

        with open(self._path, "rb") as fh:
            fh.seek(start)
            raw = fh.read()
        base = start  # the absolute offset raw[0] sits at
        if first_poll and start > 0:
            # Seeded from mid-file: the first line is cut off, drop it.
            newline = raw.find(b"\n")
            cut = len(raw) if newline < 0 else newline + 1
            raw, base = raw[cut:], base + cut
        # Only whole lines: one still being written is picked up next time.
        end = raw.rfind(b"\n") + 1
        self._offset = base + end

        latest: dict[str, Login] = {}
        for line in raw[:end].decode("utf-8", errors="replace").splitlines():
            login = parse_line(line)
            if login and (login.username not in latest or login.at >= latest[login.username].at):
                latest[login.username] = login
        return latest
