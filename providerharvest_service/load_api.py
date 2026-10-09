"""The Load Box's published API: ``GET /data/{source name}`` returns a source's newest successfully transformed
output, exactly as stored (its own content type), so a consumer -- or a CKAN resource that links to it -- always
gets the current data from one stable address.

Public by design, like ``/proxy`` (and exempt from the site login for that reason), but only for a source an
admin has *published* (``LoadTarget.api_enabled``) and that is active. Everything else -- unknown name, not
published, not active, nothing transformed yet -- is the same 404, so the route cannot be used to find out which
sources exist. A caller gets an ``ETag`` (the output's id) and a conditional request answers 304; the same
per-address limit as the proxy applies.
"""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from providerharvest_service import harvest_db
from providerharvest_service.client_addr import sender_address
from providerharvest_service.db import get_session
from providerharvest_service.harvest_models import TransformedFile
from providerharvest_service.models import LoadTarget, ProviderSource
from providerharvest_service.proxy import _per_ip_rate_limiter_for

router = APIRouter(prefix="/data", tags=["load-api"])

PER_IP_PER_MINUTE = 60
CACHE_SECONDS = 60


def published_source(session: Session, source_name: str) -> ProviderSource | None:
    source = session.scalar(select(ProviderSource).where(ProviderSource.name == source_name))
    if source is None or source.status != "active":
        return None
    target = session.scalar(select(LoadTarget).where(LoadTarget.source_id == source.id))
    return source if target is not None and target.api_enabled else None


def latest_output(hsession: Session, source_id: str):
    """The newest good output that still has its contents (retention may have emptied older ones)."""
    return hsession.scalar(
        select(TransformedFile)
        .where(TransformedFile.source_id == source_id, TransformedFile.status == "ok", func.length(TransformedFile.content) > 0)
        .order_by(TransformedFile.created_at.desc()).limit(1)
    )


@router.get("/{source_name}")
@router.get("/{source_name}/{filename}")
def get_data(source_name: str, request: Request, filename: str = "", session: Session = Depends(get_session),
             hsession: Session = Depends(harvest_db.get_session)) -> Response:
    source = published_source(session, source_name)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such published data")
    if not _per_ip_rate_limiter_for("data:" + source.id, sender_address(request) or "unknown", PER_IP_PER_MINUTE).try_acquire():
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded -- try again shortly", headers={"Retry-After": "60"})
    output = latest_output(hsession, source.id)
    if output is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such published data")
    etag = '"%s"' % output.id
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=%d" % CACHE_SECONDS,
        "Content-Disposition": "inline; filename*=UTF-8\'\'%s" % urllib.parse.quote(output.filename or "data"),
        "Last-Modified": output.created_at.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "X-Content-Type-Options": "nosniff",
    }
    if etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=output.content, media_type=output.content_type, headers=headers)
