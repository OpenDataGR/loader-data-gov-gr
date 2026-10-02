"""Push-mode ingestion: the provider calls *us* instead of us pulling
from them -- for providers whose own systems can only push files
outward (or who'd rather not expose their own endpoint at all), and as
an HTTPS-only alternative to a real FTP/SFTP receiving server (which
needs its own public IP/network path this VM doesn't have yet -- see
README's roadmap). Needs zero new infrastructure: this is just another
route on the same app, reachable over the exact same HTTPS path
everything else already uses.

Deliberately narrow, matching the agreed first slice:
  * Only ``transport_type="push"`` sources, only ``status="active"``
    ones -- same "don't even reveal a pending/rejected source exists"
    posture ``proxy.py`` already uses (a single 404 for "no such
    source", "wrong transport type", and "not active yet" alike).
  * One file per call, authenticated by the per-source API key
    generated at registration (``provider_sources._push_fields``) and
    checked here via bcrypt -- never decrypted/reversible, the key is
    only ever verified, never read back.
  * Read in capped chunks (the same system-wide
    ``harvest_runner.MAX_FILE_SIZE_BYTES`` the pull-mode file transports use), so an oversized upload is
    rejected before being fully buffered into memory -- same reasoning
    as ``harvest_runner._read_capped``, just against an async
    ``UploadFile`` instead of a sync stream.
"""

from __future__ import annotations

import json

import bcrypt
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from providerharvest_service import failed_auth, harvest_db, harvest_runner, provider_sources, sender_allowlist
from providerharvest_service.client_addr import sender_address
from providerharvest_service.db import get_session
from providerharvest_service.models import ProviderSource, _utcnow

router = APIRouter(prefix="/push", tags=["push-ingest"])

_READ_CHUNK_BYTES = 65536


def _get_active_push_source_or_404(session: Session, source_name: str) -> ProviderSource:
    source = session.scalar(select(ProviderSource).where(ProviderSource.name == source_name))
    # Same 404 whether the name doesn't exist, isn't a push source, or
    # isn't active yet -- a pending/rejected/paused source's existence
    # must not be discoverable via this public route, same posture
    # proxy.py's own lookup already uses.
    if source is None or source.transport_type != "push" or source.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such push source")
    return source


def _check_api_key(source: ProviderSource, presented_key: str) -> None:
    config = json.loads(source.config or "{}")
    stored_hash = config.get("push_api_key_hash", "")
    # bcrypt.checkpw itself is constant-time on the hash comparison; an
    # empty stored_hash (should never happen for a real push source,
    # but never trust that blindly) must still fail closed rather than
    # raise or vacuously pass.
    if not stored_hash or not bcrypt.checkpw(presented_key.encode("utf-8"), stored_hash.encode("ascii")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Async counterpart to ``harvest_runner._read_capped`` -- same
    "cap while streaming, never fully buffer an oversized upload"
    reasoning, just against ``UploadFile``'s async read instead of a
    sync file-like object."""
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


@router.post("/{source_name}/upload")
async def push_upload(
    source_name: str,
    request: Request,
    file: UploadFile = File(...),
    x_api_key: str = Header(...),
    session: Session = Depends(get_session),
    hsession: Session = Depends(harvest_db.get_session),
) -> dict:
    source = _get_active_push_source_or_404(session, source_name)
    # Before the key is even looked at (no bcrypt for an address that is out anyway):
    # a provider may limit a source to its own machine(s) when registering it. An
    # address that cannot be determined is not "in the list" either.
    sender = sender_address(request)
    allowed = provider_sources.allowed_senders(source)
    if not sender_allowlist.permits(allowed, sender):
        await run_in_threadpool(failed_auth.record, request, failed_auth.PUSH_ADDRESS, source_name, 403)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this source does not accept uploads from your address")
    _check_api_key(source, x_api_key)

    try:
        content = await _read_capped(file, harvest_runner.MAX_FILE_SIZE_BYTES)
    except harvest_runner.FileTooLargeError as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc

    run = harvest_runner.record_pushed_file(hsession, source, file.filename or "upload.bin", content)
    source.last_received_at = _utcnow()
    if sender:
        source.last_sender = sender
    session.commit()
    return {"status": run.status, "run_id": run.id, "size": len(content)}
