"""Command line interface: sqlghost ask | validate | shadow | profile | schema | demo | serve | sample."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .db import SourceError
from .engine import Ghost
from .render import bold, checklist, indent, paint, summary_line, table
from .validate import FAIL, ValidatorOptions


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", help="SQLite file or postgresql:// URL (default: bundled sample, or $SQLGHOST_DB)")
    p.add_argument("--shadow-url", help="Postgres only: scratch database for the shadow schemas")
    p.add_argument("--seed", type=int, default=7, help="seed for synthetic data (default 7)")
    p.add_argument("--rows-cap", type=int, default=2000, help="max rows in the largest shadow table")
    p.add_argument("--limit", type=int, default=100, help="row limit for the real query")
    p.add_argument("--timeout", type=float, default=5.0, help="seconds before a query is cancelled")
    p.add_argument("--json", action="store_true", help="machine readable output")


def _ghost(args: argparse.Namespace) -> Ghost:
    return Ghost(
        args.db,
        shadow_url=args.shadow_url,
        seed=args.seed,
        rows_cap=args.rows_cap,
        options=ValidatorOptions(limit=args.limit, shadow_timeout=args.timeout),
    )


def cmd_ask(args: argparse.Namespace) -> int:
    ghost = _ghost(args)
    res = ghost.ask(args.question, provider=args.provider, run=not args.no_run, timeout=args.timeout)
    if args.json:
        print(json.dumps(res.to_dict(), indent=2, default=str))
        return 0 if res.report and res.report.passed else 1
    if not res.translation:
        print(f"{paint('no translation', FAIL)}: {res.error}")
        if res.suggestions:
            print("Try one of:")
            for s in res.suggestions:
                print(f"  - {s}")
        return 2
    print(bold("SQL") + f"  ({res.translation.engine}{', attempt ' + str(res.attempts) if res.attempts > 1 else ''})")
    print(indent(res.translation.sql))
    print()
    _print_report(res.report)
    if res.result is not None:
        print()
        print(bold("Result from the real database") + f"  ({'; '.join(res.result.notes)})")
        print(indent(table(res.result, max_rows=args.limit)))
    elif res.error:
        print(f"\n{paint('error', FAIL)}: {res.error}")
    elif res.report and res.report.passed:
        print("\nApproved. Not run on the real database (--no-run).")
    return 0 if res.report and res.report.passed else 1


def _print_report(report) -> None:  # type: ignore[no-untyped-def]
    print(bold("Validation against the shadow database"))
    print(checklist(report))
    print("  " + summary_line(report))


def cmd_validate(args: argparse.Namespace) -> int:
    ghost = _ghost(args)
    sql = sys.stdin.read() if args.sql == "-" else args.sql
    report = ghost.validate(sql)
    result = None
    error = None
    if report.passed and args.run:
        try:
            result = ghost.run_real(report, args.timeout)
        except SourceError as exc:
            error = str(exc)
    if args.json:
        data = report.to_dict()
        data["result"] = result.to_dict() if result else None
        data["error"] = error
        print(json.dumps(data, indent=2, default=str))
        return 0 if report.passed else 1
    _print_report(report)
    if report.final_sql and report.passed:
        print("\n" + bold("Query that would run on the real database"))
        print(indent(report.final_sql))
    if result is not None:
        print("\n" + bold("Result") + f"  ({'; '.join(result.notes)})")
        print(indent(table(result, max_rows=args.limit)))
    if error:
        print(f"\n{paint('error', FAIL)}: {error}")
    return 0 if report.passed else 1


def cmd_shadow(args: argparse.Namespace) -> int:
    ghost = _ghost(args)
    shadows = ghost.build_shadows(force=args.force)
    if args.json:
        print(
            json.dumps(
                {"location": shadows.location, "primary": shadows.info.to_dict(), "alt": shadows.alt_info.to_dict()},
                indent=2,
            )
        )
        return 0
    print(bold("Shadow database") + f"  {shadows.location}")
    print(f"  seed {shadows.info.seed}; perturbed copy uses seed {shadows.alt_info.seed}; an empty copy is kept too")
    print()
    print(f"  {'table':<16}{'real rows':>12}{'shadow rows':>14}{'refused':>10}   constraints")
    for name in ghost.schema.dependency_order():
        t = ghost.schema.tables[name]
        cons = []
        if t.primary_key:
            cons.append("PK")
        if any(c.unique for c in t.columns if not c.primary_key) or t.unique_sets:
            cons.append("UNIQUE")
        if t.foreign_keys:
            cons.append("FK->" + ",".join(sorted({fk.ref_table for fk in t.foreign_keys})))
        if any(c.check_values or c.check_min is not None or c.check_max is not None for c in t.columns) or t.checks:
            cons.append("CHECK")
        nn = sum(1 for c in t.columns if not c.nullable)
        cons.append(f"{nn} NOT NULL")
        print(
            f"  {name:<16}{ghost.profile.row_count(name):>12,}{shadows.info.rows.get(name, 0):>14,}"
            f"{shadows.info.rejected.get(name, 0):>10}   {' '.join(cons)}"
        )
    print()
    print("  'refused' counts candidate rows the shadow database rejected (CHECK, UNIQUE or FK) and regenerated.")
    print("  No real row was copied. Run `sqlghost profile` to see the only data that left the source.")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    ghost = _ghost(args)
    print(json.dumps(ghost.profile.to_dict(), indent=2, default=str))
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    ghost = _ghost(args)
    if args.json:
        from dataclasses import asdict

        print(json.dumps(asdict(ghost.schema), indent=2, default=str))
        return 0
    for name in ghost.schema.dependency_order():
        t = ghost.schema.tables[name]
        print(bold(name) + f"  ({ghost.profile.row_count(name):,} rows)")
        for c in t.columns:
            flags = []
            if c.primary_key:
                flags.append("PK")
            if not c.nullable and not c.primary_key:
                flags.append("NOT NULL")
            if c.unique and not c.primary_key:
                flags.append("UNIQUE")
            for fk in t.foreign_keys:
                if c.name in fk.columns:
                    flags.append(f"-> {fk.ref_table}.{fk.ref_columns[fk.columns.index(c.name)]}")
            if c.check_values:
                flags.append("IN (" + ", ".join(map(str, c.check_values)) + ")")
            print(f"  {c.name:<18}{c.type:<12}{' '.join(flags)}")
        print()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import BAD_CANDIDATES, QUESTIONS, run_demo

    ghost = Ghost(args.db, seed=args.seed)
    passed, blocked = run_demo(ghost)
    return 0 if passed == len(QUESTIONS) and blocked == len(BAD_CANDIDATES) else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(_ghost(args), host=args.host, port=args.port, provider=args.provider)
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    from . import sample

    path = sample.build(Path(args.out))
    print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sqlghost", description=__doc__)
    parser.add_argument("--version", action="version", version=f"sql-ghost {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ask", help="translate a question, validate it on the shadow, then run it")
    _common(p)
    p.add_argument("question")
    p.add_argument("--provider", default="auto", help="auto, rules, anthropic or openai (default auto)")
    p.add_argument("--no-run", action="store_true", help="validate only; never touch the real database")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("validate", help="validate a SQL query against the shadow ('-' reads stdin)")
    _common(p)
    p.add_argument("sql")
    p.add_argument("--run", action="store_true", help="run on the real database if it passes")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("shadow", help="build the synthetic shadow database and show a summary")
    _common(p)
    p.add_argument("--force", action="store_true", help="rebuild even if a cached shadow exists")
    p.set_defaults(func=cmd_shadow)

    p = sub.add_parser("profile", help="print the statistical profile, the only data read from the source")
    _common(p)
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("schema", help="print the introspected schema")
    _common(p)
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("demo", help="offline end to end demo on the bundled sample database")
    p.add_argument("--db", help=argparse.SUPPRESS)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("serve", help="local web UI")
    _common(p)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--provider", default="auto")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("sample", help="regenerate the sample toll road database")
    p.add_argument("out", nargs="?", default="tolls.sqlite")
    p.set_defaults(func=cmd_sample)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
