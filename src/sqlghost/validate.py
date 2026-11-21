"""Validate candidate SQL against the shadow database.

Every check returns PASS, WARN, FAIL or SKIP. A candidate is approved when no
check fails. Nothing in this module touches the real database except an
optional Postgres EXPLAIN, which plans a query without running it and is only
issued after the read-only guard has passed.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify

from .db import QueryResult, QueryTimeout, SourceError
from .guard import check_read_only, parse_one_statement
from .model import Profile, Schema
from .shadow import ShadowSet

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"

NONDETERMINISTIC = {
    "random",
    "randomblob",
    "now",
    "current_timestamp",
    "current_date",
    "current_time",
    "clock_timestamp",
    "statement_timestamp",
    "timeofday",
    "random_uuid",
    "gen_random_uuid",
    "uuid",
    "rand",
}

CHECK_TITLES = {
    "parse": "Parses as valid SQL",
    "read_only": "Read-only (single SELECT, no writes, DDL or PRAGMA)",
    "schema": "Tables and columns exist",
    "values": "Literal values exist in the data",
    "shadow_run": "Runs on the shadow database",
    "cost": "Query plan cost is acceptable",
    "row_count": "Row count is sane",
    "determinism": "Results are deterministic",
    "sensitivity": "Result depends on the data (perturbation test)",
    "empty_db": "Behaves on empty tables",
    "monotonic": "Deleting rows never adds results (metamorphic test)",
}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    @property
    def title(self) -> str:
        return CHECK_TITLES.get(self.name, self.name)

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "title": self.title, "status": self.status, "detail": self.detail}


@dataclass
class Report:
    sql: str
    dialect: str
    checks: list[Check] = field(default_factory=list)
    final_sql: str | None = None
    shadow_result: QueryResult | None = None
    estimated_cost: float | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.status != FAIL for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    def add(self, name: str, status: str, detail: str = "") -> Check:
        check = Check(name, status, detail)
        self.checks.append(check)
        return check

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "final_sql": self.final_sql,
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "shadow_result": self.shadow_result.to_dict() if self.shadow_result else None,
            "estimated_cost": self.estimated_cost,
        }


@dataclass
class ValidatorOptions:
    limit: int = 100
    shadow_timeout: float = 5.0
    cost_warn: float = 5e6  # SQLite: estimated rows visited; Postgres: planner cost units / 10
    cost_fail: float = 5e8
    delete_fraction: float = 0.3
    explain_on_real: bool = True  # Postgres only; plan only, never executes


class Validator:
    def __init__(
        self,
        schema: Schema,
        profile: Profile,
        shadows: ShadowSet,
        source: Any = None,
        options: ValidatorOptions | None = None,
    ):
        self.schema = schema
        self.profile = profile
        self.shadows = shadows
        self.source = source
        self.dialect = schema.dialect
        self.opts = options or ValidatorOptions()
        self.mapping = {t.name: {c.name: "TEXT" for c in t.columns} for t in schema.tables.values()}

    # ------------------------------------------------------------------

    def validate(self, sql: str) -> Report:
        sql = sql.strip()
        report = Report(sql=sql, dialect=self.dialect)

        statements, error = parse_one_statement(sql, self.dialect)
        if error:
            report.add("parse", FAIL, error)
            return self._skip_rest(report)
        report.add("parse", PASS, f"{len(statements)} statement(s), dialect {self.dialect}")

        guard = check_read_only(sql, self.dialect)
        if not guard.ok:
            report.add("read_only", FAIL, "; ".join(guard.reasons))
            return self._skip_rest(report)
        report.add("read_only", PASS, "single read-only query")
        tree = guard.expression
        assert tree is not None

        base_tables, aliases = self._tables(tree)
        qualified = self._check_schema(report, tree, base_tables)
        if qualified is None:
            return self._skip_rest(report)

        self._check_values(report, qualified)
        cartesian = self._cartesian(qualified)

        shadow_sql = self._shadow_sql(tree)
        report.final_sql = apply_limit(tree, self.opts.limit, self.dialect)
        try:
            result = self.shadows.primary.run(shadow_sql, limit=5000, timeout=self.opts.shadow_timeout)
            self.shadows.primary.run(self._shadow_sql_text(report.final_sql), limit=1, timeout=self.opts.shadow_timeout)
        except QueryTimeout as exc:
            report.add("shadow_run", FAIL, f"{exc} on a {self._shadow_size()} row shadow")
            return self._skip_rest(report)
        except SourceError as exc:
            report.add("shadow_run", FAIL, f"shadow rejected the query: {exc}")
            return self._skip_rest(report)
        report.shadow_result = result
        report.add("shadow_run", PASS, f"{len(result.rows)} row(s) in {result.elapsed_ms:.1f} ms on synthetic data")

        self._check_cost(report, shadow_sql, base_tables, aliases, cartesian)
        self._check_row_count(report, tree, result, base_tables)
        self._check_determinism(report, tree, shadow_sql, result)
        self._check_sensitivity(report, tree, shadow_sql, result)
        self._check_empty(report, tree, shadow_sql)
        self._check_monotonic(report, tree, shadow_sql, result, base_tables)
        return report

    def _skip_rest(self, report: Report) -> Report:
        done = {c.name for c in report.checks}
        for name in CHECK_TITLES:
            if name not in done:
                report.add(name, SKIP, "not run because an earlier check failed")
        return report

    def _shadow_size(self) -> int:
        return sum(self.shadows.info.rows.values())

    # -- helpers ---------------------------------------------------------

    def _tables(self, tree: exp.Expression) -> tuple[list[str], dict[str, str]]:
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        tables: list[str] = []
        aliases: dict[str, str] = {}
        for t in tree.find_all(exp.Table):
            name = t.name
            if not name or name.lower() in ctes:
                continue
            if name not in tables:
                tables.append(name)
            aliases[(t.alias_or_name or name).lower()] = name
        return tables, aliases

    def _shadow_sql(self, tree: exp.Expression) -> str:
        if not any(t.args.get("db") for t in tree.find_all(exp.Table)):
            return tree.sql(dialect=self.dialect)
        stripped = tree.copy()
        for t in stripped.find_all(exp.Table):
            t.set("db", None)
        return stripped.sql(dialect=self.dialect)

    def _shadow_sql_text(self, sql: str) -> str:
        statements, _ = parse_one_statement(sql, self.dialect)
        return self._shadow_sql(statements[0]) if statements else sql

    # -- checks ----------------------------------------------------------

    def _check_schema(self, report: Report, tree: exp.Expression, tables: list[str]) -> exp.Expression | None:
        unknown = [t for t in tables if not self.schema.has_table(t)]
        if unknown:
            known = ", ".join(sorted(self.schema.tables))
            report.add("schema", FAIL, f"unknown table(s): {', '.join(unknown)}. Known tables: {known}")
            return None
        try:
            qualified = qualify(
                tree.copy(),
                schema=self.mapping,
                dialect=self.dialect,
                validate_qualify_columns=True,
                quote_identifiers=False,
            )
        except OptimizeError as exc:
            report.add("schema", FAIL, str(exc).split(". Line")[0])
            return None
        except SqlglotError as exc:  # unusual syntax the resolver cannot follow
            report.add("schema", WARN, f"could not fully resolve columns: {exc}")
            return tree
        report.add("schema", PASS, f"tables: {', '.join(tables) or '(none)'}")
        return qualified

    def _alias_map(self, tree: exp.Expression) -> dict[str, str]:
        out = {}
        for t in tree.find_all(exp.Table):
            out[(t.alias_or_name or t.name).lower()] = t.name
        return out

    def _check_values(self, report: Report, qualified: exp.Expression) -> None:
        aliases = self._alias_map(qualified)
        problems: list[str] = []
        hard = False
        checked = 0
        for node in qualified.find_all(exp.EQ, exp.NEQ, exp.In):
            col, literals = _column_and_literals(node)
            if col is None or not literals:
                continue
            table = aliases.get((col.table or "").lower())
            if not table:
                continue
            prof = self.profile.column(table, col.name)
            try:
                schema_col = self.schema.table(table).column(col.name)
            except KeyError:
                continue
            if schema_col.check_values or schema_col.enum_values:
                known = [str(v) for v in (schema_col.check_values or schema_col.enum_values or [])]
                complete = True
            elif prof and prof.categories:
                known = [str(c[0]) for c in prof.categories]
                complete = prof.categories_complete
            else:
                continue
            for lit in literals:
                checked += 1
                if lit in known:
                    continue
                close = [k for k in known if k.lower() == lit.lower()]
                hint = f" (did you mean '{close[0]}'?)" if close else f" (known: {', '.join(known[:8])})"
                problems.append(f"{table}.{col.name} never equals '{lit}'{hint}")
                hard = hard or complete
        if problems:
            report.add("values", FAIL if hard else WARN, "; ".join(problems))
        elif checked:
            report.add("values", PASS, f"{checked} literal(s) match known values")
        else:
            report.add("values", PASS, "no categorical literals to check")

    def _cartesian(self, qualified: exp.Expression) -> list[str]:
        """Base tables in one SELECT that no join predicate connects."""
        issues: list[str] = []
        for select in qualified.find_all(exp.Select):
            sources: dict[str, str] = {}
            from_ = select.args.get("from_") or select.args.get("from")
            items = []
            if from_ is not None:
                items.append(from_.this)
            items.extend(j.this for j in select.args.get("joins") or [])
            for item in items:
                if isinstance(item, exp.Table):
                    sources[(item.alias_or_name or item.name).lower()] = item.name
            if len(sources) < 2:
                continue
            parent = {a: a for a in sources}

            def find(a: str, parent: dict[str, str] = parent) -> str:
                while parent[a] != a:
                    a = parent[a]
                return a

            preds: list[exp.Expression] = []
            where = select.args.get("where")
            if where is not None:
                preds.append(where)
            for j in select.args.get("joins") or []:
                if j.args.get("on") is not None:
                    preds.append(j.args["on"])
                if j.args.get("using"):
                    preds.append(j)
            for pred in preds:
                for eq in pred.find_all(exp.EQ):
                    refs = {c.table.lower() for c in eq.find_all(exp.Column) if c.table}
                    refs &= set(sources)
                    refs_list = sorted(refs)
                    for other in refs_list[1:]:
                        parent[find(other)] = find(refs_list[0])
            groups = {find(a) for a in sources}
            if len(groups) > 1:
                names = sorted({sources[a] for a in sources})
                issues.append(" x ".join(names))
        return issues

    def _check_cost(
        self, report: Report, shadow_sql: str, tables: list[str], aliases: dict[str, str], cartesian: list[str]
    ) -> None:
        if self.dialect == "postgres":
            self._check_cost_postgres(report, shadow_sql, cartesian)
            return
        try:
            plan = self.shadows.primary.explain(shadow_sql)
        except Exception as exc:
            report.add("cost", WARN, f"EXPLAIN failed: {exc}")
            return
        cost, steps = estimate_sqlite_cost(plan, aliases, self.profile)
        report.estimated_cost = cost
        plan_text = "; ".join(steps[:4])
        if cartesian:
            report.add(
                "cost",
                FAIL,
                f"cartesian product with no join condition: {', '.join(cartesian)} "
                f"(about {_human(cost)} row visits on real data)",
            )
        elif cost > self.opts.cost_fail:
            report.add("cost", FAIL, f"about {_human(cost)} row visits on real data. Plan: {plan_text}")
        elif cost > self.opts.cost_warn:
            report.add("cost", WARN, f"about {_human(cost)} row visits on real data. Plan: {plan_text}")
        else:
            report.add("cost", PASS, f"about {_human(cost)} row visits on real data. Plan: {plan_text}")

    def _check_cost_postgres(self, report: Report, shadow_sql: str, cartesian: list[str]) -> None:
        where = "real database planner (EXPLAIN only, nothing executed)"
        try:
            if self.opts.explain_on_real and self.source is not None:
                plan = self.source.explain(report.sql)
            else:
                plan = self.shadows.primary.explain(shadow_sql)
                where = "shadow planner"
        except Exception as exc:
            report.add("cost", WARN, f"EXPLAIN failed: {exc}")
            return
        top = plan[0]["Plan"] if isinstance(plan, list) else plan["Plan"]
        cost = float(top.get("Total Cost", 0.0))
        report.estimated_cost = cost
        detail = f"planner cost {cost:,.0f} from the {where}, top node {top.get('Node Type')}"
        warn, fail = self.opts.cost_warn / 10, self.opts.cost_fail / 10
        if cartesian:
            report.add("cost", FAIL, f"cartesian product with no join condition: {', '.join(cartesian)}; {detail}")
        elif cost > fail:
            report.add("cost", FAIL, detail)
        elif cost > warn:
            report.add("cost", WARN, detail)
        else:
            report.add("cost", PASS, detail)

    def _check_row_count(self, report: Report, tree: exp.Expression, result: QueryResult, tables: list[str]) -> None:
        n = len(result.rows)
        if is_ungrouped_aggregate(tree):
            if n == 1:
                report.add("row_count", PASS, "aggregate without GROUP BY returned exactly one row")
            else:
                report.add("row_count", FAIL, f"aggregate without GROUP BY returned {n} rows, expected 1")
            return
        if n == 0:
            report.add("row_count", WARN, "no rows on the shadow; a filter may never match real data")
            return
        scales = [self.shadows.info.scale.get(self._real_name(t), 1.0) for t in tables]
        scale = min(scales) if scales else 1.0
        grouped = tree.find(exp.Group) is not None or tree.find(exp.Distinct) is not None
        estimate = n if grouped else math.ceil(n / scale) if scale else n
        if not grouped and has_limit(tree) is None and estimate > self.opts.limit:
            report.add(
                "row_count",
                WARN,
                f"{n} shadow rows suggests about {estimate:,} real rows; output will be capped "
                f"at LIMIT {self.opts.limit}",
            )
            return
        report.add("row_count", PASS, f"{n} shadow row(s); about {estimate:,} expected on real data")

    def _real_name(self, name: str) -> str:
        for t in self.schema.tables:
            if t.lower() == name.lower():
                return t
        return name

    def _check_determinism(self, report: Report, tree: exp.Expression, sql: str, first: QueryResult) -> None:
        funcs = {(f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower() for f in tree.find_all(exp.Func)}
        funcs |= {type(n).__name__.lower() for n in tree.find_all(exp.CurrentTimestamp, exp.CurrentDate)}
        volatile = sorted(funcs & NONDETERMINISTIC)
        if volatile:
            report.add("determinism", WARN, f"uses non-deterministic function(s): {', '.join(volatile)}")
            return
        limit_node = has_limit(tree)
        if limit_node is not None and not top_order(tree):
            report.add("determinism", WARN, "LIMIT without ORDER BY returns an arbitrary subset of rows")
            return
        second = self.shadows.primary.run(sql, limit=5000, timeout=self.opts.shadow_timeout)
        if not same_result(first, second, ordered=bool(top_order(tree))):
            report.add("determinism", WARN, "two runs on the same data returned different results")
            return
        report.add("determinism", PASS, "identical results on repeated runs")

    def _check_sensitivity(self, report: Report, tree: exp.Expression, sql: str, first: QueryResult) -> None:
        try:
            alt = self.shadows.alt.run(sql, limit=5000, timeout=self.opts.shadow_timeout)
        except SourceError as exc:
            report.add("sensitivity", WARN, f"failed on the perturbed shadow: {exc}")
            return
        if not list(tree.find_all(exp.Table)):
            report.add("sensitivity", WARN, "query reads no tables; result is a constant")
            return
        if same_result(first, alt, ordered=False):
            empty = not first.rows or all(all(v in (None, 0) for v in r) for r in first.rows)
            msg = "same result on two independently generated datasets"
            if empty:
                report.add("sensitivity", WARN, msg + " and it is empty or zero; filters probably never match")
            else:
                report.add("sensitivity", WARN, msg + "; the result may not depend on the data")
            return
        report.add("sensitivity", PASS, "result changed when the shadow data was regenerated with another seed")

    def _check_empty(self, report: Report, tree: exp.Expression, sql: str) -> None:
        try:
            res = self.shadows.empty.run(sql, limit=50, timeout=self.opts.shadow_timeout)
        except SourceError as exc:
            report.add("empty_db", FAIL, f"errors on empty tables: {exc}")
            return
        if is_ungrouped_aggregate(tree):
            if len(res.rows) == 1:
                report.add("empty_db", PASS, f"one row on empty tables: {list(res.rows[0])}")
            else:
                report.add("empty_db", WARN, f"{len(res.rows)} rows on empty tables, expected 1")
            return
        if res.rows and not tree.find(exp.Union):
            report.add("empty_db", WARN, f"returns {len(res.rows)} row(s) even when every table is empty")
            return
        report.add("empty_db", PASS, "no rows when the tables are empty")

    def _check_monotonic(
        self, report: Report, tree: exp.Expression, sql: str, full: QueryResult, tables: list[str]
    ) -> None:
        kind = monotone_kind(tree)
        if kind is None:
            report.add(
                "monotonic",
                SKIP,
                "query uses aggregates other than COUNT, negation, outer joins "
                "or LIMIT, so deleting rows may legitimately change it",
            )
            return
        try:
            reduced = self.shadows.primary.run_after_deleting(
                sql, tables, self.opts.delete_fraction, seed=11, limit=5000, timeout=self.opts.shadow_timeout
            )
        except SourceError as exc:
            report.add("monotonic", SKIP, f"could not perturb shadow: {exc}")
            return
        if kind == "rows":
            missing = Counter(_norm_rows(reduced.rows)) - Counter(_norm_rows(full.rows))
            if missing:
                report.add("monotonic", FAIL, f"{sum(missing.values())} row(s) appeared after deleting data")
            else:
                report.add(
                    "monotonic",
                    PASS,
                    f"after deleting {int(self.opts.delete_fraction * 100)}% of rows "
                    f"the result shrank from {len(full.rows)} to {len(reduced.rows)} rows "
                    "and gained none",
                )
            return
        # grouped COUNT: every group in the reduced result must have a count <= the original
        ok, detail = _counts_shrink(full, reduced)
        report.add("monotonic", PASS if ok else FAIL, detail)


# ---------------------------------------------------------------------------
# Static helpers


def _column_and_literals(node: exp.Expression) -> tuple[exp.Column | None, list[str]]:
    if isinstance(node, exp.In):
        col = node.this if isinstance(node.this, exp.Column) else None
        lits = [e.this for e in node.expressions if isinstance(e, exp.Literal) and e.is_string]
        return col, lits
    left, right = node.this, node.expression
    if isinstance(left, exp.Column) and isinstance(right, exp.Literal) and right.is_string:
        return left, [right.this]
    if isinstance(right, exp.Column) and isinstance(left, exp.Literal) and left.is_string:
        return right, [left.this]
    return None, []


def _top_select(tree: exp.Expression) -> exp.Select | None:
    root = tree.unnest() if isinstance(tree, (exp.Subquery, exp.Paren)) else tree
    return root if isinstance(root, exp.Select) else None


def is_ungrouped_aggregate(tree: exp.Expression) -> bool:
    select = _top_select(tree)
    if select is None or select.args.get("group"):
        return False
    has_agg = False
    for proj in select.expressions:
        aggs = [a for a in proj.find_all(exp.AggFunc) if not a.find_ancestor(exp.Subquery, exp.Window)]
        if aggs:
            has_agg = True
        elif not isinstance(proj.unalias(), exp.Literal) and proj.find(exp.Column):
            return False
    return has_agg


def has_limit(tree: exp.Expression) -> exp.Expression | None:
    root = tree.unnest() if isinstance(tree, (exp.Subquery, exp.Paren)) else tree
    return root.args.get("limit")


def top_order(tree: exp.Expression) -> exp.Expression | None:
    root = tree.unnest() if isinstance(tree, (exp.Subquery, exp.Paren)) else tree
    return root.args.get("order")


def monotone_kind(tree: exp.Expression) -> str | None:
    """'rows' for plain select-project-join queries, 'count' for grouped COUNTs."""
    select = _top_select(tree)
    if select is None or select.args.get("limit") or select.args.get("offset"):
        return None
    if any(isinstance(n, (exp.Not, exp.Except, exp.Window, exp.Having, exp.Case, exp.NEQ)) for n in tree.walk()):
        return None
    for j in tree.find_all(exp.Join):
        if (j.args.get("side") or "").upper() in ("LEFT", "RIGHT", "FULL"):
            return None
    if list(tree.find_all(exp.Subquery)) or list(tree.find_all(exp.CTE)):
        return None
    aggs = list(tree.find_all(exp.AggFunc))
    if not aggs:
        return "rows"
    if all(isinstance(a, exp.Count) for a in aggs) and select.args.get("group"):
        return "count"
    return None


def _norm(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    return value


def _norm_rows(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [tuple(_norm(v) for v in r) for r in rows]


def same_result(a: QueryResult, b: QueryResult, ordered: bool) -> bool:
    ra, rb = _norm_rows(a.rows), _norm_rows(b.rows)
    if ordered:
        return ra == rb
    return Counter(ra) == Counter(rb)


def _counts_shrink(full: QueryResult, reduced: QueryResult) -> tuple[bool, str]:
    def split(res: QueryResult) -> dict[tuple[Any, ...], tuple[Any, ...]]:
        out = {}
        for row in res.rows:
            key = tuple(v for v in row if not isinstance(v, int) or isinstance(v, bool))
            nums = tuple(v for v in row if isinstance(v, int) and not isinstance(v, bool))
            out[key] = nums
        return out

    before, after = split(full), split(reduced)
    for key, nums in after.items():
        if key not in before:
            return False, f"group {key} appeared only after deleting rows"
        if any(x > y for x, y in zip(nums, before[key], strict=False)):
            return False, f"count for group {key} grew after deleting rows"
    return True, f"after deleting rows, {len(after)} group(s) remained and no count grew"


def apply_limit(tree: exp.Expression, limit: int, dialect: str) -> str:
    root = tree.unnest() if isinstance(tree, (exp.Subquery, exp.Paren)) else tree
    if isinstance(root, exp.Select):
        current = root.args.get("limit")
        value = current.expression if current is not None else None
        if isinstance(value, exp.Literal) and not value.is_string and int(value.this) <= limit:
            return root.sql(dialect=dialect)
        return root.copy().limit(limit).sql(dialect=dialect)
    return f"SELECT * FROM ({root.sql(dialect=dialect)}) AS sqlghost_q LIMIT {limit}"


def estimate_sqlite_cost(
    plan: list[tuple[Any, ...]], aliases: dict[str, str], profile: Profile
) -> tuple[float, list[str]]:
    """Rough count of row visits on the real data, from EXPLAIN QUERY PLAN.

    SQLite runs joins as nested loops, so the visits inside one block multiply.
    Independent blocks (materialized CTEs, uncorrelated subqueries) add up.
    A full SCAN costs the real row count; an index SEARCH costs a small fan-out.
    """
    nodes = {row[0]: (row[1], str(row[3])) for row in plan}
    blocks: dict[int, float] = {}
    steps = []
    for _node_id, (parent, detail) in nodes.items():
        upper = detail.upper()
        if not (upper.startswith("SCAN") or upper.startswith("SEARCH")):
            continue
        if "CONSTANT ROW" in upper:
            continue
        steps.append(detail)
        words = detail.split()
        target = words[1] if len(words) > 1 else ""
        table = aliases.get(target.lower())
        rows = profile.row_count(table) if table else 100
        factor = float(rows if upper.startswith("SCAN") else _search_fanout(detail, table, rows, profile))
        block = 0
        p = parent
        while p in nodes:
            pdetail = nodes[p][1].upper()
            if (
                "SUBQUERY" in pdetail or "MATERIALIZE" in pdetail or "CO-ROUTINE" in pdetail
            ) and "CORRELATED" not in pdetail:
                block = p
                break
            p = nodes[p][0]
        blocks[block] = blocks.get(block, 1.0) * max(factor, 1.0)
    return (sum(blocks.values()) if blocks else 1.0), steps


def _search_fanout(detail: str, table: str | None, rows: int, profile: Profile) -> float:
    """Rows visited per lookup for a SEARCH step, using distinct counts from the profile."""
    upper = detail.upper()
    if "PRIMARY KEY" in upper or "(ROWID=" in upper:
        return 1.0
    match = re.search(r"\((.*)\)\s*$", detail)
    if not match or not table:
        return float(min(rows, 20))
    fanout = float(rows)
    for term in match.group(1).split(" AND "):
        eq = re.match(r"\s*(\w+)\s*=\s*\?", term)
        if eq:
            prof = profile.column(table, eq.group(1))
            distinct = prof.n_distinct if prof and prof.n_distinct else 10
            fanout /= max(distinct, 1)
        elif re.search(r"[<>]", term):
            fanout /= 4
    return max(fanout, 1.0)


def _human(n: float) -> str:
    for unit, size in (("billion", 1e9), ("million", 1e6), ("thousand", 1e3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n:.0f}"
