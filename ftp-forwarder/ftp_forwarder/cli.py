"""Entrypoint for the file-relay side: watches every configured
account's directory and forwards completed uploads to loader's
push-ingest endpoint. See ``sync_cli.py`` for the separate
account-sync entrypoint (a different responsibility, its own systemd
unit -- account provisioning and file relaying fail independently and
shouldn't be coupled into one process)."""

from __future__ import annotations

import argparse
import logging
import sys

from ftp_forwarder.config import load_config
from ftp_forwarder.pool import ForwarderPool
from ftp_forwarder.watcher import watch_forever


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forwards pushed files to loader's push-ingest endpoint")
    parser.add_argument("config", help="Path to the forwarder's YAML config file")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config)
    pool = ForwarderPool(config)
    try:
        watch_forever(pool, config)
    finally:
        pool.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
