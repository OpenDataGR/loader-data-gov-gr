import datetime
import os
import sqlite3

import pytest

from ftp_forwarder import account_sync, archive_cleanup
from ftp_forwarder.archive_cleanup import prune, prune_account

NOW = datetime.datetime(2026, 10, 9, 12, 30, tzinfo=datetime.timezone.utc)


def _hour(days_ago, hour=0):
    base = (NOW - datetime.timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return base.strftime("%Y%m%d%H")


@pytest.fixture
def root(tmp_path):
    (tmp_path / "ftp").mkdir()
    return tmp_path / "ftp"


def _home(root, name="acme", hours=()):
    home = root / name
    (home / ".forwarded").mkdir(parents=True)
    for h in hours:
        (home / ".forwarded" / h).mkdir()
        (home / ".forwarded" / h / "Detector.xml").write_text("x")
    return home


def _left(home):
    return sorted(p.name for p in (home / ".forwarded").iterdir())


def test_hour_folders_wholly_older_than_the_period_are_removed_and_newer_ones_kept(root):
    old, mid, new = _hour(40), _hour(29), _hour(1)
    home = _home(root, hours=[old, mid, new])
    assert prune_account(home.as_posix(), 30, root.as_posix(), now=NOW) == 1
    assert _left(home) == sorted([mid, new])


def test_an_hour_only_partly_past_the_cutoff_stays(root):
    # cutoff = 2026-10-02 12:30. The 12:00 hour runs until 13:00, so part of it is still inside the period.
    inside, outside = "2026100212", "2026100211"
    home = _home(root, hours=[inside, outside])
    prune_account(home.as_posix(), 7, root.as_posix(), now=NOW)
    assert _left(home) == [inside]


@pytest.mark.parametrize("days", [0, -1, 3651, 99999, True, "30", None, 1.5])
def test_a_number_that_is_not_a_whole_period_between_1_and_3650_deletes_nothing(root, days):
    home = _home(root, hours=[_hour(500)])
    assert prune_account(home.as_posix(), days, root.as_posix(), now=NOW) == 0
    assert len(_left(home)) == 1


def test_only_hour_named_directories_are_ever_touched(root):
    home = _home(root, hours=[_hour(100)])
    for name in ("keep-me", "2026-10-01", "20260101", "20260101xx", "x2026010100"):
        (home / ".forwarded" / name).mkdir()
    (home / ".forwarded" / "2026010100.txt").write_text("file")
    prune_account(home.as_posix(), 30, root.as_posix(), now=NOW)
    for name in ("keep-me", "2026-10-01", "20260101", "20260101xx", "x2026010100", "2026010100.txt"):
        assert name in _left(home), name
    assert _hour(100) not in _left(home)


def test_a_symlink_named_like_an_hour_is_neither_followed_nor_removed(root, tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "file").write_text("do not delete")
    home = _home(root)
    try:
        os.symlink(outside, home / ".forwarded" / _hour(100))
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    prune_account(home.as_posix(), 30, root.as_posix(), now=NOW)
    assert (outside / "file").read_text() == "do not delete" and _left(home) == [_hour(100)]


def test_an_archive_that_is_itself_a_symlink_is_not_entered(root, tmp_path):
    target = tmp_path / "elsewhere"
    (target / _hour(100)).mkdir(parents=True)
    home = root / "acme"
    home.mkdir()
    try:
        os.symlink(target, home / ".forwarded")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    assert prune_account(home.as_posix(), 30, root.as_posix(), now=NOW) == 0 and (target / _hour(100)).exists()


@pytest.mark.parametrize("home", ["{tmp}/outside", "{root}", "{root}/../outside", "/"])
def test_a_home_that_is_not_strictly_under_the_upload_root_is_left_alone(root, tmp_path, home):
    outside = tmp_path / "outside"
    (outside / ".forwarded" / _hour(100)).mkdir(parents=True)
    path = home.format(tmp=tmp_path.as_posix(), root=root.as_posix())
    assert prune_account(path, 30, root.as_posix(), now=NOW) == 0
    assert (outside / ".forwarded" / _hour(100)).exists()


def test_files_still_waiting_to_be_forwarded_are_never_touched(root):
    home = _home(root, hours=[_hour(100)])
    (home / "Detector.xml").write_text("waiting")
    (home / "old-waiting.xml").write_text("waiting too")
    os.utime(home / "old-waiting.xml", (0, 0))  # ancient
    prune_account(home.as_posix(), 30, root.as_posix(), now=NOW)
    assert (home / "Detector.xml").read_text() == "waiting" and (home / "old-waiting.xml").exists()


def test_a_missing_archive_is_fine(root):
    (root / "acme").mkdir()
    assert prune_account((root / "acme").as_posix(), 30, root.as_posix(), now=NOW) == 0


# -- the policy table, from the account sync ---------------------------------------------------------------

def _db(tmp_path, accounts):
    path = str(tmp_path / "accounts.db")
    account_sync.reconcile(path, accounts)
    return path


def _acct(root, name, **extra):
    return {"username": name, "password_hash": "x", "home_directory": (root / name).as_posix(), "protocol": "ftp", **extra}


def test_the_sync_stores_each_accounts_days_and_anything_else_means_forever(tmp_path, root):
    db = _db(tmp_path, [
        _acct(root, "a", archive_days=30), _acct(root, "b"), _acct(root, "c", archive_days="30"),
        _acct(root, "d", archive_days=-5), _acct(root, "e", archive_days=True), _acct(root, "f", archive_days=0),
    ])
    got = {u: d for u, _, d in archive_cleanup.policies(db)}
    assert got == {"a": 30, "b": 0, "c": 0, "d": 0, "e": 0, "f": 0}


def test_a_changed_period_is_updated_and_a_removed_account_loses_its_policy(tmp_path, root):
    db = _db(tmp_path, [_acct(root, "a", archive_days=30), _acct(root, "b", archive_days=7)])
    account_sync.reconcile(db, [_acct(root, "a", archive_days=90)])
    assert {u: d for u, _, d in archive_cleanup.policies(db)} == {"a": 90}
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM archive_policy WHERE username = 'b'").fetchone()[0] == 0
    conn.close()


def test_prune_applies_each_accounts_own_period(tmp_path, root):
    a = _home(root, "a", hours=[_hour(20), _hour(2)])
    b = _home(root, "b", hours=[_hour(20), _hour(2)])
    c = _home(root, "c", hours=[_hour(400)])
    d = _home(root, "d", hours=[_hour(400)])
    db = _db(tmp_path, [_acct(root, "a", archive_days=7), _acct(root, "b", archive_days=30), _acct(root, "c", archive_days=0), _acct(root, "d")])
    assert prune(db, root.as_posix(), now=NOW) == {"a": 1}
    assert _left(a) == [_hour(2)] and len(_left(b)) == 2 and len(_left(c)) == 1 and len(_left(d)) == 1  # 0 and unsent: forever


def test_one_accounts_trouble_does_not_stop_the_others(tmp_path, root, monkeypatch):
    a = _home(root, "a", hours=[_hour(100)])
    b = _home(root, "b", hours=[_hour(100)])
    db = _db(tmp_path, [_acct(root, "a", archive_days=7), _acct(root, "b", archive_days=7)])
    real = archive_cleanup.shutil.rmtree

    def flaky(path, *args, **kwargs):
        if "/a/" in str(path).replace("\\", "/"):
            raise OSError("disk trouble")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(archive_cleanup.shutil, "rmtree", flaky)
    assert prune(db, root.as_posix(), now=NOW) == {"b": 1}
    assert len(_left(a)) == 1 and _left(b) == []
