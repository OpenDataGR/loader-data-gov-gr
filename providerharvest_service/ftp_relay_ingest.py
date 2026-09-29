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

import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import harvest_db, harvest_runner
from providerharvest_service.admin_auth import require_ftp_sync_key
from providerharvest_service.db import get_session
from providerharvest_service.models import FtpAccount, ProviderSource

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
                "file exceeds max_file_size_bytes=%d (stopped after %d bytes read)" % (max_bytes, total)
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/{username}/upload")
async def relay_upload(
    username: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    hsession: Session = Depends(harvest_db.get_session),
) -> dict:
    source = _get_forwarding_target_or_404(session, username)

    config = json.loads(source.config or "{}")
    max_file_size = config.get("max_file_size_bytes", harvest_runner.DEFAULT_MAX_FILE_SIZE_BYTES)
    try:
        content = await _read_capped(file, max_file_size)
    except harvest_runner.FileTooLargeError as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc

    run = harvest_runner.record_pushed_file(hsession, source, file.filename or "upload.bin", content)
    return {"status": run.status, "run_id": run.id, "size": len(content)}
