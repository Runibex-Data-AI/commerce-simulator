"""Phase-2 channels: sell-in converges with sell-out per retail quarter (AC1 by construction).

Runs the real retail, demand plan and promotion modules for all four retailers
at the default config plus the overlay of the three default scenario windows
(phase-2 design section 5.7), from 2025-01-06 over five retail quarters. Other
modules are fakes: fulfilment (ships the next working day, POD two working
days later), billing, feeds and deductions (capture the calls). No database.

* Case A (warehouse with unlimited stock): per retailer and checked retail
  quarter (2025 Q2, Q3, Q4 for everyone, 2026 Q1 for all but Mylo, whose
  quarter overlaps ``retailer_overstock``), ``abs(sell-in / sell-out - 1) <
  0.05`` with sell-in = delivered units by PO week and sell-out = units sold
  from the captured feeds calls. Weekly sell-in / sell-out still diverges.
  Gift sets taper out before 1 January; Shopnova's July sale quarter passes.
* Case C (unlimited stock, five seeds, through 2026 Q3): every checked
  quarter of every retailer stays within 4 % (margin below the 5 % band), so
  convergence does not depend on the default seed's catalogue and calendar.
* Case B (the real warehouse module): Mylo pushes 30 % above demand for the 13
  window weeks (sell-in / sell-out > 1.20) and then orders far less than it
  sells in the cut weeks (< 0.80).
"""

from __future__ import annotations

import math
import sys
import types
from collections import defaultdict
from datetime import date, datetime, timedelta

import pytest

from cpgsim.config import Config
from cpgsim.domain import demandplan, promotions, retail  # noqa: F401  (real modules)
from cpgsim.domain import warehouse as real_warehouse
from cpgsim.domain.events import SalesDocCreated
from cpgsim.domain.model import ENTITY_KINDS, BillingDoc, BillingItem, Counters, Delivery, DeliveryLine
from cpgsim.domain.reference import build_reference
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import EventQueue
from cpgsim.engine.registry import ARRIVAL_SOURCES, HANDLERS
from cpgsim.engine.world import World
from cpgsim.models.retail_calendar import iso_monday, quarter_weeks
from cpgsim.scenarios.manager import ScenarioManager

HIST = date(2025, 1, 6)
END_A = date(2026, 4, 13)  # 2026 Q1 ends Sunday 29 March; two more weeks deliver its last POs
END_B = date(2026, 5, 18)  # end of the retailer_overstock cut phase

DEFAULT_WINDOWS = [
    {"id": "influencer_cohort_churn", "influencer_code": "GLOWWITHMIA", "repurchase_multiplier": 0.3,
     "acquisition_boost": 2.5, "start": "2025-04-07", "end": "2025-06-29"},
    {"id": "retailer_overstock", "retailer": "mylo", "over_order": 1.30, "duration_weeks": 13, "cut_weeks": 6,
     "start": "2026-01-05"},
    {"id": "hero_oos_substitution", "sku": "auto", "duration_weeks": 3, "substitution_share": 0.40,
     "start": "2026-04-13"},
]

RETAILERS = ("drogerie_kranz", "mylo", "shopnova", "belle")
CHECKED = {
    "drogerie_kranz": [(2025, 2), (2025, 3), (2025, 4), (2026, 1)],
    "mylo": [(2025, 2), (2025, 3), (2025, 4)],
    "shopnova": [(2025, 2), (2025, 3), (2025, 4), (2026, 1)],
    "belle": [(2025, 2), (2025, 3), (2025, 4), (2026, 1)],
}
RETAIL_SOURCES = ("retail.sellout", "retail.po_cycle")
WAREHOUSE_SOURCES = ("wh.plan", "wh.snapshot", "wh.quality_block")

_REF: dict = {}


def default_cfg() -> Config:
    return Config.model_validate({"scenarios": DEFAULT_WINDOWS})


def make_world(cfg: Config):
    collected: list = []
    world = World(cfg, EntityStore(), EventQueue(), emit=collected.append)
    for kind, cls in ENTITY_KINDS.items():
        world.store.register(kind, cls)
    key = (cfg.seed, tuple(sorted(cfg.channels.retailers())))
    if key not in _REF:
        _REF[key] = build_reference(cfg)
    world.ref = _REF[key]
    world.store.put("counters", "all", Counters())
    world.now = world.history_start = world.at_local(HIST, 0)
    world.scenarios = ScenarioManager(cfg)
    world.scenarios.attach(world)
    return world, collected


class UnlimitedWarehouse:
    def module(self) -> types.ModuleType:
        mod = types.ModuleType("cpgsim.domain.warehouse")
        mod.reserve = lambda world, sku_id, qty: int(qty)
        mod.unreserve = lambda world, sku_id, qty: None
        return mod


class Fakes:
    """Fulfilment, billing, feeds and deductions fakes; ``issue`` books goods issues at a real warehouse."""

    def __init__(self, issue: bool) -> None:
        self.issue = issue
        self.deliveries: dict[int, tuple[str, str, date]] = {}  # delivery -> (channel, dc, PO date)
        self.delivered: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))  # dc -> PO week
        self.weeks: list[tuple] = []
        self.days: list[tuple] = []
        self.cuts: list[tuple] = []

    def install(self, monkeypatch) -> None:
        fake = self

        ful = types.ModuleType("cpgsim.domain.fulfilment")

        def create_delivery(world, kind, sales_doc, lines, ship_to_account, ship_to, carrier,
                            planned_ship_date, order_id, seq_in_order=1):
            did = world.next_id("delivery")
            d = Delivery(
                delivery_id=did, kind=kind, sales_doc_id=sales_doc.sales_doc_id, ship_to_account=ship_to_account,
                ship_to=ship_to, carrier=carrier or world.cfg.carriers.pallet_forwarder,
                lines=[DeliveryLine(i, s, q, n) for i, (s, q, n) in enumerate(lines, start=1)],
                created_at=world.now, planned_ship_date=planned_ship_date, order_id=order_id,
                seq_in_order=seq_in_order,
            )
            world.store.put("delivery", did, d)
            po_day = world.local_date(sales_doc.created_at)
            fake.deliveries[did] = (sales_doc.channel, ship_to_account, po_day)
            world.schedule("retail.shipped", world.at_local(planned_ship_date, 12), f"retail.shipped:{did}",
                           {"delivery_id": did})
            pod_day = world.ref.calendar.add_working_days(planned_ship_date, 2, "DE")
            world.schedule("retail.pod", world.at_local(pod_day, 10), f"retail.pod:{did}", {"delivery_id": did})
            return d

        ful.create_delivery = create_delivery
        ful.cancel_delivery = lambda world, delivery_id, reason: False

        bil = types.ModuleType("cpgsim.domain.billing")

        def create_invoice(world, sales_doc, delivery, items, billing_date, due_date, include_shipping):
            by_item = {i.item_no: i for i in sales_doc.items}
            bitems = [
                BillingItem(item_no=n * 10, sku_id=line.sku_id, qty=line.qty,
                            net_value=round(by_item[line.sales_item_no].net_value / by_item[line.sales_item_no].qty
                                            * line.qty, 2),
                            tax=0.0, cost=0.0, sales_item_no=line.sales_item_no,
                            delivery_id=delivery.delivery_id, delivery_line_no=line.line_no)
                for n, line in enumerate(delivery.lines, start=1)
            ]
            bid = world.next_id("billing")
            doc = BillingDoc(bid, "invoice", sales_doc.sales_doc_id, sales_doc.sold_to, "EUR", 1.0,
                             billing_date, world.now, due_date, bitems)
            world.store.put("billing", bid, doc)
            return doc

        bil.create_invoice = create_invoice
        bil.clear = lambda world, billing_id, amount=None, status=None: None

        feeds = types.ModuleType("cpgsim.domain.feeds")
        feeds.record_sellout_week = lambda world, ch, dc, ws, sold, lost, demand, on_hand: fake.weeks.append(
            (ch, dc, ws, dict(sold), dict(on_hand)))
        feeds.record_marketplace_day = lambda world, ch, dc, day, sold, demand, on_hand, on_order: fake.days.append(
            (ch, dc, day, dict(sold), dict(demand), dict(on_hand)))

        ded = types.ModuleType("cpgsim.domain.deductions")
        ded.record_sellout = lambda world, ch, dc, first, days, sold: None
        ded.record_pod = lambda world, po_id, ch, dc, delivery_id, lines: None
        ded.record_cut = lambda world, po_id, ch, dc, o, c, v: fake.cuts.append((po_id, ch, dc, o, c, v))
        ded.take_on_payment = lambda world, ch, billing_id, gross: 0.0

        for name, mod in (("fulfilment", ful), ("billing", bil), ("feeds", feeds), ("deductions", ded)):
            monkeypatch.setitem(sys.modules, f"cpgsim.domain.{name}", mod)

    def on_event(self, world, ev) -> None:
        if ev.kind == "retail.shipped" and self.issue:
            d = world.store.get("delivery", ev.data["delivery_id"])
            if d is not None:
                wh = sys.modules["cpgsim.domain.warehouse"]
                for line in d.lines:
                    wh.issue(world, line.sku_id, line.qty, d.delivery_id)
        elif ev.kind == "retail.pod":
            info = self.deliveries.get(ev.data["delivery_id"])
            d = world.store.get("delivery", ev.data["delivery_id"])
            if info is not None and d is not None:
                _, dc, po_day = info
                self.delivered[dc][retail.monday(po_day)] += sum(line.qty for line in d.lines)

    def sellout(self) -> dict[str, dict[date, float]]:
        """``{dc: {week Monday: units sold}}`` (daily reports summed into their weeks)."""
        out: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
        for _, dc, ws, sold, _ in self.weeks:
            out[dc][ws] += sum(sold.values())
        for _, dc, day, sold, _, _ in self.days:
            out[dc][retail.monday(day)] += sum(sold.values())
        return out


def run(world, until: date, sources, fakes: Fakes, step_hours: float = 24.0) -> None:
    t = world.now
    end_t = world.at_local(until, 0)
    step = timedelta(hours=step_hours)
    while t < end_t:
        end = min(t + step, end_t)
        for name in sorted(sources):
            for ev in ARRIVAL_SOURCES[name](world, t, end):
                assert t < ev.due <= end
                world.queue.push(ev)
        while (due := world.queue.peek_due()) is not None and due <= end:
            ev = world.queue.pop()
            world.now = ev.due
            HANDLERS[ev.kind](world, ev)
            fakes.on_event(world, ev)
        world.now = end
        t = end


def simulate(monkeypatch, until: date, use_real_warehouse: bool, seed: int | None = None):
    cfg = default_cfg() if seed is None else Config.model_validate({"scenarios": DEFAULT_WINDOWS, "seed": seed})
    world, collected = make_world(cfg)
    fakes = Fakes(issue=use_real_warehouse)
    fakes.install(monkeypatch)
    sources = RETAIL_SOURCES
    if use_real_warehouse:
        monkeypatch.setitem(sys.modules, "cpgsim.domain.warehouse", real_warehouse)
        real_warehouse.init_stock(world)
        real_warehouse.bootstrap_quality_blocks(world)
        sources = RETAIL_SOURCES + WAREHOUSE_SOURCES
    else:
        monkeypatch.setitem(sys.modules, "cpgsim.domain.warehouse", UnlimitedWarehouse().module())
    retail.bootstrap_retail(world)
    run(world, until, sources, fakes)
    return types.SimpleNamespace(world=world, events=collected, fakes=fakes)


def by_retailer(world, per_dc: dict[str, dict[date, float]]) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for ch, dc, _ in retail.dc_plan(world):
        for wk, v in per_dc.get(dc, {}).items():
            out[ch][wk] += v
    return out


def quarter_ratio(sell_in: dict[date, float], sell_out: dict[date, float], weeks: list[date]) -> float:
    return sum(sell_in.get(w, 0.0) for w in weeks) / sum(sell_out.get(w, 0.0) for w in weeks)


@pytest.fixture(scope="module")
def case_a():
    mp = pytest.MonkeyPatch()
    try:
        yield simulate(mp, END_A, use_real_warehouse=False)
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def case_b():
    mp = pytest.MonkeyPatch()
    try:
        yield simulate(mp, END_B, use_real_warehouse=True)
    finally:
        mp.undo()


def test_sellin_converges_with_sellout_per_retail_quarter(case_a):
    world = case_a.world
    sin = by_retailer(world, case_a.fakes.delivered)
    sout = by_retailer(world, case_a.fakes.sellout())
    ratios = {}
    for ch in RETAILERS:
        assert len(CHECKED[ch]) >= 3
        assert any(q == 3 for _, q in CHECKED[ch]) and any(q == 4 for _, q in CHECKED[ch])
        for y, q in CHECKED[ch]:
            ratios[(ch, y, q)] = quarter_ratio(sin[ch], sout[ch], quarter_weeks(y, q))
    bad = {k: round(v, 4) for k, v in ratios.items() if abs(v - 1) >= 0.05}
    assert not bad, (bad, {k: round(v, 4) for k, v in ratios.items()})


SEEDS_C = (20261114, 1, 3, 5, 13)
END_C = date(2026, 10, 19)  # 2026 Q3 ends Sunday 27 September; three more weeks deliver its last POs
CHECKED_C = [(2025, 2), (2025, 3), (2025, 4), (2026, 1), (2026, 3)]  # 2026 Q2: hero window (and Mylo's overstock)


@pytest.fixture(scope="module")
def case_c():
    out = {}
    for seed in SEEDS_C:
        mp = pytest.MonkeyPatch()
        try:
            case = simulate(mp, END_C, use_real_warehouse=False, seed=seed)
            sin = by_retailer(case.world, case.fakes.delivered)
            sout = by_retailer(case.world, case.fakes.sellout())
            for ch in RETAILERS:
                for y, q in CHECKED_C:
                    if ch == "mylo" and (y, q) == (2026, 1):
                        continue  # retailer_overstock
                    out[(seed, ch, y, q)] = quarter_ratio(sin[ch], sout[ch], quarter_weeks(y, q))
        finally:
            mp.undo()
            for k in [k for k in _REF if k[0] == seed]:
                del _REF[k]  # one catalogue per seed: keep memory flat
    return out


def test_sellin_converges_for_several_seeds(case_c):
    """AC1 holds by construction, not by the default seed: five seeds, to 2026 Q3, margin 4 %."""
    assert len(case_c) == len(SEEDS_C) * (len(RETAILERS) * len(CHECKED_C) - 1)
    bad = {k: round(v, 4) for k, v in case_c.items() if abs(v - 1) >= 0.04}
    assert not bad, bad


def test_weekly_sellin_still_diverges(case_a):
    world = case_a.world
    sin = by_retailer(world, case_a.fakes.delivered)
    sout = by_retailer(world, case_a.fakes.sellout())
    for ch in ("drogerie_kranz", "mylo", "belle"):
        weeks = [iso_monday(2025, k) for k in range(14, 53)]
        r = [sin[ch].get(w, 0.0) / sout[ch][w] for w in weeks if sout[ch].get(w)]
        mean = sum(r) / len(r)
        sd = math.sqrt(sum((x - mean) ** 2 for x in r) / len(r))
        assert sd > 0.03, ch


def test_gift_sets_taper_out_before_new_year(case_a):
    world = case_a.world
    cat = world.ref.catalogue
    gifts = {s.sku_id for s in cat.skus if s.season_profile == "gift"}
    q4 = set(quarter_weeks(2025, 4))
    sell_in: dict[str, float] = defaultdict(float)
    # Q4 gift-set sell-in per DC from the created sales documents (unlimited stock: delivered = ordered)
    for e in case_a.events:
        if isinstance(e, SalesDocCreated) and retail.monday(world.local_date(e.at)) in q4:
            sell_in[e.doc.ship_to] += sum(i.qty for i in e.doc.items if i.sku_id in gifts)
    left = gift_stock_after_new_year(case_a)
    for _, dc, _ in retail.dc_plan(world):
        assert sell_in[dc] > 0, dc
        assert left[dc] <= 0.10 * sell_in[dc], (dc, left[dc], sell_in[dc])


def gift_stock_after_new_year(case) -> dict[str, int]:
    """Gift-set units on hand per DC after the last sell-out of 2025 (weekly: week of 29 Dec; daily: 31 Dec)."""
    world = case.world
    gifts = {s.sku_id for s in world.ref.catalogue.skus if s.season_profile == "gift"}
    out: dict[str, int] = defaultdict(int)
    last_week = date(2025, 12, 22)
    for _, dc, ws, _, on_hand in case.fakes.weeks:
        if ws == last_week:
            out[dc] = sum(v for s, v in on_hand.items() if s in gifts)
    for _, dc, day, _, _, on_hand in case.fakes.days:
        if day == date(2025, 12, 31):
            out[dc] = sum(v for s, v in on_hand.items() if s in gifts)
    return out


def test_shopnova_july_sale_quarter(case_a):
    world = case_a.world
    sin = by_retailer(world, case_a.fakes.delivered)
    sout = by_retailer(world, case_a.fakes.sellout())
    sale = iso_monday(2025, date(2025, 7, 15).isocalendar()[1])
    weeks = quarter_weeks(2025, 3)
    assert sale in weeks
    avg = sum(sout["shopnova"][w] for w in weeks) / len(weeks)
    assert sout["shopnova"][sale] > 2.0 * avg  # the sale shows in sell-out
    assert abs(quarter_ratio(sin["shopnova"], sout["shopnova"], weeks) - 1) < 0.05


def test_mylo_push_and_cut_phase_with_real_warehouse(case_b):
    world = case_b.world
    sin = by_retailer(world, case_b.fakes.delivered)
    sout = by_retailer(world, case_b.fakes.sellout())
    push = [date(2026, 1, 5) + timedelta(weeks=k) for k in range(13)]
    cut = [date(2026, 4, 6) + timedelta(weeks=k) for k in range(6)]
    r_push = quarter_ratio(sin["mylo"], sout["mylo"], push)
    r_cut = quarter_ratio(sin["mylo"], sout["mylo"], cut)
    assert r_push > 1.20, r_push
    assert r_cut < 0.80, r_cut
    # truth: one retail_push row per Mylo PO in the window, one cut-phase row per DC and order day
    from cpgsim.domain.events import PlantedEffect

    eff = [e for e in case_b.events if isinstance(e, PlantedEffect) and e.scenario_id == "retailer_overstock"]
    pushed = {e.entity_id for e in eff if e.effect == "retail_push"}
    mylo_docs = {str(e.doc.sales_doc_id) for e in case_b.events if isinstance(e, SalesDocCreated)
                 and e.doc.channel == "mylo" and push[0] <= world.local_date(e.at) < cut[0]}
    assert pushed == mylo_docs and pushed
    cuts = [e for e in eff if e.effect == "post_overstock_cut"]
    assert len(cuts) == 2 * 6 and len({e.entity_id for e in cuts}) == len(cuts)
    assert sum(e.magnitude for e in cuts) / len(cuts) < 0.8


def test_hero_block_with_real_warehouse(case_b):
    """The hero window (2026-04-13) blocks the hero centrally; retail cuts are recorded."""
    from cpgsim.domain.events import PlantedEffect, StockMoved

    eff = [e for e in case_b.events if isinstance(e, PlantedEffect) and e.scenario_id == "hero_oos_substitution"]
    kinds = {e.effect for e in eff}
    assert {"stock_block", "substitution_target"} <= kinds
    block = next(e for e in eff if e.effect == "stock_block")
    moves = [e for e in case_b.events if isinstance(e, StockMoved) and (e.reference or "").startswith("QI-BLOCK")]
    assert moves and all(m.sku_id == int(block.entity_id) for m in moves)
    assert "hero_oos_cut" in kinds
