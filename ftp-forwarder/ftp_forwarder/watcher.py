"""The only genuinely Linux-specific piece of this package: watches
each configured account's directory for ``IN_CLOSE_WRITE`` and submits
each completed file to the shared worker pool.

Deliberately ``IN_CLOSE_WRITE``, not ``IN_CREATE``: ``IN_CREATE`` fires
the moment an upload *starts* (the instant the FTP/SFTP server opens
the destination file), which would mean trying to send a half-uploaded
file. ``IN_CLOSE_WRITE`` fires only once a file that was opened for
writing has actually been closed -- i.e. the transfer genuinely
finished. Confirmed as the correct signal for this exact "don't act on
a file until whatever's writing it is done" problem, not assumed.

This module is a thin wrapper around a well-tested OS primitive, in
the same spirit as this project's own ``engine/transport/
ssh_common.py`` real paramiko calls -- not unit-tested directly (that
would mean faking the kernel's own inotify queue); ``sweep.py``, the
part that actually decides what to do with a file, is where the real
test coverage lives. Only ever imported when actually running the
watcher -- ``inotify_simple`` doesn't work outside Linux, so nothing
else in this package imports this module, keeping the rest testable
on any platform.
"""

from __future__ import annotations

import logging
import pathlib
import threading

from inotify_simple import INotify, flags

from ftp_forwarder.config import ForwarderConfig
from ftp_forwarder.pool import ForwarderPool
from ftp_forwarder.sweep import catch_up_sweep

logger = logging.getLogger(__name__)


def watch_forever(pool: ForwarderPool, config: ForwarderConfig) -> None:
    inotify = INotify()
    watch_to_account = {}
    for account in config.accounts:
        wd = inotify.add_watch(str(account.watch_dir), flags.CLOSE_WRITE)
        watch_to_account[wd] = account
        logger.info("watching %s (account=%s)", account.watch_dir, account.name)

    stop_sweep = threading.Event()

    def _periodic_sweep() -> None:
        while not stop_sweep.wait(config.catch_up_interval_s):
            submitted = catch_up_sweep(pool, config)
            if submitted:
                logger.info("catch-up sweep submitted %d file(s)", submitted)

    sweep_thread = threading.Thread(target=_periodic_sweep, name="catch-up-sweep", daemon=True)
    sweep_thread.start()

    # Pick up anything that arrived (or failed to send) before this
    # process started, rather than waiting for the first periodic tick.
    catch_up_sweep(pool, config)

    try:
        while True:
            for event in inotify.read():
                account = watch_to_account.get(event.wd)
                if account is None:
                    continue
                file_path = pathlib.Path(account.watch_dir) / event.name
                pool.submit(account, file_path)
    finally:
        stop_sweep.set()
