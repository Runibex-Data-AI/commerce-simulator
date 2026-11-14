"""Scenario manager: windows, overrides, factors, promotions and truth recording."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cpgsim.config import Config
from cpgsim.domain.events import PlantedEffect, ScenarioWindowClosed, ScenarioWindowRecorded
from cpgsim.domain.model import ENTITY_KINDS
from cpgsim.domain.reference import build_reference
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import EventQueue
from cpgsim.engine.registry import ARRIVAL_SOURCES, BOOTSTRAPS, HANDLERS
from cpgsim.engine.world import World
from cpgsim.scenarios import (
    ScenarioManager,
    ScenarioParamError,
    UnknownScenarioError,
)
from cpgsim.scenarios import manager as mgr

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


def local(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=BERLIN).astimezone(UTC)


def make_cfg(scenarios=None, **kw):
    data = {"scenarios": scenarios if scenarios is not None else [
        {"id": "carrier_decay", "carrier": "Swiftbox", "region": "DE-BY", "start": "2026-08-03",
         "delay_multiplier": 2.5, "exception_multiplier": 3.0},
        {"id": "promo_margin_trap", "brand": "Velmora", "discount_pct": 0.25, "free_shipping": True,
         "start": "2026-03-02", "end": "2026-03-29"},
    ]}
    data.update(kw)
    return Config.model_validate(data)


@pytest.fixture(scope="module")
def ref():
    return build_reference(Config())


def make_world(cfg, ref, history_start):
    collected: list = []
    store = EntityStore()
    for kind, cls in ENTITY_KINDS.items():
        store.register(kind, cls)
    world = World(cfg, store, EventQueue(), emit=collected.append)
    world.ref = ref
    world.scenarios = ScenarioManager(cfg)
    world.now = history_start
    world.history_start = history_start
    return world, collected


def run(world, until, slice_len=timedelta(hours=1)):
    """Minimal runner: arrival sources per slice, then drain the queue."""
    q = world.queue
    clock = world.now
    while clock < until:
        end = min(clock + slice_len, until)
        for name in sorted(ARRIVAL_SOURCES):
            if name != mgr.RECORD_KIND:
                continue
            for ev in ARRIVAL_SOURCES[name](world, clock, end):
                assert clock < ev.due <= end
                q.push(ev)
        while (due := q.peek_due()) is not None and due <= end:
            ev = q.pop()
            world.now = ev.due
            HANDLERS[ev.kind](world, ev)
        world.now = clock = end


def bootstrap(world):
    for _order, name, fn in BOOTSTRAPS:
        if fn is mgr.record_initial_windows:
            fn(world)


# -- construction ----------------------------------------------------------------


def test_config_windows_local_dates_end_inclusive():
    sm = ScenarioManager(make_cfg())
    ws = sm.windows()
    assert [w.window_id for w in ws] == ["carrier_decay:config:0", "promo_margin_trap:config:1"]
    cd, pm = ws
    assert cd.start == local(2026, 8, 3) and cd.end is None and cd.source == "config"
    assert pm.start == local(2026, 3, 2)
    assert pm.end == local(2026, 3, 30)  # end day inclusive -> midnight after
    assert pm.params == {"brand": "Velmora", "discount_pct": 0.25, "free_shipping": True}
    assert cd.params["exception_multiplier"] == 3.0
    assert sm.version == 0
    assert sm.active("promo_margin_trap", local(2026, 3, 29, 23, 59))
    assert not sm.active("promo_margin_trap", local(2026, 3, 30))
    assert not sm.active("promo_margin_trap", local(2026, 3, 1, 23, 59))
    assert sm.windows("carrier_decay") == [cd]


def test_default_config_builds():
    sm = ScenarioManager(Config.model_validate({"scenarios": [
        {"id": "carrier_decay", "start": "2026-08-03"}]}))
    (w,) = sm.windows()
    assert w.params == {"carrier": "Swiftbox", "region": "DE-BY", "delay_multiplier": 2.5,
                        "exception_multiplier": 3.0}


def test_unknown_scenario_raises():
    with pytest.raises(UnknownScenarioError, match="carier_decay"):
        ScenarioManager(make_cfg([{"id": "carier_decay"}]))


def test_phase2_scenario_warns_and_is_ignored(caplog):
    with caplog.at_level(logging.WARNING, logger="cpgsim.scenarios"):
        sm = ScenarioManager(make_cfg([{"id": "retailer_overstock", "start": "2026-01-05"}]))
    assert sm.windows() == []
    assert "not implemented yet" in caplog.text


def test_disabled_and_bad_params():
    sm = ScenarioManager(make_cfg([{"id": "carrier_decay", "enabled": False}]))
    assert sm.windows() == []
    with pytest.raises(ScenarioParamError, match="unknown carrier"):
        ScenarioManager(make_cfg([{"id": "carrier_decay", "carrier": "Fastpost"}]))
    with pytest.raises(ScenarioParamError, match="unknown brand"):
        ScenarioManager(make_cfg([{"id": "promo_margin_trap", "brand": "Acme"}]))
    with pytest.raises(ScenarioParamError, match="delay_multiplier"):
        ScenarioManager(make_cfg([{"id": "carrier_decay", "delay_multiplier": "fast"}]))
    with pytest.raises(ScenarioParamError, match="before start"):
        ScenarioManager(make_cfg([{"id": "carrier_decay", "start": "2026-03-02", "end": "2026-03-01"}]))


def test_discount_percent_normalised():
    sm = ScenarioManager(make_cfg([{"id": "promo_margin_trap", "discount_pct": 30,
                                    "free_shipping": "false", "brand": "velmora"}]))
    (w,) = sm.windows()
    assert w.params == {"brand": "Velmora", "discount_pct": 0.3, "free_shipping": False}


# -- factors -----------------------------------------------------------------------


def test_carrier_decay_factors():
    sm = ScenarioManager(make_cfg())
    t_in, t_before = local(2026, 8, 3, 9), local(2026, 8, 2, 23)
    ctx = dict(carrier="Swiftbox", region="DE-BY", country="DE")
    assert sm.factor("carrier_delay", t_in, **ctx) == 2.5
    assert sm.factor("carrier_exception", t_in, **ctx) == 3.0
    assert sm.factor("carrier_delay", t_before, **ctx) == 1.0
    assert sm.factor("carrier_delay", t_in, carrier="Nordpost", region="DE-BY", country="DE") == 1.0
    assert sm.factor("carrier_delay", t_in, carrier="Swiftbox", region="DE-BW", country="DE") == 1.0
    assert sm.factor("carrier_delay", t_in) == 1.0
    assert sm.factor("web_orders", t_in) == 1.0
    assert sm.factor("no_such_factor", t_in, **ctx) == 1.0


def test_country_wide_region_param():
    sm = ScenarioManager(make_cfg([{"id": "carrier_decay", "region": "at"}]))
    t = local(2026, 1, 1)
    assert sm.factor("carrier_delay", t, carrier="swiftbox", region="AT-9", country="AT") == 2.5
    assert sm.factor("carrier_delay", t, carrier="Swiftbox", region="DE-BY", country="DE") == 1.0


def test_factor_records_planted_effects(ref):
    cfg = make_cfg()
    world, out = make_world(cfg, ref, local(2026, 8, 1))
    world.now = local(2026, 8, 4, 10)
    sm = world.scenarios
    ctx = dict(carrier="Swiftbox", region="DE-BY", country="DE")
    f = sm.factor("carrier_delay", world.now, world=world, record=("delivery", 42), **ctx)
    assert f == 2.5
    (e,) = out
    assert isinstance(e, PlantedEffect)
    assert e.effect_id == "carrier_decay:config:0:delivery:42:carrier_delay"
    assert e.entity_id == "42" and e.entity_type == "delivery" and e.magnitude == 2.5
    assert e.detail == ctx and e.at == world.now and e.window_id == "carrier_decay:config:0"
    # same entity/effect again: no duplicate row
    sm.factor("carrier_delay", world.now, world=world, record=("delivery", 42), **ctx)
    assert len(out) == 1
    # no effect when nothing applies
    sm.factor("carrier_delay", world.now, world=world, record=("delivery", 43), carrier="Nordpost",
              region="DE-BY")
    assert len(out) == 1


def test_factor_is_product_over_windows():
    sm = ScenarioManager(make_cfg())
    rows = [(1, "carrier_decay", "enable", local(2026, 9, 1), {"delay_multiplier": 2.0})]
    sm.load_overrides(rows)
    ctx = dict(carrier="Swiftbox", region="DE-BY", country="DE")
    assert sm.factor("carrier_delay", local(2026, 9, 2), **ctx) == pytest.approx(5.0)
    (w,) = [w for w in sm.windows() if w.source == "cli"]
    # missing params filled from implementation defaults
    assert w.params == {"carrier": "Swiftbox", "region": "DE-BY", "delay_multiplier": 2.0,
                        "exception_multiplier": 3.0}
    assert w.window_id == "carrier_decay:cli:1"


def test_record_effect(ref):
    world, out = make_world(make_cfg(), ref, local(2026, 3, 1))
    world.now = local(2026, 3, 5, 12)
    sm = world.scenarios
    sm.record_effect(world, "promo_margin_trap", None, "web_order", 7, "promo_discount", 4.5,
                     detail={"free_shipping_waived": 4.9, "at": world.now})
    (e,) = out
    assert e.effect_id == "promo_margin_trap:config:1:web_order:7:promo_discount"
    assert e.window_id == "promo_margin_trap:config:1"
    assert e.detail == {"free_shipping_waived": 4.9, "at": world.now.isoformat()}
    sm.record_effect(world, "promo_margin_trap", "x:cli:9", "web_order", 8, "promo_discount", None)
    assert out[-1].effect_id == "x:cli:9:web_order:8:promo_discount" and out[-1].magnitude is None


# -- overrides ---------------------------------------------------------------------


def test_overrides_enable_disable_and_version():
    sm = ScenarioManager(make_cfg([]))
    assert sm.version == 0
    t1, t2 = datetime(2026, 11, 14, 9, tzinfo=UTC), datetime(2026, 11, 14, 10, tzinfo=UTC)
    rows = [(5, "promo_margin_trap", "enable", t1, '{"discount_pct": 0.3}')]
    sm.load_overrides(rows)
    assert sm.version == 1
    sm.load_overrides(list(rows))  # same rows every tick: no change
    sm.load_overrides([tuple(r) for r in rows])
    assert sm.version == 1
    assert sm.active("promo_margin_trap", t1 + timedelta(minutes=1))
    rows2 = rows + [(6, "promo_margin_trap", "disable", t2, {})]
    sm.load_overrides(rows2)
    assert sm.version == 2
    (w,) = sm.windows("promo_margin_trap")
    assert w.end == t2 and not sm.active("promo_margin_trap", t2)
    # disable when nothing is open: no change
    sm.load_overrides(rows2 + [(7, "promo_margin_trap", "disable", t2 + timedelta(hours=1), {})])
    assert sm.version == 2
    sm.load_overrides([])
    assert sm.version == 3 and sm.windows() == []


def test_disable_closes_latest_open_window():
    sm = ScenarioManager(make_cfg())  # carrier_decay config window open from 2026-08-03
    t1, t2, t3 = local(2026, 9, 1), local(2026, 9, 2), local(2026, 9, 3)
    sm.load_overrides([
        (1, "carrier_decay", "enable", t1, {"region": "DE-BW"}),
        (2, "carrier_decay", "disable", t2, {}),
    ])
    cfgw, cliw = sm.windows("carrier_decay")
    assert cliw.end == t2 and cfgw.end is None
    sm.load_overrides([
        (1, "carrier_decay", "enable", t1, {"region": "DE-BW"}),
        (2, "carrier_decay", "disable", t2, {}),
        (3, "carrier_decay", "disable", t3, {}),
    ])
    cfgw, cliw = sm.windows("carrier_decay")
    assert cfgw.end == t3
    assert sm._config_windows[0].end is None  # base config window untouched


def test_bad_override_rows_are_ignored(caplog):
    sm = ScenarioManager(make_cfg([]))
    t = datetime(2026, 11, 14, 9, tzinfo=UTC)
    with caplog.at_level(logging.WARNING, logger="cpgsim.scenarios"):
        sm.load_overrides([
            (1, "nope", "enable", t, {}),
            (2, "carrier_decay", "enable", t, {"delay_multiplier": -1}),
            (3, "hero_oos_substitution", "enable", t, {}),
        ])
    assert sm.windows() == [] and sm.version == 0
    assert "unknown" in caplog.text and "delay_multiplier" in caplog.text


# -- promotions ----------------------------------------------------------------------


def test_scenario_promotions():
    sm = ScenarioManager(make_cfg())
    (p,) = sm.scenario_promotions()
    assert p == {
        "window_id": "promo_margin_trap:config:1", "scenario_id": "promo_margin_trap",
        "brand": "Velmora", "discount_pct": 0.25, "free_shipping": True,
        "start": local(2026, 3, 2), "end": local(2026, 3, 30), "index": 1, "live": False,
    }
    t = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
    sm.load_overrides([(4, "promo_margin_trap", "enable", t, {"brand": "Cutara"})])
    live = sm.scenario_promotions()[1]
    assert live["live"] is True and live["index"] == 1004 and live["brand"] == "Cutara"
    assert live["start"] == t and live["end"] == t + timedelta(days=28)  # open-ended cap
    sm.load_overrides([(4, "promo_margin_trap", "enable", t, {"brand": "Cutara"}),
                       (5, "promo_margin_trap", "disable", t + timedelta(days=3), {})])
    assert sm.scenario_promotions()[1]["end"] == t + timedelta(days=3)
    sm.load_overrides([(4, "promo_margin_trap", "enable", t, {}),
                       (5, "promo_margin_trap", "disable", t, {})])
    assert len(sm.scenario_promotions()) == 1  # empty window: no promotion


def test_promotion_without_start_uses_history_start(ref):
    cfg = make_cfg([{"id": "promo_margin_trap"}])
    world, _ = make_world(cfg, ref, local(2025, 1, 1))
    bootstrap(world)
    (p,) = world.scenarios.scenario_promotions()
    assert p["start"] == local(2025, 1, 1) and p["end"] == local(2025, 1, 29)


# -- truth recording -------------------------------------------------------------------


def test_truth_recording_bootstrap_arrivals_and_close(ref):
    cfg = make_cfg([
        {"id": "carrier_decay", "start": "2024-06-03"},  # before history start
        {"id": "promo_margin_trap", "start": "2025-01-06", "end": "2025-01-12"},
        {"id": "promo_margin_trap", "start": "2024-01-01", "end": "2024-02-01"},  # entirely before
    ])
    hs = local(2025, 1, 1)
    world, out = make_world(cfg, ref, hs)
    bootstrap(world)
    assert len(out) == 1
    rec = out[0]
    assert isinstance(rec, ScenarioWindowRecorded)
    assert rec.window_id == "carrier_decay:config:0" and rec.at == hs
    assert rec.start == local(2024, 6, 3) and rec.end is None and rec.source == "config"

    run(world, local(2025, 1, 10))
    rec2 = out[1]
    assert isinstance(rec2, ScenarioWindowRecorded)
    assert rec2.window_id == "promo_margin_trap:config:1"
    assert rec2.at == local(2025, 1, 6) and rec2.end == local(2025, 1, 13)

    # live: enable then disable
    t1, t2 = local(2025, 1, 10, 9, 15), local(2025, 1, 10, 14, 40)
    world.scenarios.load_overrides([(1, "carrier_decay", "enable", t1, {"region": "DE-BW"})])
    run(world, local(2025, 1, 10, 12))
    rec3 = out[2]
    assert rec3.window_id == "carrier_decay:cli:1" and rec3.at == t1 and rec3.end is None
    assert rec3.source == "cli" and rec3.params["region"] == "DE-BW"
    world.scenarios.load_overrides([(1, "carrier_decay", "enable", t1, {"region": "DE-BW"}),
                                    (2, "carrier_decay", "disable", t2, {})])
    run(world, local(2025, 1, 11))
    closed = out[3]
    assert isinstance(closed, ScenarioWindowClosed)
    assert closed.window_id == "carrier_decay:cli:1" and closed.end == t2 and closed.at == t2
    assert len(out) == 4


def test_truth_events_independent_of_slice_length(ref):
    cfg = make_cfg([
        {"id": "promo_margin_trap", "start": "2025-01-06", "end": "2025-01-12"},
        {"id": "carrier_decay", "start": "2025-01-08"},
    ])
    rows = [(1, "carrier_decay", "enable", local(2025, 1, 9, 13, 7), {}),
            (2, "carrier_decay", "disable", local(2025, 1, 9, 17, 0), {})]
    results = []
    for slice_len in (timedelta(minutes=7), timedelta(hours=1), timedelta(days=3)):
        world, out = make_world(cfg, ref, local(2025, 1, 1))
        world.scenarios.load_overrides(rows)
        bootstrap(world)
        run(world, local(2025, 1, 15), slice_len)
        results.append([(type(e).__name__, e.at, getattr(e, "window_id", None)) for e in out])
    assert results[0] == results[1] == results[2]
    kinds = [r[0] for r in results[0]]
    assert kinds == ["ScenarioWindowRecorded"] * 3 + ["ScenarioWindowClosed"]
    # cli window disabled before its record was due -> recorded with end
    assert results[0][2][2] == "carrier_decay:cli:1"


def test_window_at_history_start_recorded_once(ref):
    cfg = make_cfg([{"id": "carrier_decay", "start": "2025-01-01"}])
    world, out = make_world(cfg, ref, local(2025, 1, 1))
    bootstrap(world)
    run(world, local(2025, 1, 3))
    assert [e.window_id for e in out] == ["carrier_decay:config:0"]


def test_factor_performance():
    sm = ScenarioManager(make_cfg())
    t = local(2026, 9, 1)
    import time

    t0 = time.perf_counter()
    for _ in range(100_000):
        sm.factor("carrier_delay", t, carrier="Nordpost", region="DE-HE", country="DE")
        sm.factor("sku_demand", t, channel="web", sku_id=3)
    assert time.perf_counter() - t0 < 2.0
