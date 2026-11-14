"""Cross-system reconciliation on a short real run (shop, erp; one business, several systems).

The systems disagree only where the specification says they do (keys,
conventions). Money and statuses of the same order must reconcile:

* shop order total == ERP vbak.netwr + sum(vbap.mwsbp) (document currency),
  shop tax_total == sum(vbap.mwsbp), and line by line the same net/tax split;
* per sales item, the non-tax pricing conditions add up to vbap.netwr and the
  MWST condition equals vbap.mwsbp (the shipping charge is item 000900);
* for fully shipped, non-returned orders: invoices minus credit memos == shop total;
* every sales and billing item joins to the material master;
* header statuses agree with item statuses, shop timestamps with the lifecycle;
* master data dates are plausible.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import psycopg
import pytest

from .conftest import simulation, small_cfg, utc

pytestmark = [pytest.mark.db, pytest.mark.slow]

START = date(2025, 11, 10)  # Black Friday promotions (ZPRO, free shipping) and their returns
UNTIL = utc(date(2025, 12, 22))

WEB_ORDERS = """
    SELECT o.id, o.total_amount, o.tax_total, o.shipping_fee, h.vbeln, h.netwr, h.waerk, o.currency
    FROM shop.orders o JOIN erp.vbak h ON h.bstnk = o.id::text AND h.auart = 'ZWEB'
"""


@pytest.fixture(scope="module")
def run(fresh_db_module):
    dsn = fresh_db_module()
    with simulation(dsn, small_cfg(), START) as sim:
        sim.backfill(UNTIL)
    conn = psycopg.connect(dsn)
    yield conn
    conn.close()


def test_order_value_shop_equals_erp(run):
    rows = run.execute(f"""
        WITH o AS ({WEB_ORDERS}),
        i AS (SELECT vbeln, sum(netwr) net, sum(mwsbp) tax, count(*) FILTER (WHERE posnr = '000900') fr
              FROM erp.vbap GROUP BY vbeln)
        SELECT o.id, o.total_amount, o.tax_total, o.shipping_fee, o.netwr, i.net, i.tax, i.fr, o.waerk, o.currency
        FROM o JOIN i USING (vbeln)
    """).fetchall()
    assert len(rows) > 1000
    bad = [r for r in rows if r[1] != r[4] + r[6] or r[2] != r[6] or r[4] != r[5] or r[8] != r[9]]
    assert not bad, bad[:5]
    # orders with a shipping charge carry exactly one freight item, the others none
    assert all((r[3] > 0) == (r[7] == 1) for r in rows)
    assert sum(1 for r in rows if r[3] > 0) > 100


def test_line_tax_split_shop_equals_erp(run):
    bad = run.execute("""
        SELECT l.order_id, l.line_no, l.line_total, l.tax_amount, p.netwr, p.mwsbp
        FROM shop.order_lines l
        JOIN erp.vbak h ON h.bstnk = l.order_id::text AND h.auart = 'ZWEB'
        JOIN erp.vbap p ON p.vbeln = h.vbeln AND p.posnr = lpad((l.line_no * 10)::text, 6, '0')
        WHERE l.tax_amount <> p.mwsbp OR l.line_total <> p.netwr + p.mwsbp
        LIMIT 5
    """).fetchall()
    assert not bad


def test_pricing_conditions_add_up(run):
    bad = run.execute("""
        SELECT p.vbeln, p.posnr, p.netwr, p.mwsbp,
               sum(k.kwert) FILTER (WHERE k.kschl <> 'MWST') net, sum(k.kwert) FILTER (WHERE k.kschl = 'MWST') tax
        FROM erp.vbap p JOIN erp.vbak h USING (vbeln) JOIN erp.konv k ON k.knumv = h.knumv AND k.kposn = p.posnr
        GROUP BY 1, 2, 3, 4
        HAVING sum(k.kwert) FILTER (WHERE k.kschl <> 'MWST') <> p.netwr
            OR coalesce(sum(k.kwert) FILTER (WHERE k.kschl = 'MWST'), 0) <> p.mwsbp
        LIMIT 5
    """).fetchall()
    assert not bad
    hd00 = run.execute("SELECT DISTINCT kposn FROM erp.konv WHERE kschl = 'HD00'").fetchall()
    assert hd00 == [("000900",)]
    # promotion conditions carry the promotion's nominal rate
    rates = {r[0] for r in run.execute("SELECT DISTINCT kbetr FROM erp.konv WHERE kschl = 'ZPRO'")}
    nominal = {-100 * r[0] for r in run.execute("SELECT DISTINCT discount_pct FROM shop.promotions")}
    assert rates and rates <= nominal, rates - nominal


def test_invoices_minus_credit_memos_equal_shop_total(run):
    rows = run.execute(f"""
        WITH o AS ({WEB_ORDERS}),
        shipped AS (
            SELECT order_id FROM shop.order_lines GROUP BY order_id
            HAVING bool_and(qty_shipped = quantity AND qty_cancelled = 0 AND qty_returned = 0)),
        b AS (
            SELECT d.aubel vbeln,
                   sum(CASE WHEN k.fkart = 'F2' THEN 1 ELSE -1 END * (d.netwr + d.mwsbp)) amount,
                   count(*) FILTER (WHERE k.fkart = 'G2') cms
            FROM erp.vbrp d JOIN erp.vbrk k USING (vbeln) GROUP BY d.aubel)
        SELECT o.id, o.total_amount, b.amount
        FROM o JOIN shipped s ON s.order_id = o.id JOIN b USING (vbeln)
        WHERE b.cms = 0 AND NOT EXISTS (SELECT 1 FROM shop.refunds r WHERE r.order_id = o.id)
    """).fetchall()
    assert len(rows) > 1000
    bad = [r for r in rows if r[1] != r[2]]
    assert not bad, bad[:5]
    # orders refunded in full: invoices and credit memos cancel out
    full = run.execute("""
        SELECT o.id, sum(CASE WHEN k.fkart = 'F2' THEN 1 ELSE -1 END * (d.netwr + d.mwsbp))
        FROM shop.orders o JOIN erp.vbak h ON h.bstnk = o.id::text
        JOIN erp.vbrp d ON d.aubel = h.vbeln JOIN erp.vbrk k ON k.vbeln = d.vbeln
        WHERE o.status = 'refunded' GROUP BY o.id
    """).fetchall()
    assert all(r[1] == Decimal("0.00") for r in full), [r for r in full if r[1] != 0][:5]


def test_items_join_material_master(run):
    for table in ("erp.vbap", "erp.vbrp"):
        missing = run.execute(
            f"SELECT count(*) FROM {table} x LEFT JOIN erp.mara m USING (matnr) WHERE m.matnr IS NULL"
        ).fetchone()[0]
        assert missing == 0, table
    fr = run.execute(
        "SELECT DISTINCT p.matnr, p.pstyv, p.lfsta = '', p.lgort FROM erp.vbap p JOIN erp.vbak h USING (vbeln)"
        " WHERE h.auart = 'ZWEB' AND p.posnr = '000900'").fetchall()
    assert fr == [("SHIPPING", "TAD", True, "")]
    assert run.execute("SELECT count(*) FROM erp.vbap WHERE matnr = 'SHIPPING' AND posnr <> '000900'").fetchone() == (0,)
    # the freight item is never delivered
    assert run.execute("SELECT count(*) FROM erp.lips WHERE matnr = 'SHIPPING'").fetchone()[0] == 0


def test_header_status_agrees_with_items(run):
    stuck = run.execute("""
        SELECT h.vbeln, h.gbstk, h.lfstk, h.fkstk FROM erp.vbak h
        WHERE h.gbstk <> 'C' AND NOT EXISTS (SELECT 1 FROM erp.vbap i WHERE i.vbeln = h.vbeln AND i.gbsta <> 'C')
        LIMIT 5
    """).fetchall()
    assert not stuck
    # cut items: statuses relative to the confirmed quantity
    cut = run.execute("""
        SELECT vbeln, posnr FROM erp.vbap
        WHERE abgru = '' AND kbmeng < kwmeng AND kbmeng = zzlfimg AND kbmeng = zzfkimg
          AND (gbsta, lfsta, fksta) <> ('C', 'C', 'C')
        LIMIT 5
    """).fetchall()
    assert not cut


def test_shop_lifecycle_timestamps(run):
    rows = run.execute("""
        SELECT count(*) FILTER (WHERE delivered_at IS NOT NULL AND shipped_at IS NULL),
               count(*) FILTER (WHERE shipped_at > delivered_at),
               -- a received return is refunded or rejected within days, never left 'returned'
               count(*) FILTER (WHERE status = 'returned' AND returned_at < %s)
        FROM shop.orders o
    """, (utc(date(2025, 12, 12)),)).fetchone()
    assert rows == (0, 0, 0), rows
    refunds = dict(run.execute("SELECT status, count(*) FROM shop.refunds GROUP BY 1").fetchall())
    assert refunds.get("completed", 0) > 0
    assert run.execute(
        "SELECT count(*) FROM shop.refunds WHERE status = 'completed'"
        " AND (completed_at IS NULL OR completed_at <= issued_at OR updated_at <> completed_at)"
    ).fetchone()[0] == 0
    old_open = run.execute(
        "SELECT count(*) FROM shop.refunds WHERE status = 'issued' AND issued_at < %s",
        (utc(date(2025, 12, 12)),)).fetchone()[0]
    assert old_open == 0


def test_master_data_dates_plausible(run):
    assert run.execute("SELECT count(*) FROM erp.mara WHERE mstde < ersda").fetchone()[0] == 0
    assert run.execute("SELECT count(*) FROM erp.mara WHERE ersda > laeda").fetchone()[0] == 0
    first_customer = run.execute("SELECT min(registered_at)::date FROM shop.customers").fetchone()[0]
    latest_account = run.execute("SELECT max(erdat) FROM erp.kna1").fetchone()[0]
    assert latest_account < first_customer and latest_account < START
    groups = {r[0] for r in run.execute("SELECT DISTINCT matkl FROM erp.mara")}
    assert "KP9010" in groups or not run.execute(
        "SELECT 1 FROM erp.mara WHERE prdha LIKE '_____GIFTS%'").fetchone()
    assert not any(g.startswith("KP8") for g in groups)
