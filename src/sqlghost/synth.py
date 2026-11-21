"""Synthetic value generators driven by a column profile.

Nothing here ever sees a real row. Every value is drawn from the aggregate
profile (quantiles, category frequencies, text shape) with a seeded RNG, so the
same seed always gives the same shadow.
"""

from __future__ import annotations

import datetime as dt
import random
import string
import uuid
from typing import Any

from . import model as m
from .model import Column, ColumnProfile

FIRST = [
    "Avery",
    "Blake",
    "Casey",
    "Devon",
    "Emery",
    "Finley",
    "Harper",
    "Jordan",
    "Kai",
    "Logan",
    "Morgan",
    "Noel",
    "Parker",
    "Quinn",
    "Riley",
    "Sage",
    "Taylor",
    "Rowan",
    "Skyler",
    "Reese",
]
LAST = [
    "Ashdown",
    "Birchwood",
    "Calloway",
    "Draper",
    "Ellery",
    "Fairbanks",
    "Grayson",
    "Hollis",
    "Ingram",
    "Juniper",
    "Kestrel",
    "Lowell",
    "Marsh",
    "Northcott",
    "Oakes",
    "Pembrook",
]
WORDS = [
    "ghost",
    "lane",
    "shadow",
    "signal",
    "harbor",
    "cedar",
    "ridge",
    "summit",
    "valley",
    "river",
    "north",
    "south",
    "copper",
    "granite",
    "maple",
    "orbit",
    "quiet",
    "rapid",
    "silver",
    "timber",
]


class ValueFactory:
    def __init__(self, rng: random.Random):
        self.rng = rng

    # -- numeric and temporal -------------------------------------------------

    def from_quantiles(self, quantiles: list[Any], kind: str) -> Any:
        if kind in (m.DATE, m.DATETIME) or isinstance(quantiles[0], str):
            nums = [_to_epoch(q) for q in quantiles]
            value = self._interpolate(nums)
            return _from_epoch(value, quantiles[0], kind)
        value = self._interpolate([float(q) for q in quantiles])
        if kind == m.INTEGER:
            return round(value)
        return round(value, 2)

    def _interpolate(self, points: list[float]) -> float:
        if len(points) == 1:
            return points[0]
        u = self.rng.random() * (len(points) - 1)
        i = min(int(u), len(points) - 2)
        frac = u - i
        return points[i] + (points[i + 1] - points[i]) * frac

    # -- text -----------------------------------------------------------------

    def text(self, prof: ColumnProfile | None, index: int, unique: bool) -> str:
        pattern = prof.pattern if prof else None
        rng = self.rng
        if pattern == "email":
            return f"{rng.choice(WORDS)}.{rng.choice(WORDS)}{index}@example.test"
        if pattern == "uuid":
            return str(uuid.UUID(int=rng.getrandbits(128), version=4))
        if pattern == "url":
            return f"https://example.test/{rng.choice(WORDS)}/{index}"
        if pattern == "name":
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            return f"{name} {index}" if unique else name
        if pattern in ("code", "phone") and prof and prof.shape:
            return self.fill_shape(prof.shape)
        if pattern == "phone":
            return f"555-01{rng.randint(0, 99):02d}"
        if pattern == "code":
            length = max(3, min(16, int(prof.avg_len or 8))) if prof else 8
            return "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(length))
        if pattern == "token":
            word = rng.choice(WORDS)
            return f"{word}_{index}" if unique else word
        return self.words(prof, index, unique)

    def fill_shape(self, shape: str) -> str:
        out = []
        for ch in shape:
            if ch == "A":
                out.append(self.rng.choice(string.ascii_uppercase))
            elif ch == "9":
                out.append(self.rng.choice(string.digits))
            else:
                out.append(ch)
        return "".join(out)

    def words(self, prof: ColumnProfile | None, index: int, unique: bool) -> str:
        target = int(prof.avg_len) if prof and prof.avg_len else 12
        lo = prof.min_len if prof and prof.min_len is not None else 1
        hi = prof.max_len if prof and prof.max_len is not None else max(target, 24)
        parts: list[str] = []
        while len(" ".join(parts)) < target:
            parts.append(self.rng.choice(WORDS))
        text = " ".join(parts)
        if unique:
            text = f"{text} {index}"
        text = text[: max(hi, len(str(index)) + 1)] if unique else text[:hi]
        if len(text) < lo:
            text = text.ljust(lo, "x")
        return text


def default_value(col: Column, factory: ValueFactory, index: int) -> Any:
    """Fallback when the profile has nothing (empty source table)."""
    rng = factory.rng
    if col.enum_values:
        return rng.choice(col.enum_values)
    if col.kind == m.INTEGER:
        return rng.randint(1, 1000)
    if col.kind == m.REAL:
        return round(rng.uniform(0, 1000), 2)
    if col.kind == m.BOOLEAN:
        return rng.random() < 0.5
    if col.kind == m.DATE:
        return (dt.date(2025, 1, 1) + dt.timedelta(days=rng.randint(0, 364))).isoformat()
    if col.kind == m.DATETIME:
        base = dt.datetime(2025, 1, 1) + dt.timedelta(seconds=rng.randint(0, 365 * 86400))
        return base.isoformat(sep=" ")
    if col.kind == m.UUID:
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    if col.kind == m.JSON:
        return "{}"
    if col.kind == m.BLOB:
        return None
    return factory.text(None, index, col.unique)


def _to_epoch(value: Any) -> float:
    text = str(value).replace("T", " ").rstrip("Z")[:19]
    parsed = dt.datetime.fromisoformat(text)
    return parsed.replace(tzinfo=dt.UTC).timestamp()


def _from_epoch(seconds: float, sample: Any, kind: str) -> str:
    stamp = dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
    text = str(sample)
    if kind == m.DATE or len(text) == 10:
        return stamp.date().isoformat()
    sep = "T" if "T" in text else " "
    if len(text) <= 16:
        return stamp.strftime(f"%Y-%m-%d{sep}%H:%M")
    return stamp.strftime(f"%Y-%m-%d{sep}%H:%M:%S")
