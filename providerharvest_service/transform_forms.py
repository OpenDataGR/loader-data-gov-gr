"""The Transform Box's forms, as plain functions: describe a step in words, and build a step from what was
submitted. Kept apart from the web layer so it can be tested without a browser, and generic: every step type
is one entry, the page renders whatever the entry says.

A form is a mapping of names to values where a name may repeat (Starlette's ``FormData`` has ``getlist``); a
plain ``dict`` of lists works too, which is what the tests use.
"""

from __future__ import annotations

from typing import Any, Mapping

from providerharvest_service import transform_engine as te
from providerharvest_service.transform_engine import TransformError

OP_LABELS = {
    "eq": "is", "ne": "is not", "in": "is one of", "not_in": "is none of", "gt": "is greater than", "ge": "is at least",
    "lt": "is less than", "le": "is at most", "contains": "contains", "exists": "has a value", "not_exists": "is empty",
}
FORMAT_LABELS = {"xml_records": "XML", "json_records": "JSON", "csv_records": "CSV", "xlsx_records": "Excel"}
TABULAR = ("csv_records", "xlsx_records")  # one record per row, columns by name: no nested records
RENAME_ROWS = 5
CONVERT_ROWS = 6


def _one(form: Mapping, key: str, default: str = "") -> str:
    value = form.get(key, default)
    if isinstance(value, list):
        value = value[0] if value else default
    return str(value if value is not None else default).strip()


def _many(form: Mapping, key: str) -> list[str]:
    if hasattr(form, "getlist"):
        return [str(v).strip() for v in form.getlist(key) if str(v).strip()]
    value = form.get(key, [])
    return [str(v).strip() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]


def label_of(step: dict) -> str:
    kind = step.get("type")
    if kind in te.READERS:
        return "Read " + te.READERS[kind]["label"]
    return te.STEPS.get(kind, {}).get("label", str(kind))


def summarize(step: dict) -> str:
    """One sentence about what a step does, for the step list."""
    kind = step.get("type")
    names = lambda items: ", ".join("%s" % i for i in items) or "nothing"  # noqa: E731
    if kind == "xml_records" or kind == "json_records":
        where = "each <%s>" % step.get("record") if kind == "xml_records" else ("each item of %s" % step["record"] if step.get("record") else "each item")
        fields = [f["name"] for f in step.get("fields") or []]
        text = "One record per %s" % where
        explode = step.get("explode")
        if explode:
            text += ", and one row per %s inside it" % explode.get("path")
            fields += [f["name"] for f in explode.get("fields") or []]
        return text + ". Fields: " + names(fields) + "." if fields else text + ". All top-level fields."
    if kind in TABULAR:
        fields = [f["name"] for f in step.get("fields") or []]
        where = " of sheet «%s»" % step["sheet"] if step.get("sheet") else ""
        return "One record per row%s" % where + (". Columns: " + names(fields) + "." if fields else ". All columns.")
    if kind == "lookup":
        added = [a.get("as") or a.get("column") for a in step.get("add") or []]
        miss = "records without a match are dropped" if step.get("on_missing") == "drop" else "records without a match are kept, with empty values"
        return "Find «%s» in index table «%s» (column «%s») and add: %s; %s." % (step.get("key"), step.get("table"), step.get("table_key"), names(added), miss)
    if kind == "filter":
        value = "" if step.get("op") in ("exists", "not_exists") else " «%s»" % step.get("value")
        return "Keep records where «%s» %s%s." % (step.get("field"), OP_LABELS.get(step.get("op"), step.get("op")), value)
    if kind == "rename":
        return "Rename: " + ", ".join("%s → %s" % kv for kv in (step.get("map") or {}).items()) + "."
    if kind == "convert":
        return "Convert: " + ", ".join("%s → %s" % kv for kv in (step.get("fields") or {}).items()) + "."
    if kind == "select":
        return "Keep only: " + names(step.get("fields") or []) + "."
    if kind == "drop":
        return "Remove: " + names(step.get("fields") or []) + "."
    if kind == "add_field":
        how = "template" if step.get("kind") == "template" else "constant"
        return "Add «%s» = %s «%s»." % (step.get("name"), how, step.get("value"))
    return str(kind)


# -- building steps from forms ---------------------------------------------------------------------------------------------

def suggest_name(path: str, taken: set[str]) -> str:
    """A field name for a path: its last part, made a legal name, made unique."""
    base = path.rsplit("@", 1)[-1] if "@" in path else path.rsplit("/", 1)[-1]
    base = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in base).strip("_") or "field"
    if not (base[0].isalpha() or base[0] == "_"):
        base = "f_" + base
    base, name, n = base[:60], base[:60], 2
    while name in taken:
        name, n = "%s_%d" % (base, n), n + 1
    return name


def build_reader(kind: str, form: Mapping) -> dict:
    """A reader step from the record/field chooser."""
    if kind not in te.READERS:
        raise TransformError("Unknown format.")
    chosen = _many(form, "path")
    fields, explode_fields = [], []
    explode_path = _one(form, "explode")
    taken: set[str] = set()
    for path in chosen:
        name = _one(form, "name::" + path) or suggest_name(path, taken)
        taken.add(name)
        if kind in TABULAR:
            fields.append({"name": name, "path": path})
        elif explode_path and (path.startswith(explode_path + "/") or path.startswith(explode_path + "@")):
            relative = path[len(explode_path):].lstrip("/")
            explode_fields.append({"name": name, "path": relative})
        else:
            fields.append({"name": name, "path": path})
    step: dict[str, Any] = {"type": kind}
    if kind == "csv_records":
        step["delimiter"] = _one(form, "delimiter", ",") or ","
        step["encoding"] = _one(form, "encoding", "utf-8-sig") or "utf-8-sig"
    elif kind == "xlsx_records":
        if _one(form, "sheet"):
            step["sheet"] = _one(form, "sheet")
    else:
        step["record"] = _one(form, "record")
    step["fields"] = fields
    if explode_path and kind not in TABULAR:
        step["explode"] = {"path": explode_path, "fields": explode_fields, "keep_empty": bool(_one(form, "keep_empty"))}
    problems = te.validate([step])
    if problems:
        raise TransformError(problems[0].replace("Step 1: ", ""))
    return step


def build_step(kind: str, form: Mapping, *, tables: dict[str, list[str]] | None = None) -> dict:
    """A (non-reader) step from its form. ``tables`` maps index-table names to their columns."""
    if kind == "lookup":
        table = _one(form, "table")
        columns = (tables or {}).get(table)
        if columns is None:
            raise TransformError("Choose an index table (upload one first if there is none).")
        added = [{"column": c, "as": _one(form, "as::" + c) or c} for c in _many(form, "add")]
        step = {"type": "lookup", "table": table, "key": _one(form, "key"), "table_key": _one(form, "table_key"), "add": added,
                "on_missing": _one(form, "on_missing", "keep") or "keep"}
        te.step_lookup([], step, te.Context(lookups={table: {"columns": columns, "rows": [], "index": {}}}))  # validates
    elif kind == "filter":
        step = {"type": "filter", "field": _one(form, "field"), "op": _one(form, "op")}
        if step["op"] not in ("exists", "not_exists"):
            step["value"] = _one(form, "value")
        te.step_filter([], step, te.Context(lookups={}))
    elif kind == "rename":
        mapping = {}
        for i in range(RENAME_ROWS):
            old, new = _one(form, "old%d" % i), _one(form, "new%d" % i)
            if old and new:
                mapping[old] = new
        step = {"type": "rename", "map": mapping}
        te.step_rename([], step, te.Context(lookups={}))
    elif kind == "convert":
        fields = {}
        for i in range(CONVERT_ROWS):
            field, to = _one(form, "field%d" % i), _one(form, "to%d" % i)
            if field and to:
                fields[field] = to
        step = {"type": "convert", "fields": fields}
        te.step_convert([], step, te.Context(lookups={}))
    elif kind in ("select", "drop"):
        step = {"type": kind, "fields": _many(form, "fields")}
        (te.step_select if kind == "select" else te.step_drop)([], step, te.Context(lookups={}))
    elif kind == "add_field":
        step = {"type": "add_field", "name": _one(form, "name"), "kind": _one(form, "value_kind", "constant") or "constant", "value": _one(form, "value")}
        te.step_add_field([], step, te.Context(lookups={}))
    else:
        raise TransformError("Unknown step.")
    return step


def fields_after(steps: list[dict]) -> list[str]:
    """The field names a list of steps produces, worked out from the configuration alone (no data)."""
    names: list[str] = []
    for step in steps:
        kind = step.get("type")
        if kind in ("xml_records", "json_records", "csv_records", "xlsx_records"):
            names = [f["name"] for f in step.get("fields") or []] + [f["name"] for f in (step.get("explode") or {}).get("fields") or []]
        elif kind == "lookup":
            names += [a.get("as") or a.get("column") for a in step.get("add") or [] if (a.get("as") or a.get("column")) not in names]
        elif kind == "rename":
            names = [(step.get("map") or {}).get(n, n) for n in names]
        elif kind == "select":
            names = list(step.get("fields") or [])
        elif kind == "drop":
            names = [n for n in names if n not in (step.get("fields") or [])]
        elif kind == "add_field" and step.get("name") not in names:
            names.append(step.get("name"))
    return names
