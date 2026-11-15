"""influencer_cohort_churn at default volume, pooled over several seeds (design section 6.4, WP6).

One seed acquires only 250-500 customers with the scenario's code inside the window, so the share
with a second order within 180 days carries a standard error of about 0.05 on the code-vs-others
ratio, whose expected value is about 0.4 (not 0.3: some second orders come from paths the repurchase
factor does not scale). At one seed the 0.5 limit is only about two standard errors away (seed 3
measured 0.48). Pooling four seeds quadruples the cohort without changing the scenario.
Measured when written: per-seed ratios 0.33 / 0.39 / 0.45 / 0.48, pooled 224/1578 = 0.142 vs
1162/3337 = 0.348, ratio 0.41 with a standard error of about 0.027 (the limit about 3.4 SE away).

The full default config (every channel, feeds, deductions, chaos and the five default windows) runs
per seed, only ``seed`` differs; nothing is switched off, so no claim about which parts of the
business the web cohort depends on is needed. History starts 2025-03-03, five weeks before the
window (2025-04-07..2025-06-29), and the backfill runs to 2026-01-05, past the last cohort
customer's 180-day horizon. The seeds run in parallel processes, one scratch database each
(about eight minutes on an 11-core laptop with the dev Postgres in Docker).
"""

from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date

import psycopg
import pytest

from cpgsim.config import load_config

from . import phase2_checks as p2
from .conftest import simulation, utc

pytestmark = [pytest.mark.db, pytest.mark.slow, pytest.mark.acceptance]

START, UNTIL = date(2025, 3, 3), date(2026, 1, 5)
SEEDS = (20261114, 1, 2, 3)  # the config seed and three others
HORIZON_DAYS = 180
MAX_RATIO = 0.5


def _run_seed(dsn: str, seed: int) -> tuple[int, p2.InfluencerCohorts, float]:
    """Backfill the default config with ``seed`` into ``dsn``; return the window's cohort counts."""
    t0 = time.monotonic()
    with simulation(dsn, load_config(seed=seed), START) as sim:
        sim.backfill(utc(UNTIL))
    with psycopg.connect(dsn) as conn:
        cc = p2.influencer_cohort_counts(conn, HORIZON_DAYS)
    return seed, cc, time.monotonic() - t0


@pytest.fixture(scope="module")
def cohorts(fresh_db_module):
    dsns = [fresh_db_module() for _ in SEEDS]
    with ProcessPoolExecutor(len(SEEDS), mp_context=multiprocessing.get_context("spawn")) as pool:
        return list(pool.map(_run_seed, dsns, SEEDS))


def _row(seed, cc: p2.InfluencerCohorts, secs: float | None = None) -> dict:
    t, o = cc.target, cc.other
    row = {"seed": seed, "target": f"{t.second}/{t.n} = {t.share:.4f}", "other": f"{o.second}/{o.n} = {o.share:.4f}",
           "ratio": round(t.share / o.share, 3) if o.share else None}
    return row | ({"secs": round(secs)} if secs is not None else {})


def test_influencer_cohort_second_orders_pooled_over_seeds(cohorts):
    rows = [_row(seed, cc, secs) for seed, cc, secs in cohorts]
    for seed, cc, _ in cohorts:
        assert cc is not None, (seed, "no influencer_cohort_churn window")
        assert cc.code == "GLOWWITHMIA" and (cc.start, cc.end) == (date(2025, 4, 7), date(2025, 6, 30)), (seed, cc)
        # every seed points the same way: the code's cohort comes back less often than the other codes'
        assert cc.target.n > 0 and cc.target.share < cc.other.share, rows
    target = sum((cc.target for _, cc, _ in cohorts), p2.Cohort())
    other = sum((cc.other for _, cc, _ in cohorts), p2.Cohort())
    pooled = _row("pooled", p2.InfluencerCohorts("GLOWWITHMIA", START, UNTIL, HORIZON_DAYS, target, other))
    print(*rows, pooled, sep="\n")  # shown with -s or -rP
    assert target.share < MAX_RATIO * other.share, (pooled, rows)
