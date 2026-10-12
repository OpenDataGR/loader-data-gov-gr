"""The Data boxes page's chart hover must stay cheap: a chart carries up to a week of readings, and the old handler
re-parsed them and searched them quadratically on every mouse move (about 35 ms each on a 7-day chart, so the page
stalled while the pointer moved over it)."""

import pathlib

TEMPLATE = pathlib.Path("providerharvest_service/templates/admin/ftp.html").read_text(encoding="utf-8")


def test_the_chart_hover_reads_each_chart_once_and_answers_once_per_screen_refresh():
    assert "wrap._chart" in TEMPLATE                      # parsed once per chart, then kept
    assert "requestAnimationFrame" in TEMPLATE            # a burst of mouse moves is one update
    assert "all.indexOf(" not in TEMPLATE                 # the quadratic de-duplication is gone
    assert TEMPLATE.count("JSON.parse(wrap.getAttribute('data-points')") == 1  # not on every move


def test_the_page_still_refreshes_itself_only_while_it_is_being_looked_at():
    assert "document.hidden" in TEMPLATE and TEMPLATE.count("setInterval(") == 1
