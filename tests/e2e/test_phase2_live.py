"""Phase-2 live behaviour (design section 6.6, WP6).

* A scenario switched on live (``promo_margin_trap`` from the CLI) creates its marketing campaigns
  causally: every ``campaign_id`` in ``mkt.campaign_spend_daily`` and ``mkt.attributions`` exists in
  ``mkt.campaigns``, also right after the switch, and regular campaign ids do not change.
* A status error is visible between its set and its fix: a run stopped in between shows the wrong
  status, and the same run continued shows the corrected status with the residue timestamp.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import psycopg
import pytest

from . import phase2_checks as p2
from .conftest import simulation, small_cfg, utc
from .test_live import insert_override

pytestmark = [pytest.mark.db, pytest.mark.slow]

ORPHANS = """
SELECT (SELECT count(*) FROM mkt.campaign_spend_daily s WHERE NOT EXISTS
          (SELECT 1 FROM mkt.campaigns c WHERE c.campaign_id = s.campaign_id)),
       (SELECT count(*) FROM mkt.attributions a WHERE a.campaign_id IS NOT NULL AND NOT EXISTS
          (SELECT 1 FROM mkt.campaigns c WHERE c.campaign_id = a.campaign_id))
"""


def q(dsn: str, query: str, params=()):
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def test_live_scenario_campaigns_are_causal(fresh_db):
    start, until = date(2025, 11, 3), utc(date(2025, 11, 24))
    t1 = utc(date(2025, 11, 12), 14.5)  # enable promo_margin_trap from the CLI
    cfg = small_cfg(scenarios=[])
    a, b = fresh_db(), fresh_db()
    with simulation(a, cfg, start) as sim:
        sim.backfill(t1 - timedelta(seconds=1))
        insert_override(a, "promo_margin_trap", "enable", t1)
        # just past the start and the first export that can see it
        sim.backfill(t1 + timedelta(hours=16))
        assert q(a, ORPHANS) == [(0, 0)]
        sim.backfill(until)
    assert q(a, ORPHANS) == [(0, 0)]
    camps = q(a, "SELECT campaign_id, channel, status, created_date, start_date FROM mkt.campaigns"
                 " WHERE promo_ref IS NOT NULL AND campaign_id LIKE '%%-X%%' ORDER BY 1")
    assert {c[1] for c in camps} == {"email", "affiliate"}, camps
    assert all(c[3] >= t1.date() for c in camps)  # defined no earlier than the switch
    spend = q(a, "SELECT count(*), min(spend_date) FROM mkt.campaign_spend_daily WHERE campaign_id = ANY(%s)",
              ([c[0] for c in camps],))[0]
    assert spend[0] > 0 and spend[1] >= t1.date()
    # regular campaign ids are the same as without the switch
    with simulation(b, cfg, start) as sim:
        sim.backfill(until)
    regular = "SELECT campaign_id FROM mkt.campaigns WHERE campaign_id NOT LIKE '%%-X%%' ORDER BY 1"
    assert q(a, regular) == q(b, regular)


def test_status_error_is_visible_between_set_and_fix(fresh_db):
    start, until = date(2025, 9, 1), utc(date(2025, 10, 6))
    cfg = small_cfg(chaos={"status_error_rate": 0.05})
    a, b = fresh_db(), fresh_db()
    with simulation(a, cfg, start) as sim:
        sim.backfill(until)
    rows = q(a, "SELECT system_refs->>'shop_order_id', detail FROM truth.planted_effects"
                " WHERE scenario_id = 'status_error' AND detail->>'system' = 'shop' ORDER BY effective_at LIMIT 1")
    assert rows, "no shop status error planted"
    oid, detail = rows[0]
    set_at = datetime.fromisoformat(detail["set_at"])
    fixed_at = datetime.fromisoformat(detail["corrected_at"])
    # just after the error: a real lifecycle step before the fix (e.g. shipping) overwrites the status, as in
    # the real systems, so the row is read right after the set
    mid = set_at + timedelta(seconds=1)
    with simulation(b, cfg, start) as sim:
        sim.backfill(mid)
        ((status, cancelled_at),) = q(b, "SELECT status, cancelled_at FROM shop.orders WHERE id = %s", (oid,))
        assert status == "cancelled" and cancelled_at == set_at  # the error, as a BI tool would load it
        sim.backfill(until)
    ((status, cancelled_at, updated_at),) = q(
        b, "SELECT status, cancelled_at, updated_at FROM shop.orders WHERE id = %s", (oid,))
    assert status != "cancelled" and cancelled_at == set_at and updated_at >= fixed_at  # corrected, residue kept
    assert p2.one(psycopg.connect(b), "SELECT count(*) FROM shop.orders WHERE status <> 'cancelled'"
                  " AND cancelled_at IS NOT NULL") > 0
