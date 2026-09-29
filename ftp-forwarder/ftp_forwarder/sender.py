"""Sends one already-completed file to loader's trusted-relay ingest
endpoint (``POST /admin/ftp-relay/{username}/upload`` -- see
``providerharvest_service/ftp_relay_ingest.py`` on loader's side),
with retry-with-backoff on a transient failure -- and archives it
locally only after a *confirmed* success. A failed send must leave the
file exactly where it was found, never silently lose it: the next
catch-up sweep (``sweep.py``) will find it again and retry, same "a
failed attempt is exactly as valid a result as a successful one, and
must never disappear" discipline the loader app itself already applies
to its own harvest runs.
"""

from __future__ import annotations

import datetime
import logging
import pathlib
import time

import requests

from ftp_forwarder.accounts import RelayAccount
from ftp_forwarder.config import ForwarderConfig

logger = logging.getLogger(__name__)


class SendError(RuntimeError):
    pass


def send_file(
    session: requests.Session,
    base_url: str,
    sync_key: str,
    account: RelayAccount,
    file_path: pathlib.Path,
    *,
    max_retries: int = 5,
    backoff_s: float = 1.0,
    timeout_s: float = 30.0,
    sleep=time.sleep,
) -> None:
    """Raises ``SendError`` once every retry is exhausted -- never
    swallowed here, the caller decides what happens next. A 4xx
    response (unknown account, no forwarding target configured, target
    source not active, file too large) stops immediately rather than
    retrying: that request will never succeed no matter how many times
    it's repeated, so retrying it would only waste time and mask the
    real problem. A 5xx or a network-level failure is treated as
    transient and retried with exponential backoff.
    """
    url = "%s/admin/ftp-relay/%s/upload" % (base_url.rstrip("/"), account.username)
    last_error: SendError | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with file_path.open("rb") as fh:
                resp = session.post(
                    url,
                    files={"file": (file_path.name, fh)},
                    headers={"X-Ftp-Sync-Key": sync_key},
                    timeout=timeout_s,
                )
        except requests.RequestException as exc:
            last_error = SendError("%s: %s" % (url, exc))
        else:
            if resp.status_code == 200:
                return
            if 400 <= resp.status_code < 500:
                raise SendError(
                    "%s rejected %s: %d %s" % (url, file_path.name, resp.status_code, resp.text[:200])
                )
            last_error = SendError("%s: unexpected status %d" % (url, resp.status_code))
        if attempt < max_retries:
            sleep(backoff_s * (2 ** (attempt - 1)))
    raise last_error


def archive_file(file_path: pathlib.Path, archive_subdir: str, *, now=None) -> pathlib.Path:
    """Moves a successfully-sent file into a same-directory archive
    subfolder, sharded by hour (``YYYYMMDDHH``) -- at this service's
    real cadence (potentially over a million files/day across all
    accounts), one flat directory of ever-accumulating archived files
    would eventually degrade filesystem/directory-listing performance;
    sharding keeps each individual directory bounded."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    shard = now.strftime("%Y%m%d%H")
    dest_dir = file_path.parent / archive_subdir / shard
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / file_path.name
    file_path.rename(dest_path)
    return dest_path


def process_one(
    session: requests.Session,
    config: ForwarderConfig,
    account: RelayAccount,
    file_path: pathlib.Path,
) -> bool:
    """Returns True if the file was actually sent and archived, False
    if it was skipped for a benign reason (not a real file -- e.g. a
    directory, or it vanished/was already archived by a concurrent
    sweep). Re-raises ``SendError`` if sending genuinely failed after
    every retry -- callers log it and leave the file in place."""
    if not file_path.is_file():
        return False
    send_file(
        session, config.loader_base_url, config.sync_key, account, file_path,
        max_retries=config.max_retries, backoff_s=config.retry_backoff_s,
    )
    archive_file(file_path, config.archive_subdir)
    return True
