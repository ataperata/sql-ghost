from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..model import Profile, Schema


class TranslationError(RuntimeError):
    def __init__(self, message: str, suggestions: list[str] | None = None):
        super().__init__(message)
        self.suggestions = suggestions or []


@dataclass
class Translation:
    sql: str
    engine: str
    note: str = ""
    attempts: list[str] = field(default_factory=list)


class Translator(Protocol):
    name: str

    def translate(
        self, question: str, schema: Schema, profile: Profile, feedback: str | None = None
    ) -> Translation: ...
