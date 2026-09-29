"""Discovers which accounts to watch and relay from this box's own
local ProFTPD account database (``ftp_account`` table) -- the same
database ``account_sync.py`` already keeps in sync with loader's admin
panel (``GET /admin/ftp-accounts-sync``, every ``--interval-s``
seconds). Loader's admin panel is the one true source of "which
accounts exist and where do they live on disk" (``home_directory``);
this just reads that, so the file-relay side never needs its own
separate, hand-maintained, driftable copy of the same list.
"""

from __future__ import annotations

import dataclasses
import pathlib
import sqlite3


@dataclasses.dataclass(frozen=True)
class RelayAccount:
    username: str
    watch_dir: pathlib.Path


def load_accounts(db_path: str) -> tuple[RelayAccount, ...]:
    """Returns every account currently in the local database, in a
    stable (username) order. Safe to call even before account_sync.py
    has ever run once -- ``account_sync.reconcile``'s own schema
    (``CREATE TABLE IF NOT EXISTS``) means a brand-new, empty database
    still has the right shape, just zero rows."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ftp_account ("
            "username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, "
            "home_directory TEXT NOT NULL, protocol TEXT NOT NULL)"
        )
        rows = conn.execute(
            "SELECT username, home_directory FROM ftp_account ORDER BY username"
        ).fetchall()
    finally:
        conn.close()
    return tuple(RelayAccount(username=username, watch_dir=pathlib.Path(home)) for username, home in rows)
