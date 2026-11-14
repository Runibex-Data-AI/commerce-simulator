"""Statistical sanity checks on generated distributions (SPEC: Tests).

One reduced-volume year through the real engine, then checks that seasonal
peaks fall in the right months, events lift demand, and rates sit within
tolerance of their configured defaults.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import psycopg
import pytest

from cpgsim.domain.reference import build_reference

from .conftest import simulation, small_cfg, utc

pytestmark = [pytest.mark.db, pytest.mark.slow]

START = date(2025, 1, 6)
UNTIL = utc(date(2026, 1, 12))


@pytest.fixture(scope="module")
def year(fresh_db_module):
    dsn = fresh_db_module()
    cfg = small_cfg(scenarios=[])
    with simulation(dsn, cfg, START) as sim:
        sim.backfill(UNTIL)
    conn = psycopg.connect(dsn)
    yield conn, cfg
    conn.close()


def units_by_month_and_category(conn, cfg):
    cat = {s.code: s.category for s in build_reference(cfg).catalogue.skus}
    rows = conn.execute(
        "SELECT date_trunc('month', o.placed_at AT TIME ZONE 'Europe/Berlin')::date, l.sku, sum(l.quantity)"
        " FROM shop.order_lines l JOIN shop.orders o ON o.id = l.order_id"
        " WHERE o.placed_at >= '2025-01-06' AND o.placed_at < '2026-01-01' GROUP BY 1, 2"
    ).fetchall()
    out: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    total: dict[int, float] = defaultdict(float)
    for month, sku, qty in rows:
        out[cat[sku]][month.month] += float(qty)
        total[month.month] += float(qty)
    share = {c: {m: v / total[m] for m, v in months.items()} for c, months in out.items()}
    return out, share


def test_seasonal_peaks(year):
    conn, cfg = year
    units, share = units_by_month_and_category(conn, cfg)
    sun = share["sun_care"]
    assert max(sun, key=sun.get) in (5, 6, 7, 8)
    assert sun[7] > 4 * sun[1]
    lip = share["lip_care"]
    assert (lip[1] + lip[12]) > 1.5 * (lip[6] + lip[7])
    gift = units["gift_set"]
    assert (gift[11] + gift[12]) / sum(gift.values()) > 0.75


def test_no_seasonal_products_on_subscriptions(year):
    conn, cfg = year
    seasonal = {s.code for s in build_reference(cfg).catalogue.skus if s.season_profile in ("gift", "sun", "after_sun")}
    sub_skus = {r[0] for r in conn.execute(
        "SELECT DISTINCT l.sku FROM shop.order_lines l JOIN shop.orders o ON o.id = l.order_id"
        " WHERE o.subscription_id IS NOT NULL").fetchall()}
    assert sub_skus and not (sub_skus & seasonal)


def _orders_per_day(conn) -> dict[date, int]:
    return dict(conn.execute(
        "SELECT (placed_at AT TIME ZONE 'Europe/Berlin')::date, count(*) FROM shop.orders"
        " WHERE placed_at >= '2025-10-27' AND placed_at < '2025-12-22' GROUP BY 1").fetchall())


def _mean(per_day: dict[date, int], first: date, last: date) -> float:
    xs = [n for d, n in per_day.items() if first <= d <= last]
    assert len(xs) == (last - first).days + 1
    return sum(xs) / len(xs)


def test_black_friday_lifts_web_orders(year):
    """All web orders (new, repeat and subscription) in Black Friday week (24-30 Nov 2025)
    against the three weeks before: black_friday_lift is 2.5."""
    conn, cfg = year
    per_day = _orders_per_day(conn)
    before = _mean(per_day, date(2025, 11, 3), date(2025, 11, 23))
    lift = _mean(per_day, date(2025, 11, 24), date(2025, 11, 30)) / before
    assert 2.2 < lift < 2.9, lift


def test_christmas_run_up_lifts_web_orders(year):
    """1-20 December against the three weeks before Black Friday week: christmas_lift is 1.6."""
    conn, cfg = year
    per_day = _orders_per_day(conn)
    before = _mean(per_day, date(2025, 11, 3), date(2025, 11, 23))
    lift = _mean(per_day, date(2025, 12, 1), date(2025, 12, 20)) / before
    assert 1.4 < lift < 1.8, lift


def test_existing_customers_respond_to_black_friday(year):
    """Repeat orders (customers registered before the order day) rise too, not only first orders."""
    conn, _ = year
    rows = dict(conn.execute(
        "SELECT o.placed_at >= '2025-11-23 23:00+00', count(*) FROM shop.orders o"
        " JOIN shop.customers c ON c.id = o.customer_id"
        " WHERE o.subscription_id IS NULL AND o.placed_at - c.created_at > interval '1 day'"
        " AND o.placed_at >= '2025-11-02 23:00+00' AND o.placed_at < '2025-11-30 23:00+00' GROUP BY 1"
    ).fetchall())
    lift = rows[True] / (rows[False] / 3)
    assert lift > 1.7, lift


def test_intraday_shape(year):
    conn, _ = year
    by_hour = dict(conn.execute(
        "SELECT extract(hour FROM placed_at AT TIME ZONE 'Europe/Berlin')::int, count(*)"
        " FROM shop.orders WHERE subscription_id IS NULL GROUP BY 1").fetchall())
    assert max(by_hour, key=by_hour.get) in (18, 19, 20, 21)
    assert min(by_hour, key=by_hour.get) in (2, 3, 4, 5)


def test_rates_within_tolerance(year):
    conn, cfg = year
    delivered, returned = conn.execute(
        "SELECT count(*) FILTER (WHERE delivered_at IS NOT NULL),"
        " count(*) FILTER (WHERE delivered_at IS NOT NULL AND return_requested_at IS NOT NULL)"
        " FROM shop.orders WHERE placed_at < '2025-12-01'").fetchone()
    assert 0.02 < returned / delivered < 0.045
    mix = dict(conn.execute(
        "SELECT p.method, count(*)::float / sum(count(*)) OVER () FROM shop.payments p"
        " JOIN shop.orders o ON o.id = p.order_id WHERE o.country = 'DE' AND p.attempt_no = 1"
        " GROUP BY 1").fetchall())
    assert abs(mix["paypal"] - 0.38) < 0.05
    assert abs(mix["invoice_bnpl"] - 0.27) < 0.05
    failed = conn.execute(
        "SELECT avg((status = 'payment_failed')::int) FROM shop.orders").fetchone()[0]
    assert 0.005 < float(failed) < 0.03


def test_hero_skus_carry_about_half_the_units(year):
    conn, cfg = year
    heroes = {s.code for s in build_reference(cfg).catalogue.skus if s.hero}
    rows = conn.execute("SELECT sku, sum(quantity) FROM shop.order_lines GROUP BY 1").fetchall()
    total = sum(float(q) for _, q in rows)
    hero = sum(float(q) for s, q in rows if s in heroes)
    assert 0.35 < hero / total < 0.65


def test_retail_orders_weekly_per_dc(year):
    conn, _ = year
    weeks = conn.execute(
        "SELECT zzkunwe, count(*), count(DISTINCT date_trunc('week', audat))"
        " FROM erp.vbak WHERE auart = 'ZEDI' GROUP BY 1").fetchall()
    # 3 + 2 drugstore DCs and 2 perfumery warehouses order weekly, 2 marketplace FCs Mon/Wed/Fri
    assert len(weeks) == 9
    per_week = sorted(docs / distinct_weeks for _, docs, distinct_weeks in weeks)
    assert per_week == [1.0] * 7 + [3.0] * 2, weeks
    assert all(distinct_weeks >= 50 for *_, distinct_weeks in weeks)
