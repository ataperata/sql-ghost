"""Read-only guard built on a real SQL parser.

The candidate SQL is parsed with sqlglot in the source dialect. It passes only
if it is exactly one statement, the statement is a query (SELECT or a set
operation of SELECTs), and nowhere in its syntax tree is there a write, DDL,
transaction control, PRAGMA, ATTACH, SELECT INTO, row lock, side effect
function or system catalog read.

Regex filters fail on comments, quoting and nested statements. Walking the
tree does not, because it looks at what the statement is, not how it is spelled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .db import DENIED_FUNCTIONS

logging.getLogger("sqlglot").setLevel(logging.ERROR)

_FORBIDDEN_NAMES = [
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "Command",
    "Pragma",
    "Transaction",
    "Commit",
    "Rollback",
    "Set",
    "Use",
    "Copy",
    "Attach",
    "Detach",
    "Grant",
    "Revoke",
    "TruncateTable",
    "Analyze",
    "Into",
    "Lock",
    "LoadData",
    "Kill",
    "Refresh",
    "Cache",
    "Uncache",
    "Declare",
    "Execute",
]
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))

_LABELS = {
    "Into": "SELECT INTO creates a table",
    "Lock": "row locking clause (FOR UPDATE / FOR SHARE)",
    "Command": "statement sqlglot cannot classify as a query",
    "Pragma": "PRAGMA statement",
    "Attach": "ATTACH DATABASE",
    "Detach": "DETACH DATABASE",
}

SYSTEM_PREFIXES = ("sqlite_", "pg_", "pragma_")
SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "sqlite_master", "sqlite_temp", "temp", "main"}


@dataclass
class GuardResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    expression: exp.Expression | None = None

    @property
    def summary(self) -> str:
        return "read-only SELECT" if self.ok else "; ".join(self.reasons)


def parse_one_statement(sql: str, dialect: str) -> tuple[list[exp.Expression], str | None]:
    try:
        parsed = [e for e in sqlglot.parse(sql, read=dialect) if e is not None and not isinstance(e, exp.Semicolon)]
    except (ParseError, TokenError) as exc:
        message = str(exc).splitlines()[0]
        return [], f"parse error: {message}"
    if not parsed:
        return [], "empty statement"
    return parsed, None


def check_read_only(sql: str, dialect: str) -> GuardResult:
    statements, error = parse_one_statement(sql, dialect)
    if error:
        return GuardResult(False, [error])
    if len(statements) > 1:
        kinds = ", ".join(type(s).__name__.upper() for s in statements)
        return GuardResult(False, [f"{len(statements)} statements found ({kinds}); only one query is allowed"])

    tree = statements[0]
    reasons: list[str] = []
    root = tree.unnest() if isinstance(tree, (exp.Subquery, exp.Paren)) else tree
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        reasons.append(f"statement type {type(root).__name__.upper()} is not a query")

    for node in tree.walk():
        if node is root and reasons:
            continue  # already reported as the statement type
        if isinstance(node, FORBIDDEN_NODES):
            name = type(node).__name__
            label = _LABELS.get(name, f"{name.upper()} found inside the statement")
            if label not in reasons:
                reasons.append(label)

    for func in tree.find_all(exp.Func):
        name = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if name in DENIED_FUNCTIONS:
            reasons.append(f"function {name}() is not allowed")

    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if isinstance(table.this, exp.Func):  # table-valued function, e.g. pragma_table_info(...)
            fn = table.this
            name = (fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).lower()
            if name in DENIED_FUNCTIONS:
                reasons.append(f"function {name}() is not allowed")
        qualifier = (table.db or "").lower()
        if name.startswith(SYSTEM_PREFIXES) or qualifier in SYSTEM_SCHEMAS or qualifier.startswith("pg_"):
            reasons.append(f"reads system catalog or pragma {table.sql(dialect=dialect)}")
        elif table.catalog:
            reasons.append(f"cross-database reference {table.sql(dialect=dialect)}")

    # de-duplicate while keeping order
    seen: set[str] = set()
    reasons = [r for r in reasons if not (r in seen or seen.add(r))]
    return GuardResult(not reasons, reasons, tree)
