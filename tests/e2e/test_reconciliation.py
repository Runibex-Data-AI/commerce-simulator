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

from .conftest import CHAOS_OFF, simulation, small_cfg, utc

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
    with simulation(dsn, small_cfg(**CHAOS_OFF), START) as sim:  # exact reconciliation: chaos off (AMB-20)
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
    # web promotion conditions carry the promotion's nominal rate; retail ones the brand-funded off-invoice share
    zpro = ("SELECT DISTINCT k.kbetr FROM erp.konv k JOIN erp.vbak h ON h.knumv = k.knumv"
            " WHERE k.kschl = 'ZPRO' AND h.auart = %s")
    rates = {r[0] for r in run.execute(zpro, ("ZWEB",))}
    nominal = {-100 * r[0] for r in run.execute("SELECT DISTINCT discount_pct FROM shop.promotions")}
    assert rates and rates <= nominal, rates - nominal
    pcfg = small_cfg(**CHAOS_OFF).promotions
    funded = {Decimal(str(round(-100 * pcfg.retail_funding_share * d, 3))) for d in pcfg.retail_discount_choices}
    retail_rates = {r[0] for r in run.execute(zpro, ("ZEDI",))}
    assert retail_rates and retail_rates <= funded, retail_rates - funded


def test_influencer_first_order_discount_one_code_per_order(run):
    """Influencer code orders: shop order, lines, redemption and ERP ZINF agree; never with another promotion."""
    orders = run.execute("""
        SELECT o.id, o.promo_code, c.influencer_code, o.is_repeat_customer, p.code, p.discount_pct,
               o.discount_total, r.discount_amount, r.code,
               (SELECT sum(l.discount_amount) FROM shop.order_lines l WHERE l.order_id = o.id),
               (SELECT max(abs(l.discount_amount - round(l.unit_price * l.quantity * p.discount_pct, 2)))
                FROM shop.order_lines l WHERE l.order_id = o.id)
        FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id
        JOIN shop.promotions p ON p.id = o.promotion_id
        LEFT JOIN shop.promo_redemptions r ON r.order_id = o.id AND r.promotion_id = o.promotion_id
        WHERE p.code IN (SELECT code FROM mkt.influencer_codes)
    """).fetchall()
    assert len(orders) > 30
    bad = [r for r in orders if not (
        r[1] == r[2] == r[4] == r[8] and not r[3] and r[5] == Decimal("0.1500")
        and r[6] == r[7] == r[9] > 0 and r[10] <= Decimal("0.01"))]
    assert not bad, bad[:5]
    # the code never costs the free shipping: the threshold applies to the goods before its discount
    web = small_cfg(**CHAOS_OFF).channels.web
    fee_added = run.execute("""
        SELECT count(*) FROM shop.orders o JOIN shop.promotions p ON p.id = o.promotion_id
        WHERE p.code IN (SELECT code FROM mkt.influencer_codes) AND o.shipping_fee > 0
          AND o.items_subtotal >= CASE o.currency WHEN 'CHF' THEN %s ELSE %s END
    """, (web.free_shipping_chf, web.free_shipping_eur)).fetchone()[0]
    assert fee_added == 0
    # an influencer customer's first order without any promotion would have been granted the discount.
    # Holds with chaos off only: a duplicate account of an influencer customer keeps the code on its shop
    # record, but its first order is the person's repeat order and never gets the discount.
    missing = run.execute("""
        SELECT count(*) FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id
        WHERE c.influencer_code IS NOT NULL AND NOT o.is_repeat_customer AND o.promotion_id IS NULL
    """).fetchone()[0]
    assert missing == 0
    # first orders of influencer customers that another promotion took (Black Week code, regular promotions)
    other = run.execute("""
        SELECT count(*) FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id
        JOIN shop.promotions p ON p.id = o.promotion_id
        WHERE c.influencer_code IS NOT NULL AND NOT o.is_repeat_customer AND p.code IS DISTINCT FROM c.influencer_code
    """).fetchone()[0]
    assert other > 0
    # ERP: ZINF exactly on the influencer code orders' items, never together with ZPRO
    erp = run.execute("""
        SELECT h.bstnk, bool_or(k.kschl = 'ZINF'), bool_or(k.kschl = 'ZPRO'), array_agg(DISTINCT k.kbetr)
                 FILTER (WHERE k.kschl = 'ZINF')
        FROM erp.vbak h JOIN erp.konv k ON k.knumv = h.knumv
        WHERE h.auart = 'ZWEB' GROUP BY h.bstnk HAVING bool_or(k.kschl = 'ZINF')
    """).fetchall()
    released = {r[0] for r in run.execute("SELECT bstnk FROM erp.vbak WHERE auart = 'ZWEB'")}
    assert {r[0] for r in erp} == {str(r[0]) for r in orders} & released  # payment failed: never in the ERP
    assert len(erp) > 30
    assert all(not r[2] and r[3] == [Decimal("-15.000")] for r in erp), [r for r in erp if r[2]][:5]


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


# -- trade deductions (section 4.3 R1-R3) -----------------------------------------------------------


def test_r1_paid_retail_invoices_reconcile_with_clearing_items(run):
    rows = run.execute("""
        SELECT k.vbeln, k.netwr + k.mwsbk gross, k.zzpaid_amount,
               coalesce((SELECT sum(c.dmbtr) FROM erp.zdeduct_clr c
                         WHERE c.zzkind = 'T' AND c.vbeln_vf = k.vbeln), 0) taken
        FROM erp.vbrk k JOIN erp.kna1 a ON a.kunnr = k.kunag
        WHERE k.fkart = 'F2' AND k.zzpaid_amount IS NOT NULL AND a.ktokd = 'Z001'
    """).fetchall()
    assert rows  # Belle pays after 30 days: some retail invoices are paid inside the window
    bad = [r for r in rows if r[2] + r[3] != r[1]]
    assert not bad, bad[:5]


def test_r2_closed_deductions_reconcile_with_clearing_items(run):
    bad = run.execute("""
        SELECT z.belnr FROM erp.zdeduct z LEFT JOIN erp.zdeduct_clr c USING (belnr)
        WHERE z.zzclose_date IS NOT NULL GROUP BY z.belnr, z.zzaccepted, z.zztaken, z.zzrepaid
        HAVING coalesce(sum(c.dmbtr) FILTER (WHERE c.zzkind = 'T'), 0)
             - coalesce(sum(c.dmbtr) FILTER (WHERE c.zzkind = 'P'), 0) <> coalesce(z.zzaccepted, 0)
            OR coalesce(sum(c.dmbtr) FILTER (WHERE c.zzkind = 'T'), 0) <> z.zztaken
            OR coalesce(sum(c.dmbtr) FILTER (WHERE c.zzkind = 'P'), 0) <> z.zzrepaid
    """).fetchall()
    assert not bad
    orphans = run.execute(
        "SELECT count(*) FROM erp.zdeduct_clr c WHERE NOT EXISTS (SELECT 1 FROM erp.zdeduct z WHERE z.belnr = c.belnr)"
    ).fetchone()[0]
    assert orphans == 0


def test_r3_promotion_claims_match_items_and_agreements(run):
    bad = run.execute("""
        SELECT z.belnr FROM erp.zdeduct z JOIN erp.zdeduct_item i USING (belnr)
        LEFT JOIN erp.kona k ON k.knuma = z.knuma
        WHERE z.zztype = 'ZP01' GROUP BY z.belnr, z.wrbtr
        HAVING z.wrbtr <> sum(i.wrbtr) OR NOT bool_and(k.knuma IS NOT NULL)
    """).fetchall()
    assert not bad
