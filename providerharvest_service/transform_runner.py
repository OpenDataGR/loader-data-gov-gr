"""Runs a source's transform on what it received, and stores the output beside the original.

* ``on_new_file`` is called when a pushed file has been recorded (``push_ingest`` and ``ftp_relay_ingest``).
  It never raises and never touches the original: a transform that fails leaves an error row (so the Transform
  Box can show what went wrong) and the file arrives exactly as it would have without one.
* ``reprocess`` re-runs the transform over a source's earlier runs -- after the steps changed, or for files
  that arrived before the transform was switched on. It stops after a time budget and says how far it got.
* ``preview`` runs the steps on a chosen (by default the newest) stored file without storing anything.

The transform and its index tables live in the config database (``Transform``, ``LookupTable``); the outputs
are ``TransformedFile`` rows in the harvest database, one per run.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import time

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from providerharvest_service import transform_engine as engine
from providerharvest_service.harvest_models import HarvestedFile, HarvestRun, TransformedFile
from providerharvest_service.models import LookupTable, Transform

logger = logging.getLogger(__name__)

REPROCESS_CHOICES = (10, 50, 200)
REPROCESS_BUDGET_S = 25.0
_NAME = engine._NAME


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


DEFAULT_PIPELINE = "default"


def list_pipelines(session: Session, source_id: str) -> list[Transform]:
    """A source's pipelines, the ``default`` one first, then by name."""
    rows = session.scalars(select(Transform).where(Transform.source_id == source_id)).all()
    return sorted(rows, key=lambda t: (t.name != DEFAULT_PIPELINE, t.name or ""))


def get_pipeline(session: Session, source_id: str, name: str) -> Transform | None:
    return session.scalar(select(Transform).where(Transform.source_id == source_id, Transform.name == name))


def get_transform(session: Session, source_id: str) -> Transform | None:
    """The source's ``default`` pipeline -- what every caller that predates several pipelines means by "the transform"."""
    return get_pipeline(session, source_id, DEFAULT_PIPELINE)


def steps_of(transform: Transform) -> list[dict]:
    try:
        steps = json.loads(transform.steps or "[]")
    except ValueError:
        return []
    return steps if isinstance(steps, list) else []


def lookup_names(steps: list[dict]) -> set[str]:
    return {s["table"] for s in steps if isinstance(s, dict) and s.get("type") == "lookup" and isinstance(s.get("table"), str)}


_CACHE: dict[tuple[str, str], dict] = {}  # (table id, uploaded_at) -> parsed table; a new upload is a new key


def load_lookups(session: Session, names: set[str]) -> dict[str, dict]:
    """The index tables a transform needs, parsed once per upload and kept (a tens-of-thousands-row table
    would otherwise be re-read from JSON for every file)."""
    out: dict[str, dict] = {}
    for table in session.scalars(select(LookupTable).where(LookupTable.name.in_(names))).all() if names else []:
        key = (table.id, str(table.uploaded_at))
        if key not in _CACHE:
            if len(_CACHE) > 20:
                _CACHE.clear()
            _CACHE[key] = engine.lookup_table(json.loads(table.columns), json.loads(table.rows))
        out[table.name] = _CACHE[key]
    return out


def lookups_in_use(session: Session, name: str) -> list[str]:
    """Source ids whose transform uses the index table ``name``."""
    return [t.source_id for t in session.scalars(select(Transform)).all() if name in lookup_names(steps_of(t))]


def apply(session: Session, transform: Transform, content: bytes, filename: str = "") -> engine.Result:
    """Run ``transform`` on ``content``. Raises ``engine.TransformError``."""
    steps = steps_of(transform)
    return engine.run(
        content, steps, output=transform.output_format, lookups=load_lookups(session, lookup_names(steps)),
        root=transform.root_name, record=transform.record_name,
        delimiter=transform.csv_delimiter or ",", bom=True if transform.csv_bom is None else bool(transform.csv_bom),
        single=bool(transform.output_single),
    )


def _store(hsession: Session, transform: Transform, source_id: str, run_id: str, filename: str, result: engine.Result | None, error: str | None) -> TransformedFile:
    hsession.execute(delete(TransformedFile).where(TransformedFile.run_id == run_id))
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    row = TransformedFile(
        run_id=run_id, source_id=source_id, transform_id=transform.id, status="error" if error else "ok", error_message=error,
        filename=("%s.%s" % (stem or "output", result.extension)) if result else "",
        content_type=result.content_type if result else "application/octet-stream",
        size=len(result.content) if result else 0, content=result.content if result else b"",
        records_in=result.records_in if result else 0, records_out=result.records_out if result else 0,
        stats=json.dumps(result.stats if result else {}),
    )
    hsession.add(row)
    hsession.commit()
    return row


def process_run(config_session: Session, hsession: Session, source_id: str, run_id: str, filename: str, content: bytes,
                *, only_if_enabled: bool = True) -> TransformedFile | None:
    """Transform one run's file and store the outcome. None when the source has no (enabled) transform."""
    transform = get_transform(config_session, source_id)
    if transform is None or (only_if_enabled and not transform.enabled):
        return None
    try:
        result = apply(config_session, transform, content, filename)
        return _store(hsession, transform, source_id, run_id, filename, result, None)
    except engine.TransformError as exc:
        return _store(hsession, transform, source_id, run_id, filename, None, str(exc))


def on_new_file(config_session: Session, hsession: Session, source, run: HarvestRun, filename: str, content: bytes) -> None:
    """Called after a pushed file was recorded. Never raises: the file has arrived whatever happens here."""
    try:
        if run.status == "success":
            process_run(config_session, hsession, source.id, run.id, filename, content)
    except Exception:  # noqa: BLE001 -- ingest must not fail because of a transform
        logger.exception("transform for source %s failed unexpectedly", getattr(source, "name", "?"))
        try:
            hsession.rollback()
        except Exception:  # noqa: BLE001
            pass


# -- looking at stored files ------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Sample:
    run_id: str
    filename: str
    content: bytes
    fetched_at: datetime.datetime


def sample_file(hsession: Session, source_id: str, run_id: str | None = None) -> Sample | None:
    """A stored file of this source to look at: the given run's, or the newest that still has contents."""
    query = (
        select(HarvestedFile.run_id, HarvestedFile.filename, HarvestedFile.content, HarvestedFile.fetched_at)
        .join(HarvestRun, HarvestRun.id == HarvestedFile.run_id)
        .where(HarvestRun.source_id == source_id, func.length(HarvestedFile.content) > 0)
    )
    if run_id:
        query = query.where(HarvestedFile.run_id == run_id)
    row = hsession.execute(query.order_by(HarvestedFile.fetched_at.desc()).limit(1)).first()
    return Sample(*row) if row else None


MAX_SAMPLE_BYTES = 200_000
PASTED_SAMPLE_NAME = "pasted sample.json"


def sample_for(config_session: Session, hsession: Session, source_id: str, run_id: str | None = None) -> Sample | None:
    """What the steps are built and tried on: a stored file of the source (the newest, or the given run's) -- and, for a source
    that stores nothing (a live proxy), the sample answer an admin pasted."""
    stored = sample_file(hsession, source_id, run_id)
    if stored is not None or run_id:
        return stored
    transform = get_transform(config_session, source_id)
    if transform is not None and transform.sample:
        return Sample("", PASTED_SAMPLE_NAME, transform.sample.encode("utf-8"), transform.updated_at or _now())
    return None


def set_sample(transform: Transform, text: str | None) -> bool:
    """Keep (or, when empty, remove) a pasted sample answer. Raises ``engine.TransformError`` for something that is not JSON or is
    too big. True when a sample is now kept."""
    text = (text or "").strip()
    if not text:
        transform.sample = None
        return False
    if len(text.encode("utf-8")) > MAX_SAMPLE_BYTES:
        raise engine.TransformError("The sample is larger than %d kB." % (MAX_SAMPLE_BYTES // 1000))
    try:
        json.loads(text)
    except ValueError as exc:
        raise engine.TransformError("The sample is not valid JSON (%s)." % exc) from None
    transform.sample = text
    return True


def preview(config_session: Session, hsession: Session, transform: Transform, run_id: str | None = None) -> tuple[Sample | None, engine.Result | None, str | None]:
    """(the file used, the result, an error message) -- nothing is stored."""
    sample = sample_for(config_session, hsession, transform.source_id, run_id)
    if sample is None:
        return None, None, "There is nothing to try this on yet: no stored file, and no pasted sample."
    try:
        return sample, apply(config_session, transform, sample.content, sample.filename), None
    except engine.TransformError as exc:
        return sample, None, str(exc)


def reprocess(config_session: Session, hsession: Session, source_id: str, *, limit: int,
              budget_s: float = REPROCESS_BUDGET_S) -> dict:
    """Run the transform again over this source's newest ``limit`` runs that still have their file.
    Returns {"done", "ok", "failed", "skipped_removed", "stopped_early"}."""
    transform = get_transform(config_session, source_id)
    if transform is None:
        return {"done": 0, "ok": 0, "failed": 0, "skipped_removed": 0, "stopped_early": False}
    started = time.monotonic()
    rows = hsession.execute(
        select(HarvestedFile.run_id, HarvestedFile.filename, HarvestedFile.size, func.length(HarvestedFile.content))
        .join(HarvestRun, HarvestRun.id == HarvestedFile.run_id)
        .where(HarvestRun.source_id == source_id, HarvestRun.status == "success")
        .order_by(HarvestedFile.fetched_at.desc()).limit(limit)
    ).all()
    summary = {"done": 0, "ok": 0, "failed": 0, "skipped_removed": 0, "stopped_early": False}
    for run_id, filename, size, held in rows:
        if size and not held:
            summary["skipped_removed"] += 1  # its contents were removed by retention
            continue
        if time.monotonic() - started > budget_s:
            summary["stopped_early"] = True
            break
        content = hsession.scalar(select(HarvestedFile.content).where(HarvestedFile.run_id == run_id).limit(1)) or b""
        stored = process_run(config_session, hsession, source_id, run_id, filename, content, only_if_enabled=False)
        summary["done"] += 1
        summary["ok" if stored is not None and stored.status == "ok" else "failed"] += 1
    return summary


def recent_outputs(hsession: Session, source_id: str, limit: int = 10, *, failed_only: bool = False):
    """The newest outputs as rows (id, created_at, status, error_message, records_in/out, size, has_content) --
    never the contents themselves, which can be large and are only needed for a download."""
    query = select(
        TransformedFile.id, TransformedFile.created_at, TransformedFile.status, TransformedFile.error_message,
        TransformedFile.filename, TransformedFile.content_type,
        TransformedFile.records_in, TransformedFile.records_out, TransformedFile.size,
        (func.length(TransformedFile.content) > 0).label("has_content"),
    ).where(TransformedFile.source_id == source_id)
    if failed_only:
        query = query.where(TransformedFile.status == "error")
    return hsession.execute(query.order_by(TransformedFile.created_at.desc()).limit(limit)).all()


def output_counts(hsession: Session, source_id: str) -> dict:
    ok, failed, latest = hsession.execute(
        select(
            func.count().filter(TransformedFile.status == "ok"), func.count().filter(TransformedFile.status == "error"),
            func.max(TransformedFile.created_at),
        ).where(TransformedFile.source_id == source_id)
    ).one()
    return {"ok": int(ok or 0), "failed": int(failed or 0), "latest": latest}


def delete_output(hsession: Session, source_id: str, output_id: str) -> bool:
    """Delete one stored output of this source (the original file and the run are not touched). False when
    there is no such output for this source -- another source's output is never deleted through this one."""
    result = hsession.execute(delete(TransformedFile).where(TransformedFile.id == output_id, TransformedFile.source_id == source_id))
    hsession.commit()
    return bool(result.rowcount)


def delete_all_outputs(hsession: Session, source_id: str) -> int:
    """Delete every stored output of this source. Returns how many."""
    result = hsession.execute(delete(TransformedFile).where(TransformedFile.source_id == source_id))
    hsession.commit()
    return int(result.rowcount or 0)


# -- is this transform ready to run? ----------------------------------------------------------------------------------------------

def check(config_session: Session, transform: Transform) -> list[str]:
    """Everything wrong with a transform that can be seen without data: a missing reader, an index table that
    does not exist or lacks a column, a step that names a field no earlier step produces. Empty: ready."""
    from providerharvest_service import transform_forms

    steps = steps_of(transform)
    problems = engine.validate(steps)
    if problems:
        return problems
    tables = {t.name: json.loads(t.columns) for t in config_session.scalars(select(LookupTable)).all()}
    for number, step in enumerate(steps[1:], 2):
        available = transform_forms.fields_after(steps[:number - 1])
        kind = step.get("type")

        def need(field, what="field"):
            if field not in available:
                problems.append("Step %d: no earlier step produces the %s %r." % (number, what, field))

        if kind == "lookup":
            columns = tables.get(step.get("table"))
            if columns is None:
                problems.append("Step %d: the index table %r does not exist." % (number, step.get("table")))
                continue
            need(step.get("key"))
            for column in [step.get("table_key")] + [a.get("column") for a in step.get("add") or []]:
                if column not in columns:
                    problems.append("Step %d: the index table %r has no column %r." % (number, step.get("table"), column))
        elif kind == "add_field" and step.get("kind") == "template":
            for placeholder in set(engine._PLACEHOLDER.findall(str(step.get("value") or ""))):
                need(placeholder)
        elif kind == "filter":
            need(step.get("field"))
        elif kind == "utm":
            need(step.get("x"), "easting field")
            need(step.get("y"), "northing field")
            for new in (step.get("lat_name"), step.get("lon_name")):
                if new in available:
                    problems.append("Step %d: a field called %r already exists -- choose another name." % (number, new))
        elif kind == "rename":
            for old in step.get("map") or {}:
                need(old)
            after = [(step.get("map") or {}).get(n, n) for n in available]
            if len(set(after)) != len(after):
                problems.append("Step %d: renaming makes two fields share a name." % number)
        elif kind == "convert":
            for field in step.get("fields") or {}:
                need(field)
        elif kind in ("select", "drop"):
            for field in step.get("fields") or []:
                need(field)
    return problems
