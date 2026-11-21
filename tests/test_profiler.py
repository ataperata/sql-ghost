from __future__ import annotations

import sqlite3

import pytest

from sqlghost.db import SQLiteSource
from sqlghost.profiler import QUANTILE_POINTS, _quantiles, profile_source


def _naive_quantiles(conn: sqlite3.Connection, column: str, non_null: int) -> list:
    points = []
    for i in range(QUANTILE_POINTS):
        offset = min(non_null - 1, round(i * (non_null - 1) / (QUANTILE_POINTS - 1)))
        sql = f"SELECT {column} FROM t WHERE {column} IS NOT NULL ORDER BY {column} LIMIT 1 OFFSET {offset}"
        points.append(conn.execute(sql).fetchone()[0])
    return points


@pytest.mark.parametrize("values", [[5], [3, 1], [1, 1, 2, 2, 2, 9, None, None, 4], list(range(1000, 0, -7))])
def test_quantiles_match_one_query_per_offset(tmp_path, values):
    path = tmp_path / "q.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    conn.commit()
    non_null = sum(v is not None for v in values)
    expected = _naive_quantiles(conn, "v", non_null)
    conn.close()

    assert _quantiles(SQLiteSource(path), '"t"', '"v"', non_null) == expected


def test_profile_uses_one_connection(custom_db, monkeypatch):
    source = SQLiteSource(custom_db)
    schema = source.introspect()
    connects = []
    original = SQLiteSource._connect

    def counting(self):
        connects.append(1)
        return original(self)

    monkeypatch.setattr(SQLiteSource, "_connect", counting)
    profile = profile_source(source, schema)
    assert len(connects) == 1
    assert source._session is None
    assert profile.row_count("people") == 400
    # Outside a session each query still opens and closes its own connection.
    assert source.query("SELECT COUNT(*) FROM teams") == [(20,)]
    assert len(connects) == 2
