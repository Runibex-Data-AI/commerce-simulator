"""hero_oos_substitution in the warehouse, the demand plan and retail (phase-2 design 2.3, 2.5, 5.7).

The warehouse puts the hero's available stock under a quality block for the
window, keeps it blocked (unreserved and returned units, held production
receipts) and releases it at the window end, also after an early CLI disable.
The demand plan moves ``substitution_share`` of the hero's web demand to the
sibling; retail moves that share of the hero's *lost* units at each DC.
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime, timedelta

import pytest

from cpgsim.config import Config
from cpgsim.domain import demandplan, retail
from cpgsim.domain import warehouse as wh
from cpgsim.domain.events import PlantedEffect, SalesDocCreated, StockMoved
from cpgsim.domain.model import ENTITY_KINDS, Counters
from cpgsim.domain.reference import build_reference
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import Event, EventQueue
from cpgsim.engine.registry import ARRIVAL_SOURCES, HANDLERS
from cpgsim.engine.world import World
from cpgsim.scenarios import hero_oos_substitution as hero_mod
from cpgsim.scenarios.manager import ScenarioManager

from test_channels_phase2 import Fakes  # same directory (pytest prepend import mode)

_REF: dict = {}


def window(start: str | None = "2025-06-02", **params) -> dict:
    w = {"id": "hero_oos_substitution", "sku": "auto", "duration_weeks": 3, "substitution_share": 0.40}
    if start is not None:
        w["start"] = start
    w.update(params)
    return w


def cfg_with(scenarios: list[dict], **kw) -> Config:
    return Config.model_validate({"scenarios": scenarios, **kw})


def make_world(cfg: Config, hist: date = date(2025, 5, 26)):
    collected: list = []
    world = World(cfg, EntityStore(), EventQueue(), emit=collected.append)
    for kind, cls in ENTITY_KINDS.items():
        world.store.register(kind, cls)
    key = tuple(sorted(cfg.channels.retailers()))
    if key not in _REF:
        _REF[key] = build_reference(cfg)
    world.ref = _REF[key]
    world.store.put("counters", "all", Counters())
    world.now = world.history_start = world.at_local(hist, 0)
    world.scenarios = ScenarioManager(cfg)
    world.scenarios.attach(world)
    return world, collected


def moves(events, prefix: str | None = None, sku: int | None = None) -> list[StockMoved]:
    out = [e for e in events if isinstance(e, StockMoved)]
    if prefix is not None:
        out = [e for e in out if (e.reference or "").startswith(prefix)]
    if sku is not None:
        out = [e for e in out if e.sku_id == sku]
    return out


def effects(events, effect: str | None = None) -> list[PlantedEffect]:
    out = [e for e in events if isinstance(e, PlantedEffect)]
    return [e for e in out if effect is None or e.effect == effect]


def fire(world, kind: str, at: datetime, data: dict) -> None:
    world.now = at
    HANDLERS[kind](world, Event(due=at, kind=kind, key=f"{kind}:test", data=data))


# ---------------------------------------------------------------------------
# Warehouse block
# ---------------------------------------------------------------------------


def test_block_lifecycle_unreserve_returns_and_held_receipts():
    world, ev = make_world(cfg_with([window()]))
    wh.init_stock(world)
    (b,) = wh._blocks(world)
    hero, sib = b.sku_id, b.sibling_id
    assert (b.start, b.end) == (world.at_local(date(2025, 6, 2), 0), world.at_local(date(2025, 6, 23), 0))
    # arrival source: block at the start, unblock at the self-limited end
    evs = ARRIVAL_SOURCES["wh.quality_block"](world, world.now, world.at_local(date(2025, 7, 1), 0))
    assert [(e.kind, e.key, e.due, e.priority) for e in evs] == [
        ("wh.block", f"wh.block:{b.window_id}", b.start, 10),
        ("wh.unblock", f"wh.unblock:{b.window_id}", b.end, 10),
    ]
    assert wh.reserve(world, hero, 10) == 10  # reserved for an open delivery before the block
    on_hand0 = world.store.get("stock", hero).on_hand
    fire(world, "wh.block", b.start, evs[0].data)
    pos = world.store.get("stock", hero)
    q = on_hand0 - 10
    assert (pos.on_hand, pos.reserved, pos.blocked) == (10, 10, q) and wh.available(world, hero) == 0
    (m,) = moves(ev, "QI-BLOCK")
    assert (m.movement_type, m.qty, m.reference) == ("adjustment", -q, f"QI-BLOCK {b.window_id}")
    sb, st = effects(ev, "stock_block"), effects(ev, "substitution_target")
    assert [(e.entity_type, e.entity_id, e.magnitude) for e in sb] == [("sku", str(hero), q)]
    assert [(e.entity_type, e.entity_id, e.magnitude) for e in st] == [("sku", str(sib), 0.4)]
    assert sb[0].detail == {"window_id": b.window_id, "hero": hero, "sibling": sib, "resolved_on": "2025-06-02"}

    # released and returned units go straight into the block; reserved stock still ships
    world.now = b.start + timedelta(days=2)
    wh.unreserve(world, hero, 4)
    wh.receive_return(world, hero, 3, return_id=77, resaleable=True)
    wh.receive_return(world, hero, 2, return_id=78, resaleable=False)
    wh.issue(world, hero, 6, delivery_id=5)
    pos = world.store.get("stock", hero)
    assert (pos.on_hand, pos.reserved, pos.blocked) == (0, 0, q + 4 + 3)
    assert [m.qty for m in moves(ev, "QI-BLOCK")] == [-q, -4, -3]
    assert wh.available(world, hero) == 0

    # a production receipt in the window is held; other SKUs are booked as usual
    pos = world.store.mut("stock", hero)
    pos.in_production = 500
    fire(world, "wh.receipt", b.start + timedelta(days=5), {"sku_id": hero, "qty": 300, "order": "PRD-A"})
    fire(world, "wh.receipt", b.start + timedelta(days=6), {"sku_id": hero, "qty": 200, "order": "PRD-B"})
    other = next(s for s, _ in world.store.items("stock") if s != hero)
    o0 = world.store.get("stock", other).on_hand
    fire(world, "wh.receipt", b.start + timedelta(days=6), {"sku_id": other, "qty": 60, "order": "PRD-C"})
    assert world.store.get("stock", other).on_hand == o0 + 60
    pos = world.store.get("stock", hero)
    assert pos.on_hand == 0 and pos.in_production == 500
    assert [r["order"] for r in pos.held_receipts] == ["PRD-A", "PRD-B"]
    assert not [m for m in moves(ev, sku=hero) if m.movement_type == "goods_receipt"]

    # the unblock scraps the blocked lot (recall) and books the held receipts in order
    fire(world, "wh.unblock", b.end, evs[1].data)
    pos = world.store.get("stock", hero)
    (r,) = moves(ev, "QI-RECALL")
    assert (r.movement_type, r.qty, r.reference) == ("scrap", -(q + 7), f"QI-RECALL {b.window_id}")
    receipts = [m for m in moves(ev, sku=hero) if m.movement_type == "goods_receipt"]
    assert [(m.qty, m.reference) for m in receipts] == [(300, "PRD-A"), (200, "PRD-B")]
    assert (pos.on_hand, pos.blocked, pos.held_receipts, pos.in_production) == (500, 0, [], 0)
    world.now = b.end + timedelta(hours=1)
    wh.unreserve(world, hero, 0)
    assert wh.receive_return(world, hero, 1, return_id=79, resaleable=True) is None
    assert world.store.get("stock", hero).on_hand == 501  # no block any more


def test_early_cli_disable_releases_at_the_new_end():
    cfg = cfg_with([])
    world, ev = make_world(cfg)
    t0 = world.at_local(date(2025, 6, 2), 9)
    t1 = world.at_local(date(2025, 6, 10), 15)
    sm = world.scenarios
    sm.load_overrides([(1, "hero_oos_substitution", "enable", t0, {})])
    wh.init_stock(world)
    (b,) = wh._blocks(world)
    assert b.end == t0 + timedelta(weeks=3)
    fire(world, "wh.block", t0, {"window_id": b.window_id, "sku_id": b.sku_id})
    world.store.mut("stock", b.sku_id).in_production = 120
    fire(world, "wh.receipt", t0 + timedelta(days=3), {"sku_id": b.sku_id, "qty": 120, "order": "PRD-X"})
    assert world.store.get("stock", b.sku_id).held_receipts
    sm.load_overrides([(1, "hero_oos_substitution", "enable", t0, {}),
                       (2, "hero_oos_substitution", "disable", t1, {})])
    (b2,) = wh._blocks(world)
    assert b2.end == t1
    evs = ARRIVAL_SOURCES["wh.quality_block"](world, t0, t0 + timedelta(weeks=4))
    assert [(e.kind, e.due) for e in evs] == [("wh.unblock", t1)]
    fire(world, "wh.unblock", t1, evs[0].data)
    pos = world.store.get("stock", b.sku_id)
    assert pos.blocked == 0 and not pos.held_receipts and pos.on_hand >= 120
    assert [m.reference for m in moves(ev, sku=b.sku_id) if m.movement_type == "goods_receipt"] == ["PRD-X"]


@pytest.mark.parametrize("start", ["2025-05-19", None])
def test_window_open_at_history_start_is_blocked_by_the_bootstrap(start):
    world, ev = make_world(cfg_with([window(start)]))
    wh.init_stock(world)
    (b,) = wh._blocks(world)
    assert b.start <= world.now < b.end
    wh.bootstrap_quality_blocks(world)
    assert wh.available(world, b.sku_id) == 0 and world.store.get("stock", b.sku_id).blocked > 0
    assert len(effects(ev, "stock_block")) == 1
    evs = ARRIVAL_SOURCES["wh.quality_block"](world, world.now, world.now + timedelta(weeks=4))
    assert [e.kind for e in evs] == ["wh.unblock"]  # no block event before or at history start


def test_resolution_is_pinned_when_a_sibling_launches_inside_the_window():
    cat = build_reference(Config()).catalogue
    hit = None
    for h in cat.heroes:
        for y in (2024, 2025, 2026):
            for m in range(1, 13):
                d0 = date(y, m, 1) - timedelta(days=7)
                a, b = cat.siblings(h.sku_id, d0), cat.siblings(h.sku_id, d0 + timedelta(days=14))
                if a and b and a[0].sku_id != b[0].sku_id:
                    hit = (h.sku_id, d0, a[0].sku_id, b[0].sku_id)
    if hit is None:
        pytest.skip("no sibling launch or delisting inside a hero window in this catalogue")
    hero, d0, first, later = hit
    world, _ = make_world(cfg_with([window(d0.isoformat(), sku=hero)]), hist=d0 - timedelta(days=3))
    (b,) = wh._blocks(world)
    assert (b.sku_id, b.sibling_id) == (hero, first)
    t_late = world.at_local(d0 + timedelta(days=14), 12)
    (tr,) = world.scenarios.transfers("sku_demand", t_late, world=world, channel="web", d=d0 + timedelta(days=14))
    assert (tr[1], tr[2]) == (hero, first) != (hero, later)
    assert {x.sibling_id for x in hero_mod.retail_blocks(world, "mylo", d0 + timedelta(days=30))} == {first}


def test_auto_hero_falls_back_to_the_first_enabled_retailer():
    no_kranz = cfg_with([window("2026-04-13")], channels={"drogerie_kranz": {"share": 0.35, "enabled": False}})
    world, _ = make_world(no_kranz, hist=date(2026, 4, 6))
    (b,) = wh._blocks(world)
    assert b.sibling_id in world.ref.catalogue.retailer_assortment("mylo", date(2026, 4, 13))


# ---------------------------------------------------------------------------
# Demand plan (web transfer) and retail substitution
# ---------------------------------------------------------------------------


def test_web_demand_transfer_conserves_share():
    d = date(2025, 6, 10)
    world, _ = make_world(cfg_with([window()]))
    base, _ = make_world(cfg_with([]))
    (b,) = wh._blocks(world)
    w, w0 = demandplan._weights(world, "web", d), demandplan._weights(base, "web", d)
    h, s = b.sku_id, b.sibling_id
    assert w.by_sku[h] == 0.0
    assert w.by_sku[s] == pytest.approx(w0.by_sku[s] + 0.4 * w0.by_sku[h])
    assert w.total == pytest.approx(w0.total - 0.6 * w0.by_sku[h])
    assert w.cum[-1] == pytest.approx(w.total) and w.cum == sorted(w.cum)
    others = [x for x in w.ids if x not in (h, s)]
    assert all(w.by_sku[x] == w0.by_sku[x] for x in others)
    # outside the window, and for retail channels, nothing moves
    after = date(2025, 6, 30)
    assert demandplan._weights(world, "web", after).by_sku[h] == demandplan._weights(base, "web", after).by_sku[h]
    for ch in ("drogerie_kranz", "shopnova"):
        assert demandplan._weights(world, ch, d).by_sku == demandplan._weights(base, ch, d).by_sku


def _sellout_once(monkeypatch, scenarios: list[dict], hero_left: int, sib_left: int):
    world, ev = make_world(cfg_with(scenarios))
    fakes = Fakes(issue=False)
    fakes.install(monkeypatch)
    retail.bootstrap_retail(world)
    blocks = hero_mod.blocks(make_world(cfg_with([window()]))[0])
    hero, sib = blocks[0].sku_id, blocks[0].sibling_id
    inv = world.store.mut("retailer_inventory", "KRANZ-DC1")
    assert hero in inv.on_hand and sib in inv.on_hand
    inv.on_hand[hero] = hero_left
    inv.on_hand[sib] = sib_left
    ws = date(2025, 6, 2)
    fire(world, "retail.sellout", world.at_local(ws + timedelta(days=7), 0, 30),
         {"channel": "drogerie_kranz", "dc": "KRANZ-DC1", "week_start": ws.isoformat()})
    (call,) = fakes.weeks
    return types.SimpleNamespace(world=world, events=ev, hero=hero, sib=sib, sold=call[3], on_hand=call[4],
                                 inv=world.store.get("retailer_inventory", "KRANZ-DC1"), fakes=fakes)


@pytest.mark.parametrize("sib_left", [10_000, 3])
def test_retail_substitution_moves_share_of_lost_units(monkeypatch, sib_left):
    with_w = _sellout_once(monkeypatch, [window()], 0, sib_left)
    without = _sellout_once(monkeypatch, [], 0, sib_left)
    h, s = with_w.hero, with_w.sib
    # the hero sold nothing: all its demand was lost (equal draws in both runs)
    assert h not in with_w.sold or with_w.sold[h] == 0
    eff = effects(with_w.events, "substituted_units")
    (e,) = eff
    lost_h = e.detail["hero_lost"]
    extra = round(0.4 * lost_h)
    assert lost_h > 0 and e.magnitude == extra
    assert (e.entity_type, e.entity_id) == ("retailer_dc_week", "KRANZ-DC1:2025-06-02")
    got = e.detail["sibling_sold_extra"]
    assert with_w.sold.get(s, 0) - without.sold.get(s, 0) == got
    left_after_base = sib_left - without.sold.get(s, 0)
    assert got == min(extra, left_after_base)
    assert with_w.inv.on_hand[s] == without.inv.on_hand[s] - got == with_w.on_hand[s]
    assert not effects(without.events, "substituted_units")


def test_hero_cut_is_recorded_once_per_sales_doc(monkeypatch):
    world, ev = make_world(cfg_with([window()]))
    fakes = Fakes(issue=True)
    fakes.install(monkeypatch)
    monkeypatch.setitem(sys.modules, "cpgsim.domain.warehouse", wh)
    wh.init_stock(world)
    retail.bootstrap_retail(world)
    (b,) = wh._blocks(world)
    fire(world, "wh.block", b.start, {"window_id": b.window_id, "sku_id": b.sku_id})
    # make sure Kranz DC1 needs the hero, then run its next order day and the confirmation
    inv = world.store.mut("retailer_inventory", "KRANZ-DC1")
    inv.on_hand[b.sku_id] = 0
    inv.on_order[b.sku_id] = 0
    d = next(b.start.date() + timedelta(days=k) for k in range(1, 8)
             if retail.is_order_day(world.cfg.channels.drogerie_kranz, 1, b.start.date() + timedelta(days=k)))
    fire(world, "retail.po_cycle", world.at_local(d, 5), {"channel": "drogerie_kranz", "dc": "KRANZ-DC1"})
    (doc,) = [e.doc for e in ev if isinstance(e, SalesDocCreated)]
    ordered = next(i.qty for i in doc.items if i.sku_id == b.sku_id)
    fire(world, "retail.confirm", world.at_local(d, 10), {"po_id": doc.order_id})
    (e,) = effects(ev, "hero_oos_cut")
    assert (e.entity_type, e.entity_id, e.magnitude) == ("sales_doc", str(doc.sales_doc_id), ordered)
    (cut,) = fakes.cuts
    assert cut[0] == doc.order_id and cut[1:3] == ("drogerie_kranz", "KRANZ-DC1") and cut[4] >= ordered


def test_partition_invariance_with_the_new_sources(monkeypatch):
    """Two tick lengths give identical POs, stock movements and truth rows across a hero window."""
    sources = ("retail.sellout", "retail.po_cycle", "wh.plan", "wh.snapshot", "wh.quality_block")
    out = []
    for step in (1.0, 13.0):
        world, ev = make_world(cfg_with([window("2025-06-04")]))
        fakes = Fakes(issue=True)
        fakes.install(monkeypatch)
        monkeypatch.setitem(sys.modules, "cpgsim.domain.warehouse", wh)
        wh.init_stock(world)
        wh.bootstrap_quality_blocks(world)
        retail.bootstrap_retail(world)
        from test_channels_phase2 import run

        run(world, date(2025, 7, 7), sources, fakes, step_hours=step)
        out.append((
            [(e.doc.sales_doc_id, e.at, [(i.sku_id, i.qty) for i in e.doc.items])
             for e in ev if isinstance(e, SalesDocCreated)],
            [(m.movement_id, m.at, m.sku_id, m.movement_type, m.qty, m.reference) for m in moves(ev)],
            [(e.effect_id, e.at, e.magnitude) for e in effects(ev)],
            fakes.days,
        ))
    assert out[0] == out[1]
    assert any(m[5] and m[5].startswith("QI-RECALL") for m in out[0][1])
    assert {e[0].split(":")[-1] for e in out[0][2]} >= {"stock_block", "substitution_target", "hero_oos_cut"}
