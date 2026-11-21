"""Postgres integration tests. Set SQLGHOST_TEST_PG to a source database URL and
SQLGHOST_TEST_PG_SHADOW to a scratch database URL to run them."""

from __future__ import annotations

import os

import pytest

PG = os.environ.get("SQLGHOST_TEST_PG")
PG_SHADOW = os.environ.get("SQLGHOST_TEST_PG_SHADOW")
pytestmark = pytest.mark.skipif(not (PG and PG_SHADOW), reason="SQLGHOST_TEST_PG and SQLGHOST_TEST_PG_SHADOW not set")

SETUP = """
DROP TABLE IF EXISTS shipments, orders, customers CASCADE;
DROP TYPE IF EXISTS order_state CASCADE;
CREATE TYPE order_state AS ENUM ('new', 'paid', 'shipped', 'cancelled');
CREATE TABLE customers (
    customer_id serial PRIMARY KEY,
    email varchar(120) NOT NULL UNIQUE,
    tier text NOT NULL CHECK (tier IN ('free', 'pro', 'enterprise')),
    joined_at timestamp NOT NULL
);
CREATE TABLE orders (
    order_id serial PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers (customer_id),
    state order_state NOT NULL,
    total numeric(10, 2) NOT NULL CHECK (total >= 0),
    placed_on date NOT NULL
);
CREATE TABLE shipments (
    shipment_id serial PRIMARY KEY,
    order_id integer NOT NULL REFERENCES orders (order_id),
    carrier text NOT NULL,
    leg integer NOT NULL,
    UNIQUE (order_id, leg)
);
CREATE INDEX orders_customer_idx ON orders (customer_id);
INSERT INTO customers (email, tier, joined_at)
SELECT 'person' || g || '@real.example', (ARRAY['free', 'pro', 'enterprise'])[1 + g % 3],
       timestamp '2024-01-01' + g * interval '7 hours'
FROM generate_series(1, 300) g;
INSERT INTO orders (customer_id, state, total, placed_on)
SELECT 1 + g % 300, (ARRAY['new', 'paid', 'shipped', 'cancelled']::order_state[])[1 + g % 4],
       (g % 97) * 3.5, date '2025-01-01' + g % 200
FROM generate_series(1, 3000) g;
INSERT INTO shipments (order_id, carrier, leg)
SELECT 1 + g % 3000, (ARRAY['ups', 'fedex', 'usps'])[1 + g % 3], 1 + g / 3000
FROM generate_series(1, 4000) g;
ANALYZE;
"""


@pytest.fixture(scope="module")
def pg_ghost():
    import psycopg

    from sqlghost.engine import Ghost

    with psycopg.connect(PG, autocommit=True) as conn:
        conn.execute(SETUP)
    return Ghost(PG, shadow_url=PG_SHADOW, rows_cap=500)


def test_introspection(pg_ghost):
    schema = pg_ghost.schema
    assert set(schema.tables) == {"customers", "orders", "shipments"}
    orders = schema.tables["orders"]
    assert orders.column("state").enum_values == ["new", "paid", "shipped", "cancelled"]
    assert schema.tables["customers"].column("tier").check_values == ["free", "pro", "enterprise"]
    assert orders.foreign_keys[0].ref_table == "customers"
    assert schema.tables["shipments"].unique_sets == [["order_id", "leg"]]


def test_shadow_respects_constraints(pg_ghost):
    import psycopg

    shadows = pg_ghost.shadows
    assert all(n > 0 for n in shadows.info.rows.values()), shadows.info.rows
    with psycopg.connect(PG_SHADOW) as conn:
        orphans = conn.execute(
            "SELECT COUNT(*) FROM sqlghost_shadow.orders o LEFT JOIN sqlghost_shadow.customers c "
            "USING (customer_id) WHERE c.customer_id IS NULL"
        ).fetchone()[0]
        leaked = conn.execute(
            "SELECT COUNT(*) FROM sqlghost_shadow.customers WHERE email LIKE '%real.example'"
        ).fetchone()[0]
    assert orphans == 0
    assert leaked == 0


def test_validate_and_run(pg_ghost):
    report = pg_ghost.validate(
        "SELECT c.tier, COUNT(*) AS orders FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
        "WHERE o.state = 'paid' GROUP BY c.tier ORDER BY orders DESC"
    )
    assert report.passed, [c.to_dict() for c in report.failures]
    cost = next(c for c in report.checks if c.name == "cost")
    assert "real database planner" in cost.detail
    result = pg_ghost.run_real(report)
    assert len(result.rows) == 3


def test_postgres_specific_blocks(pg_ghost):
    for sql in (
        "SELECT pg_sleep(5)",
        "SELECT * INTO copy FROM orders",
        "SELECT * FROM orders FOR UPDATE",
        "SELECT COUNT(*) FROM orders WHERE state = 'Paid'",
    ):
        assert not pg_ghost.validate(sql).passed, sql


def test_real_session_is_read_only(pg_ghost):
    from sqlghost.db import SourceError

    with pytest.raises(SourceError):
        pg_ghost.source.run_readonly("DELETE FROM shipments")
    assert pg_ghost.source.query("SELECT COUNT(*) FROM shipments")[0][0] == 4000


def test_rules_engine_on_postgres(pg_ghost):
    res = pg_ghost.ask("How many orders by tier", provider="rules")
    assert res.report.passed, res.translation.sql
    assert res.result is not None
