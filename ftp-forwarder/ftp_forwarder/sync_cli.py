"""Entrypoint for the account-sync side: periodically pulls loader's
FTP-account list and mirrors it into this box's local ProFTPD SQLite
database. Its own process/systemd unit, separate from the file-relay
side (``cli.py``) -- account provisioning and file relaying are
different responsibilities that should fail independently, not share
a process where one crashing takes the other down with it."""

from __future__ import annotations

import argparse
import logging
import sys
import time

import requests

from ftp_forwarder import account_sync


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
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger(__name__)

    with open(args.sync_key_file, encoding="utf-8") as fh:
        sync_key = fh.read().strip()
    session = requests.Session()

    while True:
        try:
            account_sync.sync_once(session, args.loader_base_url, sync_key, args.db_path)
        except account_sync.SyncError as exc:
            logger.error("sync failed: %s", exc)
        time.sleep(args.interval_s)


if __name__ == "__main__":
    sys.exit(main())
