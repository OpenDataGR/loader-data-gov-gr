import pathlib
import sqlite3

from ftp_forwarder.accounts import RelayAccount, load_accounts


def _make_db(tmp_path: pathlib.Path, rows: list[tuple[str, str]]) -> str:
    db_path = str(tmp_path / "ftp-accounts.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE ftp_account (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, "
            "home_directory TEXT NOT NULL, protocol TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO ftp_account (username, password_hash, home_directory, protocol) VALUES (?, ?, ?, ?)",
            [(username, "$2a$hash", home, "ftp") for username, home in rows],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_load_accounts_reads_username_and_home_directory(tmp_path):
    db_path = _make_db(tmp_path, [("acme", "/srv/ftp/acme")])

    accounts = load_accounts(db_path)

    assert accounts == (RelayAccount(username="acme", watch_dir=pathlib.Path("/srv/ftp/acme")),)


def test_load_accounts_returns_every_row_in_username_order(tmp_path):
    db_path = _make_db(tmp_path, [("zulu", "/srv/ftp/zulu"), ("alpha", "/srv/ftp/alpha")])

    accounts = load_accounts(db_path)

    assert [a.username for a in accounts] == ["alpha", "zulu"]


def test_load_accounts_on_a_brand_new_database_is_empty(tmp_path):
    db_path = str(tmp_path / "does-not-exist-yet.db")

    accounts = load_accounts(db_path)

    assert accounts == ()
