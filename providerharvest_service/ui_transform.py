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
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import (
    admin_session, build_info, harvest_db, nav, provider_sources, retention, transform_engine as te, transform_forms as tf, transform_runner,
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
            transform = transform_runner.get_transform(session, source_id)
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
        "source": source, "transform": transform, "steps": list(enumerate(steps, 1)), "problems": transform_runner.check(session, transform) if transform else [],
        "keep_label": retention.label(retention.loader_days(source)),
        "failed_only": request.query_params.get("show") == "failed",
        "outputs": transform_runner.recent_outputs(hsession, source.id, 50 if request.query_params.get("show") == "failed" else 10,
                                                   failed_only=request.query_params.get("show") == "failed") if transform else [], "counts": transform_runner.output_counts(hsession, source.id) if transform else None,
        "formats": te.OUTPUTS, "delimiters": te.CSV_DELIMITERS, "choices": transform_runner.REPROCESS_CHOICES, "has_sample": transform_runner.sample_file(hsession, source.id) is not None,
        "add_types": [(k, v) for k, v in _SECTIONS.items() if k != "read"], "preview": None,
        "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"), "size": _size, **extra,
    }


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_page(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    source = _source_or_none(session, source_id)
    if source is None:
        return _go("/transform", "No such source.", "error")
    return templates.TemplateResponse(request, "transform/source.html", _editor_context(session, hsession, source, transform_runner.get_transform(session, source_id), request))


@router.post("/sources/{source_id}/create")
def create_transform(source_id: str, request: Request, session: Session = Depends(get_session)):
    if _source_or_none(session, source_id) is None:
        return _go("/transform", "No such source.", "error")
    if transform_runner.get_transform(session, source_id) is None:
        session.add(Transform(source_id=source_id, steps="[]", enabled=False, updated_by=_who(request)))
        session.commit()
    return _go("/transform/sources/%s" % source_id, "Transform created. Add the first step: read the file.")


def _with(session: Session, source_id: str):
    source, transform = _source_or_none(session, source_id), transform_runner.get_transform(session, source_id)
    return source, transform


def _save_steps(session: Session, transform: Transform, steps: list[dict], request: Request) -> None:
    transform.steps = json.dumps(steps)
    transform.updated_at, transform.updated_by = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), _who(request)
    if transform.enabled and transform_runner.check(session, transform):
        transform.enabled = False  # a transform that no longer holds together must not keep running
    session.commit()


@router.post("/sources/{source_id}/enabled")
async def set_enabled(source_id: str, request: Request, session: Session = Depends(get_session)):
    source, transform = _with(session, source_id)
    back = "/transform/sources/%s" % source_id
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
    source, transform = _with(session, source_id)
    back = "/transform/sources/%s" % source_id
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

def _reader_context(hsession: Session, source_id: str, request: Request, existing: dict | None) -> dict:
    sample = transform_runner.sample_file(hsession, source_id)
    ctx: dict = {"sample": sample, "kind": None, "candidates": [], "record": "", "info": None, "selected": {}, "explode": "", "readers": te.READERS, "error": None,
                 "sheets": [], "sheet": ""}
    if sample is None:
        ctx["error"] = "There is no stored file from this source yet, so there is nothing to look at. Once one arrives you can choose its fields here."
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
    ctx = {"source": source, "transform": transform, "kind": kind, "number": number, "existing": existing or {}, "available": tf.fields_after(before),
           "tables": _table_columns(session), "types": te.CONVERT_TYPES, "ops": tf.OP_LABELS, "title": _SECTIONS.get(kind, kind), "rename_rows": range(tf.RENAME_ROWS),
           "convert_rows": range(tf.CONVERT_ROWS), "error": extra.pop("error", None), "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success")}
    if kind == "read":
        ctx["reader"] = _reader_context(hsession, source.id, request, existing)
    ctx.update(extra)
    return templates.TemplateResponse(request, "transform/step.html", ctx)


def _kind_from(request: Request, existing: dict | None) -> str | None:
    if existing:
        return "read" if existing.get("type") in te.READERS else existing.get("type")
    kind = request.query_params.get("type")
    return kind if kind in _SECTIONS else None


@router.get("/sources/{source_id}/steps/new", response_class=HTMLResponse)
def new_step(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    source, transform = _with(session, source_id)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    kind = _kind_from(request, None)
    if kind is None:
        return _go("/transform/sources/%s" % source_id, "Choose which step to add.", "error")
    steps = transform_runner.steps_of(transform)
    if kind == "read" and steps:
        return _go("/transform/sources/%s" % source_id, "The file is already being read (step 1). Edit that step instead.", "error")
    if kind != "read" and not steps:
        return _go("/transform/sources/%s" % source_id, "The first step must read the file.", "error")
    return _step_page(request, session, hsession, source, transform, kind, None, None)


@router.get("/sources/{source_id}/steps/{number}/edit", response_class=HTMLResponse)
def edit_step(source_id: str, number: int, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    source, transform = _with(session, source_id)
    steps = transform_runner.steps_of(transform) if transform else []
    if source is None or not 1 <= number <= len(steps):
        return _go("/transform/sources/%s" % source_id if source else "/transform", "No such step.", "error")
    existing = steps[number - 1]
    return _step_page(request, session, hsession, source, transform, _kind_from(request, existing), number, existing)


async def _submit_step(source_id: str, number: int | None, request: Request, session: Session, hsession: Session):
    source, transform = _with(session, source_id)
    if source is None or transform is None:
        return _go("/transform", "No such transform.", "error")
    steps = transform_runner.steps_of(transform)
    if number is not None and not 1 <= number <= len(steps):
        return _go("/transform/sources/%s" % source_id, "No such step.", "error")
    form = await request.form()
    kind = str(form.get("kind") or "")
    back = "/transform/sources/%s" % source_id
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
    source, transform = _with(session, source_id)
    steps = transform_runner.steps_of(transform) if transform else []
    back = "/transform/sources/%s" % source_id
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
    source, transform = _with(session, source_id)
    steps = transform_runner.steps_of(transform) if transform else []
    back = "/transform/sources/%s" % source_id
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
    source, transform = _with(session, source_id)
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
    source, transform = _with(session, source_id)
    back = "/transform/sources/%s" % source_id
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
    summary = transform_runner.reprocess(session, hsession, source_id, limit=limit)
    note = "Processed %d run%s: %d ok, %d failed." % (summary["done"], "" if summary["done"] == 1 else "s", summary["ok"], summary["failed"])
    if summary["skipped_removed"]:
        note += " %d skipped (their files were removed by retention)." % summary["skipped_removed"]
    if summary["stopped_early"]:
        note += " Stopped early to stay within the time limit -- run it again for the rest."
    return _go(back, note, "error" if summary["failed"] and not summary["ok"] else "success")


@router.post("/sources/{source_id}/outputs/{output_id}/delete")
def delete_output(source_id: str, output_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    """Delete one stored output. The original file and its run stay; the run can be transformed again."""
    back = "/transform/sources/%s" % source_id
    if _source_or_none(session, source_id) is None:
        return _go("/transform", "No such source.", "error")
    if not transform_runner.delete_output(hsession, source_id, output_id):
        return _go(back, "No such output.", "error")
    logger.info("transform output %s of source %s deleted by %s", output_id, source_id, _who(request) or "?")
    return _go(back, "Output deleted. The original file is untouched.")


@router.post("/sources/{source_id}/outputs/delete-all")
def delete_all_outputs(source_id: str, request: Request, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    """Delete every stored output of this source (the originals stay)."""
    back = "/transform/sources/%s" % source_id
    if _source_or_none(session, source_id) is None:
        return _go("/transform", "No such source.", "error")
    count = transform_runner.delete_all_outputs(hsession, source_id)
    logger.info("all %d transform outputs of source %s deleted by %s", count, source_id, _who(request) or "?")
    return _go(back, "%d output%s deleted. The original files are untouched." % (count, "" if count == 1 else "s") if count else "There were no outputs to delete.")


@router.get("/sources/{source_id}/outputs/{output_id}/download")
def download_output(source_id: str, output_id: str, session: Session = Depends(get_session), hsession: Session = Depends(harvest_db.get_session)):
    row = hsession.get(TransformedFile, output_id)
    if row is None or row.source_id != source_id:
        return _go("/transform/sources/%s" % source_id, "No such output.", "error")
    if row.size and not row.content:
        return Response("This output's contents were removed when it passed its retention period.", status_code=410)
    if row.status != "ok":
        return _go("/transform/sources/%s" % source_id, "That run failed, so there is no output: %s" % (row.error_message or ""), "error")
    return Response(content=row.content, media_type=row.content_type, headers={"Content-Disposition": 'attachment; filename="%s"' % row.filename.replace('"', "")})
