from __future__ import annotations

import sqlite3

import pytest

from sqlghost import sample
from sqlghost.demo import BAD_CANDIDATES, QUESTIONS, run_demo
from sqlghost.engine import Ghost, sample_db
from sqlghost.nl import EXAMPLES, TranslationError
from sqlghost.nl.rules import RuleEngine


def capture(ghost):
    lines: list[str] = []
    passed, blocked = run_demo(ghost, out=lines.append)
    return passed, blocked, "\n".join(lines)


def test_demo_end_to_end_and_deterministic(cache_dir, tmp_path):
    first = capture(Ghost(str(sample_db()), cache=cache_dir))
    second = capture(Ghost(str(sample_db()), cache=tmp_path))  # fresh shadow build
    assert first[0] == len(QUESTIONS)
    assert first[1] == len(BAD_CANDIDATES)
    assert first[2] == second[2]


@pytest.mark.parametrize("question", EXAMPLES)
def test_every_example_translates_and_passes(ghost, question):
    res = ghost.ask(question, provider="rules", run=True)
    assert res.translation is not None, res.error
    assert res.report.passed, [c.to_dict() for c in res.report.failures]
    assert res.result is not None


def test_rules_engine_is_deterministic(ghost):
    engine = RuleEngine(ghost.schema, ghost.profile)
    a = [engine.translate(q).sql for q in EXAMPLES]
    b = [RuleEngine(ghost.schema, ghost.profile).translate(q).sql for q in EXAMPLES]
    assert a == b


def test_rules_engine_unknown_question(ghost):
    with pytest.raises(TranslationError) as info:
        RuleEngine(ghost.schema, ghost.profile).translate("why is the sky blue")
    assert info.value.suggestions


def test_rules_engine_works_on_another_schema(custom_ghost):
    res = custom_ghost.ask("How many people by region", provider="rules")
    assert res.report.passed, res.translation.sql
    assert "JOIN teams" in res.translation.sql
    res = custom_ghost.ask("Average salary by level", provider="rules")
    assert res.report.passed, res.translation.sql


def test_known_answer(ghost):
    res = ghost.ask("How many trips were there in March 2026?", provider="rules")
    expected = ghost.source.query(
        "SELECT COUNT(*) FROM trips WHERE passed_at >= '2026-03-01' AND passed_at < '2026-04-01'"
    )[0][0]
    assert res.result.rows[0][0] == expected


def test_sample_generator_is_deterministic(tmp_path):
    a = sample.build(tmp_path / "a.sqlite")
    b = sample.build(tmp_path / "b.sqlite")

    def dump(path):
        conn = sqlite3.connect(path)
        try:
            return list(conn.iterdump())
        finally:
            conn.close()

    assert dump(a) == dump(b)
