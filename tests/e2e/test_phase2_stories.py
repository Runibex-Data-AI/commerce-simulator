"""Phase-2 stories end to end (design section 6.6, WP6): every scenario and every defect plants truth rows
and is visible in the business data.

Small config, seven months from 2025-03-03, the scenario windows scaled into it (hero 3 weeks,
influencer 6 weeks, overstock 6 weeks with a 4-week cut phase), chaos at its default rates.
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from cpgsim import chaos

from . import phase2_checks as p2
from .conftest import simulation, small_cfg, utc
from .test_scenarios_e2e import CONTRIBUTION_SQL

pytestmark = [pytest.mark.db, pytest.mark.slow]

START, UNTIL = date(2025, 3, 3), date(2025, 10, 6)
SCENARIOS = [
    {"id": "influencer_cohort_churn", "influencer_code": "GLOWWITHMIA", "repurchase_multiplier": 0.3,
     "acquisition_boost": 2.5, "start": "2025-04-07", "end": "2025-05-19"},
    {"id": "retailer_overstock", "retailer": "mylo", "over_order": 1.30, "duration_weeks": 6, "cut_weeks": 4,
     "start": "2025-05-05"},
    {"id": "hero_oos_substitution", "sku": "auto", "duration_weeks": 3, "substitution_share": 0.40,
     "start": "2025-07-07"},
    {"id": "promo_margin_trap", "brand": "Velmora", "discount_pct": 0.25, "free_shipping": True,
     "deal_seeker_share": 1.0, "deal_seeker_return_rate": 0.15, "cherry_pick_share": 0.8,
     "start": "2025-06-02", "end": "2025-06-29"},
    {"id": "carrier_decay", "carrier": "Swiftbox", "region": "DE-BY", "delay_multiplier": 2.5,
     "exception_multiplier": 3.0, "start": "2025-08-04"},
]


@pytest.fixture(scope="module")
def run(fresh_db_module):
    dsn = fresh_db_module()
    cfg = small_cfg(scenarios=SCENARIOS)
    with simulation(dsn, cfg, START) as sim:
        sim.backfill(utc(UNTIL))
    conn = psycopg.connect(dsn)
    yield conn, cfg
    conn.close()


def _bad(checks, skip=()):
    return [(c.name, c.value) for c in checks if not c.ok and not any(s in c.name for s in skip)]


def test_every_scenario_and_defect_has_truth(run):
    conn, _ = run
    wins = {r[0] for r in conn.execute("SELECT DISTINCT scenario_id FROM truth.scenario_windows")}
    assert {s["id"] for s in SCENARIOS} | set(chaos.DEFECTS) <= wins
    effects = dict(conn.execute("SELECT scenario_id, count(*) FROM truth.planted_effects GROUP BY 1").fetchall())
    missing = [s for s in [*(s["id"] for s in SCENARIOS), *chaos.DEFECTS] if not effects.get(s)]
    assert not missing, effects


def test_hero_oos_substitution_is_visible(run):
    conn, _ = run
    checks = p2.hero_checks(conn)
    # the small volume makes a retailer's hero week noisy; the dip is asserted at default volume (acceptance)
    assert not _bad(checks, skip=("dip", "glance_views", "ZL01")), checks


def test_retailer_overstock_is_visible(run):
    conn, _ = run
    assert not _bad(p2.overstock_checks(conn))


def test_influencer_cohort_churn_is_visible(run):
    conn, _ = run
    # small volume and a short horizon: the direction only (0.5x is measured at default volume)
    assert not _bad(p2.influencer_checks(conn, horizon_days=120, max_ratio=1.0))


def test_promo_margin_trap_is_visible_with_its_campaigns(run):
    conn, _ = run
    assert not _bad(p2.margin_trap_checks(conn, CONTRIBUTION_SQL))


def test_marketing_metrics_are_consistent(run):
    conn, _ = run
    # small volume: a coupon-site campaign that starts days before the end may have no order yet
    assert not _bad(p2.mkt_metrics_checks(conn), skip=("coupon",))
    assert not _bad(p2.duplicate_account_checks(conn))


def test_carrier_decay_is_visible(run):
    conn, _ = run
    assert not _bad(p2.carrier_decay_checks(conn))


def test_every_defect_has_its_footprint(run):
    conn, cfg = run
    c = cfg.chaos
    rates = {
        "duplicate_customer": c.duplicate_customer_rate, "malformed_postcode": c.malformed_postcode_rate,
        "status_error": c.status_error_rate, "duplicate_carrier_event": c.duplicate_carrier_event_rate,
        "missing_order_ref": c.missing_order_ref_rate, "unmapped_article": c.unmapped_article_rate,
        "sellout_restatement": c.sellout_restatement_rate, "free_text_return_reason": c.free_text_return_reason_rate,
        "gdpr_erasure": c.gdpr_erasure_rate_monthly,
    }
    checks = p2.noise_checks(conn, rates, months=(UNTIL - START).days / 30.44)
    assert not _bad(checks), [(x.name, x.value) for x in checks]
