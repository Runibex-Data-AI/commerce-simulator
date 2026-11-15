"""End-to-end fixtures: fresh scratch databases running the real engine.

Needs ``CPGSIM_TEST_DSN`` pointing at a Postgres database whose user may
``CREATE DATABASE`` (a superuser on a dev server). Each test gets its own
database, dropped afterwards.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from cpgsim import db
from cpgsim.config import load_config

DSN = os.environ.get("CPGSIM_TEST_DSN")
pytestmark = pytest.mark.db

BUSINESS = ("shop", "wms", "erp", "mkt", "feeds")
WRITER, READER = "cpgsim_e2e_writer", "cpgsim_e2e_reader"

#: Small but complete business: every lifecycle runs, quickly.
SMALL = {
    "channels": {"web": {"base_orders_per_day": 60, "initial_customers": 1800}},
}


#: Overrides that switch every data quality defect off: ``small_cfg(**CHAOS_OFF)``.
#: Exact-reconciliation tests check the business without noise; determinism
#: tests keep chaos on (the default).
CHAOS_OFF = {
    "chaos": {
        "duplicate_customer_rate": 0.0,
        "malformed_postcode_rate": 0.0,
        "status_error_rate": 0.0,
        "duplicate_carrier_event_rate": 0.0,
        "missing_order_ref_rate": 0.0,
        "unmapped_article_rate": 0.0,
        "sellout_restatement_rate": 0.0,
        "free_text_return_reason_rate": 0.0,
        "gdpr_erasure_rate_monthly": 0.0,
        "dst_truth": False,
    }
}


def small_cfg(**overrides):
    from cpgsim.config import _deep_merge

    return load_config(**_deep_merge(SMALL, overrides))


def _factory():
    if not DSN:
        pytest.skip("CPGSIM_TEST_DSN not set")
    created: list[str] = []
    params = conninfo_to_dict(DSN)

    def make() -> str:
        name = f"cpgsim_e2e_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        dsn = make_conninfo(**{**params, "dbname": name})
        with psycopg.connect(dsn) as conn:
            db.init_db(conn, writer_role=WRITER, reader_role=READER)
        return dsn

    yield make
    with psycopg.connect(DSN, autocommit=True) as admin:
        for name in created:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def fresh_db():
    """Factory: each call creates and initialises a new scratch database, returns its DSN."""
    yield from _factory()


@pytest.fixture(scope="module")
def fresh_db_module():
    yield from _factory()


@contextmanager
def simulation(dsn: str, cfg, start: date):
    from cpgsim.engine.runner import Simulation

    conn = psycopg.connect(dsn)
    try:
        sim = Simulation(cfg, conn)
        sim.open(history_start=start)
        yield sim
    finally:
        conn.close()


def utc(d: date, hours: float = 0) -> datetime:
    from cpgsim.engine.world import UTC

    return datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(hours=hours)


def business_tables(conn) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables"
        " WHERE table_schema = ANY(%s) AND table_type = 'BASE TABLE' ORDER BY 1, 2",
        (list(BUSINESS) + ["truth"],),
    ).fetchall()


def primary_key(conn, schema: str, table: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a"
            " ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
            " WHERE i.indrelid = %s::regclass AND i.indisprimary"
            " ORDER BY array_position(i.indkey, a.attnum)",
            (f"{schema}.{table}",),
        ).fetchall()
    ]


def digest(dsn: str) -> dict[str, tuple]:
    """Per table: row count, md5 of all rows (full text, ordered by PK)."""
    out = {}
    with psycopg.connect(dsn) as conn:
        for s, t in business_tables(conn):
            pk = ", ".join(primary_key(conn, s, t))
            q = sql.SQL(
                "SELECT count(*), md5(coalesce(string_agg(x::text, E'\\n' ORDER BY {pk}), ''))"
                " FROM {s}.{t} x"
            ).format(pk=sql.SQL(pk), s=sql.Identifier(s), t=sql.Identifier(t))
            out[f"{s}.{t}"] = tuple(conn.execute(q).fetchone())
    return out
