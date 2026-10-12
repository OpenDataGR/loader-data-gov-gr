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
import hashlib
import json
import logging
import pathlib
import re
import shutil
import time

import requests

from ftp_forwarder.accounts import RelayAccount
from ftp_forwarder.config import ForwarderConfig

logger = logging.getLogger(__name__)


class SendError(RuntimeError):
    pass


SIDECAR_SUFFIX = ".sha256"
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def sidecar_path(file_path: pathlib.Path) -> pathlib.Path:
    return file_path.with_name(file_path.name + SIDECAR_SUFFIX)


SIDECAR_MAX_AGE_S = 3600  # a checksum file this much older than its data file belongs to an earlier upload
SIDECAR_CLOCK_SLACK_S = 5


def sidecar_applies(sidecar: pathlib.Path, file_path: pathlib.Path) -> bool:
    """A checksum file speaks for the file only if it was written shortly before it (the provider sends the checksum
    first, then the file). An old one left over from an earlier upload, or one that arrived after the file, must not
    reject a good file."""
    try:
        gap = file_path.stat().st_mtime - sidecar.stat().st_mtime
    except OSError:
        return False
    return -SIDECAR_CLOCK_SLACK_S <= gap <= SIDECAR_MAX_AGE_S


def read_sidecar(path: pathlib.Path) -> str:
    """The checksum a provider put in ``<file>.sha256``: the first word of the first non-empty line, the way
    ``sha256sum`` writes it (``<64 hex digits>  <file name>``). Raises ``ValueError`` when there is none."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ValueError("cannot be read (%s)" % exc) from exc
    for line in text.splitlines():
        words = line.split()
        if words:
            if _HEX64.match(words[0]):
                return words[0].lower()
            raise ValueError("does not start with a 64-digit SHA-256 (got %r)" % words[0][:70])
    raise ValueError("is empty")


def file_sha256(file_path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
    # What this box holds, as a fingerprint: loader checks what it received against it, and answers with the
    # fingerprint of what it stored; only when both agree is the file counted as sent (and archived). An old
    # loader that does not answer with one is trusted as before.
    checksum = file_sha256(file_path)
    last_error: SendError | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with file_path.open("rb") as fh:
                resp = session.post(
                    url,
                    files={"file": (file_path.name, fh)},
                    headers={"X-Ftp-Sync-Key": sync_key, "X-Content-SHA256": checksum},
                    timeout=timeout_s,
                )
        except requests.RequestException as exc:
            last_error = SendError("%s: %s" % (url, exc))
        else:
            if resp.status_code == 200:
                try:
                    stored = json.loads(resp.text).get("sha256")
                except (ValueError, AttributeError):
                    stored = None
                if stored is None or stored == checksum:
                    return
                last_error = SendError("%s: loader stored different content for %s (checksum mismatch)" % (url, file_path.name))
                if attempt < max_retries:
                    sleep(backoff_s * (2 ** (attempt - 1)))
                continue
            if 400 <= resp.status_code < 500:
                raise SendError(
                    "%s rejected %s: %d %s" % (url, file_path.name, resp.status_code, resp.text[:200])
                )
            last_error = SendError("%s: unexpected status %d" % (url, resp.status_code))
        if attempt < max_retries:
            sleep(backoff_s * (2 ** (attempt - 1)))
    raise last_error


def reject_file(file_path: pathlib.Path, sidecar: pathlib.Path, rejected_subdir: str, reason: str, *, now=None) -> None:
    """Puts a file and its checksum file aside in ``<folder>/<rejected_subdir>/`` (names prefixed with the time, so a
    second rejection of the same name does not overwrite the first), and says why in the log."""
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y%m%d-%H%M%S")
    target = file_path.parent / rejected_subdir
    target.mkdir(exist_ok=True)
    for path in (file_path, sidecar):
        if path.is_file():
            shutil.move(str(path), str(target / ("%s-%s" % (stamp, path.name))))
    logger.warning("not forwarding %s: %s -- put aside in %s", file_path.name, reason, target)


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
    if file_path.name.endswith(SIDECAR_SUFFIX):
        return False  # a provider's checksum file belongs to the file it names, it is not data
    if not file_path.is_file():
        return False
    sidecar = sidecar_path(file_path)
    sidecar_applies_after_archive = sidecar.is_file() and sidecar_applies(sidecar, file_path)
    if sidecar_applies_after_archive:
        # The provider sent a checksum of what they sent (before the file itself -- see the README). Only a file
        # that matches it goes on; one that does not was damaged between them and this box, and is put aside
        # (not sent, not left to be retried for ever) for the provider to send again.
        try:
            problem = None if read_sidecar(sidecar) == file_sha256(file_path) else "its checksum does not match the one the provider sent"
        except ValueError as exc:
            problem = "the checksum file %s" % exc
        if problem:
            reject_file(file_path, sidecar, config.rejected_subdir, problem)
            return False
    send_file(
        session, config.loader_base_url, config.sync_key, account, file_path,
        max_retries=config.max_retries, backoff_s=config.retry_backoff_s,
    )
    archive_file(file_path, config.archive_subdir)
    if sidecar.is_file() and sidecar_applies_after_archive:
        archive_file(sidecar, config.archive_subdir)
    return True
