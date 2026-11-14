"""Phase-2 acceptance at default volume (design section 6.4, WP6).

Default config (chaos on, the five default windows), backfill 2025-01-06 -> 2026-10-19: 2026 Q1 including
Belle's P03 restatement, the hero window's dip weeks and the overstock cut phase, as in the design, and
further through 2026 Q3 (every retailer's second Q3, delivered three weeks after its end) and the
carrier_decay window from 2026-08-03 (phase-2 review). AC2 at default volume covers every scenario and
every defect. Runs about ten minutes; marked ``acceptance`` as well as ``slow``.
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from cpgsim.config import load_config
from cpgsim.models import arrivals

from . import phase2_checks as p2
from .conftest import simulation, utc
from .test_scenarios_e2e import CONTRIBUTION_SQL

pytestmark = [pytest.mark.db, pytest.mark.slow, pytest.mark.acceptance]

START, UNTIL = date(2025, 1, 6), date(2026, 10, 19)
EXPECTED = {
    "KRANZ": ["2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q3"],
    "MYLO": ["2025Q2", "2025Q3", "2025Q4", "2026Q3"],
    "SHOPNOVA": ["2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q3"],
    "BELLE": ["2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q3"],
}


@pytest.fixture(scope="module")
def run(fresh_db_module):
    dsn = fresh_db_module()
    arrivals.reset_clip_stats()
    with simulation(dsn, load_config(), START) as sim:
        sim.backfill(utc(UNTIL))
    clipped = arrivals.clip_stats()
    conn = psycopg.connect(dsn)
    yield conn, clipped
    conn.close()


def test_sellin_sellout_converge_per_quarter(run):
    conn, _ = run
    results = p2.ac1(conn, START, UNTIL)
    verdict = p2.ac1_verdict(results)
    for code, v in verdict.items():
        assert v["checked"] == EXPECTED[code], (code, v)
        assert v["ok"], (code, v, [(r.label, round(r.ratio, 4)) for r in results if r.retailer == code])


def test_value_shares(run):
    conn, _ = run
    shares = p2.value_shares(conn, date(2025, 2, 1), date(2026, 1, 1))  # before Mylo's 2026 push
    target = {"KRANZ": 0.35, "MYLO": 0.25, "SHOPNOVA": 0.20, "BELLE": 0.10, "WEB": 0.10}
    # the web goods share is the calibration target (CH-4)
    assert abs(shares["WEB"] - 0.10) <= 0.007, shares
    for k, t in target.items():
        assert abs(shares[k] - t) <= 0.015, (k, shares)


def test_hero_and_overstock_visible_at_default_volume(run):
    conn, _ = run
    bad = [(c.name, c.value) for c in p2.hero_checks(conn) + p2.overstock_checks(conn) if not c.ok]
    assert not bad


def test_influencer_cohort_churn_at_default_volume(run):
    conn, _ = run
    bad = [(c.name, c.value) for c in p2.influencer_checks(conn, horizon_days=180, max_ratio=0.5) if not c.ok]
    assert not bad


def test_promo_margin_trap_and_marketing_metrics(run):
    conn, _ = run
    checks = p2.margin_trap_checks(conn, CONTRIBUTION_SQL) + p2.mkt_metrics_checks(conn)
    bad = [(c.name, c.value) for c in checks if not c.ok]
    assert not bad


def test_carrier_decay_at_default_volume(run):
    conn, _ = run
    bad = [(c.name, c.value) for c in p2.carrier_decay_checks(conn) if not c.ok]
    assert not bad


def test_every_defect_footprint_at_default_volume(run):
    conn, _ = run
    c = load_config().chaos
    rates = {
        "duplicate_customer": c.duplicate_customer_rate, "malformed_postcode": c.malformed_postcode_rate,
        "status_error": c.status_error_rate, "duplicate_carrier_event": c.duplicate_carrier_event_rate,
        "missing_order_ref": c.missing_order_ref_rate, "unmapped_article": c.unmapped_article_rate,
        "sellout_restatement": c.sellout_restatement_rate, "free_text_return_reason": c.free_text_return_reason_rate,
        "gdpr_erasure": c.gdpr_erasure_rate_monthly,
    }
    checks = p2.noise_checks(conn, rates, months=(UNTIL - START).days / 30.44) + p2.duplicate_account_checks(conn)
    bad = [(x.name, x.value) for x in checks if not x.ok]
    assert not bad


def test_no_clipped_arrival_candidates(run):
    _, clipped = run
    assert clipped["count"] == 0, clipped
