"""The Load Box's DataStore destination: the rows of a pipeline's output are written into a table of CKAN's DataStore (a resource of an existing
dataset), by the loader itself through ``datastore_create`` / ``datastore_upsert`` -- not by uploading a file for CKAN's xloader to parse.

How a new output meets the rows already there is chosen per pipeline (``CkanTarget.datastore_config``):
* ``replace`` -- the table is emptied and loaded again: it always mirrors the newest output.
* ``append``  -- the newest output's rows are added to the ones already there (history grows; an output is never added twice).
* ``upsert``  -- rows are added, or updated where their key columns match one already there (history grows without duplicates).

This module only reads the output and works out the table; ``ckan_load.push`` makes the calls.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import re

REFRESH_MODES = ("replace", "append", "upsert")
BATCH = 1000
MAX_COLUMNS = 200
_NUMBER = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[-+]\d{2}:?\d{2})?$")


class DatastoreError(Exception):
    """Something about the output or the settings the person should be told, in words."""


def default_config() -> dict:
    return {"refresh": "replace", "keys": []}


def config_of(raw: str | None) -> dict:
    try:
        cfg = json.loads(raw or "{}")
    except ValueError:
        cfg = {}
    out = default_config()
    if isinstance(cfg, dict):
        if cfg.get("refresh") in REFRESH_MODES:
            out["refresh"] = cfg["refresh"]
        out["keys"] = [str(k) for k in cfg.get("keys") or []]
        if cfg.get("created"):
            out["created"] = str(cfg["created"])
        if isinstance(cfg.get("fields"), list):
            out["fields"] = [f for f in cfg["fields"] if isinstance(f, dict) and f.get("id") and f.get("type")]
    return out


def check_config(refresh: str, keys: list[str]) -> dict:
    if refresh not in REFRESH_MODES:
        raise DatastoreError("Choose what happens to the rows already there: replace, add, or update by key.")
    if refresh == "upsert" and not keys:
        raise DatastoreError("Choose the column(s) that identify a row, so a row can be updated instead of added twice.")
    return {"refresh": refresh, "keys": keys if refresh == "upsert" else []}


# -- reading the output --------------------------------------------------------------------------------------------------------------------------------

def _kind(filename: str, content_type: str) -> str:
    name, ctype = (filename or "").lower(), (content_type or "").lower()
    for ending, kind in ((".csv", "csv"), (".jsonl", "jsonl"), (".json", "json")):  # (the file's own name first: a type header is often just a default)
        if name.endswith(ending):
            return kind
    if "csv" in ctype:
        return "csv"
    if "jsonl" in ctype or "ndjson" in ctype:
        return "jsonl"
    if "json" in ctype and not name.endswith((".xml", ".txt")):
        return "json"
    raise DatastoreError("The DataStore takes JSON, JSON Lines or CSV rows; this output is %s. Change the pipeline's output format." % (filename or ctype or "unknown"))


def _flat(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _row(item) -> dict:
    if not isinstance(item, dict):
        return {"value": _flat(item)}
    return {str(k): _flat(v) for k, v in item.items()}


def records_of(content: bytes, filename: str = "", content_type: str = "") -> list[dict]:
    """The output's rows, each a flat dictionary (a nested value becomes JSON text)."""
    kind = _kind(filename, content_type)
    text = content.decode("utf-8-sig", errors="replace")
    try:
        if kind == "csv":
            rows = [dict(r) for r in csv.DictReader(io.StringIO(text))]
        elif kind == "jsonl":
            rows = [_row(json.loads(line)) for line in text.splitlines() if line.strip()]
        else:
            data = json.loads(text)
            if isinstance(data, dict):
                lists = [v for v in data.values() if isinstance(v, list)]
                data = lists[0] if len(lists) == 1 else [data]
            rows = [_row(item) for item in data] if isinstance(data, list) else [_row(data)]
    except ValueError as exc:
        raise DatastoreError("The output could not be read as %s: %s" % (kind.upper(), exc)) from exc
    rows = [{k: v for k, v in r.items() if k is not None} for r in rows]
    if not rows:
        raise DatastoreError("The newest output has no rows.")
    return rows


# -- the table -----------------------------------------------------------------------------------------------------------------------------------------

def _clean_name(name: str, taken: set[str]) -> str:
    base = re.sub(r"\s+", " ", str(name)).strip().lstrip("_").strip() or "column"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = "%s_%d" % (base, n), n + 1
    taken.add(candidate)
    return candidate


def _type_of(values: list) -> str:
    present = [v for v in values if v not in (None, "")]
    if not present:
        return "text"
    if all(isinstance(v, bool) for v in present):
        return "bool"
    if all((isinstance(v, (int, float)) and not isinstance(v, bool)) or (isinstance(v, str) and _NUMBER.match(v.strip())) for v in present):
        return "numeric"
    if all(isinstance(v, str) and _TIMESTAMP.match(v.strip()) for v in present):
        return "timestamp"
    return "text"


def _convert(value, kind: str):
    if value is None or value == "":
        return None
    if kind == "text":
        return value if isinstance(value, str) else str(value)
    if kind == "numeric":
        return value if isinstance(value, (int, float)) else float(value) if "." in value or "e" in value.lower() else int(value)
    if kind == "bool":
        return bool(value)
    return str(value).strip()


def table_of(rows: list[dict], known: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """``(fields, records)``: the columns (``{"id", "type"}``, in the order they first appear; the types worked out from the values, except those of
    columns the table already has (``known``: name -> type), which keep theirs) and the rows keyed by those names, values converted to the type."""
    order: list[str] = []
    for r in rows:
        for k in r:
            if k not in order:
                order.append(k)
    if len(order) > MAX_COLUMNS:
        raise DatastoreError("The output has %d columns, more than the %d the DataStore is asked to take." % (len(order), MAX_COLUMNS))
    taken: set[str] = set()
    names = {k: _clean_name(k, taken) for k in order}
    known = known or {}
    types = {k: known.get(names[k]) or _type_of([r.get(k) for r in rows]) for k in order}
    fields = [{"id": names[k], "type": types[k]} for k in order]
    records = []
    for r in rows:
        record = {}
        for k in order:
            try:
                record[names[k]] = _convert(r.get(k), types[k])
            except ValueError as exc:
                raise DatastoreError("Column %r is %s in the table, but the output has %r in it." % (names[k], types[k], r.get(k))) from exc
        records.append(record)
    return fields, records


def columns_of(output) -> list[dict] | None:
    """The columns the newest output would give (for the page), or None when it cannot be read as rows."""
    if output is None or not getattr(output, "content", b""):
        return None
    try:
        return table_of(records_of(output.content, output.filename, output.content_type)[:2000])[0]
    except DatastoreError:
        return None


def signature(fields: list[dict], keys: list[str]) -> str:
    return json.dumps([[f["id"], f["type"]] for f in fields] + [sorted(keys)])


def batches(records: list[dict]):
    for i in range(0, len(records), BATCH):
        yield records[i:i + BATCH]


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
