"""Chaos through the real engine (scratch databases): truth rows, footprints, referential integrity.

Two short backfills of a small business (6 weeks from 2025-03-03, across the
spring DST change and a monthly GDPR selection): one with raised defect rates,
one with every defect off. Each business-schema footprint equals the number of
truth rows of its defect, and with chaos off there is no footprint at all
(no phase-1 lifecycle leaves a cancellation timestamp on an uncancelled order
or shipment, which the status-error residue query relies on).
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from cpgsim import chaos, db
from cpgsim.config import load_config

pytestmark = [pytest.mark.db, pytest.mark.slow]

DSN = os.environ.get("CPGSIM_TEST_DSN")
START = date(2025, 3, 3)
UNTIL = datetime(2025, 4, 14, tzinfo=timezone.utc)
SMALL = {"channels": {"web": {"base_orders_per_day": 60, "initial_customers": 1800}}}
RAISED = {
    "duplicate_customer_rate": 0.3, "malformed_postcode_rate": 0.05, "status_error_rate": 0.05,
    "duplicate_carrier_event_rate": 0.05, "missing_order_ref_rate": 0.05, "unmapped_article_rate": 0.05,
    "free_text_return_reason_rate": 0.15, "gdpr_erasure_rate_monthly": 0.01, "dst_truth": True,
}
OFF = {k: (False if k == "dst_truth" else 0.0) for k in RAISED} | {"sellout_restatement_rate": 0.0}


def _run(chaos: dict) -> str:
    from cpgsim.engine.runner import Simulation

    params = conninfo_to_dict(DSN)
    name = f"cpgsim_chaos_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(**{**params, "dbname": name})
    with psycopg.connect(dsn) as conn:
        db.init_db(conn, writer_role="cpgsim_chaos_writer", reader_role="cpgsim_chaos_reader")
    cfg = load_config(**SMALL, chaos=chaos)
    conn = psycopg.connect(dsn)
    try:
        sim = Simulation(cfg, conn)
        sim.open(history_start=START)
        sim.backfill(UNTIL)
    finally:
        conn.close()
    return name


@pytest.fixture(scope="module")
def dbs():
    if not DSN:
        pytest.skip("CPGSIM_TEST_DSN not set")
    params = conninfo_to_dict(DSN)
    names: list[str] = []
    try:
        names.append(_run(RAISED))
        names.append(_run(OFF))
        conns = [psycopg.connect(make_conninfo(**{**params, "dbname": n})) for n in names]
        yield conns
        for c in conns:
            c.close()
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            for n in names:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(n)))


def one(conn, q: str, *params):
    return conn.execute(q, params).fetchone()[0]


def truth(conn, defect: str, extra: str = "") -> int:
    return one(conn, f"SELECT count(*) FROM truth.planted_effects WHERE scenario_id = %s {extra}", defect)


SHOP_RESIDUE = "SELECT count(*) FROM shop.orders WHERE status <> 'cancelled' AND cancelled_at IS NOT NULL"
WMS_RESIDUE = "SELECT count(*) FROM wms.shipments WHERE status_code <> 'CX' AND cancelled_local IS NOT NULL"


def test_chaos_windows_and_truth(dbs):
    on, off = dbs
    wins = dict(on.execute("SELECT window_id, source FROM truth.scenario_windows WHERE source = 'chaos'").fetchall())
    assert set(wins) == {f"{d}:chaos" for d in chaos.DEFECTS}  # sellout_restatement keeps its default rate
    for d in ("duplicate_customer", "malformed_postcode", "status_error", "duplicate_carrier_event",
              "missing_order_ref", "unmapped_article", "free_text_return_reason", "gdpr_erasure", "dst_local_time"):
        assert truth(on, d) > 0, d
        assert one(on, "SELECT count(*) FROM truth.planted_effects WHERE scenario_id = %s"
                   " AND window_id <> %s", d, f"{d}:chaos") == 0
    assert truth(on, "dst_local_time") == 1  # 2025-03-30
    assert one(off, "SELECT count(*) FROM truth.scenario_windows WHERE source = 'chaos'") == 0
    # the default scenario windows still plant their effects; no defect does
    assert one(off, "SELECT count(*) FROM truth.planted_effects WHERE scenario_id = ANY(%s)", list(chaos.DEFECTS)) == 0


def test_status_error_footprint_matches_truth(dbs):
    on, off = dbs
    assert one(on, SHOP_RESIDUE) == truth(on, "status_error", "AND detail->>'system' = 'shop'") > 0
    assert one(on, WMS_RESIDUE) == truth(on, "status_error", "AND detail->>'system' = 'wms'") > 0
    assert one(off, SHOP_RESIDUE) == 0 and one(off, WMS_RESIDUE) == 0
    # every error is corrected: truth is written at the correction, with the error's set time
    bad = one(on, "SELECT count(*) FROM truth.planted_effects WHERE scenario_id = 'status_error'"
                  " AND (detail->>'corrected_at')::timestamptz <= (detail->>'set_at')::timestamptz")
    assert bad == 0


def test_duplicate_customers_have_addresses_and_derivable_ids(dbs):
    on, _ = dbs
    n = truth(on, "duplicate_customer")
    assert one(on, "SELECT count(*) FROM truth.planted_effects e JOIN shop.customers c"
                   " ON c.id = (e.system_refs->>'shop_customer_id')::uuid"
                   " JOIN shop.addresses a ON a.customer_id = c.id"
                   " WHERE e.scenario_id = 'duplicate_customer'") == n
    assert one(on, "SELECT count(*) FROM truth.planted_effects WHERE scenario_id = 'duplicate_customer'"
                   " AND entity_id::bigint <> 50000000 + (detail->>'original_customer_id')::bigint") == 0
    # orders on duplicates reference registered shop customers
    assert one(on, "SELECT count(*) FROM shop.orders o LEFT JOIN shop.customers c ON c.id = o.customer_id"
                   " WHERE c.id IS NULL") == 0


def test_other_footprints_match_truth(dbs):
    on, off = dbs
    web_vbak = "FROM erp.vbak WHERE auart <> 'ZEDI' AND (bstnk IS NULL OR length(bstnk) < 36)"
    assert one(on, f"SELECT count(*) {web_vbak}") == truth(on, "missing_order_ref")
    assert one(off, f"SELECT count(*) {web_vbak}") == 0
    assert one(on, "SELECT count(*) FROM wms.returns WHERE reason_text IS NOT NULL") == truth(
        on, "free_text_return_reason")
    dup_scans = """SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM wms.carrier_events
                   GROUP BY shipment_id, event_code, event_local, location HAVING count(*) > 1) x"""
    assert one(on, dup_scans) == truth(on, "duplicate_carrier_event")
    assert one(off, dup_scans) == 0
    assert one(on, "SELECT count(*) FROM shop.customers WHERE status = 'anonymised'") == truth(on, "gdpr_erasure")
    assert truth(on, "malformed_postcode") == one(
        on, "SELECT count(*) FROM truth.planted_effects e JOIN shop.addresses a"
            " ON a.customer_id = (e.system_refs->>'shop_customer_id')::uuid"
            " WHERE e.scenario_id = 'malformed_postcode'"
            " AND a.postcode IS DISTINCT FROM e.detail->>'true_postcode'")
    # unmapped articles: listed articles without knmt rows
    assert one(on, "SELECT count(*) FROM erp.knmt") + truth(on, "unmapped_article") == one(off, "SELECT count(*)"
                                                                                           " FROM erp.knmt")


def test_chaos_effect_rows_have_system_refs(dbs):
    on, _ = dbs
    rows = on.execute("SELECT scenario_id, entity_type, system_refs FROM truth.planted_effects"
                      " WHERE entity_type <> 'dst_transition'").fetchall()
    assert rows and all(r[2] for r in rows), [r for r in rows if not r[2]][:3]
