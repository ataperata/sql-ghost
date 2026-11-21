"""Plain data classes describing a database schema and its statistical profile.

The profile is the only thing derived from real rows that sql-ghost keeps. It is
JSON serializable on purpose, so you can print it and read exactly what left
the database.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Logical column kinds. Physical types vary by engine; generation works on these.
INTEGER = "integer"
REAL = "real"
TEXT = "text"
DATE = "date"
DATETIME = "datetime"
BOOLEAN = "boolean"
BLOB = "blob"
JSON = "json"
UUID = "uuid"


@dataclass
class Column:
    name: str
    type: str  # declared or formatted type, e.g. "INTEGER", "character varying(80)"
    kind: str  # one of the logical kinds above
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    enum_values: list[str] | None = None  # Postgres enum labels, if any
    # Allowed values and bounds read from CHECK constraints. These come from the
    # schema, not the data, so they are safe to use and they are complete.
    check_values: list[Any] | None = None
    check_min: float | None = None
    check_max: float | None = None


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


@dataclass
class Table:
    name: str
    columns: list[Column]
    primary_key: list[str] = field(default_factory=list)
    unique_sets: list[list[str]] = field(default_factory=list)  # multi-column UNIQUE
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    ddl: str | None = None  # verbatim CREATE TABLE text when the engine gives it

    def apply_check_domains(self, dialect: str) -> None:
        from .checks import check_domains

        texts = [self.ddl] if self.ddl else [f"SELECT 1 WHERE {c}" for c in self.checks]
        for text in texts:
            for col_name, domain in check_domains(text, dialect).items():
                try:
                    col = self.column(col_name)
                except KeyError:
                    continue
                if "values" in domain:
                    col.check_values = domain["values"]
                if "min" in domain:
                    col.check_min = domain["min"]
                if "max" in domain:
                    col.check_max = domain["max"]

    def column(self, name: str) -> Column:
        for col in self.columns:
            if col.name.lower() == name.lower():
                return col
        raise KeyError(f"{self.name}.{name}")

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class Schema:
    dialect: str  # "sqlite" or "postgres"
    tables: dict[str, Table]
    indexes: list[str] = field(default_factory=list)  # verbatim CREATE INDEX text
    enums: dict[str, list[str]] = field(default_factory=dict)  # Postgres enum types

    def table(self, name: str) -> Table:
        for key, tbl in self.tables.items():
            if key.lower() == name.lower():
                return tbl
        raise KeyError(name)

    def has_table(self, name: str) -> bool:
        return any(k.lower() == name.lower() for k in self.tables)

    def sqlglot_mapping(self) -> dict[str, dict[str, str]]:
        return {t.name: {c.name: c.type or "TEXT" for c in t.columns} for t in self.tables.values()}

    def dependency_order(self) -> list[str]:
        """Tables ordered so that every parent comes before its children."""
        order: list[str] = []
        state: dict[str, int] = {}

        def visit(name: str) -> None:
            if state.get(name) == 2:
                return
            if state.get(name) == 1:  # cycle: break it, nullable FKs get handled later
                return
            state[name] = 1
            for fk in self.tables[name].foreign_keys:
                ref = _resolve(self, fk.ref_table)
                if ref and ref != name:
                    visit(ref)
            state[name] = 2
            order.append(name)

        for name in sorted(self.tables):
            visit(name)
        return order


def _resolve(schema: Schema, name: str) -> str | None:
    for key in schema.tables:
        if key.lower() == name.lower():
            return key
    return None


@dataclass
class ColumnProfile:
    null_frac: float = 0.0
    n_distinct: int = 0
    # Numeric, date and datetime columns: 11 evenly spaced quantiles (min .. max).
    quantiles: list[Any] | None = None
    mean: float | None = None
    # Low cardinality columns: [value, relative frequency] pairs. A value is only
    # kept when it appears at least k times (see profiler.K_ANON), so a rare value
    # that could identify a person never leaves the database.
    categories: list[list[Any]] | None = None
    categories_complete: bool = False  # True when no value was suppressed
    # Text columns: shape only, never the values themselves.
    pattern: str | None = None
    shape: str | None = None  # e.g. "AAA9999" when every sampled value shares it
    min_len: int | None = None
    max_len: int | None = None
    avg_len: float | None = None


@dataclass
class TableProfile:
    row_count: int
    columns: dict[str, ColumnProfile]


@dataclass
class Profile:
    dialect: str
    tables: dict[str, TableProfile]
    k_anon: int = 5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        tables = {
            name: TableProfile(
                row_count=t["row_count"],
                columns={c: ColumnProfile(**p) for c, p in t["columns"].items()},
            )
            for name, t in data["tables"].items()
        }
        return cls(dialect=data["dialect"], tables=tables, k_anon=data.get("k_anon", 5))

    def column(self, table: str, column: str) -> ColumnProfile | None:
        for tname, tprof in self.tables.items():
            if tname.lower() == table.lower():
                for cname, cprof in tprof.columns.items():
                    if cname.lower() == column.lower():
                        return cprof
        return None

    def row_count(self, table: str) -> int:
        for tname, tprof in self.tables.items():
            if tname.lower() == table.lower():
                return tprof.row_count
        return 0
