from __future__ import annotations

import pytest

from sqlghost.db import SourceError
from sqlghost.validate import FAIL, PASS, SKIP, WARN


def status(report, name):
    return next(c.status for c in report.checks if c.name == name)


def test_good_query_passes_everything(ghost):
    report = ghost.validate("SELECT status, COUNT(*) AS n FROM trips GROUP BY status")
    assert report.passed
    assert status(report, "monotonic") == PASS
    assert status(report, "sensitivity") == PASS
    assert report.final_sql.endswith("LIMIT 100")


def test_parse_error_stops_everything(ghost):
    report = ghost.validate("SELECT FROM WHERE (")
    assert not report.passed
    assert status(report, "parse") == FAIL
    assert all(c.status == SKIP for c in report.checks[1:])


def test_write_is_never_executed(ghost):
    before = ghost.source.query("SELECT COUNT(*) FROM trips")[0][0]
    report = ghost.validate("DELETE FROM trips")
    assert status(report, "read_only") == FAIL
    assert status(report, "shadow_run") == SKIP
    assert ghost.source.query("SELECT COUNT(*) FROM trips")[0][0] == before


def test_unknown_table(ghost):
    report = ghost.validate("SELECT * FROM customers")
    assert status(report, "schema") == FAIL
    assert "customers" in next(c.detail for c in report.checks if c.name == "schema")


def test_unknown_column(ghost):
    report = ghost.validate("SELECT plate_number FROM vehicles")
    assert status(report, "schema") == FAIL


def test_wrong_literal_value(ghost):
    report = ghost.validate("SELECT COUNT(*) FROM trips WHERE status = 'Disputed'")
    assert status(report, "values") == FAIL
    assert "did you mean 'disputed'" in next(c.detail for c in report.checks if c.name == "values")


def test_wrong_literal_in_list_through_alias(ghost):
    report = ghost.validate(
        "SELECT COUNT(*) FROM trips t JOIN plazas p ON p.plaza_id = t.plaza_id "
        "WHERE p.corridor IN ('Harbor Tunnel', 'Harbour Tunnel')"
    )
    assert status(report, "values") == FAIL


def test_runtime_error_on_shadow(ghost):
    report = ghost.validate("SELECT json_extract(status, '$.a') FROM trips")
    assert not report.passed
    assert status(report, "shadow_run") == FAIL


def test_cartesian_product_blocked(ghost):
    report = ghost.validate("SELECT a.email, t.trip_id FROM accounts a, trips t")
    assert status(report, "cost") == FAIL
    assert "cartesian" in next(c.detail for c in report.checks if c.name == "cost")


def test_joined_query_is_not_cartesian(ghost):
    report = ghost.validate(
        "SELECT a.email, t.trip_id FROM accounts a, vehicles v, trips t "
        "WHERE v.account_id = a.account_id AND t.vehicle_id = v.vehicle_id"
    )
    assert status(report, "cost") != FAIL


def test_limit_without_order_warns(ghost):
    report = ghost.validate("SELECT trip_id FROM trips LIMIT 5")
    assert report.passed
    assert status(report, "determinism") == WARN


def test_random_is_flagged(ghost):
    report = ghost.validate("SELECT trip_id FROM trips ORDER BY random() LIMIT 3")
    assert status(report, "determinism") == WARN


def test_impossible_filter_flagged_by_perturbation(ghost):
    report = ghost.validate("SELECT COUNT(*) FROM trips WHERE toll_cents < 0")
    assert status(report, "sensitivity") == WARN


def test_constant_query_flagged(ghost):
    report = ghost.validate("SELECT 42 AS answer")
    assert status(report, "sensitivity") == WARN
    assert status(report, "empty_db") == WARN


def test_runaway_query_times_out(ghost):
    from sqlghost.validate import Validator, ValidatorOptions

    v = Validator(ghost.schema, ghost.profile, ghost.shadows, ghost.source, ValidatorOptions(shadow_timeout=0.3))
    report = v.validate(
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT COUNT(*) FROM n, plazas"
    )
    assert status(report, "shadow_run") == FAIL
    assert "timeout" in next(c.detail for c in report.checks if c.name == "shadow_run")


def test_real_run_refuses_unvalidated(ghost):
    report = ghost.validate("DROP TABLE trips")
    with pytest.raises(SourceError):
        ghost.run_real(report)


def test_real_run_applies_limit(ghost):
    report = ghost.validate("SELECT trip_id FROM trips ORDER BY trip_id")
    result = ghost.run_real(report)
    assert len(result.rows) == 100
    assert result.truncated is False or len(result.rows) == 100
    assert "LIMIT 100" in report.final_sql


def test_existing_smaller_limit_kept(ghost):
    report = ghost.validate("SELECT trip_id FROM trips ORDER BY trip_id LIMIT 7")
    assert report.final_sql.endswith("LIMIT 7")
    assert len(ghost.run_real(report).rows) == 7


def test_union_is_wrapped_with_limit(ghost):
    report = ghost.validate("SELECT plaza_id FROM trips UNION SELECT plaza_id FROM rates")
    assert report.passed
    assert report.final_sql.startswith("SELECT * FROM (")
