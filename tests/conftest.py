from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sqlghost.engine import Ghost, sample_db

CUSTOM_DDL = [
    """CREATE TABLE teams (
        team_id INTEGER PRIMARY KEY,
        code    TEXT NOT NULL UNIQUE,
        region  TEXT NOT NULL CHECK (region IN ('north', 'south', 'east', 'west'))
    )""",
    """CREATE TABLE people (
        person_id  INTEGER PRIMARY KEY,
        email      TEXT NOT NULL UNIQUE,
        full_name  TEXT NOT NULL,
        team_id    INTEGER NOT NULL REFERENCES teams (team_id),
        manager_id INTEGER REFERENCES people (person_id),
        salary     REAL NOT NULL CHECK (salary > 0),
        level      INTEGER NOT NULL CHECK (level BETWEEN 1 AND 7),
        hired_on   DATE NOT NULL,
        nickname   TEXT
    )""",
    """CREATE TABLE badges (
        badge_id  INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL REFERENCES people (person_id),
        kind      TEXT NOT NULL,
        year      INTEGER NOT NULL,
        UNIQUE (person_id, kind, year)
    )""",
]


def build_custom(path: Path) -> Path:
    import random

    rng = random.Random(1)
    conn = sqlite3.connect(path)
    for stmt in CUSTOM_DDL:
        conn.execute(stmt)
    regions = ["north", "south", "east", "west"]
    for i in range(1, 21):
        conn.execute("INSERT INTO teams VALUES (?, ?, ?)", (i, f"T{i:03d}", rng.choice(regions)))
    for i in range(1, 401):
        manager = rng.randint(1, i - 1) if i > 1 and rng.random() < 0.8 else None
        conn.execute(
            "INSERT INTO people VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                i,
                f"real.person{i}@corp.internal",
                f"Realname Person{i}",
                rng.randint(1, 20),
                manager,
                round(rng.uniform(40000, 200000), 2),
                rng.randint(1, 7),
                f"20{rng.randint(10, 25)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                rng.choice([None, None, "Ace", "Doc", "Sky"]),
            ),
        )
    kinds = ["safety", "mentor", "quality"]
    seen = set()
    bid = 0
    while bid < 600:
        key = (rng.randint(1, 400), rng.choice(kinds), rng.randint(2020, 2025))
        if key in seen:
            continue
        seen.add(key)
        bid += 1
        conn.execute("INSERT INTO badges VALUES (?, ?, ?, ?)", (bid, *key))
    # one person with a unique, rare nickname that must never leak into the profile
    conn.execute("UPDATE people SET nickname = 'Zebulon-Unique' WHERE person_id = 7")
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="session")
def cache_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("sqlghost-cache")


@pytest.fixture(scope="session")
def ghost(cache_dir) -> Ghost:
    return Ghost(str(sample_db()), cache=cache_dir)


@pytest.fixture(scope="session")
def custom_db(tmp_path_factory) -> Path:
    return build_custom(tmp_path_factory.mktemp("custom") / "custom.sqlite")


@pytest.fixture(scope="session")
def custom_ghost(custom_db, cache_dir) -> Ghost:
    return Ghost(str(custom_db), cache=cache_dir, rows_cap=300)
