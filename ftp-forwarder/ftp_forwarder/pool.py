"""A small bounded worker pool: both the live inotify watcher and the
periodic catch-up sweep feed file paths in here, and they get sent
concurrently -- necessary at this service's real cadence (up to
roughly 20 files/sec across all accounts combined), where a serial
"handle one, wait for the network, handle the next" loop would fall
behind within seconds.

Threads, not asyncio: the work here is a simple fan-out of blocking
HTTP calls (I/O-bound), which threads handle perfectly well without
the added complexity an async rewrite of ``requests`` would need.
"""

from __future__ import annotations

import concurrent.futures
import logging
import pathlib
import threading

import requests

from ftp_forwarder import sender
from ftp_forwarder.accounts import RelayAccount
from ftp_forwarder.config import ForwarderConfig

logger = logging.getLogger(__name__)


class ForwarderPool:
    def __init__(self, config: ForwarderConfig, *, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=config.worker_count)
        # Both the live watcher and the periodic catch-up sweep can
        # observe the same file before either has finished handling
        # it -- without this, that would mean sending (and then
        # trying to archive) the same file twice concurrently, the
        # second archive attempt failing outright since the first
        # already renamed it away.
        self._in_flight: set[pathlib.Path] = set()
        self._in_flight_lock = threading.Lock()

    def submit(self, account: RelayAccount, file_path: pathlib.Path) -> concurrent.futures.Future | None:
        with self._in_flight_lock:
            if file_path in self._in_flight:
                return None
            self._in_flight.add(file_path)
        return self._executor.submit(self._handle, account, file_path)

    def _handle(self, account: RelayAccount, file_path: pathlib.Path) -> None:
        try:
            sent = sender.process_one(self._session, self._config, account, file_path)
            if sent:
                logger.info("sent %s (account=%s)", file_path, account.username)
        except sender.SendError as exc:
            logger.error("failed to send %s (account=%s): %s", file_path, account.username, exc)
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(file_path)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)
