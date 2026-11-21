from __future__ import annotations

import json
import sqlite3

from sqlghost.db import open_source
from sqlghost.profiler import profile_source
from sqlghost.shadow import build_sqlite_shadows


def rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_shadow_keeps_exact_schema(custom_ghost, custom_db):
    shadow = custom_ghost.shadows.primary.path
    real = rows(custom_db, "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name")
    fake = rows(
        shadow,
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') "
        "AND name NOT LIKE 'sqlite_stat%' ORDER BY name",
    )
    assert real == fake


def test_shadow_has_rows_in_every_table(custom_ghost):
    info = custom_ghost.shadows.info
    assert all(n > 0 for n in info.rows.values()), info.rows
    assert info.rows["people"] <= custom_ghost.profile.row_count("people")


def test_types_match_declared_kinds(custom_ghost):
    shadow = custom_ghost.shadows.primary.path
    types = rows(
        shadow, "SELECT typeof(person_id), typeof(salary), typeof(level), typeof(hired_on), typeof(email) FROM people"
    )
    assert {t for t in types} == {("integer", "real", "integer", "text", "text")}
    dates = [r[0] for r in rows(shadow, "SELECT hired_on FROM people")]
    assert all(len(d) == 10 and d[4] == "-" and d[7] == "-" for d in dates)


def test_not_null_respected(custom_ghost):
    shadow = custom_ghost.shadows.primary.path
    for col in ("email", "full_name", "team_id", "salary", "level", "hired_on"):
        assert rows(shadow, f"SELECT COUNT(*) FROM people WHERE {col} IS NULL") == [(0,)]
    # nullable columns still get some NULLs, following the real null fraction
    assert rows(shadow, "SELECT COUNT(*) FROM people WHERE nickname IS NULL")[0][0] > 0


def test_unique_respected(custom_ghost):
    shadow = custom_ghost.shadows.primary.path
    assert rows(shadow, "SELECT COUNT(*) - COUNT(DISTINCT email) FROM people") == [(0,)]
    assert rows(shadow, "SELECT COUNT(*) - COUNT(DISTINCT code) FROM teams") == [(0,)]
    dupes = rows(shadow, "SELECT person_id, kind, year, COUNT(*) FROM badges GROUP BY 1, 2, 3 HAVING COUNT(*) > 1")
    assert dupes == []


def test_foreign_keys_respected(custom_ghost):
    shadow = custom_ghost.shadows.primary.path
    conn = sqlite3.connect(shadow)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    orphans = conn.execute(
        "SELECT COUNT(*) FROM badges b LEFT JOIN people p ON p.person_id = b.person_id WHERE p.person_id IS NULL"
    ).fetchone()
    assert orphans == (0,)
    # self reference: managers exist
    bad = conn.execute(
        "SELECT COUNT(*) FROM people c LEFT JOIN people m ON m.person_id = c.manager_id "
        "WHERE c.manager_id IS NOT NULL AND m.person_id IS NULL"
    ).fetchone()
    assert bad == (0,)
    conn.close()


def test_check_constraints_and_ranges(custom_ghost):
    shadow = custom_ghost.shadows.primary.path
    regions = {r[0] for r in rows(shadow, "SELECT DISTINCT region FROM teams")}
    assert regions <= {"north", "south", "east", "west"}
    lo, hi = rows(shadow, "SELECT MIN(level), MAX(level) FROM people")[0]
    assert lo >= 1 and hi <= 7
    assert rows(shadow, "SELECT COUNT(*) FROM people WHERE salary <= 0") == [(0,)]


def test_no_real_values_copied(custom_ghost, custom_db):
    shadow = custom_ghost.shadows.primary.path
    real_emails = {r[0] for r in rows(custom_db, "SELECT email FROM people")}
    fake_emails = {r[0] for r in rows(shadow, "SELECT email FROM people")}
    assert not real_emails & fake_emails
    real_names = {r[0] for r in rows(custom_db, "SELECT full_name FROM people")}
    fake_names = {r[0] for r in rows(shadow, "SELECT full_name FROM people")}
    assert not real_names & fake_names


def test_profile_hides_rare_and_identifying_values(custom_ghost):
    text = json.dumps(custom_ghost.profile.to_dict())
    assert "Zebulon-Unique" not in text  # appears once: below k
    assert "corp.internal" not in text  # unique column: shape only
    assert "Realname" not in text
    nick = custom_ghost.profile.column("people", "nickname")
    assert nick.categories and not nick.categories_complete
    assert custom_ghost.profile.column("people", "email").pattern == "email"


def test_same_seed_same_shadow_and_other_seed_differs(custom_db, tmp_path):
    src = open_source(str(custom_db))
    schema = src.introspect()
    profile = profile_source(src, schema)
    a = build_sqlite_shadows(schema, profile, tmp_path / "a", seed=3, rows_cap=300)
    b = build_sqlite_shadows(schema, profile, tmp_path / "b", seed=3, rows_cap=300)
    c = build_sqlite_shadows(schema, profile, tmp_path / "c", seed=4, rows_cap=300)
    dump = "SELECT * FROM people ORDER BY person_id"
    assert rows(a.primary.path, dump) == rows(b.primary.path, dump)
    assert rows(a.primary.path, dump) != rows(c.primary.path, dump)
    # the perturbed copy is smaller and the empty copy has no rows
    assert sum(a.alt_info.rows.values()) < sum(a.info.rows.values())
    assert rows(a.empty.path, "SELECT COUNT(*) FROM people") == [(0,)]


def test_sample_shadow_distribution_is_realistic(ghost):
    shadow = ghost.shadows.primary.path
    shares = dict(rows(shadow, "SELECT status, COUNT(*) * 1.0 / (SELECT COUNT(*) FROM trips) FROM trips GROUP BY 1"))
    assert 0.8 < shares["posted"] < 0.97
    assert set(shares) <= {"posted", "pending", "disputed", "written_off"}
