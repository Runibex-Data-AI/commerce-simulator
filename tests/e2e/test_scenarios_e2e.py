"""Planted scenarios through the real engine: does the story show in the data?

``promo_margin_trap`` (SPEC: "Orders up, contribution margin negative"): a
reduced-volume run around the default window (2026-03-02 to 2026-03-29),
with contribution computed the way an analyst would, from the business
schemas only: billed net revenue minus credit memos (``erp.vbrk``/``vbrp``,
G2 subtracted), minus cost of goods (``vbrp.wavwr``, reversed for credited
items), minus parcel freight (``erp.likp.zzfreight_eur``), for every order
that redeemed the scenario promotion (``shop.orders`` joined to
``erp.vbak.bstnk``).
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from .conftest import CHAOS_OFF, simulation, small_cfg, utc

pytestmark = [pytest.mark.db, pytest.mark.slow]

WINDOW = ("2026-03-02", "2026-03-29")

#: Orders per day in the 4 weeks before the window and during it (local dates).
ORDERS_PER_DAY_SQL = """
SELECT (count(*) FILTER (WHERE d BETWEEN '2026-02-02' AND '2026-03-01'))::float / 28,
       (count(*) FILTER (WHERE d BETWEEN '2026-03-02' AND '2026-03-29'))::float / 28
FROM (SELECT (placed_at AT TIME ZONE 'Europe/Berlin')::date AS d FROM shop.orders) o
"""

#: CTE ``c``: one row per order that redeemed the scenario promotion, in EUR.
CONTRIBUTION_SQL = """
WITH promo AS (
    SELECT DISTINCT o.promotion_id AS id
    FROM truth.planted_effects e JOIN shop.orders o ON o.id = (e.system_refs->>'shop_order_id')::uuid
    WHERE e.scenario_id = 'promo_margin_trap' AND e.effect = 'promo_discount'
), red AS (
    SELECT o.id, c.signup_source = 'deal_site' AS deal, k.vbeln
    FROM shop.orders o
    JOIN shop.customers c ON c.id = o.customer_id
    LEFT JOIN erp.vbak k ON k.bstnk = o.id::text
    WHERE o.promotion_id IN (SELECT id FROM promo)
), bill AS (
    SELECT r.id,
           sum(CASE WHEN b.fkart = 'G2' THEN -1 ELSE 1 END * p.netwr * b.kurrf) AS net,
           sum(CASE WHEN b.fkart = 'G2' THEN -1 ELSE 1 END * coalesce(p.wavwr, 0) * b.kurrf) AS cogs
    FROM red r JOIN erp.vbrp p ON p.aubel = r.vbeln JOIN erp.vbrk b ON b.vbeln = p.vbeln
    GROUP BY r.id
), fr AS (
    SELECT r.id, sum(l.zzfreight_eur) AS freight
    FROM red r
    JOIN (SELECT DISTINCT vbeln, vgbel FROM erp.lips) i ON i.vgbel = r.vbeln
    JOIN erp.likp l ON l.vbeln = i.vbeln
    GROUP BY r.id
), est AS (
    SELECT (e.system_refs->>'shop_order_id')::uuid AS id, (e.detail->>'contribution_eur')::float AS est
    FROM truth.planted_effects e
    WHERE e.scenario_id = 'promo_margin_trap' AND e.effect = 'promo_discount'
), c AS (
    SELECT r.id, r.deal, coalesce(b.net, 0) AS net, coalesce(b.cogs, 0) AS cogs,
           coalesce(f.freight, 0) AS freight,
           coalesce(b.net, 0) - coalesce(b.cogs, 0) - coalesce(f.freight, 0) AS contrib, e.est
    FROM red r LEFT JOIN bill b ON b.id = r.id LEFT JOIN fr f ON f.id = r.id LEFT JOIN est e ON e.id = r.id
)
"""


@pytest.fixture(scope="module")
def margin_trap(fresh_db_module):
    dsn = fresh_db_module()
    cfg = small_cfg(scenarios=[{"id": "promo_margin_trap", "start": WINDOW[0], "end": WINDOW[1]}], **CHAOS_OFF)
    with simulation(dsn, cfg, date(2026, 1, 5)) as sim:
        # four weeks after the window, so returns and their credit memos are booked
        sim.backfill(utc(date(2026, 4, 27)))
    conn = psycopg.connect(dsn)
    yield conn, cfg
    conn.close()


def test_promo_margin_trap_orders_up(margin_trap):
    conn, _ = margin_trap
    before, during = conn.execute(ORDERS_PER_DAY_SQL).fetchone()
    assert during > 1.3 * before, (before, during)


def test_promo_margin_trap_contribution_negative(margin_trap):
    conn, _ = margin_trap
    n, net, cogs, freight, contrib, deal_n, deal_contrib, est = conn.execute(
        CONTRIBUTION_SQL
        + " SELECT count(*), sum(net), sum(cogs), sum(freight), sum(contrib),"
        "  count(*) FILTER (WHERE deal), sum(contrib) FILTER (WHERE deal), sum(est) FROM c"
    ).fetchone()
    assert n > 500 and deal_n > 0.25 * n, (n, deal_n)
    assert net > 0 and cogs > 0 and freight > 0
    # The planted story: the promoted orders lose money in aggregate ...
    assert contrib < 0, (net, cogs, freight, contrib)
    # ... the deal seekers' orders lose money on their own ...
    assert deal_contrib < 0
    # ... and truth carries a contribution estimate for every redeemed order that agrees in sign.
    missing = conn.execute(CONTRIBUTION_SQL + " SELECT count(*) FROM c WHERE est IS NULL").fetchone()[0]
    assert missing == 0
    # The estimate's sign rests on the dearer Austrian and Swiss parcels (trap orders ship free, so the
    # shop's shipping fee does not enter): German trap orders are slightly positive in aggregate, and in
    # this reduced-volume run the sum is about 3 standard errors below 0 (about -830 on 3700 orders; each
    # EUR 1 off the Austrian parcel cost raises it by about 500).
    assert est < 0


def test_promo_margin_trap_truth_detail(margin_trap):
    conn, _ = margin_trap
    detail = conn.execute(
        "SELECT detail FROM truth.planted_effects"
        " WHERE scenario_id = 'promo_margin_trap' AND effect = 'promo_discount' LIMIT 1"
    ).fetchone()[0]
    for k in ("net_revenue_eur", "cogs_eur", "freight_eur", "discount_eur", "shipping_waived_eur",
              "contribution_eur", "deal_seeker"):
        assert k in detail, detail
    assert detail["contribution_eur"] == pytest.approx(
        detail["net_revenue_eur"] - detail["cogs_eur"] - detail["freight_eur"], abs=0.02
    )


def test_deal_seekers_only_during_the_window(margin_trap):
    conn, _ = margin_trap
    lo, hi, returned = conn.execute(
        "SELECT min(o.placed_at AT TIME ZONE 'Europe/Berlin')::date, max(o.placed_at AT TIME ZONE 'Europe/Berlin')::date,"
        " avg((o.return_requested_at IS NOT NULL)::int) FILTER (WHERE o.delivered_at IS NOT NULL)"
        " FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id WHERE c.signup_source = 'deal_site'"
    ).fetchone()
    assert lo >= date(2026, 3, 2) and hi <= date(2026, 3, 29)
    assert 0.06 < float(returned) < 0.2
