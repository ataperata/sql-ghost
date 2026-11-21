"""Ties the pieces together: source, profile, shadow, translator, validator."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import sample
from .db import PostgresSource, QueryResult, QueryTimeout, SourceError, open_source
from .model import Profile, Schema
from .nl import Translation, TranslationError, make_translator
from .profiler import profile_source
from .shadow import ShadowSet, build_postgres_shadows, build_sqlite_shadows
from .validate import Report, Validator, ValidatorOptions


def cache_dir() -> Path:
    return Path(os.environ.get("SQLGHOST_CACHE", Path.home() / ".cache" / "sqlghost"))


def sample_db() -> Path:
    """Path to the bundled toll road database, rebuilding it if it is missing."""
    bundled = sample.default_path()
    if bundled.exists():
        return bundled
    target = cache_dir() / "tolls.sqlite"
    if not target.exists():
        sample.build(target)
    return target


def default_target() -> str:
    return os.environ.get("SQLGHOST_DB") or str(sample_db())


@dataclass
class AskResult:
    question: str
    translation: Translation | None = None
    report: Report | None = None
    result: QueryResult | None = None
    error: str | None = None
    suggestions: list[str] = field(default_factory=list)
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "engine": self.translation.engine if self.translation else None,
            "note": self.translation.note if self.translation else None,
            "sql": self.translation.sql if self.translation else None,
            "report": self.report.to_dict() if self.report else None,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
            "suggestions": self.suggestions,
            "attempts": self.attempts,
        }


class Ghost:
    def __init__(
        self,
        target: str | None = None,
        shadow_url: str | None = None,
        seed: int = 7,
        rows_cap: int = 2000,
        options: ValidatorOptions | None = None,
        cache: Path | None = None,
    ):
        self.target = target or default_target()
        self.source = open_source(self.target)
        self.schema: Schema = self.source.introspect()
        self.profile: Profile = profile_source(self.source, self.schema)
        self.shadow_url = shadow_url
        self.seed = seed
        self.rows_cap = rows_cap
        self.options = options or ValidatorOptions()
        self.cache = cache or cache_dir()
        self._shadows: ShadowSet | None = None
        self._validator: Validator | None = None

    @property
    def dialect(self) -> str:
        return self.schema.dialect

    def build_shadows(self, force: bool = False) -> ShadowSet:
        if isinstance(self.source, PostgresSource):
            if not self.shadow_url:
                raise SourceError(
                    "Postgres sources need --shadow-url: a database where sql-ghost may create "
                    "its shadow schemas. Use a scratch database, not production."
                )
            self._shadows = build_postgres_shadows(
                self.schema, self.profile, self.shadow_url, self.seed, self.rows_cap, self.source.schema_name
            )
        else:
            self._shadows = build_sqlite_shadows(
                self.schema, self.profile, self.cache, self.seed, self.rows_cap, force=force
            )
        self._validator = None
        return self._shadows

    @property
    def shadows(self) -> ShadowSet:
        if self._shadows is None:
            self.build_shadows()
        assert self._shadows is not None
        return self._shadows

    @property
    def validator(self) -> Validator:
        if self._validator is None:
            self._validator = Validator(self.schema, self.profile, self.shadows, self.source, self.options)
        return self._validator

    def validate(self, sql: str) -> Report:
        return self.validator.validate(sql)

    def run_real(self, report: Report, timeout: float = 5.0) -> QueryResult:
        if not report.passed or not report.final_sql:
            raise SourceError("refusing to run a query that did not pass validation")
        result = self.source.run_readonly(report.final_sql, limit=self.options.limit, timeout=timeout)
        result.notes.append(f"read-only session, LIMIT {self.options.limit}, timeout {timeout:.0f}s")
        return result

    def ask(
        self, question: str, provider: str = "auto", run: bool = True, timeout: float = 5.0, retries: int = 1
    ) -> AskResult:
        out = AskResult(question=question)
        try:
            translator = make_translator(provider, self.schema, self.profile)
        except TranslationError as exc:
            out.error = str(exc)
            return out
        feedback = None
        for _ in range(retries + 1):
            out.attempts += 1
            try:
                out.translation = translator.translate(question, self.schema, self.profile, feedback)
            except TranslationError as exc:
                out.error = str(exc)
                out.suggestions = exc.suggestions
                return out
            out.report = self.validate(out.translation.sql)
            if out.report.passed or translator.name == "rules":
                break
            feedback = "\n".join(f"- {c.title}: {c.detail}" for c in out.report.failures)
        if out.report and out.report.passed and run:
            try:
                out.result = self.run_real(out.report, timeout)
            except QueryTimeout as exc:
                out.error = f"real database: {exc}"
            except SourceError as exc:
                out.error = f"real database: {exc}"
        return out
