"""Live scenario switches: visibility by simulation time, late windows, early ends."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cpgsim.domain import promotions
from cpgsim.domain.events import PromotionChanged, PromotionDefined
from cpgsim.domain.model import Promotion
from cpgsim.engine.queue import Event
from cpgsim.engine.registry import ARRIVAL_SOURCES, HANDLERS
from cpgsim.scenarios import ScenarioManager
from cpgsim.scenarios import manager as mgr
from cpgsim.writers import keys
from cpgsim.writers.base import Dispatcher, WriterContext
from cpgsim.writers.shop import ShopWriter

from test_scenarios import bootstrap, local, make_cfg, make_world, ref  # noqa: F401  (fixture)

UTC = timezone.utc
EPS = timedelta(microseconds=1)


def run_gated(world, until, slice_len):
    """Runner with the engine's switch handling: advance_to per slice, split at switches."""
    sm = world.scenarios
    q = world.queue
    clock = world.now
    seen = []
    while clock < until:
        end = min(clock + slice_len, until)
        sw = sm.next_switch(clock)
        if sw is not None and sw < end:
            end = sw
        sm.advance_to(clock)
        for name in (mgr.RECORD_KIND, "promo.announce"):
            for ev in ARRIVAL_SOURCES[name](world, clock, end):
                assert clock < ev.due <= end
                q.push(ev)
        # what a demand model would see across the slice
        seen.append((end, sm.factor("carrier_delay", end, carrier="Swiftbox", region="DE-BY"), sm.version > 0))
        while (due := q.peek_due()) is not None and due <= end:
            ev = q.pop()
            world.now = ev.due
            HANDLERS[ev.kind](world, ev)
        world.now = clock = end
    return seen


def test_override_invisible_before_its_switch_time():
    sm = ScenarioManager(make_cfg([]))
    t = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
    sm.load_overrides([(1, "promo_margin_trap", "enable", t, {})])
    sm.advance_to(t - timedelta(hours=8))
    v0 = sm.version
    assert sm.active("promo_margin_trap", t + timedelta(days=1)) == []  # look-ahead sees nothing
    assert sm.scenario_promotions() == []
    assert sm.next_switch(t - timedelta(hours=8)) == t - EPS
    assert sm.next_switch(t - EPS) is None  # boundaries are strictly after
    sm.advance_to(t - 2 * EPS)
    assert sm.version == v0 and sm.windows() == []
    sm.advance_to(t - EPS)  # the slice (t - 1us, ...] contains t: visible for all of it
    assert sm.version == v0 + 1
    assert [w.window_id for w in sm.active("promo_margin_trap", t)] == ["promo_margin_trap:cli:1"]
    # a disable is gated the same way
    t2 = t + timedelta(hours=3)
    sm.load_overrides([(1, "promo_margin_trap", "enable", t, {}), (2, "promo_margin_trap", "disable", t2, {})])
    assert sm.window("promo_margin_trap:cli:1").end is None
    sm.advance_to(t2 - EPS)
    assert sm.window("promo_margin_trap:cli:1").end == t2


def test_ungated_manager_sees_every_row():
    """Without advance_to (tools, unit tests) all rows apply, as before."""
    sm = ScenarioManager(make_cfg([]))
    t = datetime(2030, 1, 1, tzinfo=UTC)
    sm.load_overrides([(1, "carrier_decay", "enable", t, {})])
    assert sm.window("carrier_decay:cli:1") is not None


@pytest.mark.parametrize("slice_len", [timedelta(minutes=7), timedelta(hours=1), timedelta(days=2)])
def test_same_results_whether_the_row_was_known_early_or_late(ref, slice_len):
    """An engine that knew the switch hours ahead (catch-up after downtime) sees
    exactly what one that learned it a moment before the switch saw."""
    cfg = make_cfg([])
    t1, t2 = local(2025, 1, 9, 13, 7), local(2025, 1, 9, 17, 0)
    rows = [(1, "carrier_decay", "enable", t1, {}), (2, "carrier_decay", "disable", t2, {})]
    out_runs = []
    for when_known in ("start", "late"):
        world, out = make_world(cfg, ref, local(2025, 1, 1))
        bootstrap(world)
        sm = world.scenarios
        sm.advance_to(world.now)
        if when_known == "start":
            sm.load_overrides(rows)
            seen = run_gated(world, local(2025, 1, 12), slice_len)
        else:
            seen = run_gated(world, t1 - timedelta(seconds=1), timedelta(seconds=30))
            sm.load_overrides(rows[:1])
            seen += run_gated(world, t2 - timedelta(seconds=1), timedelta(seconds=30))
            sm.load_overrides(rows)
            seen += run_gated(world, local(2025, 1, 12), slice_len)
        factors_at = {e: f for e, f, _ in seen}
        # the factor never applies before t1 or from t2 on
        assert all(f == 1.0 for e, f in factors_at.items() if e < t1 or e >= t2)
        out_runs.append([(type(e).__name__, e.at, getattr(e, "window_id", None)) for e in out])
    assert out_runs[0] == out_runs[1]
    assert [k for k, _, _ in out_runs[0]] == ["ScenarioWindowRecorded", "ScenarioWindowClosed"]
    assert out_runs[0][0][1] == t1 and out_runs[0][1][1] == t2


def test_late_config_window_starts_after_the_clock_and_is_recorded(ref):
    cfg = make_cfg([{"id": "promo_margin_trap", "start": "2025-01-06", "end": "2025-01-20"}])
    world, out = make_world(cfg, ref, local(2025, 1, 1))
    k = local(2025, 1, 10, 11, 30)  # the clock when a restart first saw the window
    world.now = k
    sm = world.scenarios
    sm.advance_to(k)
    sm.set_known_at({"promo_margin_trap:config:0": k})
    w = sm.window("promo_margin_trap:config:0")
    assert w.start == k + EPS and w.end == local(2025, 1, 21)
    evs = sm.truth_arrivals(k, k + timedelta(hours=1))
    assert [e.key for e in evs] == ["scen.window:promo_margin_trap:config:0"] and evs[0].due == k + EPS
    (spec,) = sm.scenario_promotions()
    assert spec["announce_not_before"] == k + EPS
    st = promotions._state(world)
    (rec,) = promotions._scenario_recs(st, world)
    assert rec.promo.announced_at == k + EPS and rec.promo.start == k + EPS
    ann = promotions.announce_arrivals(world, k, k + timedelta(minutes=1))
    assert [e.key for e in ann] == [f"promo.announce:{rec.promo.promo_id}"]


def test_late_config_window_in_the_future_only_moves_the_announcement(ref):
    cfg = make_cfg([{"id": "promo_margin_trap", "start": "2025-01-12", "end": "2025-01-20"}])
    world, _ = make_world(cfg, ref, local(2025, 1, 1))
    k = local(2025, 1, 10, 11, 30)  # less than 3 days before the start: announcement was due earlier
    world.now = k
    world.scenarios.set_known_at({"promo_margin_trap:config:0": k})
    assert world.scenarios.window("promo_margin_trap:config:0").start == local(2025, 1, 12)
    (rec,) = promotions._scenario_recs(promotions._state(world), world)
    assert rec.promo.announced_at == k + EPS and rec.promo.start == local(2025, 1, 12)


def test_late_config_window_already_over_is_dropped(ref):
    cfg = make_cfg([{"id": "carrier_decay", "start": "2025-01-02", "end": "2025-01-05"}])
    sm = ScenarioManager(cfg)
    sm.set_known_at({"carrier_decay:config:0": local(2025, 1, 10)})
    assert sm.windows() == []


def _shop_dispatcher():
    ref_ns = SimpleNamespace(catalogue=SimpleNamespace(get=lambda i: None), accounts=None)
    d = Dispatcher([ShopWriter(WriterContext(seed=3, cfg=None, ref=ref_ns, tz=None))])
    return d


def test_disable_ends_the_announced_scenario_promotion(ref):
    cfg = make_cfg([])
    world, out = make_world(cfg, ref, local(2025, 1, 1))
    sm = world.scenarios
    t1, t2 = local(2025, 1, 9, 10), local(2025, 1, 9, 11)
    rows = [(1, "promo_margin_trap", "enable", t1, {})]
    sm.advance_to(t1 - EPS)
    sm.load_overrides(rows)
    world.now = t1
    pid = promotions.SCENARIO_ID_BASE + mgr.CLI_INDEX_OFFSET + 1
    HANDLERS["promo.announce"](world, Event(due=t1, kind="promo.announce", key="a", data={"promo_id": pid}))
    defined = [e for e in out if isinstance(e, PromotionDefined)]
    assert defined and defined[0].promotion.end == t1 + timedelta(days=28) - timedelta(seconds=1)
    # disable becomes visible at t2; the close handler runs at t2
    sm.load_overrides(rows + [(2, "promo_margin_trap", "disable", t2, {})])
    sm.advance_to(t2 - EPS)
    world.now = t2
    (close,) = [e for e in sm.truth_arrivals(t2 - EPS, t2) if e.kind == mgr.CLOSE_KIND]
    HANDLERS[mgr.CLOSE_KIND](world, close)
    (changed,) = [e for e in out if isinstance(e, PromotionChanged)]
    assert changed.at == t2 and not changed.withdrawn
    assert changed.promotion.end == t2 - timedelta(seconds=1)
    assert world.store.get("promotion", pid).end == t2 - timedelta(seconds=1)
    # a second close of the same window changes nothing
    HANDLERS[mgr.CLOSE_KIND](world, close)
    assert len([e for e in out if isinstance(e, PromotionChanged)]) == 1

    # the shop writer turns it into an update of ends_at (and updated_at)
    d = _shop_dispatcher()
    d.dispatch(changed)
    buf = d.batch.table("shop.promotions")
    key = (keys.shop_promotion_id(3, pid),)
    assert buf.updates[key] == {"ends_at": t2 - timedelta(seconds=1), "updated_at": t2}


def test_shop_writer_soft_deletes_a_withdrawn_promotion():
    t = datetime(2025, 1, 9, 10, tzinfo=UTC)
    p = Promotion(promo_id=901001, channel="web", name="x", kind="automatic", code=None, discount_pct=0.25,
                  free_shipping=True, sku_ids=[], brand="Velmora", start=t, end=t, announced_at=t)
    d = _shop_dispatcher()
    d.dispatch(PromotionChanged(at=t, promotion=p, withdrawn=True))
    (ch,) = d.batch.table("shop.promotions").updates.values()
    assert ch["is_deleted"] is True and ch["deleted_at"] == t and ch["ends_at"] == t
    # retailer promotions are not in the shop
    d2 = _shop_dispatcher()
    d2.dispatch(PromotionChanged(at=t, promotion=replace(p, channel="mylo")))
    assert len(d2.batch) == 0
