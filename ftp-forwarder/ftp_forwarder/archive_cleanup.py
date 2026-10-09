"""Deletes old forwarded files from each account's archive, as loader's retention setting says.

The relay moves a file it has forwarded into ``<home>/.forwarded/<YYYYMMDDHH>/`` and never deletes it
(``sender.archive_file``). Loader sends each account's retention in the account sync (``archive_days``,
0 meaning forever) and ``account_sync.reconcile`` keeps it in the local ``archive_policy`` table; this
module applies it: an hour-folder whose whole hour is older than the account's days is removed.

This runs as root on data that came over the network, so it is deliberately narrow:

* the number is checked here again (a whole number of days from 1 to 3650; anything else, and 0, deletes
  nothing);
* only the account's own ``.forwarded`` directory, only directories named like an hour (``YYYYMMDDHH``),
  only real directories (never a symlink, never followed), and only for an account whose home is strictly
  under the upload root -- the same check ``ensure_home_directories`` makes;
* files still waiting to be forwarded are never touched: they are not in the archive.

``now`` is injectable so tests need no clock.
"""

from __future__ import annotations

import datetime
import logging
import os
import pathlib
import posixpath
import re
import shutil
import sqlite3

logger = logging.getLogger(__name__)

ARCHIVE_DIR = ".forwarded"
MIN_DAYS, MAX_DAYS = 1, 3650
_HOUR_DIR = re.compile(r"^\d{10}$")


def policies(db_path: str) -> list[tuple[str, str, int]]:
    """(username, home directory, days) for every account that has a policy."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS archive_policy (username TEXT PRIMARY KEY, days INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS ftp_account (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, home_directory TEXT NOT NULL, protocol TEXT NOT NULL)")
        return [tuple(r) for r in conn.execute(
            "SELECT a.username, a.home_directory, p.days FROM ftp_account a JOIN archive_policy p ON p.username = a.username ORDER BY a.username"
        )]
    finally:
        conn.close()


def _hour_start(name: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(name, "%Y%m%d%H").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def prune_account(home: str, days: int, upload_root: str, *, now: datetime.datetime | None = None) -> int:
    """Remove hour-folders wholly older than ``days`` from one account's archive. Returns how many."""
    if not isinstance(days, int) or isinstance(days, bool) or not MIN_DAYS <= days <= MAX_DAYS:
        return 0
    root = posixpath.normpath(upload_root)
    home = posixpath.normpath(home)
    if not home.startswith(root + "/"):
        logger.warning("archive cleanup: not touching %r: outside the upload root %r", home, root)
        return 0
    archive = pathlib.Path(home) / ARCHIVE_DIR
    if archive.is_symlink() or not archive.is_dir():
        return 0
    cutoff = (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=days)
    removed = 0
    for entry in sorted(archive.iterdir()):
        if not _HOUR_DIR.match(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        start = _hour_start(entry.name)
        if start is None or start + datetime.timedelta(hours=1) > cutoff:
            continue  # unreadable name, or the hour is not entirely past the period
        shutil.rmtree(entry)  # (does not follow symlinks inside)
        removed += 1
    return removed


def prune(db_path: str, upload_root: str, *, now: datetime.datetime | None = None) -> dict[str, int]:
    """Apply every account's policy. Returns {username: hour-folders removed} for those that removed any."""
    done: dict[str, int] = {}
    for username, home, days in policies(db_path):
        try:
            removed = prune_account(home, days, upload_root, now=now)
        except OSError as exc:  # one account's trouble must not stop the others
            logger.error("archive cleanup for %s failed: %s", username, exc)
            continue
        if removed:
            logger.info("archive cleanup: removed %d hour folder(s) older than %d day(s) for %s", removed, days, username)
            done[username] = removed
    return done
