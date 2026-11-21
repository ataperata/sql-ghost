"""Plain text rendering for the CLI."""

from __future__ import annotations

import os
import sys
from typing import Any

from .db import QueryResult
from .validate import FAIL, PASS, SKIP, WARN, Report

_COLORS = {PASS: "32", WARN: "33", FAIL: "31", SKIP: "90"}


def use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, status: str) -> str:
    if not use_color():
        return text
    return f"\033[{_COLORS.get(status, '0')}m{text}\033[0m"


def bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if use_color() else text


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def checklist(report: Report, verbose: bool = True) -> str:
    lines = []
    for c in report.checks:
        tag = paint(f"{c.status.upper():<4}", c.status)
        if verbose or c.status in (FAIL, WARN):
            detail = f"  {c.detail}" if c.detail else ""
            lines.append(f"  {tag}  {c.title}{detail if c.status != SKIP or verbose else ''}")
    return "\n".join(lines)


def summary_line(report: Report) -> str:
    counts = {s: sum(1 for c in report.checks if c.status == s) for s in (PASS, WARN, FAIL, SKIP)}
    verdict = paint("APPROVED", PASS) if report.passed else paint("BLOCKED", FAIL)
    return f"{verdict}  {counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail, {counts[SKIP]} skipped"


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def table(result: QueryResult, max_rows: int = 10, max_width: int = 28) -> str:
    rows = [[_cell(v) for v in r] for r in result.rows[:max_rows]]
    headers = list(result.columns)
    widths = [min(max_width, max([len(h)] + [len(r[i]) for r in rows])) for i, h in enumerate(headers)]

    def fmt(cells: list[str]) -> str:
        out = []
        for cell, w in zip(cells, widths, strict=False):
            cell = cell if len(cell) <= w else cell[: w - 1] + "~"
            out.append(cell.ljust(w))
        return "  ".join(out).rstrip()

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in rows]
    more = len(result.rows) - max_rows
    if more > 0 or result.truncated:
        lines.append(f"... {max(more, 0)} more row(s) shown{' (output capped by LIMIT)' if result.truncated else ''}")
    return "\n".join(lines)
