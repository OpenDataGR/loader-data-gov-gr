"""Ingestion for files forwarded by ``ftp-forwarder`` (the trusted
external FTP/SFTP-receiving box -- see ``ftp-forwarder/README.md``),
distinct from ``push_ingest.py`` (a provider calling loader directly).

Authenticated by the same narrow ``LOADER_FTP_SYNC_KEY`` that box
already holds for account-sync and its own status heartbeat (see
``admin_auth.require_ftp_sync_key``) -- deliberately *not* a per-source
API key like ``push_ingest.py`` uses. The box uploads on behalf of
whichever local FTP account a file arrived under; which push-mode
source that maps to is ``FtpAccount.push_target_source_name``, set via
the admin panel (``admin_ftp.py``), not something the box needs to
know, hold a credential for, or keep in a hand-maintained config file.
That's the whole point of this endpoint: it turns "one API key per
provider, provisioned by hand onto the box" into "one shared key the
box already has, plus a mapping loader already owns."
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from providerharvest_service import harvest_db, harvest_runner, transform_runner
from providerharvest_service.admin_auth import require_ftp_sync_key
from providerharvest_service.db import get_session
from providerharvest_service.models import FtpAccount, ProviderSource, _utcnow

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/ftp-relay", tags=["ftp-relay-ingest"], dependencies=[Depends(require_ftp_sync_key)]
)

_READ_CHUNK_BYTES = 65536


def _get_forwarding_target_or_404(session: Session, username: str) -> ProviderSource:
    account = session.scalar(select(FtpAccount).where(FtpAccount.username == username))
    # Same single-404 posture push_ingest.py's own lookup uses: an
    # unknown account, a disabled one, one with no forwarding target
    # set yet, and a target that isn't an active push source all look
    # identical to the caller -- none of those distinctions should be
    # discoverable from the outside, and internally they all mean the
    # same thing anyway ("don't accept this upload right now").
    if account is None or not account.enabled or not account.push_target_source_name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such account or no forwarding target configured")
    source = session.scalar(
        select(ProviderSource).where(ProviderSource.name == account.push_target_source_name)
    )
    if source is None or source.transport_type != "push" or source.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "forwarding target is not an active push source")
    return source


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    # Same reasoning as push_ingest.py's own copy: cap while streaming,
    # never fully buffer an oversized upload. Kept as its own small
    # copy rather than a shared import -- harvest_runner.py is
    # otherwise plain, framework-agnostic business logic, and this is
    # the only place two callers would share an async-FastAPI-specific
    # helper.
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise harvest_runner.FileTooLargeError(
                "file exceeds the %d-byte size limit (stopped after %d bytes read)" % (max_bytes, total)
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/{username}/upload")
async def relay_upload(
    username: str,
    file: UploadFile = File(...),
    x_content_sha256: str | None = Header(default=None),
    session: Session = Depends(get_session),
    hsession: Session = Depends(harvest_db.get_session),
) -> dict:
    source = _get_forwarding_target_or_404(session, username)

    try:
        content = await _read_capped(file, harvest_runner.MAX_FILE_SIZE_BYTES)
    except harvest_runner.FileTooLargeError as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc

    checksum = hashlib.sha256(content).hexdigest()
    if x_content_sha256 and x_content_sha256.strip().lower() != checksum:
        # Damaged on the way here: nothing is stored (no run), and a 5xx makes the box send it again.
        logger.warning("relay upload for %s: checksum mismatch (box says %s, received %s, %d bytes)", username, x_content_sha256, checksum, len(content))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "checksum mismatch -- the file was damaged on the way, send it again")

    run = harvest_runner.record_pushed_file(hsession, source, file.filename or "upload.bin", content)
    await run_in_threadpool(transform_runner.on_new_file, session, hsession, source, run, file.filename or "upload.bin", content)
    # Only when it arrived -- not who from: the request here comes from
    # the relay box, and the provider's own address is reported
    # separately by that box (see admin_ftp.report_ftp_box_status).
    source.last_received_at = _utcnow()
    session.commit()
    return {"status": run.status, "run_id": run.id, "size": len(content), "sha256": checksum}
