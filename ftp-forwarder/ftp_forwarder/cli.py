"""Entrypoint for the file-relay side: discovers accounts from this
box's own local ProFTPD account database (see ``accounts.py`` -- the
same database ``account_sync.py`` keeps in sync with loader's admin
panel, no separate per-provider config file needed) and forwards each
completed upload to loader's trusted-relay ingest endpoint (see
``providerharvest_service/ftp_relay_ingest.py`` on loader's side). See
``sync_cli.py`` for the separate account-sync entrypoint (a different
responsibility, its own systemd unit -- account provisioning and file
relaying fail independently and shouldn't be coupled into one
process)."""

from __future__ import annotations

import argparse
import logging
import sys

from ftp_forwarder.config import ForwarderConfig
from ftp_forwarder.pool import ForwarderPool
from ftp_forwarder.watcher import watch_forever


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watches this box's FTP accounts and relays completed uploads to loader"
    )
    parser.add_argument("--loader-base-url", required=True)
    parser.add_argument(
        "--sync-key-file", required=True,
        help="Path to a file holding the LOADER_FTP_SYNC_KEY value (never the value inline on the command line)",
    )
    parser.add_argument(
        "--accounts-db-path", required=True,
        help="Path to the local SQLite DB account_sync.py keeps synced with loader's admin panel",
    )
    parser.add_argument("--archive-subdir", default=ForwarderConfig.archive_subdir)
    parser.add_argument("--max-retries", type=int, default=ForwarderConfig.max_retries)
    parser.add_argument("--retry-backoff-s", type=float, default=ForwarderConfig.retry_backoff_s)
    parser.add_argument("--worker-count", type=int, default=ForwarderConfig.worker_count)
    parser.add_argument("--catch-up-interval-s", type=int, default=ForwarderConfig.catch_up_interval_s)
    parser.add_argument("--settle-s", type=int, default=ForwarderConfig.settle_s,
                        help="The catch-up sweep leaves a file alone until nothing has written to it for this many seconds")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    with open(args.sync_key_file, encoding="utf-8") as fh:
        sync_key = fh.read().strip()

    config = ForwarderConfig(
        loader_base_url=args.loader_base_url,
        sync_key=sync_key,
        archive_subdir=args.archive_subdir,
        max_retries=args.max_retries,
        retry_backoff_s=args.retry_backoff_s,
        worker_count=args.worker_count,
        catch_up_interval_s=args.catch_up_interval_s,
        settle_s=args.settle_s,
    )
    pool = ForwarderPool(config)
    try:
        watch_forever(pool, config, args.accounts_db_path)
    finally:
        pool.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
