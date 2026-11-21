"""Read value domains out of CHECK constraints.

`status IN ('a', 'b')`, `n BETWEEN 1 AND 5`, `amount > 0` and the Postgres
form `status = ANY (ARRAY['a', 'b'])` are recognised. Anything more complex
is left alone; the shadow insert will simply retry a row the database rejects.
"""

from __future__ import annotations

import contextlib
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


def _literal(node: exp.Expression) -> Any:
    while isinstance(node, (exp.Cast, exp.Paren)):
        node = node.this
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return -float(node.this.this)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        number = float(node.this)
        return int(number) if number.is_integer() else number
    raise ValueError("not a literal")


def check_domains(sql: str, dialect: str) -> dict[str, dict[str, Any]]:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError:
        return {}
    out: dict[str, dict[str, Any]] = {}

    def put(col: exp.Column, key: str, value: Any) -> None:
        out.setdefault(col.name, {})[key] = value

    for node in tree.find_all(exp.In):
        if isinstance(node.this, exp.Column) and not isinstance(node.parent, exp.Not):
            with contextlib.suppress(ValueError):
                put(node.this, "values", [_literal(e) for e in node.expressions])
    for node in tree.find_all(exp.EQ):
        if isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Any):
            arr = node.expression.find(exp.Array)
            if arr is not None:
                with contextlib.suppress(ValueError):
                    put(node.this, "values", [_literal(e) for e in arr.expressions])
    for node in tree.find_all(exp.Between):
        if isinstance(node.this, exp.Column):
            try:
                put(node.this, "min", _literal(node.args["low"]))
                put(node.this, "max", _literal(node.args["high"]))
            except ValueError:
                pass
    bounds = {exp.GT: ("min", 1), exp.GTE: ("min", 0), exp.LT: ("max", -1), exp.LTE: ("max", 0)}
    for cls, (key, nudge) in bounds.items():
        for node in tree.find_all(cls):
            if isinstance(node.this, exp.Column):
                try:
                    value = _literal(node.expression)
                except ValueError:
                    continue
                if isinstance(value, (int, float)):
                    put(node.this, key, value + nudge if isinstance(value, int) else value)
    return out
