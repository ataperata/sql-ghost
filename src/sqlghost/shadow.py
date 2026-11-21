"""Build and query the synthetic shadow database.

The shadow has the real schema (tables, types, NOT NULL, UNIQUE, CHECK,
foreign keys and indexes) but every row is generated from the profile. Three
variants are built from one profile:

* primary: the main shadow, used for most checks
* alt: same distributions, different seed, used to see whether a result
  actually depends on the data
* empty: schema only, used for edge case checks
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import model as m
from .db import (
    QueryResult,
    SourceError,
    quote_ident,
    run_postgres_locked,
    run_sqlite_locked,
)
from .model import Profile, Schema, Table
from .synth import ValueFactory, default_value

FORMAT_VERSION = 3
MIN_ROWS = 50
MAX_ATTEMPTS = 25
ALT_SIZE = 0.75  # the perturbed shadow is smaller, so plain counts differ too


@dataclass
class ShadowInfo:
    seed: int
    rows: dict[str, int] = field(default_factory=dict)
    target: dict[str, int] = field(default_factory=dict)
    scale: dict[str, float] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)  # rows refused by constraints

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Row generation


class RowGenerator:
    def __init__(self, schema: Schema, profile: Profile, seed: int, rows_cap: int = 2000, size_factor: float = 1.0):
        self.schema = schema
        self.size_factor = size_factor
        self.profile = profile
        self.seed = seed
        self.rng = random.Random(seed)
        self.values = ValueFactory(self.rng)
        self.rows_cap = rows_cap
        self.keys: dict[tuple[str, tuple[str, ...]], list[tuple[Any, ...]]] = {}
        self.referenced: dict[str, set[tuple[str, ...]]] = {}
        for table in schema.tables.values():
            for fk in table.foreign_keys:
                ref = _match_name(schema, fk.ref_table)
                self.referenced.setdefault(ref, set()).add(tuple(fk.ref_columns))

    def targets(self) -> dict[str, int]:
        biggest = max((t.row_count for t in self.profile.tables.values()), default=0)
        scale = min(1.0, self.rows_cap / biggest) if biggest else 1.0
        out = {}
        for name in self.schema.tables:
            real = self.profile.row_count(name)
            want = min(real, max(min(real, MIN_ROWS), round(real * scale)))
            out[name] = max(1, round(want * self.size_factor)) if want else 0
        return out

    def build(self, writer: Any) -> ShadowInfo:
        info = ShadowInfo(seed=self.seed)
        targets = self.targets()
        for name in self.schema.dependency_order():
            table = self.schema.tables[name]
            want = targets[name]
            info.target[name] = want
            real = self.profile.row_count(name)
            info.scale[name] = round(want / real, 6) if real else 1.0
            made, rejected = self._fill_table(table, want, writer)
            info.rows[name] = made
            if rejected:
                info.rejected[name] = rejected
        writer.finish()
        return info

    def _fill_table(self, table: Table, want: int, writer: Any) -> tuple[int, int]:
        seen_unique: dict[str, set[Any]] = {c.name: set() for c in table.columns if c.unique}
        seen_sets: list[set[tuple[Any, ...]]] = [set() for _ in table.unique_sets]
        pk_counter = self._pk_start(table)
        made = rejected = 0
        cols = table.column_names
        for index in range(want):
            for _attempt in range(MAX_ATTEMPTS):
                row = self._make_row(table, index, pk_counter)
                if row is None:
                    break
                if not self._unique_ok(table, row, seen_unique, seen_sets):
                    continue
                if writer.insert(table.name, cols, [row[c] for c in cols]):
                    self._remember(table, row, seen_unique, seen_sets)
                    pk_counter += 1
                    made += 1
                    break
                rejected += 1
        return made, rejected

    def _pk_start(self, table: Table) -> int:
        if len(table.primary_key) == 1:
            prof = self.profile.column(table.name, table.primary_key[0])
            if prof and prof.quantiles and isinstance(prof.quantiles[0], (int, float)):
                return int(prof.quantiles[0])
        return 1

    def _make_row(self, table: Table, index: int, pk_counter: int) -> dict[str, Any] | None:
        row: dict[str, Any] = {}
        for fk in table.foreign_keys:
            ref = _match_name(self.schema, fk.ref_table)
            parents = self.keys.get((ref, tuple(fk.ref_columns)), [])
            nullable = all(table.column(c).nullable for c in fk.columns)
            prof = self.profile.column(table.name, fk.columns[0])
            null_frac = prof.null_frac if prof else 0.0
            if (nullable and self.rng.random() < null_frac) or (not parents and nullable):
                for c in fk.columns:
                    row[c] = None
                continue
            if not parents:
                return None  # a required parent does not exist; skip this row
            choice = self.rng.choice(parents)
            for c, v in zip(fk.columns, choice, strict=True):
                row[c] = v
        for col in table.columns:
            if col.name in row:
                continue
            row[col.name] = self._value(table, col, index, pk_counter)
        return row

    def _value(self, table: Table, col: m.Column, index: int, pk_counter: int) -> Any:
        prof = self.profile.column(table.name, col.name)
        if col.primary_key and len(table.primary_key) == 1 and col.kind == m.INTEGER:
            return pk_counter
        if col.nullable and prof and prof.null_frac and self.rng.random() < prof.null_frac:
            return None
        if prof and prof.categories:
            values = [c[0] for c in prof.categories]
            weights = [c[1] for c in prof.categories]
            return self.rng.choices(values, weights)[0]
        if col.enum_values:
            return self.rng.choice(col.enum_values)
        if col.check_values:
            return self.rng.choice(col.check_values)
        if col.kind in (m.INTEGER, m.REAL) and (col.check_min is not None or col.check_max is not None):
            value = (
                self.values.from_quantiles(prof.quantiles, col.kind)
                if prof and prof.quantiles
                else default_value(col, self.values, index)
            )
            if col.check_min is not None:
                value = max(value, col.check_min)
            if col.check_max is not None:
                value = min(value, col.check_max)
            return value
        if col.kind == m.BOOLEAN:
            return self.rng.random() < 0.5 if self.schema.dialect == "postgres" else int(self.rng.random() < 0.5)
        if prof and prof.quantiles and col.kind in (m.INTEGER, m.REAL, m.DATE, m.DATETIME, m.TEXT):
            if col.unique and col.kind == m.INTEGER:
                return int(prof.quantiles[0]) + index
            return self.values.from_quantiles(prof.quantiles, col.kind)
        if col.kind in (m.TEXT, m.UUID) and prof and prof.pattern:
            return self.values.text(prof, index, col.unique)
        return default_value(col, self.values, index)

    def _unique_ok(
        self, table: Table, row: dict[str, Any], seen: dict[str, set[Any]], seen_sets: list[set[tuple[Any, ...]]]
    ) -> bool:
        for col, values in seen.items():
            if row[col] is not None and row[col] in values:
                return False
        for cols, values in zip(table.unique_sets, seen_sets, strict=True):
            key = tuple(row[c] for c in cols)
            if None not in key and key in values:
                return False
        return True

    def _remember(
        self, table: Table, row: dict[str, Any], seen: dict[str, set[Any]], seen_sets: list[set[tuple[Any, ...]]]
    ) -> None:
        for col, values in seen.items():
            if row[col] is not None:
                values.add(row[col])
        for cols, values in zip(table.unique_sets, seen_sets, strict=True):
            values.add(tuple(row[c] for c in cols))
        for cols in self.referenced.get(table.name, ()):
            key = tuple(row[_match_col(table, c)] for c in cols)
            if None not in key:
                self.keys.setdefault((table.name, cols), []).append(key)


def _match_name(schema: Schema, name: str) -> str:
    for key in schema.tables:
        if key.lower() == name.lower():
            return key
    return name


def _match_col(table: Table, name: str) -> str:
    return table.column(name).name


# ---------------------------------------------------------------------------
# Writers


class SQLiteWriter:
    def __init__(self, path: Path, schema: Schema):
        self.path = path
        with suppress(FileNotFoundError):
            path.unlink()
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        for name in schema.dependency_order():
            self.conn.execute(schema.tables[name].ddl or _generic_sqlite_ddl(schema.tables[name]))
        for idx in schema.indexes:
            self.conn.execute(idx)
        self.conn.commit()
        self.conn.execute("BEGIN")

    def insert(self, table: str, cols: list[str], values: list[Any]) -> bool:
        sql = (
            f"INSERT INTO {quote_ident(table)} ({', '.join(quote_ident(c) for c in cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})"
        )
        try:
            self.conn.execute(sql, values)
            return True
        except sqlite3.IntegrityError:
            return False

    def finish(self) -> None:
        self.conn.commit()
        self.conn.execute("ANALYZE")
        self.conn.commit()
        self.conn.close()


def _generic_sqlite_ddl(table: Table) -> str:
    parts = []
    for col in table.columns:
        piece = f"{quote_ident(col.name)} {col.type}"
        if not col.nullable:
            piece += " NOT NULL"
        parts.append(piece)
    if table.primary_key:
        parts.append(f"PRIMARY KEY ({', '.join(quote_ident(c) for c in table.primary_key)})")
    return f"CREATE TABLE {quote_ident(table.name)} ({', '.join(parts)})"


class PostgresWriter:
    def __init__(self, url: str, shadow_schema: str, schema: Schema, source_schema: str = "public"):
        import psycopg

        self.conn = psycopg.connect(url, autocommit=False)
        self.cur = self.conn.cursor()
        self.row_cur = psycopg.ClientCursor(self.conn)
        s = quote_ident(shadow_schema)
        self.cur.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
        self.cur.execute(f"CREATE SCHEMA {s}")
        self.cur.execute(f"SET LOCAL search_path TO {s}")
        for enum, labels in schema.enums.items():
            values = ", ".join("'" + v.replace("'", "''") + "'" for v in labels)
            self.cur.execute(f"CREATE TYPE {quote_ident(enum)} AS ENUM ({values})")
        for name in schema.dependency_order():
            self.cur.execute(_postgres_table_ddl(schema.tables[name]))
        for name in schema.dependency_order():
            table = schema.tables[name]
            for i, fk in enumerate(table.foreign_keys):
                self.cur.execute(
                    f"ALTER TABLE {quote_ident(name)} ADD CONSTRAINT {quote_ident(f'{name}_fk{i}')} "
                    f"FOREIGN KEY ({', '.join(quote_ident(c) for c in fk.columns)}) "
                    f"REFERENCES {quote_ident(fk.ref_table)} ({', '.join(quote_ident(c) for c in fk.ref_columns)}) "
                    "DEFERRABLE INITIALLY IMMEDIATE"
                )
        src = re.escape(source_schema)
        for idx in schema.indexes:
            self.cur.execute(re.sub(rf" ON (ONLY )?{src}\.", " ON ", idx))

    def insert(self, table: str, cols: list[str], values: list[Any]) -> bool:
        import psycopg

        # Each row gets its own savepoint so a rejected row does not abort the build.
        # Savepoint, insert and release go to the server as one query string, so an
        # accepted row costs one round trip instead of three. That needs client-side
        # parameter binding, which psycopg's ClientCursor does with proper escaping.
        sql = (
            "SAVEPOINT sqlghost_row; "
            f"INSERT INTO {quote_ident(table)} ({', '.join(quote_ident(c) for c in cols)}) "
            f"VALUES ({', '.join('%s' for _ in cols)}); "
            "RELEASE SAVEPOINT sqlghost_row"
        )
        try:
            self.row_cur.execute(sql, values)
            return True
        except (psycopg.errors.IntegrityError, psycopg.errors.DataError):
            self.row_cur.execute("ROLLBACK TO SAVEPOINT sqlghost_row; RELEASE SAVEPOINT sqlghost_row")
            return False

    def finish(self) -> None:
        self.conn.commit()
        self.conn.autocommit = True
        self.conn.execute("ANALYZE")
        self.conn.close()


def _postgres_table_ddl(table: Table) -> str:
    parts = []
    for col in table.columns:
        piece = f"{quote_ident(col.name)} {col.type}"
        if not col.nullable:
            piece += " NOT NULL"
        parts.append(piece)
    if table.primary_key:
        parts.append(f"PRIMARY KEY ({', '.join(quote_ident(c) for c in table.primary_key)})")
    for col in table.columns:
        if col.unique and not col.primary_key:
            parts.append(f"UNIQUE ({quote_ident(col.name)})")
    for cols in table.unique_sets:
        parts.append(f"UNIQUE ({', '.join(quote_ident(c) for c in cols)})")
    parts.extend(table.checks)
    return f"CREATE TABLE {quote_ident(table.name)} ({', '.join(parts)})"


class NullWriter:
    """Writer for the empty variant: schema only."""

    def __init__(self, inner: Any):
        self.inner = inner

    def insert(self, table: str, cols: list[str], values: list[Any]) -> bool:
        return False

    def finish(self) -> None:
        self.inner.finish()


# ---------------------------------------------------------------------------
# Querying a shadow


class SQLiteShadow:
    dialect = "sqlite"

    def __init__(self, path: Path):
        self.path = path

    def run(self, sql: str, limit: int = 1000, timeout: float = 5.0) -> QueryResult:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return run_sqlite_locked(conn, sql, limit, timeout)
        finally:
            conn.close()

    def run_after_deleting(
        self, sql: str, tables: list[str], frac: float, seed: int, limit: int = 1000, timeout: float = 5.0
    ) -> QueryResult:
        """Delete a random share of rows inside a transaction, run, roll back."""
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("BEGIN")
            for t in tables:
                conn.execute(
                    f"DELETE FROM {quote_ident(t)} WHERE ((rowid * 2654435761 + ?) % 1000) < ?",
                    (seed, int(frac * 1000)),
                )
            return run_sqlite_locked(conn, sql, limit, timeout)
        finally:
            conn.set_authorizer(None)
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            conn.close()

    def explain(self, sql: str) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        finally:
            conn.close()


class PostgresShadow:
    dialect = "postgres"

    def __init__(self, url: str, schema_name: str):
        self.url = url
        self.schema_name = schema_name

    def _connect(self, read_only: bool = True):  # type: ignore[no-untyped-def]
        import psycopg

        conn = psycopg.connect(self.url, autocommit=False)
        with conn.cursor() as cur:
            if read_only:
                cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT set_config('search_path', %s, true)", (self.schema_name,))
        return conn

    def run(self, sql: str, limit: int = 1000, timeout: float = 5.0) -> QueryResult:
        conn = self._connect()
        try:
            return run_postgres_locked(conn, sql, limit, timeout)
        finally:
            conn.rollback()
            conn.close()

    def run_after_deleting(
        self, sql: str, tables: list[str], frac: float, seed: int, limit: int = 1000, timeout: float = 5.0
    ) -> QueryResult:
        conn = self._connect(read_only=False)
        try:
            with conn.cursor() as cur:
                cur.execute("SET CONSTRAINTS ALL DEFERRED")
                for t in tables:
                    cur.execute(
                        f"DELETE FROM {quote_ident(t)} WHERE abs(hashtext(ctid::text || %s)) %% 1000 < %s",
                        (str(seed), int(frac * 1000)),
                    )
            return run_postgres_locked(conn, sql, limit, timeout)
        except Exception as exc:  # FK violations on delete: treat as unsupported
            raise SourceError(str(exc)) from exc
        finally:
            conn.rollback()
            conn.close()

    def explain(self, sql: str) -> Any:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("EXPLAIN (FORMAT JSON) " + sql)
                return cur.fetchone()[0]
        finally:
            conn.rollback()
            conn.close()


@dataclass
class ShadowSet:
    primary: Any
    alt: Any
    empty: Any
    info: ShadowInfo
    alt_info: ShadowInfo
    location: str


# ---------------------------------------------------------------------------


def cache_key(schema: Schema, profile: Profile, seed: int, rows_cap: int) -> str:
    blob = json.dumps(
        {
            "v": FORMAT_VERSION,
            "ddl": [t.ddl or asdict(t) for t in schema.tables.values()],
            "idx": schema.indexes,
            "profile": profile.to_dict(),
            "seed": seed,
            "cap": rows_cap,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def build_sqlite_shadows(
    schema: Schema, profile: Profile, cache_dir: Path, seed: int = 7, rows_cap: int = 2000, force: bool = False
) -> ShadowSet:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(schema, profile, seed, rows_cap)
    paths = {v: cache_dir / f"shadow-{key}-{v}.sqlite" for v in ("primary", "alt", "empty")}
    meta_path = cache_dir / f"shadow-{key}.json"
    if not force and meta_path.exists() and all(p.exists() for p in paths.values()):
        meta = json.loads(meta_path.read_text())
        info, alt_info = ShadowInfo(**meta["primary"]), ShadowInfo(**meta["alt"])
    else:
        info = RowGenerator(schema, profile, seed, rows_cap).build(SQLiteWriter(paths["primary"], schema))
        alt_info = RowGenerator(schema, profile, seed + 1, rows_cap, ALT_SIZE).build(SQLiteWriter(paths["alt"], schema))
        RowGenerator(schema, profile, seed, rows_cap).build(NullWriter(SQLiteWriter(paths["empty"], schema)))
        meta_path.write_text(json.dumps({"primary": info.to_dict(), "alt": alt_info.to_dict()}, indent=2))
    return ShadowSet(
        primary=SQLiteShadow(paths["primary"]),
        alt=SQLiteShadow(paths["alt"]),
        empty=SQLiteShadow(paths["empty"]),
        info=info,
        alt_info=alt_info,
        location=str(paths["primary"]),
    )


def build_postgres_shadows(
    schema: Schema,
    profile: Profile,
    shadow_url: str,
    seed: int = 7,
    rows_cap: int = 2000,
    source_schema: str = "public",
    prefix: str = "sqlghost_shadow",
) -> ShadowSet:
    names = {"primary": prefix, "alt": f"{prefix}_alt", "empty": f"{prefix}_empty"}
    info = RowGenerator(schema, profile, seed, rows_cap).build(
        PostgresWriter(shadow_url, names["primary"], schema, source_schema)
    )
    alt_info = RowGenerator(schema, profile, seed + 1, rows_cap, ALT_SIZE).build(
        PostgresWriter(shadow_url, names["alt"], schema, source_schema)
    )
    RowGenerator(schema, profile, seed, rows_cap).build(
        NullWriter(PostgresWriter(shadow_url, names["empty"], schema, source_schema))
    )
    return ShadowSet(
        primary=PostgresShadow(shadow_url, names["primary"]),
        alt=PostgresShadow(shadow_url, names["alt"]),
        empty=PostgresShadow(shadow_url, names["empty"]),
        info=info,
        alt_info=alt_info,
        location=f"{shadow_url} (schema {names['primary']})",
    )
