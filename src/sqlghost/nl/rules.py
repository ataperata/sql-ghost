"""Offline, deterministic natural language to SQL.

This engine needs no API key. It matches a question against a set of templates
and fills them using the schema: table names, column names, foreign key paths
and the categorical values in the profile. Most templates are generic and work
on any schema ("how many X by Y", "average X by Y", "top N X by Y", "which X
have more than N Y"). A few extra templates know the bundled toll road sample.

It is deliberately literal. Ask it to "delete" something and it writes a
DELETE, because the point of sql-ghost is that the validator, not the
translator, decides what reaches the database.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import sqlglot

from .. import model as m
from ..model import Profile, Schema, Table
from .base import Translation, TranslationError

MONTHS = {
    name: i
    for i, name in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})

SYNONYMS = {
    "transactions": "trips",
    "transaction": "trips",
    "crossings": "trips",
    "crossing": "trips",
    "customers": "accounts",
    "customer": "accounts",
    "cars": "vehicles",
    "car": "vehicles",
    "toll points": "plazas",
    "gantries": "plazas",
    "gantry": "plazas",
    "plaza": "plazas",
    "traffic": "trips",
    "users": "accounts",
}
STRIP_SUFFIXES = ("_id", "_cents", "_at", "_on", "_count")

EXAMPLES = [
    "How many trips were there in March 2026?",
    "Total revenue by corridor",
    "Top 5 plazas by number of trips",
    "What is the busiest hour of the day?",
    "Average toll by vehicle class",
    "Which accounts have more than 3 disputed trips?",
    "How many plate ocr trips were on the Harbor Tunnel?",
    "Show suspended accounts with a negative balance",
    "Dispute rate by plaza",
    "Monthly revenue",
    "How many accounts by account type",
]


@dataclass
class Hit:
    table: str
    column: str
    value: Any = None
    hops: int = 0
    span: tuple[int, int] = (0, 0)


@dataclass
class Query:
    base: str
    select: list[str] = field(default_factory=list)
    joins: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    group: list[str] = field(default_factory=list)
    having: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    limit: int | None = None
    joined: set[str] = field(default_factory=set)

    def sql(self) -> str:
        parts = [f"SELECT {', '.join(self.select)}", f"FROM {self.base}"]
        parts += self.joins
        if self.where:
            parts.append("WHERE " + " AND ".join(self.where))
        if self.group:
            parts.append("GROUP BY " + ", ".join(self.group))
        if self.having:
            parts.append("HAVING " + " AND ".join(self.having))
        if self.order:
            parts.append("ORDER BY " + ", ".join(self.order))
        if self.limit is not None:
            parts.append(f"LIMIT {self.limit}")
        return "\n".join(parts)


def _lit(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


class RuleEngine:
    name = "rules"

    def __init__(self, schema: Schema, profile: Profile):
        self.schema = schema
        self.profile = profile
        self.parents: dict[str, list[tuple[str, str, str]]] = {}  # child -> [(parent, child_col, parent_col)]
        for t in schema.tables.values():
            for fk in t.foreign_keys:
                if len(fk.columns) == 1:
                    parent = self._real(fk.ref_table)
                    self.parents.setdefault(t.name, []).append((parent, fk.columns[0], fk.ref_columns[0]))
        self.values = self._value_index()

    # ------------------------------------------------------------------ setup

    def _real(self, name: str) -> str:
        for t in self.schema.tables:
            if t.lower() == name.lower():
                return t
        return name

    def _value_index(self) -> list[tuple[str, str, str, Any]]:
        """(phrase, table, column, value) for every known categorical text value."""
        out = []
        for t in self.schema.tables.values():
            for col in t.columns:
                values: list[Any] = list(col.check_values or col.enum_values or [])
                prof = self.profile.column(t.name, col.name)
                if not values and prof and prof.categories and col.kind == m.TEXT:
                    values = [c[0] for c in prof.categories]
                for v in values:
                    if isinstance(v, str) and len(v) > 1:
                        out.append((v.lower().replace("_", " "), t.name, col.name, v))
        out.sort(key=lambda x: -len(x[0]))
        return out

    def _phrases(self, col: str) -> list[str]:
        name = col.lower()
        phrases = {name.replace("_", " ")}
        for suf in STRIP_SUFFIXES:
            if name.endswith(suf) and len(name) > len(suf):
                phrases.add(name[: -len(suf)].replace("_", " "))
        parts = name.split("_")
        if len(parts) > 1 and parts[-1] not in ("id", "cents", "at", "on"):
            phrases.add(parts[-1])
        return sorted(phrases, key=len, reverse=True)

    def _reachable(self, start: str) -> dict[str, tuple[int, list[tuple[str, str, str, str]]]]:
        """Tables reachable by following foreign keys upward: name -> (hops, join path)."""
        seen = {start: (0, [])}
        queue = deque([start])
        while queue:
            cur = queue.popleft()
            hops, path = seen[cur]
            for parent, ccol, pcol in self.parents.get(cur, []):
                if parent not in seen:
                    seen[parent] = (hops + 1, [*path, (cur, ccol, parent, pcol)])
                    queue.append(parent)
        return seen

    def _children(self, parent: str) -> list[tuple[str, str, str]]:
        return [(child, ccol, pcol) for child, plist in self.parents.items() for p, ccol, pcol in plist if p == parent]

    # ---------------------------------------------------------------- matching

    def _find_tables(self, text: str) -> list[tuple[int, str]]:
        found = []
        for syn, target in SYNONYMS.items():
            for mt in re.finditer(rf"\b{re.escape(syn)}\b", text):
                if self.schema.has_table(target):
                    found.append((mt.start(), self._real(target)))
        for t in self.schema.tables:
            forms = {t.lower(), t.lower().rstrip("s"), t.lower().replace("_", " ")}
            if t.lower().endswith("ies"):
                forms.add(t.lower()[:-3] + "y")
            for form in forms:
                for mt in re.finditer(rf"\b{re.escape(form)}s?\b", text):
                    found.append((mt.start(), t))
        found.sort()
        return found

    def _value_hits(self, text: str, scope: dict[str, Any]) -> list[Hit]:
        hits: list[Hit] = []
        taken: list[tuple[int, int]] = []
        for phrase, table, col, value in self.values:
            if table not in scope:
                continue
            for mt in re.finditer(rf"\b{re.escape(phrase)}\b", text):
                span = mt.span()
                if any(a < span[1] and span[0] < b for a, b in taken):
                    continue
                same = [h for h in hits if h.span == span]
                hops = scope[table][0]
                if same and same[0].hops <= hops:
                    continue
                hits = [h for h in hits if h.span != span]
                hits.append(Hit(table, col, value, hops, span))
        for h in hits:
            taken.append(h.span)
        # one value per column: keep the nearest table
        best: dict[tuple[str, str, Any], Hit] = {}
        for h in hits:
            best.setdefault((h.table, h.column, h.value), h)
        return list(best.values())

    def _column_hit(self, phrase: str, scope: dict[str, Any], kinds: tuple[str, ...] | None = None) -> Hit | None:
        phrase = phrase.strip()
        best: Hit | None = None
        best_score = (-1, 99)
        for table in scope:
            hops = scope[table][0]
            for col in self.schema.tables[table].columns:
                if kinds and col.kind not in kinds:
                    continue
                if col.primary_key:
                    continue
                for p in self._phrases(col.name):
                    if re.search(rf"\b{re.escape(p)}\b", phrase):
                        score = (len(p), -hops)
                        if score > (best_score[0], -best_score[1]):
                            best, best_score = Hit(table, col.name, hops=hops), (len(p), hops)
        return best

    def _date_column(self, table: str) -> str | None:
        for col in self.schema.tables[table].columns:
            if col.kind in (m.DATETIME, m.DATE):
                return col.name
        for col in self.schema.tables[table].columns:
            prof = self.profile.column(table, col.name)
            if prof and prof.pattern in ("iso_date", "iso_datetime"):
                return col.name
        return None

    def _label(self, table: str) -> str:
        t = self.schema.tables[table]
        for pref in ("name", "title", "label", "email", "plate", "code"):
            for col in t.columns:
                if col.name.lower() == pref or col.name.lower().endswith("_" + pref):
                    return col.name
        for col in t.columns:
            if col.kind == m.TEXT and col.unique:
                return col.name
        return t.primary_key[0] if t.primary_key else t.columns[0].name

    def _join(self, q: Query, table: str) -> None:
        if table == q.base or table in q.joined:
            return
        reach = self._reachable(q.base)
        if table not in reach:
            raise TranslationError(f"no foreign key path from {q.base} to {table}")
        for child, ccol, parent, pcol in reach[table][1]:
            if parent in q.joined or parent == q.base:
                continue
            q.joins.append(f"JOIN {parent} ON {parent}.{pcol} = {child}.{ccol}")
            q.joined.add(parent)

    def _apply_filters(self, q: Query, text: str) -> list[str]:
        scope = self._reachable(q.base)
        notes = []
        hits = self._value_hits(text, scope)
        by_col: dict[tuple[str, str], list[Any]] = {}
        for h in hits:
            by_col.setdefault((h.table, h.column), []).append(h.value)
        for (table, col), values in by_col.items():
            self._join(q, table)
            if len(values) == 1:
                q.where.append(f"{table}.{col} = {_lit(values[0])}")
            else:
                q.where.append(f"{table}.{col} IN ({', '.join(_lit(v) for v in values)})")
            notes.append(f"{table}.{col} = {', '.join(map(str, values))}")
        # sign and threshold phrases on numeric columns: "negative balance", "toll over 500"
        for mt in re.finditer(r"\b(negative|positive|zero)\s+([a-z ]+?)(?=$|\b(and|with|in|on|for|by)\b|[,?.])", text):
            hit = self._column_hit(mt.group(2), scope, (m.INTEGER, m.REAL))
            if hit:
                self._join(q, hit.table)
                op = {"negative": "< 0", "positive": "> 0", "zero": "= 0"}[mt.group(1)]
                q.where.append(f"{hit.table}.{hit.column} {op}")
        for mt in re.finditer(r"\b([a-z ]+?)\s+(over|above|greater than|under|below|less than)\s+(\d+)", text):
            hit = self._column_hit(mt.group(1).split()[-1], scope, (m.INTEGER, m.REAL))
            if hit:
                self._join(q, hit.table)
                op = ">" if mt.group(2) in ("over", "above", "greater than") else "<"
                q.where.append(f"{hit.table}.{hit.column} {op} {mt.group(3)}")
        date = self._date_filter(text)
        if date:
            col = self._date_column(q.base)
            if col:
                lo, hi = date
                q.where.append(f"{q.base}.{col} >= '{lo}' AND {q.base}.{col} < '{hi}'")
        return notes

    def _date_filter(self, text: str) -> tuple[str, str] | None:
        mt = re.search(r"\b(?:in|during|for)\s+([a-z]+)\s+(\d{4})\b", text)
        if mt and mt.group(1) in MONTHS:
            year, month = int(mt.group(2)), MONTHS[mt.group(1)]
            nxt = (year + (month == 12), month % 12 + 1)
            return f"{year:04d}-{month:02d}-01", f"{nxt[0]:04d}-{nxt[1]:02d}-01"
        mt = re.search(r"\b(?:in|during)\s+(\d{4})\b", text)
        if mt:
            year = int(mt.group(1))
            return f"{year}-01-01", f"{year + 1}-01-01"
        return None

    def _group(self, q: Query, phrase: str) -> str | None:
        scope = self._reachable(q.base)
        lowered = phrase.strip()
        if lowered in ("month", "monthly"):
            col = self._date_column(q.base)
            if col:
                expr = f"strftime('%Y-%m', {q.base}.{col})"
                q.select.insert(0, f"{expr} AS month")
                q.group.append(expr)
                q.order.append("month")
                return "month"
        for t in scope:
            if re.fullmatch(rf"{re.escape(t.lower().rstrip('s'))}s?", lowered):
                label = self._label(t)
                self._join(q, t)
                q.select.insert(0, f"{t}.{label}")
                q.group.append(f"{t}.{label}")
                return label
        hit = self._column_hit(lowered, scope)
        if not hit:
            return None
        self._join(q, hit.table)
        q.select.insert(0, f"{hit.table}.{hit.column}")
        q.group.append(f"{hit.table}.{hit.column}")
        return hit.column

    # ------------------------------------------------------------------ rules

    def translate(
        self, question: str, schema: Schema | None = None, profile: Profile | None = None, feedback: str | None = None
    ) -> Translation:
        text = re.sub(r"[^\w\s'\-%]", " ", question.lower())
        text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        for rule in (
            self._delete,
            self._domain_hour,
            self._domain_dispute_rate,
            self._revenue,
            self._having,
            self._top,
            self._aggregate,
            self._count,
            self._list,
        ):
            result = rule(text)
            if result:
                sql, note = result
                pretty = sqlglot.transpile(sql, read="sqlite", write=self.schema.dialect, pretty=True)[0]
                return Translation(sql=pretty, engine=self.name, note=note)
        raise TranslationError("the offline engine has no template for that question", EXAMPLES)

    def _first_table(self, text: str) -> str | None:
        found = self._find_tables(text)
        return found[0][1] if found else None

    def _delete(self, text: str) -> tuple[str, str] | None:
        mt = re.match(r"^(delete|remove|purge|drop|wipe|clear out)\b(.*)$", text)
        if not mt:
            return None
        table = self._first_table(mt.group(2))
        if not table:
            return None
        hits = self._value_hits(mt.group(2), {table: (0, [])})
        where = " AND ".join(f"{h.column} = {_lit(h.value)}" for h in hits)
        sql = f"DELETE FROM {table}" + (f" WHERE {where}" if where else "")
        return sql, "literal translation of a write request"

    def _count(self, text: str) -> tuple[str, str] | None:
        mt = re.match(r"^(how many|count( of)?|number of|total number of)\b(.*)$", text)
        if not mt:
            return None
        rest = mt.group(3)
        rest_tables = self._find_tables(rest)
        if not rest_tables:
            return None
        base = rest_tables[0][1]
        by = re.search(r"\b(?:by|per|for each)\s+(.+)$", rest)
        q = Query(base=base, select=[f"COUNT(*) AS {base}"])
        head = rest[: by.start()] if by else rest
        notes = self._apply_filters(q, head)
        if by:
            label = self._group(q, by.group(1))
            if not label:
                return None
            if not q.order:
                q.order.append(f"{base} DESC")
        return q.sql(), "count" + (f" filtered on {'; '.join(notes)}" if notes else "")

    def _aggregate(self, text: str) -> tuple[str, str] | None:
        mt = re.match(
            r"^(?:what is |what's |show )?(?:the )?(average|avg|mean|total|sum of|sum|maximum|max|highest|"
            r"minimum|min|lowest) (.+?)(?:\s+(?:by|per|for each)\s+(.+))?$",
            text,
        )
        if not mt:
            return None
        func = {
            "average": "AVG",
            "avg": "AVG",
            "mean": "AVG",
            "total": "SUM",
            "sum of": "SUM",
            "sum": "SUM",
            "maximum": "MAX",
            "max": "MAX",
            "highest": "MAX",
            "minimum": "MIN",
            "min": "MIN",
            "lowest": "MIN",
        }[mt.group(1)]
        subject = mt.group(2)
        hit = None
        for table in self.schema.tables:
            candidate = self._column_hit(subject, {table: (0, [])}, (m.INTEGER, m.REAL))
            if candidate and (hit is None or len(candidate.column) > len(hit.column)):
                hit = candidate
        if not hit:
            return None
        q = Query(base=hit.table)
        expr = f"{func}({hit.table}.{hit.column})"
        if func == "AVG":
            expr = f"ROUND({expr}, 2)"
        alias = f"{func.lower()}_{hit.column}"
        q.select.append(f"{expr} AS {alias}")
        self._apply_filters(q, subject)
        if mt.group(3):
            if not self._group(q, mt.group(3)):
                return None
            q.select.append("COUNT(*) AS rows_counted")
            if not q.order:
                q.order.append(q.group[0])
        return q.sql(), f"{func} of {hit.table}.{hit.column}"

    def _list(self, text: str) -> tuple[str, str] | None:
        mt = re.match(r"^(show|list|find|which|what|get|give me)\b(.*)$", text)
        if not mt:
            return None
        found = self._find_tables(mt.group(2))
        if not found:
            return None
        base = found[0][1]
        t = self.schema.tables[base]
        q = Query(base=base, select=[f"{base}.*"])
        self._apply_filters(q, mt.group(2))
        if t.primary_key:
            q.order.append(f"{base}.{t.primary_key[0]}")
        q.limit = 50
        return q.sql(), "row listing"

    def _top(self, text: str) -> tuple[str, str] | None:
        mt = re.search(r"\b(top|bottom|busiest|quietest) (\d+ )?([a-z ]+?) by (.+)$", text)
        if not mt:
            return None
        n = int(mt.group(2) or (1 if mt.group(1) in ("busiest", "quietest") else 10))
        entity = self._first_table(mt.group(3))
        if not entity:
            return None
        metric = mt.group(4)
        label = self._label(entity)
        pk = self.schema.tables[entity].primary_key[0]
        child = None
        for c, _ccol, _pcol in self._children(entity) + self._descendants(entity):
            if self._first_table(metric) == c:
                child = c
        if child is None and re.search(r"\b(revenue|traffic|trips)\b", metric) and self.schema.has_table("trips"):
            child = "trips"
        if child is None:
            return None
        q = Query(base=child)
        self._join(q, entity)
        direction = "ASC" if mt.group(1) in ("bottom", "quietest") else "DESC"
        if "revenue" in metric and self._money_column(child):
            money = self._money_column(child)
            q.select = [f"{entity}.{label}", f"ROUND(SUM({child}.{money}) / 100.0, 2) AS revenue"]
            q.order = [f"revenue {direction}"]
            self._posted_filter(q, child)
        else:
            q.select = [f"{entity}.{label}", f"COUNT(*) AS {child}"]
            q.order = [f"{child} {direction}"]
        self._apply_filters(q, metric)
        q.group = [f"{entity}.{pk}", f"{entity}.{label}"]
        q.limit = n
        return q.sql(), f"top {n} {entity}"

    def _descendants(self, table: str) -> list[tuple[str, str, str]]:
        out = []
        for child, ccol, pcol in self._children(table):
            out.extend(self._children(child))
            _ = (ccol, pcol)
        return out

    def _having(self, text: str) -> tuple[str, str] | None:
        mt = re.match(
            r"^(?:which|what|list|show) ([a-z ]+?) (?:have|has|with|had) (more than|over|at least|fewer than|"
            r"less than|exactly) (\d+) (.+)$",
            text,
        )
        if not mt:
            return None
        entity = self._first_table(mt.group(1))
        child = self._first_table(mt.group(4))
        if not entity or not child or entity == child:
            return None
        if entity not in self._reachable(child):
            return None
        op = {"more than": ">", "over": ">", "at least": ">=", "fewer than": "<", "less than": "<", "exactly": "="}[
            mt.group(2)
        ]
        label = self._label(entity)
        pk = self.schema.tables[entity].primary_key[0]
        q = Query(base=child)
        self._join(q, entity)
        self._apply_filters(q, mt.group(4))
        q.select = [f"{entity}.{pk}", f"{entity}.{label}", f"COUNT(*) AS {child}"]
        q.group = [f"{entity}.{pk}", f"{entity}.{label}"]
        q.having = [f"COUNT(*) {op} {mt.group(3)}"]
        q.order = [f"{child} DESC", f"{entity}.{pk}"]
        return q.sql(), f"{entity} grouped by {child} count"

    # -- templates that know the toll road sample ----------------------------

    def _money_column(self, table: str) -> str | None:
        for col in self.schema.tables[table].columns:
            if col.name.endswith("_cents") and col.kind == m.INTEGER:
                return col.name
        return None

    def _posted_filter(self, q: Query, table: str) -> None:
        if table == "trips" and self._has_col("trips", "status"):
            q.where.append("trips.status = 'posted'")

    def _has_col(self, table: str, col: str) -> bool:
        if not self.schema.has_table(table):
            return False
        t: Table = self.schema.table(table)
        return any(c.name == col for c in t.columns)

    def _revenue(self, text: str) -> tuple[str, str] | None:
        if "revenue" not in text or not self._has_col("trips", "toll_cents"):
            return None
        q = Query(base="trips", select=["ROUND(SUM(trips.toll_cents) / 100.0, 2) AS revenue"])
        self._posted_filter(q, "trips")
        if re.search(r"\b(monthly|per month|by month|each month)\b", text):
            self._group(q, "month")
            return q.sql(), "posted toll revenue by month"
        by = re.search(r"\b(?:by|per|for each)\s+(.+)$", text)
        head = text[: by.start()] if by else text
        self._apply_filters(q, head.replace("revenue", ""))
        if by:
            if not self._group(q, by.group(1)):
                return None
            q.order = ["revenue DESC"]
        return q.sql(), "posted toll revenue in dollars"

    def _domain_hour(self, text: str) -> tuple[str, str] | None:
        if not (re.search(r"\b(busiest|peak|quietest)\b.*\bhour\b", text) and self._has_col("trips", "passed_at")):
            return None
        direction = "ASC" if "quietest" in text else "DESC"
        sql = (
            "SELECT CAST(strftime('%H', passed_at) AS INTEGER) AS hour_of_day, COUNT(*) AS trips "
            "FROM trips GROUP BY hour_of_day "
            f"ORDER BY trips {direction}, hour_of_day LIMIT 1"
        )
        return sql, "hour with the most trips"

    def _domain_dispute_rate(self, text: str) -> tuple[str, str] | None:
        if "dispute rate" not in text or not self.schema.has_table("disputes"):
            return None
        by = re.search(r"\b(?:by|per|for each)\s+(.+)$", text)
        q = Query(base="trips")
        q.joins.append("LEFT JOIN disputes ON disputes.trip_id = trips.trip_id")
        q.select = [
            "COUNT(*) AS trips",
            "COUNT(disputes.dispute_id) AS disputes",
            "ROUND(100.0 * COUNT(disputes.dispute_id) / COUNT(*), 2) AS dispute_rate_pct",
        ]
        if by and not self._group(q, by.group(1)):
            return None
        if by:
            q.order = ["dispute_rate_pct DESC"]
        return q.sql(), "share of trips that were disputed"
