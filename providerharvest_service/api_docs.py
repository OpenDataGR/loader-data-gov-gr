"""Documentation of a published address, in the style of an API reference: an OpenAPI description (``/api-docs/<source>[/<pipeline>]/openapi.json``) and a
Swagger UI page over it (``/api-docs/<source>[/<pipeline>]``), made from what the loader already knows -- the path, the method, the request template's
parameters, the output format and, for a source that stores data, the columns of the newest output.

Public like the address it describes (and exempt from the site login for that reason), and only for a pipeline that is published of an active source: anything
else is the same 404 as the address itself gives. The Swagger UI script and style come from a CDN, so nothing needs installing on the VM.
"""

from __future__ import annotations

import html
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from providerharvest_service import ckan_datastore, harvest_db, load_api, proxy, publishing, request_template, transform_engine as te, transform_runner
from providerharvest_service.db import get_session
from providerharvest_service.models import ProviderSource, Transform

router = APIRouter(prefix="/api-docs", tags=["api-docs"])

SWAGGER_UI = "5.17.14"
PER_IP_PER_MINUTE = load_api.PER_IP_PER_MINUTE
_JSON_TYPES = {"numeric": {"type": "number"}, "bool": {"type": "boolean"}, "timestamp": {"type": "string", "format": "date-time"}, "text": {"type": "string"}}


def docs_path(source: ProviderSource, pipeline: Transform) -> str:
    """Where the documentation of a pipeline's address is: the default pipeline at /api-docs/<source>, any other at /api-docs/<source>/<pipeline>."""
    if pipeline.name == transform_runner.DEFAULT_PIPELINE:
        return "/api-docs/%s" % source.name
    return "/api-docs/%s/%s" % (source.name, pipeline.name)


def public_base(request: Request | None = None) -> str:
    return (os.environ.get("LOADER_PUBLIC_BASE_URL") or (str(request.base_url) if request is not None else "")).rstrip("/")


def _sample(hsession: Session | None, source: ProviderSource, pipeline: Transform):
    """(columns, example rows, content) of the newest output of a source that stores data; (None, [], b"") when there is none to read."""
    if hsession is None or source.transport_type == "http":
        return None, [], None
    output = load_api.latest_output(hsession, source.id, pipeline.id)
    if output is None or not output.content:
        return None, [], None
    try:
        rows = ckan_datastore.records_of(output.content, output.filename, output.content_type)[:3]
        fields, records = ckan_datastore.table_of(rows)
        return fields, records, output.content
    except ckan_datastore.DatastoreError:
        return None, [], output.content


def _response_for(source: ProviderSource, pipeline: Transform, fields, records, content) -> tuple[str, dict, str]:
    """(content type, media-type object, what it says) of the 200 answer."""
    api = source.transport_type == "http"
    if api and not pipeline.enabled:
        return "application/json", {"schema": {"type": "object", "description": "Whatever the provider answers, unchanged."}}, "The provider's own answer, as it is at the moment of the call."
    kind = pipeline.output_format or "json"
    ctype = te.OUTPUTS.get(kind, te.OUTPUTS["json"])[0].split(";")[0]
    when = "built from the provider's answer at the moment of the call" if api else "the newest successful output of the pipeline"
    if kind in ("json", "jsonl") and fields:
        item = {"type": "object", "properties": {f["id"]: dict(_JSON_TYPES.get(f["type"], {"type": "string"})) for f in fields}}
        if kind == "json":
            return ctype, {"schema": {"type": "array", "items": item}, "example": records}, "JSON, a list of records: %s." % when
        return ctype, {"schema": {"type": "string", "description": "One JSON record per line."}, "example": "\n".join(json.dumps(r, ensure_ascii=False) for r in records)}, "JSON Lines, one record per line: %s." % when
    if kind == "csv" and fields:
        text = ",".join(f["id"] for f in fields) + "\n" + "\n".join(",".join("" if r[f["id"]] is None else str(r[f["id"]]) for f in fields) for r in records)
        return ctype, {"schema": {"type": "string"}, "example": text}, "CSV with a header row: %s." % when
    example = (content or b"")[:600].decode("utf-8", "replace") if content else None
    media: dict = {"schema": {"type": "string" if kind != "json" else "array", **({"items": {"type": "object"}} if kind == "json" else {})}}
    if example:
        media["example"] = example
    return ctype, media, "%s: %s." % (te.OUTPUTS.get(kind, te.OUTPUTS["json"])[2].split(" (")[0], when)


def build_spec(source: ProviderSource, pipeline: Transform, base: str, hsession: Session | None = None) -> dict:
    """The OpenAPI 3.1 description of a published pipeline's address."""
    api = source.transport_type == "http"
    path = publishing.address_of(source, pipeline)
    method = proxy.consumer_method(source, pipeline).lower() if api else "get"
    fields, records, content = _sample(hsession, source, pipeline)
    ctype, media, says = _response_for(source, pipeline, fields, records, content)
    title = source.title if pipeline.name == transform_runner.DEFAULT_PIPELINE else "%s / %s" % (source.title, pipeline.name)
    notes = ["Public, no key needed. At most %d requests a minute from one address." % PER_IP_PER_MINUTE]
    operation: dict = {"summary": "Get %s" % title, "operationId": "get_%s" % (source.name.replace("-", "_") + ("" if pipeline.name == transform_runner.DEFAULT_PIPELINE else "_" + pipeline.name)),
                       "tags": [source.title]}
    parameters = []
    if api and proxy.request_is_swapped(pipeline):
        names = request_template.placeholders(pipeline.request_template)
        notes.append("The loader turns the call into the provider's own request (%s), using the parameters below." % ("POST" if source.proxy_method == "POST" else "GET"))
        if method == "get":
            parameters = [{"name": n, "in": "query", "required": True, "schema": {"type": "string"}, "description": "Passed to the provider as %s." % n} for n in names]
        else:
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {n: {"type": "string"} for n in names}, "required": names}}}}
    elif api and method == "post":
        operation["requestBody"] = {"required": False, "content": {"application/json": {"schema": {"type": "object", "description": "Sent to the provider as it is."}}}}
    elif api:
        notes.append("Query parameters are passed on to the provider.")
    if api:
        notes.insert(0, "Asked of the provider at the moment of the call; nothing is stored.")
    else:
        notes.insert(0, "Always the newest successful output of the pipeline; it changes whenever a new file has been processed.")
    operation["description"] = " ".join(notes)
    if parameters:
        operation["parameters"] = parameters
    headers = {} if api else {"ETag": {"schema": {"type": "string"}, "description": "Identifies the output; send it back as If-None-Match to get a 304 when nothing changed."},
                              "Cache-Control": {"schema": {"type": "string"}, "description": "public, max-age=60"}}
    responses: dict = {"200": {"description": says, "content": {ctype: media}, **({"headers": headers} if headers else {})},
                       "404": {"description": "Unknown, not public, or (for stored data) nothing has been processed yet."},
                       "429": {"description": "Too many requests from this address; try again after the number of seconds in Retry-After."}}
    if api:
        responses["502"] = {"description": "The provider could not be reached or refused the call."}
    if not api:
        responses["304"] = {"description": "Nothing changed since the ETag that was sent."}
    operation["responses"] = responses
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": "1.0", "description": "Published by loader.data.gov.gr. " + ("The source: %s." % source.title)},
        "servers": [{"url": base}] if base else [],
        "paths": {path: {method: operation}},
        "tags": [{"name": source.title}],
    }


def page(title: str, spec_url: str) -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s -- API documentation</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@%(v)s/swagger-ui.css">
<style>body{margin:0;background:#fff}</style></head>
<body><div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@%(v)s/swagger-ui-bundle.js"></script>
<script>window.onload = function () { window.ui = SwaggerUIBundle({url: %(url)s, dom_id: '#swagger-ui', deepLinking: true, docExpansion: 'full'}); };</script>
</body></html>""" % {"title": html.escape(title), "v": SWAGGER_UI, "url": json.dumps(spec_url)}


def _found(session: Session, request: Request, source_name: str, pipeline: str) -> tuple[ProviderSource, Transform]:
    found = load_api._resolve(session, source_name, pipeline)
    if found is None or (pipeline and found[1].name != pipeline):  # (a name that is not a pipeline is not a page either)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such published data")
    if not proxy._per_ip_rate_limiter_for("docs:" + found[1].id, load_api.sender_address(request) or "unknown", PER_IP_PER_MINUTE).try_acquire():
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded -- try again shortly", headers={"Retry-After": "60"})
    return found


@router.get("/{source_name}/openapi.json")
@router.get("/{source_name}/{pipeline}/openapi.json")
def spec(source_name: str, request: Request, pipeline: str = "", session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    source, found = _found(session, request, source_name, pipeline)
    return JSONResponse(build_spec(source, found, public_base(request), hsession), headers={"Cache-Control": "public, max-age=60"})


@router.get("/{source_name}", response_class=HTMLResponse)
@router.get("/{source_name}/{pipeline}", response_class=HTMLResponse)
def docs(source_name: str, request: Request, pipeline: str = "", session: Session = Depends(get_session)):
    source, found = _found(session, request, source_name, pipeline)
    return HTMLResponse(page(source.title if found.name == transform_runner.DEFAULT_PIPELINE else "%s / %s" % (source.title, found.name), docs_path(source, found) + "/openapi.json"))
