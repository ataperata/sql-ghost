from __future__ import annotations

import sqlite3

import pytest

from sqlghost.db import install_sqlite_authorizer
from sqlghost.guard import check_read_only

BLOCKED_SQLITE = [
    "DELETE FROM trips",
    "UPDATE accounts SET balance_cents = 0",
    "INSERT INTO plazas (name) VALUES ('x')",
    "DROP TABLE trips",
    "CREATE TABLE x (a INT)",
    "ALTER TABLE trips ADD COLUMN x INT",
    "REPLACE INTO plazas VALUES (1, 'a', 'b', 'NB', 1, '2020-01-01')",
    "SELECT 1; DROP TABLE trips",
    "SELECT * FROM plazas; DELETE FROM trips",
    "SELECT * FROM plazas /* comment */ ; UPDATE trips SET toll_cents = 0",
    "SELECT name FROM plazas; -- harmless\nDROP TABLE trips",
    "PRAGMA writable_schema = ON",
    "PRAGMA foreign_keys = OFF",
    "pragma table_info(accounts)",
    "SELECT * FROM pragma_table_info('accounts')",
    "SELECT * FROM sqlite_master",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "DETACH DATABASE x",
    "VACUUM INTO '/tmp/copy.db'",
    "BEGIN TRANSACTION",
    "WITH d AS (DELETE FROM trips RETURNING *) SELECT * FROM d",
    "SELECT load_extension('/tmp/evil.so')",
    "SELECT writefile('/tmp/x', 'data')",
    "SELECT readfile('/etc/passwd')",
    "ANALYZE",
    "REINDEX",
    "",
    "SELEC * FROM trips",
]

BLOCKED_POSTGRES = [
    "SELECT pg_sleep(30)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * INTO backup FROM trips",
    "SELECT * FROM trips FOR UPDATE",
    "SELECT * FROM pg_catalog.pg_user",
    "SELECT * FROM information_schema.tables",
    "COPY trips TO '/tmp/out.csv'",
    "SET statement_timeout = 0",
    "GRANT ALL ON trips TO public",
    "TRUNCATE trips",
    "SELECT set_config('search_path', 'x', false)",
    "SELECT nextval('trips_trip_id_seq')",
    "SELECT dblink_exec('host=x', 'DROP TABLE t')",
    "CALL do_things()",
    "SELECT 1; COMMIT; DROP TABLE trips",
]

ALLOWED = [
    "SELECT * FROM trips",
    "select count(*) from trips where status = 'disputed'",
    "WITH busy AS (SELECT plaza_id, COUNT(*) n FROM trips GROUP BY 1) SELECT * FROM busy ORDER BY n DESC",
    "SELECT plaza_id FROM trips UNION SELECT plaza_id FROM rates",
    "SELECT * FROM (SELECT trip_id FROM trips) t",
    "SELECT trip_id, ROW_NUMBER() OVER (PARTITION BY plaza_id ORDER BY passed_at) FROM trips",
    "SELECT 'DROP TABLE trips; DELETE' AS harmless_text",
    "SELECT * FROM trips;",
    "-- a comment\nSELECT 1",
]


@pytest.mark.parametrize("sql", BLOCKED_SQLITE)
def test_blocks_sqlite(sql):
    result = check_read_only(sql, "sqlite")
    assert not result.ok, sql
    assert result.reasons


@pytest.mark.parametrize("sql", BLOCKED_POSTGRES)
def test_blocks_postgres(sql):
    assert not check_read_only(sql, "postgres").ok, sql


@pytest.mark.parametrize("sql", ALLOWED)
@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_allows_plain_queries(sql, dialect):
    result = check_read_only(sql, dialect)
    assert result.ok, (sql, result.reasons)


def test_multi_statement_reason_names_both():
    result = check_read_only("SELECT 1; DROP TABLE trips", "sqlite")
    assert "2 statements" in result.reasons[0]
    assert "DROP" in result.reasons[0]


def test_authorizer_blocks_writes_even_without_the_guard(tmp_path):
    db = tmp_path / "a.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (a INT)")
    conn.commit()
    install_sqlite_authorizer(conn)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)
    for sql in (
        "INSERT INTO t VALUES (1)",
        "DELETE FROM t",
        "PRAGMA writable_schema = 1",
        "ATTACH DATABASE ':memory:' AS x",
        "SELECT load_extension('x')",
        "DROP TABLE t",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    conn.close()
