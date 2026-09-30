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
import os
import pathlib
import posixpath
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


def ensure_home_directories(
    accounts: list[dict],
    upload_root: str,
    *,
    uid: int | None = None,
    gid: int | None = None,
    mode: int = 0o2775,
) -> list[str]:
    """Creates each account's home directory if it doesn't exist yet --
    ProFTPD (``DefaultRoot ~``) can't log a virtual user in without one,
    and the file relay's inotify watch needs it to attach to. Group
    -writable plus setgid (``2775``, owned by the virtual-user account)
    so the relay -- a member of that group, not root -- can create its
    ``.forwarded/`` archive folder inside and move sent files into it.

    Only ever creates a path strictly under ``upload_root``: the account
    list comes from loader, and this runs as root, so a bad
    ``home_directory`` must not become an arbitrary root-owned directory
    anywhere on this box (loader validates the same thing on its side --
    this is the second, independent check). Returns what was created."""
    root = posixpath.normpath(upload_root)
    created = []
    for account in accounts:
        home = posixpath.normpath(account["home_directory"])
        if not home.startswith(root + "/"):
            logger.warning(
                "not creating home directory %r for %r: outside upload root %r",
                home, account["username"], root,
            )
            continue
        path = pathlib.Path(home)
        if path.is_dir():
            continue
        path.mkdir(parents=True, exist_ok=True)
        if uid is not None and gid is not None:
            os.chown(path, uid, gid)
        os.chmod(path, mode)
        created.append(home)
        logger.info("created home directory %s for %r", home, account["username"])
    return created


def sync_once(
    session: requests.Session,
    base_url: str,
    sync_key: str,
    db_path: str,
    *,
    upload_root: str | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> None:
    accounts = fetch_accounts(session, base_url, sync_key)
    if upload_root:
        # Before reconcile, so an account never becomes loginable
        # (present in the ProFTPD database) before its directory exists.
        ensure_home_directories(accounts, upload_root, uid=uid, gid=gid)
    reconcile(db_path, accounts)


def local_account_count(db_path: str) -> int:
    """However the last successful sync left it -- read from this box's
    own local database rather than trusting whatever this cycle's pull
    returned, so a *failed* pull still reports a meaningful count (the
    last known-good one) instead of 0."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        return conn.execute("SELECT COUNT(*) FROM ftp_account").fetchone()[0]
    finally:
        conn.close()


def report_status(
    session: requests.Session,
    base_url: str,
    sync_key: str,
    *,
    hostname: str,
    health: dict,
    account_count: int,
    sync_ok: bool,
    sync_message: str | None,
    clients: list[dict] | None = None,
    timeout_s: float = 30.0,
) -> bool:
    """Best-effort: a failed heartbeat is logged, never raised -- this
    is telemetry for loader's own admin panel, not something that
    should crash the sync loop itself (that loop's actual job, keeping
    ProFTPD's account database current, must keep running regardless
    of whether reporting on it succeeds). Returns whether loader
    accepted the report, so the caller can hold on to anything (the
    ``clients`` logins) that must be re-sent after a failure.

    ``clients``: ``{"username", "ip"}`` for each provider that logged in
    since the last delivered report (see ``auth_log.py``)."""
    url = "%s/admin/ftp-box-status" % base_url.rstrip("/")
    payload = {
        "hostname": hostname,
        "account_count": account_count,
        "sync_ok": sync_ok,
        "sync_message": sync_message,
        "clients": clients or [],
        **health,
    }
    try:
        resp = session.put(url, json=payload, headers={"X-Ftp-Sync-Key": sync_key}, timeout=timeout_s)
        if resp.status_code != 200:
            logger.error("status report to %s failed: %d %s", url, resp.status_code, resp.text[:200])
            return False
    except requests.RequestException as exc:
        logger.error("status report to %s failed: %s", url, exc)
        return False
    return True
