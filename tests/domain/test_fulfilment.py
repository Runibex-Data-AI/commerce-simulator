"""Warehouse, billing and fulfilment: unit tests on a lightweight World.

Other agents' modules (demandplan, masterdata) are replaced by stubs, and the
events handled by other modules (``web.*``, ``retail.*``) are collected as
notifications instead of being dispatched.
"""

from __future__ import annotations

import math
import statistics
import sys
import types
from datetime import date, datetime, timedelta

import pytest

import cpgsim.domain as domain_pkg
from cpgsim.config import Config
from cpgsim.domain import billing, fulfilment, warehouse
from cpgsim.domain.events import (
    BillingDocChanged,
    BillingDocCreated,
    CarrierScan,
    DeliveryChanged,
    DeliveryCreated,
    SalesDocChanged,
    StockMoved,
    StockSnapshotTaken,
)
from cpgsim.domain.model import (
    ENTITY_KINDS,
    Address,
    Condition,
    Counters,
    SalesDoc,
    SalesItem,
    StockPosition,
)
from cpgsim.domain.reference import build_reference
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import EventQueue
from cpgsim.engine.registry import ARRIVAL_SOURCES, HANDLERS
from cpgsim.engine.world import World

CFG = Config()
REF = build_reference(CFG)
START = date(2025, 3, 3)  # a Monday
OWNED = ("ful", "wh")
DORTMUND = Address("DE", "DE-NW", "44135", "Dortmund", "Teststraße", "1")
MUNICH = Address("DE", "DE-BY", "80331", "München", "Teststraße", "2")


# -- test scaffolding --------------------------------------------------------------


class StubScenarios:
    version = 0

    def __init__(self, factors=None):
        self.factors = factors or {}
        self.calls = []

    def factor(self, name, t, world=None, record=None, **ctx):
        self.calls.append((name, record, ctx, world is not None))
        f = self.factors.get(name, 1.0)
        return f(ctx) if callable(f) else f

    def active(self, *a, **k):
        return []

    def record_effect(self, *a, **k):
        pass

    def scenario_promotions(self):
        return []


def _expected_daily_units(world, sku_id, d, channel=None):
    sku = world.ref.catalogue.get(sku_id)
    if not world.ref.catalogue.is_active(sku, d):
        return 0.0
    return 40.0 * sku.popularity + 0.5


@pytest.fixture(autouse=True)
def stub_modules(monkeypatch):
    dp = types.ModuleType("cpgsim.domain.demandplan")
    dp.expected_daily_units = _expected_daily_units
    md = types.ModuleType("cpgsim.domain.masterdata")
    md.std_cost = lambda world, sku_id, d: world.ref.catalogue.get(sku_id).std_cost_eur
    md.fx_rate = lambda world, currency, d: 1.0 if currency == "EUR" else 1.05
    for name, mod in (("demandplan", dp), ("masterdata", md)):
        monkeypatch.setitem(sys.modules, f"cpgsim.domain.{name}", mod)
        monkeypatch.setattr(domain_pkg, name, mod, raising=False)


def make_world(start: date = START, scenarios=None):
    store = EntityStore()
    for kind, cls in ENTITY_KINDS.items():
        store.register(kind, cls)
    events: list = []
    w = World(CFG, store, EventQueue(), emit=events.append)
    w.ref = REF
    w.scenarios = scenarios or StubScenarios()
    w.now = w.history_start = w.at_local(start, 0)
    store.put("counters", "all", Counters())
    return w, events


def drain(w, until: datetime, notes: list | None = None) -> None:
    """Process queued events up to ``until``; other modules' events go to ``notes``."""
    q = w.queue
    while True:
        due = q.peek_due()
        if due is None or due > until:
            break
        ev = q.pop()
        w.now = ev.due
        if ev.kind.split(".", 1)[0] in OWNED:
            HANDLERS[ev.kind](w, ev)
        elif notes is not None:
            notes.append(ev)
    w.now = max(w.now, until)


def run_arrivals(w, until: datetime, notes: list | None = None, step=timedelta(hours=6)) -> None:
    """Advance with the warehouse arrival sources, like the runner does."""
    t = w.now
    while t < until:
        end = min(t + step, until)
        for name in ("wh.plan", "wh.snapshot"):
            for ev in ARRIVAL_SOURCES[name](w, t, end):
                w.queue.push(ev)
        drain(w, end, notes)
        t = end


def put_stock(w, sku_id: int, on_hand: int) -> None:
    w.store.put("stock", sku_id, StockPosition(sku_id=sku_id, on_hand=on_hand))


def web_sales_doc(w, lines: list[tuple[int, int]], shipping_net=4.16, shipping_tax=0.79, freight=True) -> SalesDoc:
    sid = w.next_id("sales_doc")
    items = []
    for i, (sku_id, qty) in enumerate(lines):
        sku = REF.catalogue.get(sku_id)
        net = round(sku.rrp_eur / 1.19 * qty, 2)
        items.append(
            SalesItem(
                item_no=10 * (i + 1), sku_id=sku_id, qty=qty, confirmed_qty=qty, net_value=net,
                tax=round(net * 0.19, 2), cost=round(sku.std_cost_eur * qty, 2),
                conditions=[Condition("list_price", sku.rrp_eur / 1.19, net)],
            )
        )
    if freight and shipping_net > 0:  # like web.on_release: the shipping charge is item 900
        items.append(
            SalesItem(
                item_no=900, sku_id=0, qty=1, confirmed_qty=1, net_value=shipping_net, tax=shipping_tax,
                cost=0.0, conditions=[Condition("shipping", shipping_net, shipping_net)], item_type="freight",
            )
        )
    doc = SalesDoc(
        sales_doc_id=sid, doc_type="web", sold_to="WEB-DE", ship_to="WEB-DE", currency="EUR",
        fx_rate_eur=1.0, created_at=w.now, requested_date=w.local_date(), external_ref=str(sid),
        items=items, channel="web", order_id=sid, shipping_net=shipping_net, shipping_tax=shipping_tax,
    )
    w.store.put("sales_doc", sid, doc)
    return doc


def web_delivery(w, lines=((1, 2), (5, 1)), ship_to=DORTMUND, carrier=None, stock=True):
    if stock:
        for sku_id, qty in lines:
            if not w.store.has("stock", sku_id):
                put_stock(w, sku_id, 100_000)
            assert warehouse.reserve(w, sku_id, qty) == qty
    doc = web_sales_doc(w, list(lines))
    dl = [(sku, qty, 10 * (i + 1)) for i, (sku, qty) in enumerate(lines)]
    return fulfilment.create_delivery(
        w, "web", doc, dl, "WEB-DE", ship_to, carrier, w.local_date(), order_id=doc.order_id
    )


def of(events, cls, **match):
    return [e for e in events if isinstance(e, cls) and all(getattr(e, k) == v for k, v in match.items())]


# -- warehouse -----------------------------------------------------------------------


def test_reserve_unreserve_issue_and_returns():
    w, events = make_world()
    put_stock(w, 1, 10)
    assert warehouse.available(w, 1) == 10
    assert warehouse.reserve(w, 1, 4) == 4
    assert warehouse.available(w, 1) == 6
    assert warehouse.reserve(w, 1, 10) == 6  # cut to what is available
    pos = w.store.get("stock", 1)
    assert pos.reserved == 10 and pos.backorder_qty == 4
    warehouse.unreserve(w, 1, 3)
    assert pos.reserved == 7
    warehouse.issue(w, 1, 5, delivery_id=77)
    assert (pos.on_hand, pos.reserved, pos.issued_today) == (5, 2, 5)
    gi = of(events, StockMoved, movement_type="goods_issue")
    assert len(gi) == 1 and gi[0].qty == -5 and gi[0].delivery_id == 77

    warehouse.receive_return(w, 1, 2, return_id=9, resaleable=True)
    assert pos.on_hand == 7
    warehouse.receive_return(w, 1, 1, return_id=10, resaleable=False, delivery_id=77)
    assert pos.on_hand == 7
    moves = [(e.movement_type, e.qty, e.return_id) for e in of(events, StockMoved)]
    assert moves[1:] == [("customer_return", 2, 9), ("customer_return", 1, 10), ("scrap", -1, 10)]
    ids = [e.movement_id for e in of(events, StockMoved)]
    assert ids == sorted(set(ids))


def test_reserve_on_unknown_sku_creates_empty_position():
    w, _ = make_world()
    future = next(s for s in REF.catalogue.skus if s.launch_date > START)
    assert not w.store.has("stock", future.sku_id)
    assert warehouse.available(w, future.sku_id) == 0
    assert warehouse.reserve(w, future.sku_id, 3) == 0
    pos = w.store.get("stock", future.sku_id)
    assert pos.on_hand == 0 and pos.reserved == 0 and pos.backorder_qty == 3


def test_bootstrap_initial_inventory():
    w, events = make_world()
    warehouse.init_stock(w)
    active = REF.catalogue.active(START)
    assert w.store.count("stock") == len(active)
    adj = of(events, StockMoved, movement_type="adjustment")
    assert len(adj) == len(active)
    cover = CFG.company.warehouse.initial_cover_days
    for sku in active:
        pos = w.store.get("stock", sku.sku_id)
        rate = _expected_daily_units(w, sku.sku_id, START)
        assert pos.on_hand % sku.units_per_case == 0
        assert rate * cover * 0.74 <= pos.on_hand <= rate * cover * 1.26 + sku.units_per_case
        assert pos.avg_daily_out == pytest.approx(rate, rel=1e-3)
    assert all(e.at == w.history_start for e in adj)


def test_plan_and_snapshot_arrivals():
    w, _ = make_world(date(2025, 4, 14))  # week before Easter Monday (21 Apr)
    start, end = w.now, w.now + timedelta(days=14)
    plans = list(ARRIVAL_SOURCES["wh.plan"](w, start, end))
    days = [w.local(e.due) for e in plans]
    assert all(d.hour == 7 and d.minute == 0 for d in days)
    assert all(d.weekday() < 5 for d in days)
    assert date(2025, 4, 18) not in {d.date() for d in days}  # Good Friday
    assert date(2025, 4, 21) not in {d.date() for d in days}  # Easter Monday
    assert len(plans) == 10 - 2
    snaps = list(ARRIVAL_SOURCES["wh.snapshot"](w, start, end))
    assert len(snaps) == 14
    assert all((w.local(e.due).hour, w.local(e.due).minute) == (23, 55) for e in snaps)
    # Slicing does not change the arrivals.
    pieces = []
    t = start
    while t < end:
        pieces += list(ARRIVAL_SOURCES["wh.plan"](w, t, t + timedelta(hours=5)))
        t += timedelta(hours=5)
    assert [e.key for e in pieces if e.due <= end] == [e.key for e in plans]


def test_planning_orders_production_and_receives_it():
    w, events = make_world()
    sku = next(s for s in REF.catalogue.active(START) if s.hero)
    put_stock(w, sku.sku_id, 0)
    w.now = w.at_local(START, 7)
    HANDLERS["wh.plan"](w, types.SimpleNamespace(data={"date": START.isoformat()}))
    pos = w.store.get("stock", sku.sku_id)
    lot = sku.units_per_case * warehouse.LOT_CASES
    assert pos.in_production > 0 and pos.in_production % lot == 0
    rate = _expected_daily_units(w, sku.sku_id, START)
    assert pos.in_production >= rate * CFG.company.warehouse.target_cover_days * 0.6
    key = f"wh.receipt:{sku.sku_id}:{START:%Y%m%d}"
    assert w.queue.contains(key)
    qty = pos.in_production
    drain(w, w.now + timedelta(days=40))
    gr = of(events, StockMoved, movement_type="goods_receipt", sku_id=sku.sku_id)
    assert len(gr) == 1 and gr[0].qty == qty and gr[0].reference.startswith("PRD20250303")
    assert pos.on_hand == qty and pos.in_production == 0
    rl = w.local(gr[0].at)
    assert 8 <= rl.hour < 14 and warehouse.is_working_day(w, rl.date())
    assert (rl.date() - START).days >= 5


def test_planning_skips_well_stocked_and_creates_prelaunch_positions():
    w, events = make_world()
    warehouse.init_stock(w)
    d = START
    launching = [s for s in REF.catalogue.skus if d < s.launch_date <= d + timedelta(days=28)]
    w.now = w.at_local(d, 7)
    HANDLERS["wh.plan"](w, types.SimpleNamespace(data={"date": d.isoformat()}))
    for s in launching:
        pos = w.store.get("stock", s.sku_id)
        assert pos is not None and pos.in_production > 0, s.sku_id
    # Initial cover is 31-52 days, far above the 21-day reorder point: almost nothing ordered.
    ordered = [p for _, p in w.store.items("stock") if p.in_production > 0]
    assert len(ordered) - len(launching) <= 0.15 * len(REF.catalogue.active(d))


def test_delisted_stock_is_written_off():
    w, events = make_world()
    sku = next(s for s in REF.catalogue.skus if s.delist_date and s.delist_date > START)
    put_stock(w, sku.sku_id, 37)
    d = sku.delist_date + timedelta(days=warehouse.DELIST_WRITE_OFF_DAYS + 3)
    w.now = w.at_local(d, 7)
    HANDLERS["wh.plan"](w, types.SimpleNamespace(data={"date": d.isoformat()}))
    assert not w.store.has("stock", sku.sku_id)
    sc = of(events, StockMoved, movement_type="scrap", sku_id=sku.sku_id)
    assert len(sc) == 1 and sc[0].qty == -37


def test_snapshot_rows_and_ewma():
    w, events = make_world()
    put_stock(w, 3, 50)
    put_stock(w, 1, 20)
    warehouse.reserve(w, 1, 5)
    warehouse.issue(w, 1, 5, delivery_id=1)
    w.now = w.at_local(START, 23, 55)
    HANDLERS["wh.snapshot"](w, types.SimpleNamespace(data={"date": START.isoformat()}))
    snap = of(events, StockSnapshotTaken)[0]
    assert snap.snapshot_date == START
    assert [(r.sku_id, r.on_hand, r.reserved) for r in snap.rows] == [(1, 15, 0), (3, 50, 0)]
    p1 = w.store.get("stock", 1)
    assert p1.issued_today == 0 and p1.avg_daily_out == pytest.approx(5 * warehouse.EWMA_ALPHA)


def test_warehouse_runs_for_months_with_rare_stockouts(monkeypatch):
    """Plan + snapshot + lumpy demand with promo spikes: stock-outs are rare but happen."""

    def expected(world, sku_id, d, channel=None):
        base = _expected_daily_units(world, sku_id, d)
        if 7 <= d.day <= 13 and (sku_id + d.month) % 3 == 0:  # a modelled promo week
            base *= 2.5
        return base

    monkeypatch.setattr(sys.modules["cpgsim.domain.demandplan"], "expected_daily_units", expected)
    w, events = make_world()
    warehouse.init_stock(w)
    skus = [s.sku_id for s in REF.catalogue.active(START)][:40]
    short = asked = 0
    t_end = w.now + timedelta(days=200)
    half_day = 0
    while w.now < t_end:
        run_arrivals(w, w.now + timedelta(hours=12))
        d = w.local_date()
        for sku_id in skus:
            mean = expected(w, sku_id, d) / 2
            r = w.rng("test.demand", sku_id, half_day)
            qty = r.poisson(mean * 0.4)
            if r.random() < 0.1:  # retailer-sized orders
                qty += r.poisson(mean * 6)
            got = warehouse.reserve(w, sku_id, qty)
            asked += qty
            short += qty - got
            if got:
                warehouse.issue(w, sku_id, got, delivery_id=1)
        half_day += 1
    fill = 1 - short / asked
    assert 0.96 < fill < 0.999
    assert of(events, StockMoved, movement_type="goods_receipt")
    assert len(of(events, StockSnapshotTaken)) >= 199
    # No runaway stock: average cover stays within a few target periods.
    for sku_id in skus:
        pos = w.store.get("stock", sku_id)
        assert pos.on_hand + pos.in_production <= expected(w, sku_id, w.local_date()) * 120


# -- billing -------------------------------------------------------------------------


def test_invoice_pro_rata_shipping_and_sales_doc_status():
    w, events = make_world()
    doc = web_sales_doc(w, [(1, 3), (5, 1)])
    inv1 = billing.create_invoice(w, doc, None, [(10, 2)], START, START, include_shipping=True)
    it = doc.items[0]
    assert [i.item_no for i in inv1.items] == [10, 900]
    assert inv1.items[0].net_value == round(it.net_value / 3 * 2, 2)
    ship = inv1.items[1]
    assert (ship.sku_id, ship.net_value, ship.tax, ship.qty) == (0, 4.16, 0.79, 1)
    assert ship.sales_item_no == 900  # bills the order's freight item
    assert doc.billing_status == "partial"
    inv2 = billing.create_invoice(w, doc, None, [(10, 1), (20, 1)], START, START, include_shipping=True)
    assert [i.item_no for i in inv2.items] == [10, 20]  # shipping billed only once
    assert round(inv1.items[0].net_value + inv2.items[0].net_value, 2) == it.net_value
    assert round(inv1.items[0].tax + inv2.items[0].tax, 2) == it.tax
    assert doc.billing_status == "full"
    sdc = of(events, SalesDocChanged)
    assert sdc[0].header == {"billing_status": "partial", "status": "in_process"}
    assert sdc[0].items == {10: {"billed_qty": 2, "confirmed_qty": 3}, 900: {"billed_qty": 1, "confirmed_qty": 1}}
    assert sdc[1].header["billing_status"] == "full"
    assert sdc[1].items[20] == {"billed_qty": 1, "confirmed_qty": 1}
    assert len(of(events, BillingDocCreated)) == 2
    assert w.store.get("billing", inv1.billing_id) is inv1
    assert inv1.items[0].cost == round(REF.catalogue.get(1).std_cost_eur * 2, 2)
    assert w.queue.contains(f"ful.billing_purge:{inv1.billing_id}")


def test_freight_item_billed_once_and_completes_document():
    w, events = make_world()
    doc = web_sales_doc(w, [(1, 1)])
    fr = doc.items[-1]
    assert billing.flow_status(doc, "delivery") == "none"
    doc.items[0].delivered_qty = 1
    assert billing.flow_status(doc, "delivery") == "full"  # freight never needs delivering
    inv = billing.create_invoice(w, doc, None, [(10, 1)], START, START, include_shipping=True)
    assert fr.billed_qty == 1 and [(i.item_no, i.sales_item_no) for i in inv.items] == [(10, 10), (900, 900)]
    assert (doc.status, doc.delivery_status, doc.billing_status) == ("completed", "full", "full")
    # invoice value = order value: net and tax of all items, freight included
    assert inv.net_value == doc.net_value and inv.tax == round(sum(i.tax for i in doc.items), 2)
    # a rejected freight item is never billed
    doc2 = web_sales_doc(w, [(1, 1)])
    doc2.items[-1].rejection_reason = "customer_cancelled"
    inv2 = billing.create_invoice(w, doc2, None, [(10, 1)], START, START, include_shipping=True)
    assert [i.item_no for i in inv2.items] == [10]


def test_legacy_doc_without_freight_item_still_bills_shipping():
    w, _ = make_world()
    doc = web_sales_doc(w, [(1, 1)], freight=False)
    inv = billing.create_invoice(w, doc, None, [(10, 1)], START, START, include_shipping=True)
    assert [(i.item_no, i.sales_item_no, i.net_value) for i in inv.items] == [(10, 10, doc.items[0].net_value),
                                                                              (900, 10, 4.16)]


def test_credit_memo_and_clearing():
    w, events = make_world()
    doc = web_sales_doc(w, [(1, 2), (5, 1)])
    d = types.SimpleNamespace(delivery_id=42, lines=[
        types.SimpleNamespace(line_no=1, sku_id=1, qty=2, sales_item_no=10),
        types.SimpleNamespace(line_no=2, sku_id=5, qty=1, sales_item_no=20),
    ], billing_id=None)
    inv = billing.create_invoice(w, doc, d, None, START, START, include_shipping=True)
    assert [(i.delivery_id, i.delivery_line_no) for i in inv.items] == [(42, 1), (42, 2), (42, None)]
    cm = billing.create_credit_memo(w, inv.billing_id, [(10, 1)], "return")
    assert cm.doc_kind == "credit_memo" and cm.reference_billing_id == inv.billing_id
    assert cm.items[0].reference_item_no == 10 and cm.items[0].qty == 1
    assert cm.items[0].net_value == round(inv.items[0].net_value / 2, 2)
    assert cm.reason == "return"
    cm2 = billing.create_credit_memo(w, inv.billing_id, [(10, 5), (20, 1)], "lost", include_shipping=True)
    assert [(i.item_no, i.qty, i.reference_item_no) for i in cm2.items] == [(10, 2, 10), (20, 1, 20), (900, 1, 900)]
    assert cm2.net_value == inv.net_value and cm2.tax == inv.tax
    assert billing.create_credit_memo(w, inv.billing_id, [], "lost") is None

    billing.clear(w, inv.billing_id)
    ch = of(events, BillingDocChanged)[-1]
    assert ch.fields["status"] == "cleared" and ch.fields["paid_amount"] == round(inv.net_value + inv.tax, 2)
    assert inv.paid_at == w.now
    billing.clear(w, cm.billing_id, amount=0.5)
    assert of(events, BillingDocChanged)[-1].fields["status"] == "partially_cleared"

    drain(w, w.now + timedelta(days=200))
    assert not w.store.has("billing", inv.billing_id)


# -- fulfilment: web ---------------------------------------------------------------------


def test_web_delivery_full_lifecycle():
    w, events = make_world()
    w.now = w.at_local(START, 10, 15)
    d = web_delivery(w)
    assert d.status == "open" and d.carrier in CFG.carriers.parcel
    created = of(events, DeliveryCreated)[0]
    assert created.delivery is d
    doc = w.store.get("sales_doc", d.sales_doc_id)
    # the freight item is not delivery-relevant: delivering the goods is a full delivery
    assert doc.delivery_status == "full" and [i.delivered_qty for i in doc.items] == [2, 1, 0]
    sdc = of(events, SalesDocChanged)[0]
    assert sdc.items[10] == {"delivered_qty": 2, "confirmed_qty": 2}
    # Released after 08:00: picked the next working day.
    assert d.planned_ship_date == date(2025, 3, 4)

    notes: list = []
    drain(w, w.now + timedelta(days=20), notes)
    assert d.status == "delivered" and d.outcome == "delivered"
    statuses = [e.status for e in of(events, DeliveryChanged)]
    assert statuses[:3] == ["picked", "shipped", "in_transit"]
    assert statuses[-2:] == ["out_for_delivery", "delivered"]
    scans = of(events, CarrierScan, delivery_id=d.delivery_id)
    assert [s.scan_seq for s in scans] == list(range(1, len(scans) + 1))
    codes = [s.code for s in scans]
    assert codes[:3] == ["label_created", "picked_up", "hub_scan"]
    assert codes[-2:] == ["out_for_delivery", "delivered"]
    assert all(s.tracking_no == d.tracking_no for s in scans)
    assert d.tracking_no[:2] in ("PB", "NP", "SX") and len(d.tracking_no) == 12
    times = [s.at for s in scans]
    assert times == sorted(times)

    picked, shipped = (w.local(e.at) for e in of(events, DeliveryChanged)[:2])
    assert picked.date() == date(2025, 3, 4) and 6 <= picked.hour < 15
    assert shipped.date() == picked.date() and shipped.hour < 18
    assert timedelta(hours=1) <= shipped - picked <= timedelta(hours=4)
    deliv = w.local(d.delivered_at)
    assert 8.5 <= deliv.hour + deliv.minute / 60 <= 19.5 and deliv.weekday() < 6

    ship_ev = of(events, DeliveryChanged, status="shipped")[0]
    assert ship_ev.fields["kind"] == "web" and ship_ev.fields["tracking_no"] == d.tracking_no
    assert ship_ev.fields["freight_cost_eur"] == CFG.carriers.parcel[d.carrier].cost_eur["DE"]
    assert of(events, DeliveryChanged, status="delivered")[0].fields["delivered_at"] == d.delivered_at
    assert of(events, DeliveryChanged, status="picked")[0].fields["picked"] == {1: 2, 2: 1}

    inv = of(events, BillingDocCreated)[0].doc
    assert inv.billing_id == d.billing_id and inv.billing_date == shipped.date()
    assert [i.item_no for i in inv.items] == [10, 20, 900]
    assert w.local(of(events, BillingDocCreated)[0].at) == shipped

    gi = of(events, StockMoved, movement_type="goods_issue")
    assert sorted((e.sku_id, e.qty) for e in gi) == [(1, -2), (5, -1)]
    assert w.store.get("stock", 1).reserved == 0

    kinds = [(n.kind, n.key) for n in notes]
    assert kinds == [
        ("web.shipped", f"web.shipped:{d.delivery_id}"),
        ("web.delivery_outcome", f"web.delivery_outcome:{d.delivery_id}"),
    ]
    assert notes[0].data["billing_id"] == inv.billing_id
    assert notes[1].data["outcome"] == "delivered"
    assert "plan" not in d.meta


def test_early_release_is_picked_same_day():
    w, _ = make_world()
    w.now = w.at_local(START, 6, 40)
    d = web_delivery(w)
    assert d.planned_ship_date == START
    drain(w, w.now + timedelta(days=1))
    assert d.shipped_at is not None and w.local(d.shipped_at).date() == START


def test_cancel_delivery_before_and_after_pick():
    w, events = make_world()
    w.now = w.at_local(START, 12)
    d = web_delivery(w)
    assert fulfilment.cancel_delivery(w, d.delivery_id, "customer_cancelled") is True
    assert d.status == "cancelled"
    ev = of(events, DeliveryChanged, status="cancelled")[0]
    assert ev.fields["line_nos"] == [1, 2] and ev.fields["kind"] == "web"
    assert w.store.get("stock", 1).reserved == 0
    doc = w.store.get("sales_doc", d.sales_doc_id)
    assert [i.delivered_qty for i in doc.items] == [0, 0, 0] and doc.delivery_status == "none"
    drain(w, w.now + timedelta(days=5))
    assert d.status == "cancelled" and not of(events, CarrierScan)
    assert fulfilment.cancel_delivery(w, d.delivery_id, "again") is False

    d2 = web_delivery(w)
    drain(w, datetime.fromisoformat(d2.meta["pick_at"]))
    assert d2.status == "picked"
    assert fulfilment.cancel_delivery(w, d2.delivery_id, "customer_cancelled") is False


def _many_deliveries(n, scenarios=None, carrier=None, ship_to=DORTMUND, days=40):
    w, events = make_world(scenarios=scenarios)
    for sku_id in (1, 5):
        put_stock(w, sku_id, 10**7)
    ds = []
    notes: list = []
    for i in range(n):
        w.now = w.at_local(START, 0) + timedelta(minutes=37 * i)
        drain(w, w.now, notes)
        ds.append(web_delivery(w, lines=((1, 1),), ship_to=ship_to, carrier=carrier))
    drain(w, w.now + timedelta(days=days), notes)
    return w, events, ds, notes


def test_web_outcome_rates_and_transit():
    w, events, ds, notes = _many_deliveries(3000)
    outcomes = [d.outcome for d in ds]
    n = len(ds)
    lost = outcomes.count("lost") / n
    rts = outcomes.count("returned_to_sender") / n
    exceptions = sum(1 for d in ds if d.meta.get("exception_notified")) / n
    assert outcomes.count(None) == 0
    assert 0.0005 <= lost <= 0.012
    assert 0.015 <= exceptions <= 0.05
    assert 0.002 <= rts <= exceptions * 0.6
    delivered = [d for d in ds if d.outcome == "delivered"]
    # Median transit in delivery days from DE: next day for most parcels.
    nextday = sum(1 for d in delivered
                  if (w.local(d.delivered_at).date() - w.local(d.shipped_at).date()).days <= 3) / len(delivered)
    assert nextday > 0.8
    # Carrier shares for DE.
    shares = {c: sum(1 for d in ds if d.carrier == c) / n for c in CFG.carriers.parcel}
    for c, cc in CFG.carriers.parcel.items():
        assert abs(shares[c] - cc.share["DE"]) < 0.04
    # Notifications: one outcome per delivery, one exception note per excepted delivery.
    outc = [x for x in notes if x.kind == "web.delivery_outcome"]
    assert len(outc) == n and len({x.key for x in outc}) == n
    exc_notes = [x for x in notes if x.kind == "web.delivery_exception"]
    assert len(exc_notes) == round(exceptions * n)
    # Lost parcels: no scans after the hub, declared lost 10+ days after shipping.
    for d in ds:
        if d.outcome == "lost":
            codes = [s.code for s in of(events, CarrierScan, delivery_id=d.delivery_id)]
            assert codes == ["label_created", "picked_up", "hub_scan"]
            lost_ev = of(events, DeliveryChanged, delivery_id=d.delivery_id, status="lost")[0]
            gap = w.local(lost_ev.at).date() - w.local(d.shipped_at).date()
            assert gap.days >= CFG.fulfilment.lost_declared_after_days
    # Delivered times: delivery days only, no Sundays.
    assert all(w.local(d.delivered_at).weekday() != 6 for d in delivered)
    # Scan local times are plausible.
    for s in of(events, CarrierScan):
        h = w.local(s.at).hour
        if s.code == "out_for_delivery":
            assert 6 <= h <= 9
        if s.code in ("delivered", "delivery_failed"):
            assert 8 <= h <= 19


def test_exception_paths_redelivery_and_return_to_sender():
    scen = StubScenarios({"carrier_exception": 1000.0})
    w, events, ds, notes = _many_deliveries(200, scenarios=scen)
    excepted = [d for d in ds if d.meta.get("exception_notified")]
    assert len(excepted) > 150
    redelivered = [d for d in excepted if d.outcome == "delivered"]
    returned = [d for d in excepted if d.outcome == "returned_to_sender"]
    assert redelivered and returned
    assert 0.5 < len(redelivered) / len(excepted) < 0.9
    d = redelivered[0]
    codes = [s.code for s in of(events, CarrierScan, delivery_id=d.delivery_id)]
    assert "delivery_failed" in codes and "redelivery_scheduled" in codes and codes[-1] == "delivered"
    assert d.attempts == 2
    st = [e.status for e in of(events, DeliveryChanged, delivery_id=d.delivery_id)]
    assert "exception" in st and st[-1] == "delivered"
    r = returned[0]
    codes = [s.code for s in of(events, CarrierScan, delivery_id=r.delivery_id)]
    assert codes[-1] == "returned_to_sender"
    back = of(events, StockMoved, movement_type="customer_return", delivery_id=r.delivery_id)
    assert len(back) == 1 and back[0].qty == 1 and back[0].return_id is None
    rts_at = w.local(back[0].at)
    assert warehouse.is_working_day(w, rts_at.date()) and 9 <= rts_at.hour < 15
    out = [x for x in notes if x.kind == "web.delivery_outcome" and x.data["delivery_id"] == r.delivery_id]
    assert out[0].data["outcome"] == "returned_to_sender"


def test_carrier_decay_factors_slow_down_one_carrier_in_one_region():
    def delay(ctx):
        return 2.5 if ctx["carrier"] == "Swiftbox" and ctx["region"] == "DE-BY" else 1.0

    scen = StubScenarios({"carrier_delay": delay})
    _, _, by, _ = _many_deliveries(300, scenarios=scen, carrier="Swiftbox", ship_to=MUNICH, days=30)
    _, _, base, _ = _many_deliveries(300, scenarios=StubScenarios(), carrier="Swiftbox", ship_to=MUNICH, days=30)
    med_by = statistics.median(d.meta["transit_days"] for d in by)
    med_base = statistics.median(d.meta["transit_days"] for d in base)
    assert med_by / med_base == pytest.approx(2.5, rel=0.15)
    assert med_base == pytest.approx(CFG.carriers.parcel["Swiftbox"].median_days["DE-BY"], rel=0.1)
    delay_calls = [c for c in scen.calls if c[0] == "carrier_delay"]
    assert {c[1] for c in delay_calls} == {("delivery", d.delivery_id) for d in by}
    assert all(c[3] for c in delay_calls)
    assert delay_calls[0][2] == {"carrier": "Swiftbox", "region": "DE-BY", "country": "DE"}
    assert {c[0] for c in scen.calls} == {"carrier_delay", "carrier_exception"}


def test_country_medians_for_austria_and_switzerland():
    vienna = Address("AT", "AT-9", "1010", "Wien")
    zurich = Address("CH", "CH-ZH", "8001", "Zürich")
    _, _, at, _ = _many_deliveries(200, carrier="Nordpost", ship_to=vienna, days=30)
    _, _, ch, _ = _many_deliveries(200, carrier="Nordpost", ship_to=zurich, days=30)
    assert statistics.median(d.meta["transit_days"] for d in at) == pytest.approx(2.3, rel=0.12)
    assert statistics.median(d.meta["transit_days"] for d in ch) == pytest.approx(2.4, rel=0.12)
    assert all(d.freight_cost_eur == CFG.carriers.parcel["Nordpost"].cost_eur["CH"] for d in ch)


# -- fulfilment: retail ----------------------------------------------------------------------


def test_retail_pallet_delivery_with_pod():
    w, events = make_world()
    w.now = w.at_local(START, 9, 30)
    dc = REF.accounts.dcs("drogerie_kranz")[0]
    lines = [(1, 120), (5, 48)]
    for sku_id, qty in lines:
        put_stock(w, sku_id, 1000)
        warehouse.reserve(w, sku_id, qty)
    sid = w.next_id("sales_doc")
    doc = SalesDoc(
        sales_doc_id=sid, doc_type="retail", sold_to=REF.accounts.retailer("drogerie_kranz").account_id,
        ship_to=dc.account_id, currency="EUR", fx_rate_eur=1.0, created_at=w.now,
        requested_date=START + timedelta(days=3), external_ref="PO-1", channel="drogerie_kranz", order_id=7,
        items=[SalesItem(10, 1, 120, 120, 300.0, 57.0, 100.0), SalesItem(20, 5, 48, 48, 150.0, 28.5, 50.0)],
    )
    w.store.put("sales_doc", sid, doc)
    planned = START + timedelta(days=1)
    d = fulfilment.create_delivery(w, "retail", doc, [(1, 120, 10), (5, 48, 20)], dc.account_id,
                                   dc.address, None, planned, order_id=7)
    assert d.carrier == CFG.carriers.pallet_forwarder and d.planned_ship_date == planned
    notes: list = []
    drain(w, w.now + timedelta(days=10), notes)
    assert d.status == "delivered" and d.outcome == "delivered"
    assert d.tracking_no.startswith("FW") and len(d.tracking_no) == 11
    cases = math.ceil(120 / REF.catalogue.get(1).units_per_case) + math.ceil(48 / REF.catalogue.get(5).units_per_case)
    assert d.freight_cost_eur == round(CFG.carriers.pallet_cost_eur_per_case * cases, 2)
    codes = [s.code for s in of(events, CarrierScan)]
    assert codes == ["picked_up", "delivered", "pod_signed"]
    picked = w.local(of(events, DeliveryChanged, status="picked")[0].at)
    shipped = w.local(d.shipped_at)
    assert picked.date() == planned and 6 <= picked.hour < 10
    assert shipped.date() == planned and 11 <= shipped.hour < 15
    deliv = of(events, DeliveryChanged, status="delivered")[0]
    assert deliv.fields["pod_signed_by"] and deliv.fields["kind"] == "retail"
    assert deliv.fields["delivered_at"] == d.delivered_at
    assert w.ref.calendar.is_working_day(w.local(d.delivered_at).date(), dc.address.country, dc.address.region)
    assert not of(events, BillingDocCreated)  # retail invoices after POD, by the retail module
    assert [n.kind for n in notes] == ["retail.shipped", "retail.pod"]
    assert notes[1].data == {"delivery_id": d.delivery_id, "order_id": 7}


def test_tracking_numbers_are_unique():
    w, _ = make_world()
    nums = {fulfilment.tracking_number(w, "Paketblitz", i, "web") for i in range(1, 50_001)}
    assert len(nums) == 50_000
    pallets = {fulfilment.tracking_number(w, "Frachtwerk Logistik", i, "retail") for i in range(1, 20_001)}
    assert len(pallets) == 20_000


def test_deterministic_replay():
    def run():
        w, events = make_world()
        w.now = w.at_local(START, 11)
        for _ in range(30):
            web_delivery(w)
            w.now += timedelta(minutes=50)
        drain(w, w.now + timedelta(days=20))
        return [repr(e) for e in events]

    assert run() == run()
