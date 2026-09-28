"""Periodic (and startup) catch-up sweep: submits every file already
sitting in each watched directory to the pool. Covers two cases the
live inotify watcher alone can't: a file that arrived while this
process wasn't running at all, and a file whose send failed every
retry last time (``sender.process_one`` deliberately leaves a failed
file in place, never deleting or archiving it, specifically so this
sweep finds it again).

Deliberately its own module, separate from ``watcher.py``: this is
plain ``pathlib`` directory scanning, nothing OS-specific, so it's
fully testable on any platform -- unlike the live inotify watcher,
which only really runs on Linux.
"""

from __future__ import annotations

import logging
import pathlib

from ftp_forwarder.config import ForwarderConfig
from ftp_forwarder.pool import ForwarderPool

logger = logging.getLogger(__name__)


def catch_up_sweep(pool: ForwarderPool, config: ForwarderConfig) -> int:
    """Returns how many files were newly submitted (already-in-flight
    files, see ``ForwarderPool.submit``, don't count twice)."""
    submitted = 0
    for account in config.accounts:
        watch_dir = pathlib.Path(account.watch_dir)
        if not watch_dir.is_dir():
            logger.warning("account %s: watch_dir %s does not exist, skipping this sweep", account.name, watch_dir)
            continue
        for path in watch_dir.iterdir():
            if not path.is_file():
                continue  # e.g. the archive_subdir itself, or another stray directory
            if pool.submit(account, path) is not None:
                submitted += 1
    return submitted
