"""LLM backed translators for Anthropic and OpenAI.

Only the schema and the profile are sent: table definitions, row counts and
the allowed values of categorical columns. No rows from the real database are
ever put in a prompt. Calls use the standard library HTTP client, so there is
no SDK dependency.

Environment:
    ANTHROPIC_API_KEY, SQLGHOST_ANTHROPIC_MODEL (default claude-sonnet-4-5)
    OPENAI_API_KEY,    SQLGHOST_OPENAI_MODEL    (default gpt-4.1-mini)
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from ..model import Profile, Schema
from .base import Translation, TranslationError

SYSTEM = (
    "You translate questions into a single read-only SQL query for the database described below. "
    "Rules: answer with exactly one SELECT statement (CTEs are fine) inside a ```sql code block. "
    "Never write INSERT, UPDATE, DELETE, DDL, PRAGMA or more than one statement. Use only tables and "
    "columns that exist. Use the exact categorical values listed. Prefer explicit JOIN ... ON. "
    "Add ORDER BY whenever you use LIMIT."
)


def build_prompt(question: str, schema: Schema, profile: Profile, feedback: str | None = None) -> str:
    lines = [f"Dialect: {schema.dialect}", "", "Schema:"]
    for name in schema.dependency_order():
        table = schema.tables[name]
        rows = profile.row_count(name)
        if table.ddl:
            lines.append(f"-- {rows} rows")
            lines.append(table.ddl.strip() + ";")
        else:
            cols = ", ".join(f"{c.name} {c.type}{'' if c.nullable else ' NOT NULL'}" for c in table.columns)
            lines.append(f"-- {rows} rows")
            lines.append(f"CREATE TABLE {name} ({cols});")
            for fk in table.foreign_keys:
                lines.append(
                    f"--   {name}({', '.join(fk.columns)}) references {fk.ref_table}({', '.join(fk.ref_columns)})"
                )
    values = []
    for name, table in schema.tables.items():
        for col in table.columns:
            known = col.check_values or col.enum_values
            prof = profile.column(name, col.name)
            if not known and prof and prof.categories:
                known = [c[0] for c in prof.categories]
            if known:
                values.append(f"{name}.{col.name}: {', '.join(map(str, known))}")
            elif prof and prof.pattern in ("iso_date", "iso_datetime") and prof.quantiles:
                values.append(f"{name}.{col.name}: text timestamps like {prof.quantiles[0]!s}")
    if values:
        lines += ["", "Known values:", *values]
    lines += ["", f"Question: {question}"]
    if feedback:
        lines += ["", "Your previous attempt failed validation. Fix these problems:", feedback]
    return "\n".join(lines)


def extract_sql(text: str) -> str:
    block = re.search(r"```(?:sql)?\s*(.+?)```", text, flags=re.S | re.I)
    sql = block.group(1) if block else text
    return sql.strip()


def _post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST", headers={"content-type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise TranslationError(f"API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TranslationError(f"could not reach the API: {exc.reason}") from exc


class AnthropicTranslator:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("SQLGHOST_ANTHROPIC_MODEL", "claude-sonnet-4-5")
        if not self.api_key:
            raise TranslationError("ANTHROPIC_API_KEY is not set")

    def translate(self, question: str, schema: Schema, profile: Profile, feedback: str | None = None) -> Translation:
        data = _post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            {
                "model": self.model,
                "max_tokens": 1024,
                "system": SYSTEM,
                "messages": [{"role": "user", "content": build_prompt(question, schema, profile, feedback)}],
            },
        )
        text = "".join(part.get("text", "") for part in data.get("content", []))
        return Translation(sql=extract_sql(text), engine=f"anthropic:{self.model}")


class OpenAITranslator:
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("SQLGHOST_OPENAI_MODEL", "gpt-4.1-mini")
        if not self.api_key:
            raise TranslationError("OPENAI_API_KEY is not set")

    def translate(self, question: str, schema: Schema, profile: Profile, feedback: str | None = None) -> Translation:
        data = _post(
            "https://api.openai.com/v1/chat/completions",
            {"authorization": f"Bearer {self.api_key}"},
            {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": build_prompt(question, schema, profile, feedback)},
                ],
            },
        )
        text = data["choices"][0]["message"]["content"] or ""
        return Translation(sql=extract_sql(text), engine=f"openai:{self.model}")
