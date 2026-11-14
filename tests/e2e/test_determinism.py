"""The determinism guarantee (SPEC: same seed gives identical row counts and totals).

Same seed + config + history start must give identical rows whatever the tick
length, commit batching or restarts.
"""

from __future__ import annotations

from datetime import date

import pytest

from .conftest import digest, simulation, small_cfg, utc

pytestmark = [pytest.mark.db, pytest.mark.slow]

START = date(2025, 11, 10)  # covers Black Friday week, month end and a first promo cycle
UNTIL = utc(date(2025, 12, 1))


def test_same_rows_across_tick_length_batching_and_restart(fresh_db):
    a, b = fresh_db(), fresh_db()

    with simulation(a, small_cfg(), START) as sim:
        sim.backfill(UNTIL)

    # Run B: stop mid-way (inside a working day), restart with a different
    # tick length and commit interval, stop again, resume with a third setting.
    with simulation(b, small_cfg(), START) as sim:
        sim.backfill(utc(date(2025, 11, 17), 10.5))
    with simulation(b, small_cfg(backfill={"tick_minutes": 15, "commit_every_hours": 5}), START) as sim:
        sim.backfill(utc(date(2025, 11, 26), 3.25))
    with simulation(b, small_cfg(backfill={"tick_minutes": 7, "commit_every_hours": 30}), START) as sim:
        sim.backfill(UNTIL)

    da, dbb = digest(a), digest(b)
    assert sum(n for n, _ in da.values()) > 50_000
    diff = {k: (da.get(k), dbb.get(k)) for k in sorted(set(da) | set(dbb)) if da.get(k) != dbb.get(k)}
    assert not diff, f"tables differ: {diff}"


def test_different_seed_gives_a_different_business(fresh_db):
    a, b = fresh_db(), fresh_db()
    until = utc(date(2025, 11, 14))
    with simulation(a, small_cfg(), START) as sim:
        sim.backfill(until)
    with simulation(b, small_cfg(seed=7), START) as sim:
        sim.backfill(until)
    assert digest(a)["shop.orders"] != digest(b)["shop.orders"]
