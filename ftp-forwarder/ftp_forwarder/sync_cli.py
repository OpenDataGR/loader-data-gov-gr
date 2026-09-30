"""Entrypoint for the account-sync side: periodically pulls loader's
FTP-account list and mirrors it into this box's local ProFTPD SQLite
database. Its own process/systemd unit, separate from the file-relay
side (``cli.py``) -- account provisioning and file relaying are
different responsibilities that should fail independently, not share
a process where one crashing takes the other down with it."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import time

import requests

from ftp_forwarder import account_sync, box_health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Periodically syncs loader's FTP accounts into this box's local ProFTPD database"
    )
    parser.add_argument("--loader-base-url", required=True)
    parser.add_argument(
        "--sync-key-file", required=True,
        help="Path to a file holding the LOADER_FTP_SYNC_KEY value (never the value inline on the command line)",
    )
    parser.add_argument("--db-path", required=True, help="Path to the local SQLite DB ProFTPD's mod_sql_sqlite reads")
    parser.add_argument("--interval-s", type=int, default=60)
    parser.add_argument(
        "--upload-root",
        help="If set, create each synced account's home directory under this root (e.g. /srv/ftp) when it's missing",
    )
    parser.add_argument(
        "--upload-owner", default="virtualftp",
        help="User and group that own the created home directories (only used with --upload-root)",
    )
    parser.add_argument("--proftpd-unit", default="proftpd")
    parser.add_argument("--relay-unit", default="loader-ftp-forwarder")
    parser.add_argument("--ftp-port", type=int, default=21)
    parser.add_argument("--sftp-port", type=int, default=2222)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger(__name__)

    with open(args.sync_key_file, encoding="utf-8") as fh:
        sync_key = fh.read().strip()
    session = requests.Session()
    hostname = socket.gethostname()

    uid = gid = None
    if args.upload_root:
        # Linux-only lookups, and resolved once up front: a typo'd owner
        # should fail loudly at startup (systemd restarts it, the journal
        # says why), not silently create root-owned directories forever.
        import grp
        import pwd
        uid = pwd.getpwnam(args.upload_owner).pw_uid
        gid = grp.getgrnam(args.upload_owner).gr_gid

    while True:
        sync_ok = True
        sync_message = "ok"
        try:
            account_sync.sync_once(
                session, args.loader_base_url, sync_key, args.db_path,
                upload_root=args.upload_root, uid=uid, gid=gid,
            )
        except (account_sync.SyncError, OSError) as exc:
            logger.error("sync failed: %s", exc)
            sync_ok = False
            sync_message = str(exc)

        health = box_health.collect_status(
            proftpd_unit=args.proftpd_unit,
            relay_unit=args.relay_unit,
            ftp_port=args.ftp_port,
            sftp_port=args.sftp_port,
        )
        account_sync.report_status(
            session, args.loader_base_url, sync_key,
            hostname=hostname,
            health=health,
            account_count=account_sync.local_account_count(args.db_path),
            sync_ok=sync_ok,
            sync_message=sync_message,
        )
        time.sleep(args.interval_s)


if __name__ == "__main__":
    sys.exit(main())
