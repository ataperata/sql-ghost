"""Database connectors for SQLite and Postgres.

Each source can introspect its schema, answer the small set of trusted
aggregate queries the profiler needs, and run an already validated query in a
locked down read-only session with a row limit and a timeout.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import model as m
from .model import Column, ForeignKey, Schema, Table


class SourceError(RuntimeError):
    pass


class QueryTimeout(SourceError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool = False
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": [[_jsonable(v) for v in r] for r in self.rows],
            "truncated": self.truncated,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "notes": self.notes,
        }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Type mapping


def kind_from_sqlite_type(declared: str) -> str:
    t = (declared or "").upper()
    if "BOOL" in t:
        return m.BOOLEAN
    if "INT" in t:
        return m.INTEGER
    if "DATETIME" in t or "TIMESTAMP" in t:
        return m.DATETIME
    if "DATE" in t:
        return m.DATE
    if "JSON" in t:
        return m.JSON
    if "UUID" in t:
        return m.UUID
    if any(s in t for s in ("CHAR", "CLOB", "TEXT")):
        return m.TEXT
    if any(s in t for s in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return m.REAL
    if t == "" or "BLOB" in t:
        return m.BLOB
    return m.TEXT


def kind_from_pg_type(data_type: str) -> str:
    t = data_type.lower()
    if t in ("smallint", "integer", "bigint"):
        return m.INTEGER
    if t in ("numeric", "real", "double precision", "money"):
        return m.REAL
    if t == "boolean":
        return m.BOOLEAN
    if t == "date":
        return m.DATE
    if t.startswith("timestamp"):
        return m.DATETIME
    if t in ("json", "jsonb"):
        return m.JSON
    if t == "uuid":
        return m.UUID
    if t == "bytea":
        return m.BLOB
    return m.TEXT


# ---------------------------------------------------------------------------
# Function denylist shared by the guard and the SQLite authorizer

DENIED_FUNCTIONS = {
    # SQLite: file system, extension loading and shell helpers
    "load_extension",
    "readfile",
    "writefile",
    "edit",
    "fts3_tokenizer",
    "zipfile",
    "sqlite_compileoption_get",
    "sqlar_compress",
    "sqlar_uncompress",
    # Postgres: sleeping, file access, side effects, cross-database access
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "lo_unlink",
    "lo_create",
    "dblink",
    "dblink_exec",
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "nextval",
    "setval",
    "pg_advisory_lock",
    "pg_advisory_xact_lock",
    "pg_try_advisory_lock",
    "txid_current",
    "pg_notify",
    "query_to_xml",
    "query_to_xml_and_xmlschema",
    "table_to_xml",
    "cursor_to_xml",
    "pg_logical_emit_message",
    "pg_switch_wal",
    "pg_create_restore_point",
    "pg_promote",
    "pg_import_system_collations",
    "copy",
}


# ---------------------------------------------------------------------------
# SQLite


class SQLiteSource:
    dialect = "sqlite"

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        if not self.path.exists():
            raise SourceError(f"SQLite file not found: {self.path}")
        self.label = self.path.name

    # Trusted connection used for introspection and profiling. Read only.
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = 1")
        return conn

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def introspect(self) -> Schema:
        conn = self._connect()
        try:
            return introspect_sqlite(conn)
        finally:
            conn.close()

    def run_readonly(self, sql: str, limit: int = 100, timeout: float = 5.0) -> QueryResult:
        conn = self._connect()
        try:
            return run_sqlite_locked(conn, sql, limit, timeout)
        finally:
            conn.close()

    def explain(self, sql: str) -> list[tuple[Any, ...]]:
        conn = self._connect()
        try:
            install_sqlite_authorizer(conn)
            return conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        finally:
            conn.close()


def introspect_sqlite(conn: sqlite3.Connection) -> Schema:
    tables: dict[str, Table] = {}
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for name, ddl in rows:
        info = conn.execute(f"PRAGMA table_info({quote_ident(name)})").fetchall()
        pk_cols = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        columns = [
            Column(
                name=r[1],
                type=r[2] or "",
                kind=kind_from_sqlite_type(r[2] or ""),
                nullable=not (r[3] or r[1] in pk_cols),
                primary_key=r[1] in pk_cols,
            )
            for r in info
        ]
        fks: dict[int, ForeignKey] = {}
        for fk in conn.execute(f"PRAGMA foreign_key_list({quote_ident(name)})").fetchall():
            fk_id, _seq, ref_table, from_col, to_col = fk[0], fk[1], fk[2], fk[3], fk[4]
            entry = fks.setdefault(fk_id, ForeignKey(columns=[], ref_table=ref_table, ref_columns=[]))
            entry.columns.append(from_col)
            entry.ref_columns.append(to_col)
        unique_sets: list[list[str]] = []
        for idx in conn.execute(f"PRAGMA index_list({quote_ident(name)})").fetchall():
            idx_name, is_unique, origin = idx[1], idx[2], idx[3]
            if not is_unique or origin == "pk":
                continue
            cols = [r[2] for r in conn.execute(f"PRAGMA index_info({quote_ident(idx_name)})")]
            if len(cols) == 1:
                for col in columns:
                    if col.name == cols[0]:
                        col.unique = True
            elif cols:
                unique_sets.append(cols)
        if len(pk_cols) == 1:
            columns[[c.name for c in columns].index(pk_cols[0])].unique = True
        # Resolve FK targets that omit the column list (REFERENCES parent)
        for fk in fks.values():
            if any(rc is None for rc in fk.ref_columns):
                ref_info = conn.execute(f"PRAGMA table_info({quote_ident(fk.ref_table)})").fetchall()
                ref_pk = [r[1] for r in sorted((r for r in ref_info if r[5]), key=lambda r: r[5])]
                fk.ref_columns = ref_pk[: len(fk.columns)]
        checks = re.findall(r"CHECK\s*\((.*?)\)\s*(?:,|\n|$)", ddl or "", flags=re.I | re.S)
        tables[name] = Table(
            name=name,
            columns=columns,
            primary_key=pk_cols,
            unique_sets=unique_sets,
            foreign_keys=list(fks.values()),
            checks=checks,
            ddl=ddl,
        )
    indexes = [
        r[0]
        for r in conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL ORDER BY name")
    ]
    for table in tables.values():
        table.apply_check_domains("sqlite")
    return Schema(dialect="sqlite", tables=tables, indexes=indexes)


# SQLite authorizer action codes (see sqlite3.h)
_SQLITE_READ = 20
_SQLITE_SELECT = 21
_SQLITE_FUNCTION = 31
_SQLITE_RECURSIVE = 33
_ALLOWED_ACTIONS = {_SQLITE_READ, _SQLITE_SELECT, _SQLITE_FUNCTION, _SQLITE_RECURSIVE}


def install_sqlite_authorizer(conn: sqlite3.Connection) -> None:
    """Second line of defense after the parser based guard.

    SQLite asks this callback about every operation while compiling a
    statement. Anything other than reading tables and calling safe functions
    is refused, so even a statement that slipped past the guard cannot write,
    attach another file or change a pragma.
    """

    def authorizer(action: int, arg1: Any, arg2: Any, _db: Any, _trigger: Any) -> int:
        if action not in _ALLOWED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == _SQLITE_FUNCTION and str(arg2).lower() in DENIED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)


def run_sqlite_locked(conn: sqlite3.Connection, sql: str, limit: int, timeout: float) -> QueryResult:
    install_sqlite_authorizer(conn)
    deadline = time.monotonic() + timeout
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    start = time.perf_counter()
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(limit + 1)
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc):
            raise QueryTimeout(f"query exceeded {timeout:.1f}s timeout") from exc
        raise SourceError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        raise SourceError(str(exc)) from exc
    finally:
        conn.set_progress_handler(None, 0)
    elapsed = (time.perf_counter() - start) * 1000
    columns = [d[0] for d in cur.description or []]
    truncated = len(rows) > limit
    return QueryResult(columns=columns, rows=rows[:limit], truncated=truncated, elapsed_ms=elapsed)


# ---------------------------------------------------------------------------
# Postgres


class PostgresSource:
    dialect = "postgres"

    def __init__(self, url: str):
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise SourceError("Postgres support needs psycopg: pip install 'sql-ghost[postgres]'") from exc
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        self.schema_name = qs.pop("schema", ["public"])[0]
        query = "&".join(f"{k}={v[0]}" for k, v in qs.items())
        self.url = parsed._replace(query=query).geturl()
        self.label = f"{parsed.hostname or 'localhost'}/{parsed.path.lstrip('/')}"

    def _connect(self):  # type: ignore[no-untyped-def]
        import psycopg

        conn = psycopg.connect(self.url, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT set_config('search_path', %s, true)", (self.schema_name,))
        return conn

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or None)
                return cur.fetchall()
        finally:
            conn.rollback()
            conn.close()

    def introspect(self) -> Schema:
        conn = self._connect()
        try:
            return introspect_postgres(conn, self.schema_name)
        finally:
            conn.rollback()
            conn.close()

    def run_readonly(self, sql: str, limit: int = 100, timeout: float = 5.0) -> QueryResult:
        conn = self._connect()
        try:
            return run_postgres_locked(conn, sql, limit, timeout)
        finally:
            conn.rollback()
            conn.close()

    def explain(self, sql: str) -> Any:
        """Plan only. EXPLAIN without ANALYZE never executes the query."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = 5000")
                cur.execute("EXPLAIN (FORMAT JSON) " + sql)
                return cur.fetchone()[0]
        finally:
            conn.rollback()
            conn.close()


def introspect_postgres(conn: Any, schema_name: str) -> Schema:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') ORDER BY c.relname",
            (schema_name,),
        )
        names = [r[0] for r in cur.fetchall()]
        cur.execute(
            "SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) "
            "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
            "JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = %s GROUP BY t.typname",
            (schema_name,),
        )
        enums = {r[0]: list(r[1]) for r in cur.fetchall()}
        tables: dict[str, Table] = {}
        for name in names:
            cur.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "t.typname, COALESCE(ic.data_type, '') "
                "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_type t ON t.oid = a.atttypid "
                "LEFT JOIN information_schema.columns ic ON ic.table_schema = n.nspname "
                "AND ic.table_name = c.relname AND ic.column_name = a.attname "
                "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum",
                (schema_name, name),
            )
            columns = []
            for attname, fmt, notnull, typname, data_type in cur.fetchall():
                enum_values = enums.get(typname)
                columns.append(
                    Column(
                        name=attname,
                        type=fmt,
                        kind=m.TEXT if enum_values else kind_from_pg_type(data_type or fmt),
                        nullable=not notnull,
                        enum_values=enum_values,
                    )
                )
            cur.execute(
                "SELECT con.contype, pg_get_constraintdef(con.oid), "
                "ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, o) "
                "  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n ORDER BY k.o), "
                "rc.relname, "
                "ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, o) "
                "  JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n ORDER BY k.o) "
                "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_class rc ON rc.oid = con.confrelid "
                "WHERE n.nspname = %s AND c.relname = %s ORDER BY con.contype, con.conname",
                (schema_name, name),
            )
            table = Table(name=name, columns=columns)
            for contype, condef, cols, ref_table, ref_cols in cur.fetchall():
                cols = list(cols)
                if contype == "p":
                    table.primary_key = cols
                    for col in columns:
                        if col.name in cols:
                            col.primary_key = True
                            col.nullable = False
                    if len(cols) == 1:
                        table.column(cols[0]).unique = True
                elif contype == "u":
                    if len(cols) == 1:
                        table.column(cols[0]).unique = True
                    else:
                        table.unique_sets.append(cols)
                elif contype == "f":
                    table.foreign_keys.append(ForeignKey(cols, ref_table, list(ref_cols)))
                elif contype == "c":
                    table.checks.append(condef)
            tables[name] = table
        cur.execute(
            "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND NOT i.indisprimary AND NOT i.indisunique",
            (schema_name,),
        )
        indexes = [r[0] for r in cur.fetchall()]
    for table in tables.values():
        table.apply_check_domains("postgres")
    return Schema(dialect="postgres", tables=tables, indexes=indexes, enums=enums)


def run_postgres_locked(conn: Any, sql: str, limit: int, timeout: float) -> QueryResult:
    import psycopg

    start = time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout * 1000)}")
            cur.execute("SET LOCAL lock_timeout = 1000")
            cur.execute(sql)
            rows = cur.fetchmany(limit + 1)
            columns = [d.name for d in cur.description or []]
    except psycopg.errors.QueryCanceled as exc:
        raise QueryTimeout(f"query exceeded {timeout:.1f}s timeout") from exc
    except psycopg.Error as exc:
        raise SourceError(str(exc).strip()) from exc
    elapsed = (time.perf_counter() - start) * 1000
    truncated = len(rows) > limit
    return QueryResult(columns=columns, rows=list(rows[:limit]), truncated=truncated, elapsed_ms=elapsed)


# ---------------------------------------------------------------------------


def open_source(target: str) -> SQLiteSource | PostgresSource:
    if target.startswith(("postgres://", "postgresql://")):
        return PostgresSource(target)
    if target.startswith("sqlite:///"):
        target = target[len("sqlite:///") :]
    return SQLiteSource(target)
