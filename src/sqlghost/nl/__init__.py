"""Natural language to SQL translators."""

from __future__ import annotations

import os

from ..model import Profile, Schema
from .base import Translation, TranslationError, Translator
from .llm import AnthropicTranslator, OpenAITranslator
from .rules import EXAMPLES, RuleEngine

__all__ = ["EXAMPLES", "Translation", "TranslationError", "Translator", "make_translator"]


def make_translator(provider: str, schema: Schema, profile: Profile) -> Translator:
    """provider: auto, rules, anthropic or openai. auto picks the first key it finds."""
    provider = (provider or "auto").lower()
    if provider == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        else:
            provider = "rules"
    if provider == "anthropic":
        return AnthropicTranslator()
    if provider == "openai":
        return OpenAITranslator()
    if provider in ("rules", "offline"):
        return RuleEngine(schema, profile)
    raise TranslationError(f"unknown provider {provider!r}; use auto, rules, anthropic or openai")
