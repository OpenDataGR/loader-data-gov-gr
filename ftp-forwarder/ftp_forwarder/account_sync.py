"""Mirrors loader's FTP-account list (``GET /admin/ftp-accounts-sync``)
into this box's own local SQLite database -- the one ProFTPD's
``mod_sql_sqlite`` actually authenticates against (same
``SQLUserInfo ftp_account username password_hash NULL NULL
home_directory NULL`` shape already proven on the main loader VM, see
``deploy/README.md`` there).

Loader's admin panel stays the single place accounts are created,
edited, or disabled -- this is what keeps this box eventually
consistent with that, via a periodic *pull* (never a push: this box
never needs to be reachable by loader at all, same outbound-only shape
the file forwarder itself already uses).
"""

from __future__ import annotations

import logging
import sqlite3

import requests

logger = logging.getLogger(__name__)

# Includes the empty ``groups`` table for the same reason it exists on
# the main loader VM: ProFTPD's mod_sql unconditionally queries a
# table literally named "groups" during auth, even with no
# SQLGroupInfo directive configured -- without it, every login fails
# with "unrecoverable backend error: no such table: groups", confirmed
# there already, not guessed here.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ftp_account (
  username TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  home_directory TEXT NOT NULL,
  protocol TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
  groupname TEXT PRIMARY KEY,
  gid INTEGER,
  members TEXT
);
"""


class SyncError(RuntimeError):
    pass


def fetch_accounts(
    session: requests.Session, base_url: str, sync_key: str, *, timeout_s: float = 30.0
) -> list[dict]:
    url = "%s/admin/ftp-accounts-sync" % base_url.rstrip("/")
    try:
        resp = session.get(url, headers={"X-Ftp-Sync-Key": sync_key}, timeout=timeout_s)
    except requests.RequestException as exc:
        raise SyncError("%s: %s" % (url, exc)) from exc
    if resp.status_code != 200:
        raise SyncError("%s: unexpected status %d: %s" % (url, resp.status_code, resp.text[:200]))
    return resp.json()


def reconcile(db_path: str, accounts: list[dict]) -> None:
    """Replaces the local ``ftp_account`` table's contents with
    exactly what loader currently reports -- an account no longer
    returned (disabled or deleted there) is removed here too, not left
    behind with credentials that should no longer work."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        current_usernames = {a["username"] for a in accounts}
        existing_usernames = {row[0] for row in conn.execute("SELECT username FROM ftp_account")}

        for account in accounts:
            conn.execute(
                "INSERT INTO ftp_account (username, password_hash, home_directory, protocol) "
                "VALUES (:username, :password_hash, :home_directory, :protocol) "
                "ON CONFLICT(username) DO UPDATE SET "
                "password_hash = excluded.password_hash, "
                "home_directory = excluded.home_directory, "
                "protocol = excluded.protocol",
                account,
            )

        removed = existing_usernames - current_usernames
        for username in removed:
            conn.execute("DELETE FROM ftp_account WHERE username = ?", (username,))

        conn.commit()
    finally:
        conn.close()

    if removed:
        logger.info("removed %d account(s) no longer present in loader: %s", len(removed), sorted(removed))
    logger.info("synced %d account(s)", len(accounts))


def sync_once(session: requests.Session, base_url: str, sync_key: str, db_path: str) -> None:
    accounts = fetch_accounts(session, base_url, sync_key)
    reconcile(db_path, accounts)
