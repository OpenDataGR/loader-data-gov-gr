"""The Load Box's published API: ``GET /data/{source name}`` returns a source's newest successfully transformed
output, exactly as stored (its own content type), so a consumer -- or a CKAN resource that links to it -- always
gets the current data from one stable address.

A source's pipelines each answer at ``/data/{source name}/{pipeline}``; its ``default`` pipeline also at ``/data/{source name}`` (and an
optional trailing file name is ignored, so a link can end in ``.csv``).

Public by design (and exempt from the site login for that reason), but only for a pipeline an admin has *published*
(``Transform.published``) of a source that is active. Everything else -- unknown name, not
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
from providerharvest_service.models import ProviderSource, Transform
from providerharvest_service import proxy, publishing, transform_runner
from providerharvest_service.proxy import _per_ip_rate_limiter_for

router = APIRouter(prefix="/data", tags=["load-api"])

PER_IP_PER_MINUTE = 60
CACHE_SECONDS = 60


def published_source(session: Session, source_name: str) -> ProviderSource | None:
    """The source, when its default pipeline is published and it is active."""
    found = _resolve(session, source_name, "")
    return found[0] if found else None


def _resolve(session: Session, source_name: str, second: str) -> tuple[ProviderSource, Transform] | None:
    """The (source, pipeline) an address names: ``second`` is a pipeline's name, or else (a file name, ignored) the default pipeline.
    None -- one and the same 404 -- for an unknown name, a source that is not active, and a pipeline that is not published."""
    source = session.scalar(select(ProviderSource).where(ProviderSource.name == source_name))
    if source is None or source.status != "active":
        return None
    named = transform_runner.get_pipeline(session, source.id, second) if second else None
    if named is not None:
        return (source, named) if publishing.is_pipeline_published(named) else None
    default = transform_runner.get_transform(session, source.id)
    return (source, default) if publishing.is_pipeline_published(default) else None


def latest_output(hsession: Session, source_id: str, transform_id: str | None = None):
    """The newest good output (of one pipeline, when given) that still has its contents (retention may have emptied older ones)."""
    query = select(TransformedFile).where(TransformedFile.source_id == source_id, TransformedFile.status == "ok", func.length(TransformedFile.content) > 0)
    if transform_id:
        query = query.where(TransformedFile.transform_id == transform_id)
    return hsession.scalar(query.order_by(TransformedFile.created_at.desc()).limit(1))


def _serve_output(source: ProviderSource, pipeline: Transform, request: Request, hsession: Session) -> Response:
    """A source that stores data: its newest good output, exactly as stored."""
    if not _per_ip_rate_limiter_for("data:" + pipeline.id, sender_address(request) or "unknown", PER_IP_PER_MINUTE).try_acquire():
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded -- try again shortly", headers={"Retry-After": "60"})
    output = latest_output(hsession, source.id, pipeline.id)
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
@router.get("/{source_name}/{second}")
@router.get("/{source_name}/{second}/{filename}")
async def get_data(source_name: str, request: Request, second: str = "", filename: str = "", session: Session = Depends(get_session),
                   hsession: Session = Depends(harvest_db.get_session)) -> Response:
    found = _resolve(session, source_name, second)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such published data")
    source, pipeline = found
    if source.transport_type == "http":
        # An API source stores nothing: the provider is asked at this moment (see proxy.py: request template, credential, pipeline).
        return await proxy.answer_get(session, source, pipeline, request)
    return await run_in_threadpool(_serve_output, source, pipeline, request, hsession)


@router.post("/{source_name}")
@router.post("/{source_name}/{second}")
async def post_data(source_name: str, request: Request, second: str = "", session: Session = Depends(get_session)) -> Response:
    """Only an API source is asked with a body (a JSON body, passed on or turned into the provider's query -- see proxy.py)."""
    found = _resolve(session, source_name, second)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such published data")
    source, pipeline = found
    if source.transport_type != "http":
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "this address answers GET requests", headers={"Allow": "GET"})
    return await proxy.answer_post(session, source, pipeline, request)
