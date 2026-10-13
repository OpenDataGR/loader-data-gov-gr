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
from starlette.concurrency import run_in_threadpool

from providerharvest_service import harvest_db
from providerharvest_service.client_addr import sender_address
from providerharvest_service.db import get_session
from providerharvest_service.harvest_models import TransformedFile
from providerharvest_service.models import LoadTarget, ProviderSource
from providerharvest_service import proxy, publishing, transform_runner
from providerharvest_service.proxy import _per_ip_rate_limiter_for

router = APIRouter(prefix="/data", tags=["load-api"])

PER_IP_PER_MINUTE = 60
CACHE_SECONDS = 60


def published_source(session: Session, source_name: str) -> ProviderSource | None:
    source = session.scalar(select(ProviderSource).where(ProviderSource.name == source_name))
    if source is None or source.status != "active":
        return None
    return source if publishing.is_published(session, source.id) else None


def latest_output(hsession: Session, source_id: str, transform_id: str | None = None):
    """The newest good output (of one pipeline, when given) that still has its contents (retention may have emptied older ones)."""
    query = select(TransformedFile).where(TransformedFile.source_id == source_id, TransformedFile.status == "ok", func.length(TransformedFile.content) > 0)
    if transform_id:
        query = query.where(TransformedFile.transform_id == transform_id)
    return hsession.scalar(query.order_by(TransformedFile.created_at.desc()).limit(1))


def _serve_output(source: ProviderSource, request: Request, hsession: Session, transform_id: str) -> Response:
    """A source that stores data: its newest good output, exactly as stored."""
    if not _per_ip_rate_limiter_for("data:" + source.id, sender_address(request) or "unknown", PER_IP_PER_MINUTE).try_acquire():
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded -- try again shortly", headers={"Retry-After": "60"})
    output = latest_output(hsession, source.id, transform_id)
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


@router.get("/{source_name}")
@router.get("/{source_name}/{filename}")
async def get_data(source_name: str, request: Request, filename: str = "", session: Session = Depends(get_session),
                   hsession: Session = Depends(harvest_db.get_session)) -> Response:
    source = published_source(session, source_name)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such published data")
    if source.transport_type == "http":
        # An API source stores nothing: the provider is asked at this moment (see proxy.py: request template, credential, transform).
        return await proxy.proxy_get(source_name, request, session)
    pipeline = transform_runner.get_transform(session, source.id)  # until each pipeline has an address of its own: the default one
    if pipeline is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such published data")
    return await run_in_threadpool(_serve_output, source, request, hsession, pipeline.id)


@router.post("/{source_name}")
async def post_data(source_name: str, request: Request, session: Session = Depends(get_session)) -> Response:
    """Only an API source is asked with a body (a JSON body, passed on or turned into the provider's query -- see proxy.py)."""
    source = published_source(session, source_name)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such published data")
    if source.transport_type != "http":
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "this address answers GET requests", headers={"Allow": "GET"})
    return await proxy.proxy_post(source_name, request, session)
