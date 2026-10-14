"""A source's index tables: upload, replace, download and delete, on the source (Extract Box). A table belongs to one source and is used
by the "lookup" step of that source's pipelines only (a name is unique within its source). Behind the site-wide login
(admin_session.py). The Transform Box lists only the tables no source used when they moved here, to bind or delete."""

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

from providerharvest_service import admin_session, build_info, nav, transform_engine as te, transform_runner
from providerharvest_service.db import get_session
from providerharvest_service.models import LookupTable, ProviderSource

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/provider", tags=["source-tables-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now
templates.env.globals["pending_approvals"] = nav.pending_approvals
templates.env.filters["utc"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else ""
templates.env.filters["short_utc"] = lambda value: value.strftime("%m-%d %H:%M") if value else ""

_TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def _who(request: Request) -> str:
    return admin_session.verify_session_cookie(request.cookies.get(admin_session.COOKIE_NAME, "")) or ""


def _go(path: str, flash: str | None = None, kind: str = "success") -> RedirectResponse:
    query = "?" + urlencode({"flash": flash, "flash_kind": kind}) if flash else ""
    return RedirectResponse(path + query, status_code=303)


async def _read_upload(form, key: str = "file") -> tuple[bytes, str]:
    upload = form.get(key)
    if upload is None or not hasattr(upload, "read"):
        raise te.TransformError("Choose a file to upload.")
    data = await upload.read(te.MAX_LOOKUP_BYTES + 1)
    return data, getattr(upload, "filename", "") or ""


def _source_or_none(session: Session, source_id: str) -> ProviderSource | None:
    return session.get(ProviderSource, source_id)


def _table_of(session: Session, source_id: str, table_id: str) -> LookupTable | None:
    """The table, only when it is this source's: another source's table is never reached through this one."""
    table = session.get(LookupTable, table_id)
    return table if table is not None and table.source_id == source_id else None


@router.get("/sources/{source_id}/tables", response_class=HTMLResponse)
def tables_page(source_id: str, request: Request, session: Session = Depends(get_session)):
    source = _source_or_none(session, source_id)
    if source is None:
        return _go("/provider/sources", "No such source.", "error")
    tables = [{"table": t, "columns": json.loads(t.columns), "used_by": len(transform_runner.pipelines_using(session, source_id, t.name))}
              for t in session.scalars(select(LookupTable).where(LookupTable.source_id == source_id).order_by(LookupTable.name)).all()]
    return templates.TemplateResponse(request, "provider/source_tables.html", {
        "source": source, "tables": tables, "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
    })


@router.post("/sources/{source_id}/tables")
async def upload_table(source_id: str, request: Request, session: Session = Depends(get_session)):
    source = _source_or_none(session, source_id)
    if source is None:
        return _go("/provider/sources", "No such source.", "error")
    back = "/provider/sources/%s/tables" % source_id
    form = await request.form()
    name = str(form.get("name") or "").strip().lower()
    try:
        if not _TABLE_NAME.match(name):
            raise te.TransformError("Name the table with lowercase letters, digits and underscores, starting with a letter (for example stations).")
        if session.scalar(select(LookupTable).where(LookupTable.source_id == source_id, LookupTable.name == name)) is not None:
            raise te.TransformError("There is already a table called %r -- open it to replace its contents." % name)
        content, filename = await _read_upload(form)
        columns, rows, note = te.parse_lookup_ex(content, filename, str(form.get("sheet") or "").strip() or None)
    except te.TransformError as exc:
        return _go(back, str(exc), "error")
    session.add(LookupTable(source_id=source_id, name=name, description=str(form.get("description") or "").strip()[:200], columns=json.dumps(columns),
                            rows=json.dumps(rows), row_count=len(rows), uploaded_by=_who(request)))
    session.commit()
    return _go(back, ("Index table %s uploaded: %d rows, %d columns. %s" % (name, len(rows), len(columns), note)).strip())


@router.get("/sources/{source_id}/tables/{table_id}", response_class=HTMLResponse)
def table_page(source_id: str, table_id: str, request: Request, session: Session = Depends(get_session)):
    source, table = _source_or_none(session, source_id), _table_of(session, source_id, table_id)
    if source is None or table is None:
        return _go("/provider/sources/%s/tables" % source_id if source else "/provider/sources", "No such index table.", "error")
    return templates.TemplateResponse(request, "provider/source_table.html", {
        "source": source, "table": table, "columns": json.loads(table.columns), "rows": json.loads(table.rows)[:25],
        "used_by": transform_runner.pipelines_using(session, source_id, table.name),
        "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
    })


@router.post("/sources/{source_id}/tables/{table_id}/replace")
async def replace_table(source_id: str, table_id: str, request: Request, session: Session = Depends(get_session)):
    table = _table_of(session, source_id, table_id)
    if table is None:
        return _go("/provider/sources/%s/tables" % source_id, "No such index table.", "error")
    back = "/provider/sources/%s/tables/%s" % (source_id, table_id)
    form = await request.form()
    try:
        content, filename = await _read_upload(form)
        columns, rows, note = te.parse_lookup_ex(content, filename, str(form.get("sheet") or "").strip() or None)
        # Pipelines that use this table keep working only if the columns they need are still there.
        for pipeline in transform_runner.pipelines_using(session, source_id, table.name):
            for step in transform_runner.steps_of(pipeline):
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


@router.post("/sources/{source_id}/tables/{table_id}/delete")
def delete_table(source_id: str, table_id: str, session: Session = Depends(get_session)):
    table = _table_of(session, source_id, table_id)
    if table is None:
        return _go("/provider/sources/%s/tables" % source_id, "No such index table.", "error")
    if transform_runner.pipelines_using(session, source_id, table.name):
        return _go("/provider/sources/%s/tables/%s" % (source_id, table_id), "A transform uses this table. Remove that step first.", "error")
    name = table.name
    session.delete(table)
    session.commit()
    return _go("/provider/sources/%s/tables" % source_id, "Index table %s deleted." % name)


@router.get("/sources/{source_id}/tables/{table_id}/download")
def download_table(source_id: str, table_id: str, session: Session = Depends(get_session)):
    table = _table_of(session, source_id, table_id)
    if table is None:
        return _go("/provider/sources/%s/tables" % source_id, "No such index table.", "error")
    data = te.write_output(json.loads(table.rows), "csv")
    return Response(content=data, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="%s.csv"' % table.name})


# -- tables no source used when they moved to sources ------------------------------------------------------------------------------------------

@router.post("/tables/{table_id}/bind")
async def bind_table(table_id: str, request: Request, session: Session = Depends(get_session)):
    table = session.get(LookupTable, table_id)
    if table is None or table.source_id is not None:
        return _go("/transform", "No such unassigned index table.", "error")
    source_id = str((await request.form()).get("source_id") or "")
    source = _source_or_none(session, source_id)
    if source is None:
        return _go("/transform", "Choose the source the table is for.", "error")
    if session.scalar(select(LookupTable).where(LookupTable.source_id == source_id, LookupTable.name == table.name)) is not None:
        return _go("/transform", "%s already has a table called %r." % (source.title, table.name), "error")
    table.source_id = source_id
    session.commit()
    return _go("/provider/sources/%s/tables" % source_id, "Index table %s is now %s's." % (table.name, source.title))


@router.post("/tables/{table_id}/delete")
def delete_unassigned_table(table_id: str, session: Session = Depends(get_session)):
    table = session.get(LookupTable, table_id)
    if table is None or table.source_id is not None:
        return _go("/transform", "No such unassigned index table.", "error")
    name = table.name
    session.delete(table)
    session.commit()
    return _go("/transform", "Index table %s deleted." % name)
