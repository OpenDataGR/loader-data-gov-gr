def edit(p, pairs):
    s = open(p, newline="", encoding="utf-8").read()
    nl = "\r\n" if "\r\n" in s else "\n"
    s = s.replace(nl, "\n")
    for a, b in pairs:
        assert a in s, (p, a[:70])
        s = s.replace(a, b, 1)
    open(p, "w", newline="", encoding="utf-8").write(s.replace("\n", nl))

edit("providerharvest_service/transform_engine.py", [
('''OUTPUTS = {
    "json": ("application/json", "json", "JSON (a list of records)"),
    "jsonl": ("application/x-ndjson", "jsonl", "JSON Lines (one record per line)"),
    "xml": ("application/xml", "xml", "XML"),
    "csv": ("text/csv", "csv", "CSV"),
}''', '''OUTPUTS = {
    "json": ("application/json", "json", "JSON (a list of records)"),
    "jsonl": ("application/x-ndjson", "jsonl", "JSON Lines (one record per line)"),
    "xml": ("application/xml", "xml", "XML"),
    "csv": ("text/csv; charset=utf-8", "csv", "CSV"),
}
CSV_DELIMITERS = {",": "comma ( , )", ";": "semicolon ( ; ) -- what Excel with Greek or other European settings expects", "\t": "tab", "|": "pipe ( | )"}'''),
('''def write_output(records: list[Record], fmt: str, *, root: str = "records", record: str = "record") -> bytes:''',
 '''def write_output(records: list[Record], fmt: str, *, root: str = "records", record: str = "record", delimiter: str = ",", bom: bool = False) -> bytes:'''),
('''    if fmt == "csv":
        names: list[str] = []''', '''    if fmt == "csv":
        if delimiter not in CSV_DELIMITERS:
            raise TransformError("Choose one of the offered CSV separators.")
        names: list[str] = []'''),
('''        writer = csv.writer(buffer, lineterminator="\n")''', '''        writer = csv.writer(buffer, lineterminator="\n", delimiter=delimiter)'''),
('''        return buffer.getvalue().encode("utf-8")
    if fmt == "xml":''', '''        # A leading byte-order mark is how Excel knows a file is UTF-8 (without it Greek turns to garbage).
        return buffer.getvalue().encode("utf-8-sig" if bom else "utf-8")
    if fmt == "xml":'''),
('''def run(content: bytes, steps: list[dict], *, output: str = "json", lookups: dict[str, dict] | None = None,
        root: str = "records", record: str = "record") -> Result:''', '''def run(content: bytes, steps: list[dict], *, output: str = "json", lookups: dict[str, dict] | None = None,
        root: str = "records", record: str = "record", delimiter: str = ",", bom: bool = False) -> Result:'''),
('''        data = write_output(records, output, root=root, record=record)''', '''        data = write_output(records, output, root=root, record=record, delimiter=delimiter, bom=bom)'''),
])
edit("providerharvest_service/models.py", [
('''    record_name: Mapped[str] = mapped_column(String, nullable=False, default="record")  # XML output only''',
 '''    record_name: Mapped[str] = mapped_column(String, nullable=False, default="record")  # XML output only
    # CSV output only. NULL (a transform made before these existed) means the defaults: comma, with a byte-order mark.
    csv_delimiter: Mapped[str | None] = mapped_column(String, nullable=True)
    csv_bom: Mapped[bool | None] = mapped_column(Boolean, nullable=True)'''),
])
edit("providerharvest_service/transform_runner.py", [
('''        root=transform.root_name, record=transform.record_name,
    )''', '''        root=transform.root_name, record=transform.record_name,
        delimiter=transform.csv_delimiter or ",", bom=True if transform.csv_bom is None else bool(transform.csv_bom),
    )'''),
])
edit("providerharvest_service/ui_transform.py", [
('''    fmt = str(form.get("format") or "")
    root, record = str(form.get("root") or "records").strip(), str(form.get("record") or "record").strip()
    try:
        if fmt not in te.OUTPUTS:
            raise te.TransformError("Choose one of the offered output formats.")''', '''    fmt = str(form.get("format") or "")
    root, record = str(form.get("root") or "records").strip(), str(form.get("record") or "record").strip()
    delimiter = str(form.get("delimiter") or ",")
    try:
        if fmt not in te.OUTPUTS:
            raise te.TransformError("Choose one of the offered output formats.")
        if delimiter not in te.CSV_DELIMITERS:
            raise te.TransformError("Choose one of the offered CSV separators.")'''),
('''    transform.output_format, transform.root_name, transform.record_name = fmt, root, record''', '''    transform.output_format, transform.root_name, transform.record_name = fmt, root, record
    transform.csv_delimiter, transform.csv_bom = delimiter, bool(form.get("bom"))'''),
('''        "formats": te.OUTPUTS, "choices"''', '''        "formats": te.OUTPUTS, "delimiters": te.CSV_DELIMITERS, "choices"'''),
])
edit("providerharvest_service/templates/transform/source.html", [
('''      <p class="muted" style="margin:0;">The XML names are used only when the format is XML.</p>''', '''      <p class="muted" style="margin:0;">The XML names are used only when the format is XML.</p>
      <form class="inline-form" method="post" action="/transform/sources/{{ source.id }}/output" style="margin-top:0.6rem;">
        <input type="hidden" name="format" value="{{ transform.output_format }}">
        <input type="hidden" name="root" value="{{ transform.root_name }}">
        <input type="hidden" name="record" value="{{ transform.record_name }}">
        <div><label for="delim">CSV separator</label>
          <select id="delim" name="delimiter">{% for key, text in delimiters.items() %}<option value="{{ key }}"{% if key == (transform.csv_delimiter or ",") %} selected{% endif %}>{{ text }}</option>{% endfor %}</select></div>
        <div><label style="display:flex; gap:0.4rem; align-items:center; font-weight:400; margin:0;"><input type="checkbox" name="bom" value="1" style="width:auto; margin:0;"{% if transform.csv_bom is none or transform.csv_bom %} checked{% endif %}>
          Mark the file as UTF-8 so Excel shows Greek correctly</label></div>
        <button type="submit" class="btn-secondary">Save CSV settings</button>
      </form>
      <p class="muted" style="margin:0;">Only used when the format is CSV. Re-run earlier files (below) to apply a change to outputs already made.</p>'''),
])
print("ok")
