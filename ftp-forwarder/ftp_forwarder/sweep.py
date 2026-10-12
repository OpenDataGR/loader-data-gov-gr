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
import time

from ftp_forwarder.accounts import RelayAccount
from ftp_forwarder.pool import ForwarderPool

logger = logging.getLogger(__name__)


def catch_up_sweep(pool: ForwarderPool, accounts: tuple[RelayAccount, ...], *, settle_s: float = 0, now=time.time) -> int:
    """Returns how many files were newly submitted (already-in-flight
    files, see ``ForwarderPool.submit``, don't count twice). Takes the
    current account list as a parameter rather than reading it from a
    static config -- the caller (``watcher.py``) re-derives it from
    this box's local account database on every cycle, since the
    account list itself can change over time, unlike everything else
    in ``ForwarderConfig``.

    ``settle_s``: a file written to within that many seconds is left for a later pass. The sweep looks at
    whatever is in the directory, and an upload that is still arriving is in there too -- sending it then
    forwards a cut-off file (the live watcher never does: ``IN_CLOSE_WRITE`` only fires once the writer is
    done). A file that really is finished is picked up by the watcher at once, so waiting here costs nothing."""
    submitted = 0
    current = now()
    for account in accounts:
        watch_dir = pathlib.Path(account.watch_dir)
        if not watch_dir.is_dir():
            logger.warning(
                "account %s: watch_dir %s does not exist, skipping this sweep", account.username, watch_dir
            )
            continue
        for path in watch_dir.iterdir():
            if not path.is_file():
                continue  # e.g. the archive_subdir itself, or another stray directory
            try:
                if settle_s and current - path.stat().st_mtime < settle_s:
                    continue  # still being written (or only just finished: the watcher has it)
            except OSError:
                continue  # vanished since the listing
            if pool.submit(account, path) is not None:
                submitted += 1
    return submitted
