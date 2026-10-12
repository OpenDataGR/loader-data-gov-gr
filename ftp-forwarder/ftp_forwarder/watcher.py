"""The only genuinely Linux-specific piece of this package: watches
each currently-known account's directory for ``IN_CLOSE_WRITE`` and
submits each completed file to the shared worker pool.

Deliberately ``IN_CLOSE_WRITE``, not ``IN_CREATE``: ``IN_CREATE`` fires
the moment an upload *starts* (the instant the FTP/SFTP server opens
the destination file), which would mean trying to send a half-uploaded
file. ``IN_CLOSE_WRITE`` fires only once a file that was opened for
writing has actually been closed -- i.e. the transfer genuinely
finished. Confirmed as the correct signal for this exact "don't act on
a file until whatever's writing it is done" problem, not assumed.

The account list itself isn't static: it's re-read from this box's
local account database (``accounts.py``) on every periodic sweep tick,
and watches are added/removed to match -- so a newly admin-created
account starts being watched, and a deleted one stops, without needing
this process restarted (only the *sync* interval away, not a redeploy).

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

from ftp_forwarder.accounts import RelayAccount, load_accounts
from ftp_forwarder.config import ForwarderConfig
from ftp_forwarder.pool import ForwarderPool
from ftp_forwarder.sweep import catch_up_sweep

logger = logging.getLogger(__name__)


class _WatchSet:
    """Tracks which accounts currently have a live inotify watch, and
    reconciles that against a freshly-read account list -- add a watch
    for a new or moved account, remove one for an account that's gone."""

    def __init__(self, inotify: INotify):
        self._inotify = inotify
        self._by_wd: dict[int, RelayAccount] = {}
        self._by_username: dict[str, tuple[int, RelayAccount]] = {}

    def account_for_wd(self, wd: int) -> RelayAccount | None:
        return self._by_wd.get(wd)

    def current_accounts(self) -> tuple[RelayAccount, ...]:
        return tuple(account for _, account in self._by_username.values())

    def _add(self, account: RelayAccount) -> None:
        if not account.watch_dir.is_dir():
            logger.warning(
                "account %s: watch_dir %s does not exist yet, will retry next cycle",
                account.username, account.watch_dir,
            )
            return
        wd = self._inotify.add_watch(str(account.watch_dir), flags.CLOSE_WRITE)
        self._by_wd[wd] = account
        self._by_username[account.username] = (wd, account)
        logger.info("watching %s (account=%s)", account.watch_dir, account.username)

    def _remove(self, username: str) -> None:
        wd, _ = self._by_username.pop(username)
        self._by_wd.pop(wd, None)
        try:
            self._inotify.rm_watch(wd)
        except OSError:
            pass  # the directory (or the watch itself) may already be gone
        logger.info("stopped watching account=%s (no longer configured)", username)

    def refresh(self, latest: tuple[RelayAccount, ...]) -> None:
        latest_by_username = {account.username: account for account in latest}

        for username in set(self._by_username) - set(latest_by_username):
            self._remove(username)

        for username, account in latest_by_username.items():
            existing = self._by_username.get(username)
            if existing is None:
                self._add(account)
            elif existing[1].watch_dir != account.watch_dir:
                self._remove(username)
                self._add(account)


def watch_forever(pool: ForwarderPool, config: ForwarderConfig, accounts_db_path: str) -> None:
    inotify = INotify()
    watches = _WatchSet(inotify)
    watches.refresh(load_accounts(accounts_db_path))

    stop_sweep = threading.Event()

    def _periodic_sweep() -> None:
        while not stop_sweep.wait(config.catch_up_interval_s):
            watches.refresh(load_accounts(accounts_db_path))
            submitted = catch_up_sweep(pool, watches.current_accounts(), settle_s=config.settle_s)
            if submitted:
                logger.info("catch-up sweep submitted %d file(s)", submitted)

    sweep_thread = threading.Thread(target=_periodic_sweep, name="catch-up-sweep", daemon=True)
    sweep_thread.start()

    # Pick up anything that arrived (or failed to send) before this
    # process started, rather than waiting for the first periodic tick.
    catch_up_sweep(pool, watches.current_accounts(), settle_s=config.settle_s)

    try:
        while True:
            for event in inotify.read():
                account = watches.account_for_wd(event.wd)
                if account is None:
                    continue
                file_path = pathlib.Path(account.watch_dir) / event.name
                pool.submit(account, file_path)
    finally:
        stop_sweep.set()
