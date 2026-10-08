"""The test suite turns off SQLite's disk syncing for its throwaway databases (see conftest.py). That
must stay a property of the *tests*: the service's own database keeps SQLite's safe defaults."""

import pathlib
import re

SERVICE = pathlib.Path(__file__).resolve().parent.parent / "providerharvest_service"


def test_the_service_never_turns_off_sqlite_durability():
    offenders = [
        str(path.relative_to(SERVICE.parent))
        for path in SERVICE.rglob("*.py")
        if re.search(r"synchronous\s*=\s*(OFF|0)|journal_mode\s*=\s*(MEMORY|OFF)", path.read_text(encoding="utf-8"), re.IGNORECASE)
    ]
    assert offenders == [], "the service must keep SQLite's default durability: %s" % offenders


def test_the_test_process_does_run_with_it_off(tmp_path):
    from providerharvest_service import db

    engine = db.make_engine("sqlite:///%s" % (tmp_path / "probe.db"))
    with engine.connect() as connection:
        from sqlalchemy import text

        assert connection.execute(text("PRAGMA synchronous")).scalar() == 0  # OFF
