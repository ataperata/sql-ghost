"""Collect aggregate statistics from the real database.

Rules this module follows:

* Only aggregates leave the database: counts, null fractions, quantiles,
  lengths, and value frequencies for low cardinality columns.
* A categorical value is kept only if at least K_ANON rows share it.
* Primary keys, foreign keys and UNIQUE columns never get value frequencies.
* Text columns are reduced to a shape (email, uuid, "AAA9999" and so on). The
  few rows sampled to detect that shape stay inside this function.
"""

from __future__ import annotations

import datetime as dt
import re
from contextlib import nullcontext
from decimal import Decimal
from typing import Any

from . import model as m
from .db import quote_ident
from .model import ColumnProfile, Profile, Schema, TableProfile

K_ANON = 5
MAX_CATEGORIES = 25
QUANTILE_POINTS = 11
PATTERN_SAMPLE = 200

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("iso_datetime", re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")),
    ("iso_date", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("email", re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")),
    ("uuid", re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")),
    ("url", re.compile(r"^https?://\S+$")),
    ("phone", re.compile(r"^\+?[\d\-\s().]{7,20}$")),
    ("name", re.compile(r"^[A-Z][a-z'\-]+( [A-Z][a-z'\-\.]+){0,3}$")),
    ("code", re.compile(r"^[A-Z0-9\-]{3,16}$")),
    ("token", re.compile(r"^[a-z0-9_\-]{1,32}$")),
]


def detect_pattern(values: list[str]) -> tuple[str, str | None]:
    """Return (pattern, shape). Shape is set when all values share one."""
    if not values:
        return "words", None
    for name, rx in _PATTERNS:
        if all(rx.match(v) for v in values):
            shape = None
            if name in ("code", "phone"):
                shapes = {_shape_of(v) for v in values}
                if len(shapes) == 1:
                    shape = shapes.pop()
            return name, shape
    return "words", None


def _shape_of(value: str) -> str:
    return "".join("A" if c.isalpha() else "9" if c.isdigit() else c for c in value)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    return value


def profile_source(source: Any, schema: Schema) -> Profile:
    session = getattr(source, "session", None)
    with session() if session is not None else nullcontext():
        return _profile_tables(source, schema)


def _profile_tables(source: Any, schema: Schema) -> Profile:
    tables: dict[str, TableProfile] = {}
    for name, table in schema.tables.items():
        q = quote_ident(name)
        row_count = int(source.query(f"SELECT COUNT(*) FROM {q}")[0][0])
        fk_cols = {c.lower() for fk in table.foreign_keys for c in fk.columns}
        cols: dict[str, ColumnProfile] = {}
        for col in table.columns:
            cols[col.name] = _profile_column(source, q, col, row_count, col.name.lower() in fk_cols)
        tables[name] = TableProfile(row_count=row_count, columns=cols)
    return Profile(dialect=schema.dialect, tables=tables, k_anon=K_ANON)


def _profile_column(source: Any, qtable: str, col: m.Column, row_count: int, is_fk: bool) -> ColumnProfile:
    qc = quote_ident(col.name)
    prof = ColumnProfile()
    if row_count == 0:
        return prof
    non_null, distinct = source.query(f"SELECT COUNT({qc}), COUNT(DISTINCT {qc}) FROM {qtable}")[0]
    prof.null_frac = round(1 - non_null / row_count, 4)
    prof.n_distinct = int(distinct)
    if non_null == 0 or col.kind in (m.BLOB, m.JSON):
        return prof

    identifying = col.primary_key or col.unique or is_fk
    if not identifying and distinct <= MAX_CATEGORIES and col.kind != m.REAL:
        counts = source.query(
            f"SELECT {qc}, COUNT(*) FROM {qtable} WHERE {qc} IS NOT NULL GROUP BY {qc} ORDER BY 2 DESC, 1"
        )
        kept = [[_plain(v), round(n / non_null, 4)] for v, n in counts if n >= K_ANON]
        if kept:
            prof.categories = kept
            prof.categories_complete = len(kept) == len(counts)

    text_like = col.kind in (m.TEXT, m.UUID)
    if text_like:
        lo, hi, avg = source.query(
            f"SELECT MIN(LENGTH(CAST({qc} AS TEXT))), MAX(LENGTH(CAST({qc} AS TEXT))), "
            f"AVG(LENGTH(CAST({qc} AS TEXT))) FROM {qtable}"
        )[0]
        prof.min_len, prof.max_len, prof.avg_len = int(lo), int(hi), round(float(avg), 2)
        sample = [
            str(r[0])
            for r in source.query(
                f"SELECT CAST({qc} AS TEXT) FROM {qtable} WHERE {qc} IS NOT NULL LIMIT {PATTERN_SAMPLE}"
            )
        ]
        prof.pattern, prof.shape = detect_pattern(sample)
        del sample  # the sampled values go no further than this line

    ordered = col.kind in (m.INTEGER, m.REAL, m.DATE, m.DATETIME) or (
        text_like and prof.pattern in ("iso_date", "iso_datetime")
    )
    if ordered and not col.primary_key:
        prof.quantiles = _quantiles(source, qtable, qc, int(non_null))
        if col.kind in (m.INTEGER, m.REAL):
            avg = source.query(f"SELECT AVG({qc}) FROM {qtable}")[0][0]
            prof.mean = round(float(avg), 4) if avg is not None else None
    elif col.primary_key and col.kind in (m.INTEGER, m.REAL):
        lo, hi = source.query(f"SELECT MIN({qc}), MAX({qc}) FROM {qtable}")[0]
        prof.quantiles = [_plain(lo), _plain(hi)]
    return prof


def _quantiles(source: Any, qtable: str, qc: str, non_null: int) -> list[Any]:
    offsets = [min(non_null - 1, round(i * (non_null - 1) / (QUANTILE_POINTS - 1))) for i in range(QUANTILE_POINTS)]
    # One sort of the column picks every offset, instead of one ORDER BY ... OFFSET
    # query (and sort) per point. Works on SQLite 3.25+ and Postgres.
    wanted = ", ".join(str(o) for o in sorted(set(offsets)))
    rows = source.query(
        f"SELECT rn, v FROM (SELECT {qc} AS v, ROW_NUMBER() OVER (ORDER BY {qc}) - 1 AS rn "
        f"FROM {qtable} WHERE {qc} IS NOT NULL) AS ranked WHERE rn IN ({wanted})"
    )
    by_offset = {int(rn): value for rn, value in rows}
    return [_plain(by_offset[o]) for o in offsets]
