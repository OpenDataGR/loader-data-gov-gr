"""The Transform Box: where a source's input is turned into something else before it goes anywhere.

For each source an admin builds a transform out of steps (read the file's format, add columns from an uploaded
index table, filter, rename, convert, ...), tries it on the source's latest stored file, switches it on, and can
re-run it over older files. Outputs are stored beside the originals (never replacing them) and can be
downloaded. Index tables are uploaded here and shared by any transform.

Everything is generic: the page is driven by what a sample of the source's own file looks like and by the step
registry in ``transform_engine`` / ``transform_forms``; nothing here knows any one source. Behind the site-wide
login (admin_session.py).
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import types
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from providerharvest_service import (
    proxy, request_template, admin_session, build_info, harvest_db, nav, provider_sources, retention, transform_engine as te, transform_forms as tf, transform_runner,
)
from providerharvest_service.db import get_session
from providerharvest_service.harvest_models import TransformedFile
from providerharvest_service.models import LookupTable, ProviderSource, Transform

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/transform", tags=["transform-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now
templates.env.globals["pending_approvals"] = nav.pending_approvals
templates.env.filters["utc"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else ""
templates.env.filters["short_utc"] = lambda value: value.strftime("%m-%d %H:%M") if value else ""
templates.env.filters["step_label"] = tf.label_of
templates.env.filters["step_summary"] = tf.summarize
templates.env.globals["suggest_name"] = tf.suggest_name

_TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_SECTIONS = {"read": "Read the file", "lookup": "Add columns from an index table", "filter": "Keep only matching records",
             "rename": "Rename fields", "convert": "Convert field types", "select": "Keep only these fields",
             "drop": "Remove fields", "add_field": "Add a field", "utm": "UTM coordinates to latitude / longitude"}


def _who(request: Request) -> str:
    return admin_session.verify_session_cookie(request.cookies.get(admin_session.COOKIE_NAME, "")) or ""


def _go(path: str, flash: str | None = None, kind: str = "success") -> RedirectResponse:
    query = "?" + urlencode({"flash": flash, "flash_kind": kind}) if flash else ""
    return RedirectResponse(path + query, status_code=303)


def _size(count: int) -> str:
    value = float(count)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1000


def _pipeline_name(request: Request) -> str:
    """The pipeline an address is about: the ``{pipeline}`` of /transform/sources/<id>/pipelines/<name>/..., else the default one."""
    return request.path_params.get("pipeline") or transform_runner.DEFAULT_PIPELINE


def _base(request: Request, source_id: str) -> str:
    """The address of this pipeline's page: every link and redirect on a pipeline's pages starts from it."""
    name = _pipeline_name(request)
    base = "/transform/sources/%s" % source_id
    return base if name == transform_runner.DEFAULT_PIPELINE else "%s/pipelines/%s" % (base, name)


_PIPELINE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,29}$")


def _source_or_none(session: Session, source_id: str) -> ProviderSource | None:
    return session.get(ProviderSource, source_id)


def _tables(session: Session) -> list[LookupTable]:
    return list(session.scalars(select(LookupTable).order_by(LookupTable.name)).all())


def _table_columns(session: Session) -> dict[str, list[str]]:
    return {t.name: json.loads(t.columns) for t in _tables(session)}


# -- overview and index tables -------------------------------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def overview(request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    sources = provider_sources.list_all(session)
    transforms = {t.source_id: t for t in session.scalars(select(Transform)).all()}
    rows = []
    for source in sources:
        transform = transforms.get(source.id)
        counts = transform_runner.output_counts(hsession, source.id) if transform else None
        rows.append({"source": source, "transform": transform, "steps": len(transform_runner.steps_of(transform)) if transform else 0, "counts": counts})
    tables = []
    for table in _tables(session):
        tables.append({"table": table, "columns": json.loads(table.columns), "used_by": len(transform_runner.lookups_in_use(session, table.name))})
    return templates.TemplateResponse(request, "transform/overview.html", {
        "rows": rows, "tables": tables, "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
    })


async def _read_upload(form, key: str = "file") -> tuple[bytes, str]:
    upload = form.get(key)
    if upload is None or not hasattr(upload, "read"):
        raise te.TransformError("Choose a file to upload.")
    data = await upload.read(te.MAX_LOOKUP_BYTES + 1)
    return data, getattr(upload, "filename", "") or ""


@router.post("/tables")
async def upload_table(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    name = str(form.get("name") or "").strip().lower()
    try:
        if not _TABLE_NAME.match(name):
            raise te.TransformError("Name the table with lowercase letters, digits and underscores, starting with a letter (for example stations).")
        if session.scalar(select(LookupTable).where(LookupTable.name == name)) is not None:
            raise te.TransformError("There is already a table called %r -- open it to replace its contents." % name)
        content, filename = await _read_upload(form)
        columns, rows, note = te.parse_lookup_ex(content, filename, str(form.get("sheet") or "").strip() or None)
    except te.TransformError as exc:
        return _go("/transform", str(exc), "error")
    session.add(LookupTable(name=name, description=str(form.get("description") or "").strip()[:200], columns=json.dumps(columns),
                            rows=json.dumps(rows), row_count=len(rows), uploaded_by=_who(request)))
    session.commit()
    return _go("/transform", ("Index table %s uploaded: %d rows, %d columns. %s" % (name, len(rows), len(columns), note)).strip())


def _table_or_none(session: Session, table_id: str) -> LookupTable | None:
    return session.get(LookupTable, table_id)


@router.get("/tables/{table_id}", response_class=HTMLResponse)
def table_page(table_id: str, request: Request, session: Session = Depends(get_session)):
    table = _table_or_none(session, table_id)
    if table is None:
        return _go("/transform", "No such index table.", "error")
    rows = json.loads(table.rows)
    return templates.TemplateResponse(request, "transform/table.html", {
        "table": table, "columns": json.loads(table.columns), "rows": rows[:25], "used_by": transform_runner.lookups_in_use(session, table.name),
        "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
    })


@router.post("/tables/{table_id}/replace")
async def replace_table(table_id: str, request: Request, session: Session = Depends(get_session)):
    table = _table_or_none(session, table_id)
    if table is None:
        return _go("/transform", "No such index table.", "error")
    back = "/transform/tables/%s" % table_id
    form = await request.form()
    try:
        content, filename = await _read_upload(form)
        columns, rows, note = te.parse_lookup_ex(content, filename, str(form.get("sheet") or "").strip() or None)
        # Transforms that use this table keep working only if the columns they need are still there.
        for source_id in transform_runner.lookups_in_use(session, table.name):
            for transform in transform_runner.list_pipelines(session, source_id):
                for step in transform_runner.steps_of(transform):
                    if step.get("type") == "lookup" and step.get("table") == table.name:
                        needed = [step.get("table_key")] + [a.get("column") for a in step.get("add") or []]
                        missing = [c for c in needed if c not in columns]
                        if missing:
                            raise te.TransformError("A transform uses column(s) %s, which the new file does not have. Nothing was changed." % ", ".join(map(repr, missing)))
    except te.TransformError as exc:
        return _go(back, str(exc), "error")
    table.columns, table.rows, table.row_count = json.dumps(columns), json.dumps(rows), len(rows)
    table.uploaded_at, table.uploaded_by = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), _who(request)
    session.commit()
    return _go(back, ("Replaced: %d rows, %d columns. %s Files that arrive from now on use it." % (len(rows), len(columns), note)).replace("  ", " "))


@router.post("/tables/{table_id}/delete")
def delete_table(table_id: str, session: Session = Depends(get_session)):
    table = _table_or_none(session, table_id)
    if table is None:
        return _go("/transform", "No such index table.", "error")
    if transform_runner.lookups_in_use(session, table.name):
        return _go("/transform/tables/%s" % table_id, "A transform uses this table. Remove that step first.", "error")
    name = table.name
    session.delete(table)
    session.commit()
    return _go("/transform", "Index table %s deleted." % name)


@router.get("/tables/{table_id}/download")
def download_table(table_id: str, session: Session = Depends(get_session)):
    table = _table_or_none(session, table_id)
    if table is None:
        return _go("/transform", "No such index table.", "error")
    data = te.write_output(json.loads(table.rows), "csv")
    return Response(content=data, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="%s.csv"' % table.name})


# -- one source's transform ---------------------------------------------------------------------------------------------------------------

def _editor_context(session: Session, hsession: Session, source: ProviderSource, transform: Transform | None, request: Request, **extra) -> dict:
    steps = transform_runner.steps_of(transform) if transform else []
    return {
        "tbase": _base(request, source.id), "pipeline_name": _pipeline_name(request), "pipelines": [t.name for t in transform_runner.list_pipelines(session, source.id)],
        "source": source, "transform": transform, "steps": list(enumerate(steps, 1)), "problems": transform_runner.check(session, transform) if transform else [],
        "keep_label": retention.label(retention.loader_days(source)),
        "failed_only": request.query_params.get("show") == "failed",
        "outputs": transform_runner.recent_outputs(hsession, source.id, 50 if request.query_params.get("show") == "failed" else 10,
                                                   failed_only=request.query_params.get("show") == "failed", transform_id=transform.id) if transform else [], "counts": transform_runner.output_counts(hsession, source.id, transform.id) if transform else None,
        "formats": te.OUTPUTS, "delimiters": te.CSV_DELIMITERS, "choices": transform_runner.REPROCESS_CHOICES, "has_sample": transform_runner.sample_for(session, hsession, source.id) is not None,
        "is_http": source.transport_type == "http", "provider_method": source.proxy_method,
        "input_summary": _input_summary(source, transform, hsession), "output_summary": tf.output_summary(transform) if transform else "",
        "test_request": source.test_request, "source_edit_url": "/provider/sources/%s/edit" % source.id,
        "proxy_name": source.name, "pasted_sample": bool(transform and transform.sample),
        "pasted_sample_kb": round(len(transform.sample.encode("utf-8")) / 1000, 1) if transform and transform.sample else 0,
        "add_types": [(k, v) for k, v in _SECTIONS.items() if k != "read"], "preview": None,
        "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"), "size": _size, **extra,
    }


def _newest_file(hsession: Session, source_id: str):
    """(filename, size, fetched_at) of the newest stored file of a source, without its contents, or None."""
    from sqlalchemy import func as sql_func

    from providerharvest_service.harvest_models import HarvestedFile, HarvestRun

    row = hsession.execute(
        select(HarvestedFile.filename, sql_func.length(HarvestedFile.content), HarvestedFile.fetched_at)
        .join(HarvestRun, HarvestRun.id == HarvestedFile.run_id)
        .where(HarvestRun.source_id == source_id, sql_func.length(HarvestedFile.content) > 0)
        .order_by(HarvestedFile.fetched_at.desc()).limit(1)
    ).first()
    return row


def _input_summary(source: ProviderSource, transform: Transform | None, hsession: Session) -> str:
    """The Input step, in words: where the data comes from."""
    if source.transport_type == "http":
        if transform is not None and transform.sample:
            return "An answer of the provider, asked on demand. Steps are built on a sample answer (%s kB kept)." % round(len(transform.sample.encode("utf-8")) / 1000, 1)
        return "An answer of the provider, asked on demand. No sample answer yet: paste or fetch one to build the steps on."
    newest = _newest_file(hsession, source.id)
    if newest is None:
        return "Each file this source sends. None has arrived yet (or retention removed the stored ones)."
    return "Each file this source sends. Newest: %s, %.1f kB, received %s UTC." % (newest[0], (newest[1] or 0) / 1000, newest[2].strftime("%Y-%m-%d %H:%M"))


@router.get("/sources/{source_id}/steps/input", response_class=HTMLResponse)
def input_step(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    """The Input step's page: for an API source its sample answer (paste, or fetch with the source's test request); for a source that
    stores data, the newest file the steps are built on."""
    source, transform = _with(session, source_id, request)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    newest = _newest_file(hsession, source.id)
    return _step_page(request, session, hsession, source, transform, "input", None, None, title="Input", is_http=source.transport_type == "http",
                      pasted_sample=bool(transform.sample), pasted_sample_kb=round(len(transform.sample.encode("utf-8")) / 1000, 1) if transform.sample else 0,
                      test_request=source.test_request, source_edit_url="/provider/sources/%s/edit" % source.id,
                      newest=types.SimpleNamespace(filename=newest[0], size=newest[1] or 0, fetched_at=newest[2]) if newest else None)


@router.get("/sources/{source_id}/steps/output", response_class=HTMLResponse)
def output_step(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    """The Output step's page: the format and its settings."""
    source, transform = _with(session, source_id, request)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    return _step_page(request, session, hsession, source, transform, "output", None, None, title="Output", is_http=source.transport_type == "http",
                      formats=te.OUTPUTS, delimiters=te.CSV_DELIMITERS)


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_page(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    source = _source_or_none(session, source_id)
    if source is None:
        return _go("/transform", "No such source.", "error")
    transform = transform_runner.get_pipeline(session, source_id, _pipeline_name(request))
    if transform is None and _pipeline_name(request) != transform_runner.DEFAULT_PIPELINE:
        return _go("/transform/sources/%s" % source_id, "No such pipeline.", "error")
    return templates.TemplateResponse(request, "transform/source.html", _editor_context(session, hsession, source, transform, request))


@router.post("/sources/{source_id}/create")
async def create_transform(source_id: str, request: Request, session: Session = Depends(get_session)):
    source = _source_or_none(session, source_id)
    if source is None:
        return _go("/transform", "No such source.", "error")
    if transform_runner.get_transform(session, source_id) is None:
        transform = Transform(source_id=source_id, steps="[]", enabled=False, updated_by=_who(request))
        sample = str((await request.form()).get("sample") or "")
        try:
            if source.transport_type == "http":
                transform.output_single = True  # an API answers one question: one object back, not a list of one
                transform_runner.set_sample(transform, sample)
        except te.TransformError as exc:
            return _go(_base(request, source_id), str(exc), "error")
        session.add(transform)
        session.commit()
    return _go(_base(request, source_id), "Transform created. Add the first step: read the %s." % ("answer" if source.transport_type == "http" else "file"))


@router.post("/sources/{source_id}/fetch-sample")
async def fetch_sample(source_id: str, request: Request, session: Session = Depends(get_session)):
    """Make ONE real call to the provider with the source's test request (set on the source, in the Extract Box) and keep its answer as the
    sample -- so the steps can be built without pasting anything. Admin-only (the whole page is), confirmed in the browser, and rate-limited
    like any call."""
    source, transform = _with(session, source_id, request)
    back = _base(request, source_id)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    if source.transport_type != "http":
        return _go(back, "Only an API source has answers to fetch.", "error")
    test_request = source.test_request
    if not test_request:
        return _go(back, "This source has no test request yet: give one on the source (Extract Box, Edit), then fetch.", "error")
    try:
        if source.proxy_method == "POST":
            params, body = {}, test_request.encode("utf-8")
        else:
            params, body = dict(parse_qsl(test_request.lstrip("?"), keep_blank_values=True)), None
        upstream = await run_in_threadpool(proxy.call_provider, session, source, params, body)
        if not 200 <= upstream.status_code < 300:
            raise te.TransformError("The provider answered HTTP %d, so there is no sample to keep." % upstream.status_code)
        transform_runner.set_sample(transform, upstream.content.decode("utf-8", errors="replace"))
    except te.TransformError as exc:
        session.rollback()
        return _go(back, str(exc), "error")
    except HTTPException as exc:
        session.rollback()
        return _go(back, "The provider could not be asked: %s" % exc.detail, "error")
    transform.updated_at, transform.updated_by = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), _who(request)
    session.commit()
    logger.info("transform box: %s fetched a sample answer for source %s from the provider", _who(request) or "?", source.name)
    steps = transform_runner.steps_of(transform)
    message = "Sample fetched (%s kB). %s" % (round(len(transform.sample.encode("utf-8")) / 1000, 1), "Now choose the fields to keep." if not steps else "It replaces the previous sample.")
    if steps:  # stay on the Input page, where the replaced sample is shown
        return RedirectResponse("%s/steps/input?%s" % (back, urlencode({"flash": message, "flash_kind": "success"})), status_code=303)
    # no steps yet: straight to choosing the fields (this address already has a query, so the message goes in it too)
    return RedirectResponse("%s/steps/new?%s" % (back, urlencode({"type": "read", "flash": message, "flash_kind": "success"})), status_code=303)


@router.post("/sources/{source_id}/sample")
async def set_sample(source_id: str, request: Request, session: Session = Depends(get_session)):
    source, transform = _with(session, source_id, request)
    back = _base(request, source_id)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    try:
        kept = transform_runner.set_sample(transform, str((await request.form()).get("sample") or ""))
    except te.TransformError as exc:
        session.rollback()
        return _go(back, str(exc), "error")
    transform.updated_at, transform.updated_by = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), _who(request)
    session.commit()
    logger.info("transform box: %s %s the sample answer of source %s", _who(request) or "?", "saved" if kept else "removed", source.name)
    return _go(back, "Sample answer saved." if kept else "Sample answer removed.")


def _with(session: Session, source_id: str, request: Request):
    return _source_or_none(session, source_id), transform_runner.get_pipeline(session, source_id, _pipeline_name(request))


def _save_steps(session: Session, transform: Transform, steps: list[dict], request: Request) -> None:
    transform.steps = json.dumps(steps)
    transform.updated_at, transform.updated_by = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), _who(request)
    if transform.enabled and transform_runner.check(session, transform):
        transform.enabled = False  # a transform that no longer holds together must not keep running
    session.commit()


@router.post("/sources/{source_id}/enabled")
async def set_enabled(source_id: str, request: Request, session: Session = Depends(get_session)):
    source, transform = _with(session, source_id, request)
    back = _base(request, source_id)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    want = str((await request.form()).get("enabled") or "") == "1"
    if want:
        problems = transform_runner.check(session, transform)
        if problems:
            return _go(back, "It is not ready: %s" % problems[0], "error")
    transform.enabled = want
    session.commit()
    return _go(back, "Transform switched %s. %s" % ("on" if want else "off", "Files that arrive from now on are transformed." if want else "Files arrive as they are."))


@router.post("/sources/{source_id}/output")
async def set_output(source_id: str, request: Request, session: Session = Depends(get_session)):
    source, transform = _with(session, source_id, request)
    back = _base(request, source_id)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    form = await request.form()
    fmt = str(form.get("format") or "")
    try:
        if fmt not in te.OUTPUTS:
            raise te.TransformError("Choose one of the offered output formats.")
        # Each format's settings are sent only when that format is chosen; the others keep what they had.
        if "root" in form or "record" in form:
            root, record = str(form.get("root") or "records").strip(), str(form.get("record") or "record").strip()
            te._check_name(root, "The XML root element")
            te._check_name(record, "The XML record element")
            transform.root_name, transform.record_name = root, record
        if "single_present" in form:
            transform.output_single = bool(form.get("single"))
        if "delimiter" in form:
            delimiter = str(form.get("delimiter") or ",")
            if delimiter not in te.CSV_DELIMITERS:
                raise te.TransformError("Choose one of the offered CSV separators.")
            transform.csv_delimiter, transform.csv_bom = delimiter, bool(form.get("bom"))
    except te.TransformError as exc:
        session.rollback()
        return _go(back, str(exc), "error")
    transform.output_format = fmt
    session.commit()
    return _go(back, "Output set to %s." % te.OUTPUTS[fmt][2])


# -- steps ------------------------------------------------------------------------------------------------------------------------------------------

def _reader_context(session: Session, hsession: Session, source_id: str, request: Request, existing: dict | None) -> dict:
    sample = transform_runner.sample_for(session, hsession, source_id)
    ctx: dict = {"sample": sample, "kind": None, "candidates": [], "record": "", "info": None, "selected": {}, "explode": "", "readers": te.READERS, "error": None,
                 "sheets": [], "sheet": ""}
    if sample is None:
        src = _source_or_none(session, source_id)
        ctx["error"] = ("There is no sample answer yet, so there is nothing to look at. Paste one answer of the provider on this source's Transform page, then come back."
                        if src is not None and src.transport_type == "http"
                        else "There is no stored file from this source yet, so there is nothing to look at. Once one arrives you can choose its fields here.")
        return ctx
    params = request.query_params
    kind = params.get("format") or (existing or {}).get("type") or te.detect_format(sample.content, sample.filename)
    if kind not in te.READERS:
        ctx["error"] = "This file's format was not recognised. Choose it below."
        kind = None
    ctx["kind"] = kind
    if kind is None:
        return ctx
    try:
        ctx["candidates"] = te.candidates(sample.content, kind)
        record = params.get("record") if "record" in params else ((existing or {}).get("record") if existing else None)
        if record is None:
            record = ctx["candidates"][0]["path"] if ctx["candidates"] else ""
        ctx["record"] = record
        if kind == "xlsx_records":
            ctx["sheets"] = te.xlsx_sheets(sample.content)
            ctx["sheet"] = params.get("sheet") or (existing or {}).get("sheet") or ""
        if kind in tf.TABULAR or record or kind == "json_records":
            ctx["info"] = te.describe_record(
                sample.content, kind, record, delimiter=params.get("delimiter") or (existing or {}).get("delimiter"), sheet=ctx.get("sheet") or None,
            )
    except te.TransformError as exc:
        ctx["error"] = str(exc)
        return ctx
    if existing and existing.get("type") == kind and existing.get("record", "") == ctx["record"]:
        explode = existing.get("explode") or {}
        ctx["explode"] = explode.get("path", "")
        for field in existing.get("fields") or []:
            ctx["selected"][field["path"]] = field["name"]
        for field in explode.get("fields") or []:
            rel = field["path"]
            ctx["selected"][explode["path"] + ("" if rel.startswith("@") else "/") + rel] = field["name"]
        ctx["keep_empty"] = bool(explode.get("keep_empty"))
    if "explode" in params:
        ctx["explode"] = params.get("explode") or ""
    ctx["delimiter"] = params.get("delimiter") or (existing or {}).get("delimiter") or te.sniff_delimiter(sample.content)
    return ctx


def _step_page(request: Request, session: Session, hsession: Session, source: ProviderSource, transform: Transform, kind: str, number: int | None, existing: dict | None, **extra):
    steps = transform_runner.steps_of(transform)
    before = steps[:(number - 1) if number else len(steps)]
    ctx = {"tbase": _base(request, source.id), "pipeline_name": _pipeline_name(request), "source": source, "transform": transform, "kind": kind, "number": number, "existing": existing or {}, "available": tf.fields_after(before),
           "tables": _table_columns(session), "types": te.CONVERT_TYPES, "ops": tf.OP_LABELS, "title": _SECTIONS.get(kind, kind), "rename_rows": range(tf.RENAME_ROWS),
           "convert_rows": range(tf.CONVERT_ROWS), "error": extra.pop("error", None), "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success")}
    if kind == "read":
        ctx["reader"] = _reader_context(session, hsession, source.id, request, existing)
    ctx.update(extra)
    return templates.TemplateResponse(request, "transform/step.html", ctx)


def _kind_from(request: Request, existing: dict | None) -> str | None:
    if existing:
        return "read" if existing.get("type") in te.READERS else existing.get("type")
    kind = request.query_params.get("type")
    return kind if kind in _SECTIONS else None


@router.get("/sources/{source_id}/steps/new", response_class=HTMLResponse)
def new_step(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    source, transform = _with(session, source_id, request)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    kind = _kind_from(request, None)
    if kind is None:
        return _go(_base(request, source_id), "Choose which step to add.", "error")
    steps = transform_runner.steps_of(transform)
    if kind == "read" and steps:
        return _go(_base(request, source_id), "The file is already being read (step 1). Edit that step instead.", "error")
    if kind != "read" and not steps:
        return _go(_base(request, source_id), "The first step must read the file.", "error")
    return _step_page(request, session, hsession, source, transform, kind, None, None)


@router.get("/sources/{source_id}/steps/{number}/edit", response_class=HTMLResponse)
def edit_step(source_id: str, number: int, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    source, transform = _with(session, source_id, request)
    steps = transform_runner.steps_of(transform) if transform else []
    if source is None or not 1 <= number <= len(steps):
        return _go(_base(request, source_id) if source else "/transform", "No such step.", "error")
    existing = steps[number - 1]
    return _step_page(request, session, hsession, source, transform, _kind_from(request, existing), number, existing)


async def _submit_step(source_id: str, number: int | None, request: Request, session: Session, hsession: Session):
    source, transform = _with(session, source_id, request)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    steps = transform_runner.steps_of(transform)
    if number is not None and not 1 <= number <= len(steps):
        return _go(_base(request, source_id), "No such step.", "error")
    form = await request.form()
    kind = str(form.get("kind") or "")
    back = _base(request, source_id)
    try:
        if kind == "read":
            step = tf.build_reader(str(form.get("format") or ""), form)
        elif kind in te.STEPS:
            step = tf.build_step(kind, form, tables=_table_columns(session))
        else:
            raise te.TransformError("Unknown step.")
        if number is None:
            steps.append(step)
        else:
            steps[number - 1] = step
        # Only a change that still holds together with the other steps is saved (an edit that removes a field a
        # later step uses is refused, with the reason).
        problems = transform_runner.check(session, Transform(source_id=source_id, steps=json.dumps(steps), output_format=transform.output_format))
        if problems:
            raise te.TransformError(problems[0])
    except te.TransformError as exc:
        existing = transform_runner.steps_of(transform)[number - 1] if number else None
        return _step_page(request, session, hsession, source, transform, kind, number, existing, error=str(exc))
    _save_steps(session, transform, steps, request)
    return _go(back, "Step %s." % ("saved" if number else "added"))


@router.post("/sources/{source_id}/steps/new")
async def add_step(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    return await _submit_step(source_id, None, request, session, hsession)


@router.post("/sources/{source_id}/steps/{number}/edit")
async def save_step(source_id: str, number: int, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    return await _submit_step(source_id, number, request, session, hsession)


@router.post("/sources/{source_id}/steps/{number}/remove")
def remove_step(source_id: str, number: int, request: Request, session: Session = Depends(get_session)):
    source, transform = _with(session, source_id, request)
    steps = transform_runner.steps_of(transform) if transform else []
    back = _base(request, source_id)
    if source is None or not 1 <= number <= len(steps):
        return _go(back if source else "/transform", "No such step.", "error")
    if number == 1 and len(steps) > 1:
        return _go(back, "Remove the other steps first: the first step reads the file.", "error")
    was_on = transform.enabled
    del steps[number - 1]
    _save_steps(session, transform, steps, request)
    return _go(back, "Step removed." + (" The transform was switched off because it no longer holds together." if was_on and not transform.enabled else ""))


@router.post("/sources/{source_id}/steps/{number}/move")
async def move_step(source_id: str, number: int, request: Request, session: Session = Depends(get_session)):
    source, transform = _with(session, source_id, request)
    steps = transform_runner.steps_of(transform) if transform else []
    back = _base(request, source_id)
    if source is None or not 1 <= number <= len(steps):
        return _go(back if source else "/transform", "No such step.", "error")
    direction = str((await request.form()).get("direction") or "")
    target = number - 1 if direction == "up" else number + 1 if direction == "down" else None
    if target is None or target < 1 or target > len(steps) or number == 1 or target == 1:
        return _go(back, "That step cannot move there (the first step always reads the file).", "error")
    steps[number - 1], steps[target - 1] = steps[target - 1], steps[number - 1]
    trial = Transform(source_id=source_id, steps=json.dumps(steps))
    if transform_runner.check(session, trial):
        return _go(back, "Moving it there would use a field before it exists. Nothing was changed.", "error")
    _save_steps(session, transform, steps, request)
    return _go(back)


# -- trying it, re-running it, downloading ------------------------------------------------------------------------------------------------------

@router.post("/sources/{source_id}/preview", response_class=HTMLResponse)
def preview(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    source, transform = _with(session, source_id, request)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    problems = transform_runner.check(session, transform)
    sample, result, error = (None, None, problems[0]) if problems else transform_runner.preview(session, hsession, transform)
    shown = None
    if sample is not None or error:
        shown = {"error": error, "filename": sample.filename if sample else None, "at": sample.fetched_at if sample else None}
        if result is not None:
            columns: list[str] = []
            for record in result.preview:
                for key in record:
                    if key not in columns:
                        columns.append(key)
            shown.update({"result": result, "columns": columns, "rows": result.preview[:20], "size": len(result.content)})
    return templates.TemplateResponse(request, "transform/source.html", _editor_context(session, hsession, source, transform, request, preview=shown or {"error": error}))


@router.post("/sources/{source_id}/reprocess")
async def reprocess(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    source, transform = _with(session, source_id, request)
    back = _base(request, source_id)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    try:
        limit = int(str((await request.form()).get("limit") or ""))
    except ValueError:
        limit = 0
    if limit not in transform_runner.REPROCESS_CHOICES:
        return _go(back, "Choose how many runs to process.", "error")
    problems = transform_runner.check(session, transform)
    if problems:
        return _go(back, "It is not ready: %s" % problems[0], "error")
    summary = transform_runner.reprocess(session, hsession, source_id, limit=limit, transform=transform)
    note = "Processed %d run%s: %d ok, %d failed." % (summary["done"], "" if summary["done"] == 1 else "s", summary["ok"], summary["failed"])
    if summary["skipped_removed"]:
        note += " %d skipped (their files were removed by retention)." % summary["skipped_removed"]
    if summary["stopped_early"]:
        note += " Stopped early to stay within the time limit -- run it again for the rest."
    return _go(back, note, "error" if summary["failed"] and not summary["ok"] else "success")


@router.post("/sources/{source_id}/outputs/{output_id}/delete")
def delete_output(source_id: str, output_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    """Delete one stored output. The original file and its run stay; the run can be transformed again."""
    back = _base(request, source_id)
    if _source_or_none(session, source_id) is None:
        return _go("/transform", "No such source.", "error")
    if not transform_runner.delete_output(hsession, source_id, output_id):
        return _go(back, "No such output.", "error")
    logger.info("transform output %s of source %s deleted by %s", output_id, source_id, _who(request) or "?")
    return _go(back, "Output deleted. The original file is untouched.")


@router.post("/sources/{source_id}/outputs/delete-all")
def delete_all_outputs(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    """Delete every stored output of this source (the originals stay)."""
    back = _base(request, source_id)
    if _source_or_none(session, source_id) is None:
        return _go("/transform", "No such source.", "error")
    pipeline = transform_runner.get_pipeline(session, source_id, _pipeline_name(request))
    count = transform_runner.delete_all_outputs(hsession, source_id, pipeline.id if pipeline else None)
    logger.info("all %d transform outputs of source %s deleted by %s", count, source_id, _who(request) or "?")
    return _go(back, "%d output%s deleted. The original files are untouched." % (count, "" if count == 1 else "s") if count else "There were no outputs to delete.")


@router.get("/sources/{source_id}/outputs/{output_id}/download")
def download_output(source_id: str, output_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    row = hsession.get(TransformedFile, output_id)
    if row is None or row.source_id != source_id:
        return _go(_base(request, source_id), "No such output.", "error")
    if row.size and not row.content:
        return Response("This output's contents were removed when it passed its retention period.", status_code=410)
    if row.status != "ok":
        return _go(_base(request, source_id), "That run failed, so there is no output: %s" % (row.error_message or ""), "error")
    return Response(content=row.content, media_type=row.content_type, headers={"Content-Disposition": 'attachment; filename="%s"' % row.filename.replace('"', "")})


# -- several pipelines on one source --------------------------------------------------------------------------------------------------------

def _mirror_routes_for_pipelines() -> None:
    """Every route of one source's transform page is also served under /sources/<id>/pipelines/<name>/..., where it acts on that pipeline
    (see _pipeline_name). The routes without the pipeline in the address keep acting on the ``default`` one."""
    from fastapi.routing import APIRoute

    head = "/transform/sources/{source_id}"
    for route in list(router.routes):
        if not isinstance(route, APIRoute) or not route.path.startswith(head) or route.path.endswith("/create"):
            continue
        router.add_api_route("%s/pipelines/{pipeline}%s" % (head[len(router.prefix):], route.path[len(head):]), route.endpoint, methods=list(route.methods),
                             response_class=route.response_class, name="%s_pipeline" % route.name)


_mirror_routes_for_pipelines()


@router.post("/sources/{source_id}/pipelines")
async def add_pipeline(source_id: str, request: Request, session: Session = Depends(get_session)):
    """A new pipeline on a source, started as a copy of the one the form was sent from (its steps, output settings, sample and request
    settings), switched off so nothing changes until it is tried and switched on."""
    source = _source_or_none(session, source_id)
    if source is None:
        return _go("/transform", "No such source.", "error")
    form = await request.form()
    name = str(form.get("name") or "").strip().lower()
    origin = transform_runner.get_pipeline(session, source_id, str(form.get("from") or transform_runner.DEFAULT_PIPELINE))
    base = "/transform/sources/%s" % source_id
    if origin is None:
        return _go(base, "Create the first transform of this source before adding another pipeline.", "error")
    if not _PIPELINE_NAME.match(name):
        return _go(base, "Name the pipeline with lowercase letters, digits and hyphens, starting with a letter (for example lookup).", "error")
    if transform_runner.get_pipeline(session, source_id, name) is not None:
        return _go(base, "There is already a pipeline called %r on this source." % name, "error")
    session.add(Transform(
        source_id=source_id, name=name, steps=origin.steps, output_format=origin.output_format, root_name=origin.root_name, record_name=origin.record_name,
        csv_delimiter=origin.csv_delimiter, csv_bom=origin.csv_bom, output_single=origin.output_single, request_swap=origin.request_swap,
        request_template=origin.request_template, sample=origin.sample, enabled=False, updated_by=_who(request)))
    session.commit()
    logger.info("transform box: %s added the pipeline %s to source %s", _who(request) or "?", name, source.name)
    return _go("%s/pipelines/%s" % (base, name), "Pipeline %s added, as a copy of %s. It is switched off until you switch it on." % (name, origin.name))


@router.post("/sources/{source_id}/pipelines/{pipeline}/delete")
def delete_pipeline(source_id: str, pipeline: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    """Remove a pipeline and the outputs it stored. The ``default`` one is the source's first transform and stays."""
    base = "/transform/sources/%s" % source_id
    found = transform_runner.get_pipeline(session, source_id, pipeline)
    if found is None:
        return _go(base, "No such pipeline.", "error")
    if pipeline == transform_runner.DEFAULT_PIPELINE:
        return _go(base, "The default pipeline cannot be removed.", "error")
    count = transform_runner.delete_all_outputs(hsession, source_id, found.id)
    session.delete(found)
    session.commit()
    logger.info("transform box: %s removed the pipeline %s of source %s (%d outputs)", _who(request) or "?", pipeline, source_id, count)
    return _go(base, "Pipeline %s removed." % pipeline)
