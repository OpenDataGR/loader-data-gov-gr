def edit(p, pairs):
    s = open(p, newline="", encoding="utf-8").read()
    nl = "\r\n" if "\r\n" in s else "\n"
    s = s.replace(nl, "\n")
    for a, b in pairs:
        assert a in s, (p, a[:60])
        s = s.replace(a, b, 1)
    open(p, "w", newline="", encoding="utf-8").write(s.replace("\n", nl))

edit("providerharvest_service/templates/admin/ftp.html", [
('<details{% if c.state == "failed" or c.action == "diagnostics" and loop.first %} open{% endif %}><summary',
 '<details><summary'),
])
edit("providerharvest_service/box_commands.py", [
("MAX_OUTPUT = 8000\nSHOWN = 10", "MAX_OUTPUT = 12000\nSHOWN = 5"),
])
edit("ftp-forwarder/ftp_forwarder/box_commands.py", [
("MAX_OUTPUT = 6000", "MAX_OUTPUT = 12000\nSECTION_OUTPUT = 2000  # per section of a diagnostics snapshot, so no log can push the others out"),
('''def _clip(text: str) -> str:
    text = text.replace("\x00", "")
    if len(text) <= MAX_OUTPUT:
        return text
    return "...(cut)...\n" + text[-MAX_OUTPUT:]''',
'''def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text.replace("\x00", "")
    if len(text) <= limit:
        return text
    return "...(cut)...\n" + text[-limit:]'''),
('"-n", "25", "--no-pager"', '"-n", "15", "--no-pager"'),
('out.append("== %s\n%s" % (title, (done.stdout or done.stderr or "").strip()))',
 'out.append("== %s\n%s" % (title, _clip((done.stdout or done.stderr or "").strip(), SECTION_OUTPUT)))'),
])
print("ok")
