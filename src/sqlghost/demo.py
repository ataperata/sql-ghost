"""End to end demo with no API keys: the bundled toll road database, the
offline translator, and a set of bad candidates the validator has to stop."""

from __future__ import annotations

from collections.abc import Callable

from .engine import Ghost
from .render import bold, checklist, indent, paint, summary_line, table
from .validate import FAIL, PASS

QUESTIONS = [
    "How many trips were there in March 2026?",
    "Total revenue by corridor",
    "Top 5 plazas by number of trips",
    "What is the busiest hour of the day?",
    "Average toll by vehicle class",
    "Which accounts have more than 3 disputed trips?",
    "How many plate OCR trips were on the Harbor Tunnel?",
    "How many accounts by account type",
]

# (what went wrong, natural language question or None, candidate SQL or None)
BAD_CANDIDATES: list[tuple[str, str | None, str | None]] = [
    ("A literal translator obeys a write request", "Delete all written-off trips", None),
    ("A model guessed the casing of a status value", None, "SELECT COUNT(*) FROM trips WHERE status = 'Disputed'"),
    ("A second statement hidden after a comment", None, "SELECT name FROM plazas; -- just the names\nDROP TABLE trips"),
    ("PRAGMA abuse", None, "PRAGMA writable_schema = ON"),
    ("Table-valued pragma used to read schema internals", None, "SELECT * FROM pragma_table_info('accounts')"),
    ("A hallucinated column", None, "SELECT plate_number, owner FROM vehicles"),
    (
        "An accidental cartesian join",
        None,
        "SELECT a.email, t.toll_cents FROM accounts a, trips t WHERE t.status = 'posted'",
    ),
    ("Loading a native extension", None, "SELECT load_extension('/tmp/evil.so')"),
]


def run_demo(ghost: Ghost | None = None, out: Callable[[str], None] = print) -> tuple[int, int]:
    ghost = ghost or Ghost()
    shadows = ghost.shadows
    real_rows = sum(t.row_count for t in ghost.profile.tables.values())
    shadow_rows = sum(shadows.info.rows.values())

    out(bold("sql-ghost demo"))
    out(f"database : {ghost.source.label} (bundled sample: a fictional toll road operator)")
    out(f"profile  : {len(ghost.schema.tables)} tables, {real_rows:,} real rows reduced to aggregates")
    out(f"shadow   : {shadow_rows:,} synthetic rows, seed {shadows.info.seed}, same constraints and indexes")
    out("engine   : offline rule based translator (no API key needed)")
    out("")
    out(bold("Part 1. Questions in plain English"))
    passed = 0
    for i, question in enumerate(QUESTIONS, start=1):
        res = ghost.ask(question, provider="rules", run=True)
        out("")
        out(bold(f"[{i}/{len(QUESTIONS)}] {question}"))
        if not res.translation or not res.report:
            out(f"  no translation: {res.error}")
            continue
        out(indent(res.translation.sql))
        out("  " + summary_line(res.report))
        warn_lines = checklist(res.report, verbose=False)
        if warn_lines:
            out(warn_lines)
        if res.result is not None:
            passed += 1
            out(indent(table(res.result, max_rows=5)))
        elif res.error:
            out(f"  {paint('error', FAIL)}: {res.error}")

    out("")
    out(bold("Part 2. Candidates that must never reach the real database"))
    blocked = 0
    for i, (label, question, sql) in enumerate(BAD_CANDIDATES, start=1):
        out("")
        out(bold(f"[{i}/{len(BAD_CANDIDATES)}] {label}"))
        if question:
            res = ghost.ask(question, provider="rules", run=True)
            out(f"  question: {question}")
            assert res.translation is not None and res.report is not None
            sql, report = res.translation.sql, res.report
        else:
            assert sql is not None
            report = ghost.validate(sql)
        out(indent(sql))
        if report.passed:
            out("  " + paint("NOT CAUGHT", FAIL))
            continue
        blocked += 1
        first = report.failures[0]
        out(f'  {paint("BLOCKED", FAIL)} by "{first.title}": {first.detail}')

    out("")
    out(bold("Summary"))
    out(
        f"  {paint(str(passed), PASS)} of {len(QUESTIONS)} questions passed every check and ran on the real "
        "database (read-only, LIMIT, timeout)"
    )
    out(f"  {paint(str(blocked), PASS)} of {len(BAD_CANDIDATES)} bad candidates were blocked using only the shadow")
    return passed, blocked
