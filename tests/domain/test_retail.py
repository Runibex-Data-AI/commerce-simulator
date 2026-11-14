"""Retail channel and master data: unit tests on a lightweight World.

Other agents' modules (warehouse, fulfilment, billing, promotions, demandplan)
are replaced by small fakes injected into ``sys.modules`` so the retail and
master data logic is tested on its own. The value-share check uses the real
``demandplan`` when it is importable and falls back to a stub implementing the
contract's sizing otherwise.
"""

from __future__ import annotations

import importlib
import math
import sys
import types
from collections import defaultdict
from datetime import date, datetime, timedelta

import pytest

from cpgsim.config import Config
from cpgsim.domain import masterdata, retail
from cpgsim.domain.events import (
    B2BAccountCreated,
    CustomerMaterialListed,
    FxRatePublished,
    SalesDocChanged,
    SalesDocCreated,
    SkuListed,
    StandardCostRevised,
)
from cpgsim.domain.model import (
    ENTITY_KINDS,
    BillingDoc,
    BillingItem,
    Counters,
    Delivery,
    DeliveryLine,
    Promotion,
    goods_item_no,
)
from cpgsim.domain.reference import build_reference
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import EventQueue
from cpgsim.engine.registry import ARRIVAL_SOURCES, HANDLERS
from cpgsim.engine.world import World
from cpgsim.models.pricing import elasticity_lift
from cpgsim.models.seasonality import category_factor

HIST = date(2025, 3, 3)  # a Monday

_REF_CACHE: dict = {}


class StubScenarios:
    version = 0

    def __init__(self, factors: dict[str, float] | None = None) -> None:
        self.factors = factors or {}
        self.recorded: list = []

    def factor(self, name, t, world=None, record=None, **ctx):
        f = self.factors.get(name, 1.0)
        if world is not None and record is not None and f != 1.0:
            self.recorded.append((name, record, ctx))
        return f

    def active(self, *a, **k):
        return []

    def windows(self, *a, **k):
        return []

    def record_effect(self, *a, **k):
        return None

    def scenario_promotions(self):
        return []


def make_world(cfg: Config | None = None, start: date = HIST, scenarios=None):
    cfg = cfg or Config()
    collected: list = []
    world = World(cfg, EntityStore(), EventQueue(), emit=collected.append)
    for kind, cls in ENTITY_KINDS.items():
        world.store.register(kind, cls)
    key = cfg.fingerprint()
    if key not in _REF_CACHE:
        _REF_CACHE[key] = build_reference(cfg)
    world.ref = _REF_CACHE[key]
    world.scenarios = scenarios or StubScenarios()
    world.store.put("counters", "all", Counters())
    world.now = world.history_start = world.at_local(start, 0)
    return world, collected


def run(world, until: datetime, sources, step_hours: float = 6.0, on_event=None) -> None:
    """Drive arrival sources and drain the queue up to ``until`` (mini runner)."""
    t = world.now
    step = timedelta(hours=step_hours)
    while t < until:
        end = min(t + step, until)
        for name in sorted(sources):
            for ev in ARRIVAL_SOURCES[name](world, t, end):
                assert t < ev.due <= end
                world.queue.push(ev)
        while (due := world.queue.peek_due()) is not None and due <= end:
            ev = world.queue.pop()
            world.now = ev.due
            HANDLERS[ev.kind](world, ev)
            if on_event:
                on_event(world, ev)
        world.now = end
        t = end


# ---------------------------------------------------------------------------
# Fakes for other agents' modules
# ---------------------------------------------------------------------------


def stub_demandplan(promos_mod) -> types.ModuleType:
    """Contract sizing: retailer value = web value * share_r / share_web."""
    mod = types.ModuleType("cpgsim.domain.demandplan")
    cache: dict = {}

    def basket_stats(world):
        cat = world.ref.catalogue
        act = cat.active(world.local_date(world.history_start))
        pop = sum(s.popularity for s in act)
        price = sum(s.popularity * s.rrp_eur / 1.19 for s in act) / pop
        lines = 1.0 + world.cfg.channels.web.basket_extra_lines_mean
        qty = 1.15
        return {"lines": lines, "qty": qty, "basket_net_eur": price * lines * qty}

    def retail_weekly_demand(world, channel, dc_account, sku_id, week_start):
        key = (channel, week_start)
        if key not in cache:
            cfg = world.cfg
            rc = cfg.channels.retailers()[channel]
            cat = world.ref.catalogue
            mid = week_start + timedelta(days=3)
            ids = cat.retailer_assortment(channel, mid)
            web_value = cfg.channels.web.base_orders_per_day * 365 * basket_stats(world)["basket_net_eur"]
            value = web_value * rc.share / cfg.channels.web.share
            skus = [cat.get(i) for i in ids]
            pop = sum(s.popularity for s in skus)
            price = sum(s.popularity * s.list_price_eur * (1 - rc.trade_discount) for s in skus) / pop
            weekly = value / price * 7 / 365
            n_dcs = len(world.ref.accounts.dcs(channel))
            cache[key] = {
                s.sku_id: weekly * s.popularity * category_factor(s.season_profile, mid)
                * promos_mod.demand_factor(world, s.sku_id, channel, mid) / pop / n_dcs
                for s in skus
            }
        return cache[key].get(sku_id, 0.0)

    mod.basket_stats = basket_stats
    mod.retail_weekly_demand = retail_weekly_demand
    return mod


def fake_promotions(promos: list[Promotion], elasticity: float = -2.0) -> types.ModuleType:
    mod = types.ModuleType("cpgsim.domain.promotions")

    def is_eligible(p, sku):
        return sku.sku_id in p.sku_ids or (p.brand is not None and p.brand == sku.brand)

    def _covers(world, p, d):
        return world.local_date(p.start) <= d < world.local_date(p.end)

    def demand_factor(world, sku_id, channel, d):
        sku = world.ref.catalogue.get(sku_id)
        for p in promos:
            if p.channel == channel and is_eligible(p, sku) and _covers(world, p, d):
                return elasticity_lift(p.discount_pct, elasticity)
        return 1.0

    def retail_promos_starting(world, channel, start, end):
        return [p for p in promos if p.channel == channel and start <= world.local_date(p.start) < end]

    mod.is_eligible = is_eligible
    mod.demand_factor = demand_factor
    mod.retail_promos_starting = retail_promos_starting
    return mod


class FakeWarehouse:
    def __init__(self, short: dict[int, int] | None = None) -> None:
        self.short = short or {}  # sku_id -> units available per reservation
        self.reserved: dict[int, int] = defaultdict(int)

    def module(self) -> types.ModuleType:
        mod = types.ModuleType("cpgsim.domain.warehouse")

        def reserve(world, sku_id, qty):
            g = min(qty, self.short.get(sku_id, 10**9))
            self.reserved[sku_id] += g
            return g

        def unreserve(world, sku_id, qty):
            self.reserved[sku_id] -= qty

        mod.reserve = reserve
        mod.unreserve = unreserve
        return mod


class FakeFulfilment:
    def __init__(self) -> None:
        self.created: list[Delivery] = []

    def module(self) -> types.ModuleType:
        mod = types.ModuleType("cpgsim.domain.fulfilment")

        def create_delivery(world, kind, sales_doc, lines, ship_to_account, ship_to, carrier,
                            planned_ship_date, order_id, seq_in_order=1):
            did = world.next_id("delivery")
            d = Delivery(
                delivery_id=did, kind=kind, sales_doc_id=sales_doc.sales_doc_id,
                ship_to_account=ship_to_account, ship_to=ship_to,
                carrier=carrier or world.cfg.carriers.pallet_forwarder,
                lines=[DeliveryLine(i, s, q, n) for i, (s, q, n) in enumerate(lines, start=1)],
                created_at=world.now, planned_ship_date=planned_ship_date, order_id=order_id,
                seq_in_order=seq_in_order,
            )
            world.store.put("delivery", did, d)
            self.created.append(d)
            ship = world.at_local(planned_ship_date, 12)
            pod_day = world.ref.calendar.add_working_days(planned_ship_date, 2, "DE")
            world.schedule("retail.shipped", ship, f"retail.shipped:{did}", {"delivery_id": did})
            world.schedule("retail.pod", world.at_local(pod_day, 10), f"retail.pod:{did}",
                           {"delivery_id": did})
            return d

        def cancel_delivery(world, delivery_id, reason):
            return False

        mod.create_delivery = create_delivery
        mod.cancel_delivery = cancel_delivery
        return mod


class FakeBilling:
    def __init__(self) -> None:
        self.invoices: list[tuple[BillingDoc, datetime]] = []
        self.cleared: list[tuple[int, datetime]] = []

    def module(self) -> types.ModuleType:
        mod = types.ModuleType("cpgsim.domain.billing")

        def create_invoice(world, sales_doc, delivery, items, billing_date, due_date, include_shipping):
            by_item = {i.item_no: i for i in sales_doc.items}
            bitems = []
            for n, line in enumerate(delivery.lines, start=1):
                si = by_item[line.sales_item_no]
                bitems.append(BillingItem(
                    item_no=n * 10, sku_id=line.sku_id, qty=line.qty,
                    net_value=round(si.net_value / si.qty * line.qty, 2),
                    tax=round(si.tax / si.qty * line.qty, 2), cost=0.0,
                    sales_item_no=line.sales_item_no, delivery_id=delivery.delivery_id,
                    delivery_line_no=line.line_no,
                ))
            bid = world.next_id("billing")
            doc = BillingDoc(bid, "invoice", sales_doc.sales_doc_id, sales_doc.sold_to, "EUR", 1.0,
                             billing_date, world.now, due_date, bitems)
            world.store.put("billing", bid, doc)
            self.invoices.append((doc, world.now))
            return doc

        def clear(world, billing_id, amount=None):
            self.cleared.append((billing_id, world.now))

        mod.create_invoice = create_invoice
        mod.clear = clear
        return mod


def kranz_promo(world) -> Promotion:
    """A 20 % Kranz promotion on the five most popular listed SKUs, 14 days from 14 April."""
    cat = world.ref.catalogue
    ids = cat.retailer_assortment("drogerie_kranz", date(2025, 4, 14))
    top = sorted(ids, key=lambda i: (-cat.get(i).popularity, i))[:5]
    start = world.at_local(date(2025, 4, 14), 0)
    return Promotion(
        promo_id=500001, channel="drogerie_kranz", name="Kranz Frühjahrsaktion", kind="automatic",
        code=None, discount_pct=0.20, free_shipping=False, sku_ids=sorted(top), brand=None,
        start=start, end=start + timedelta(days=14), announced_at=start - timedelta(weeks=6),
    )


@pytest.fixture
def sim(monkeypatch):
    """A world with fakes installed; returns a namespace with the world and fakes."""
    world, collected = make_world()
    promo = kranz_promo(world)
    promos_mod = fake_promotions([promo])
    cat = world.ref.catalogue
    kranz_ids = cat.retailer_assortment("drogerie_kranz", HIST)
    # A popular SKU that is not promoted and runs short at the warehouse: 2 cases per order.
    short_sku = sorted((i for i in kranz_ids if i not in promo.sku_ids),
                       key=lambda i: (-cat.get(i).popularity, i))[0]
    wh = FakeWarehouse({short_sku: 2 * cat.get(short_sku).units_per_case + 3})
    ful, bil = FakeFulfilment(), FakeBilling()
    monkeypatch.setitem(sys.modules, "cpgsim.domain.promotions", promos_mod)
    monkeypatch.setitem(sys.modules, "cpgsim.domain.demandplan", stub_demandplan(promos_mod))
    monkeypatch.setitem(sys.modules, "cpgsim.domain.warehouse", wh.module())
    monkeypatch.setitem(sys.modules, "cpgsim.domain.fulfilment", ful.module())
    monkeypatch.setitem(sys.modules, "cpgsim.domain.billing", bil.module())
    return types.SimpleNamespace(world=world, events=collected, promo=promo, wh=wh, ful=ful,
                                 bil=bil, short_sku=short_sku)


RETAIL_SOURCES = ("retail.sellout", "retail.po_cycle")


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


def test_fx_rate_pure_monthly_walk():
    world, _ = make_world()
    assert masterdata.fx_rate(world, "EUR", date(2025, 5, 17)) == 1.0
    rates = []
    for y in range(2015, 2031):
        for m in range(1, 13):
            r = masterdata.fx_rate(world, "CHF", date(y, m, 1))
            assert r == masterdata.fx_rate(world, "CHF", date(y, m, 28))  # constant within the month
            rates.append(r)
    assert all(0.93 < r < 1.18 for r in rates)
    assert 1.0 < sum(rates) / len(rates) < 1.10
    assert len(set(rates)) > 150  # moves month to month
    other, _ = make_world(Config(seed=7))
    assert masterdata.fx_rate(other, "CHF", date(2026, 3, 1)) != masterdata.fx_rate(world, "CHF", date(2026, 3, 1))
    with pytest.raises(ValueError):
        masterdata.fx_rate(world, "USD", date(2026, 3, 1))


def test_std_cost_annual_revision():
    world, _ = make_world()
    for sku in world.ref.catalogue.skus[:40]:
        c25 = masterdata.std_cost(world, sku.sku_id, date(2025, 12, 31))
        c26 = masterdata.std_cost(world, sku.sku_id, date(2026, 1, 1))
        c27 = masterdata.std_cost(world, sku.sku_id, date(2027, 6, 1))
        assert c25 == round(sku.std_cost_eur, 2)
        assert masterdata.std_cost(world, sku.sku_id, date(2024, 1, 1)) == c25
        assert sku.std_cost_eur * 1.01 - 0.006 <= c26 <= sku.std_cost_eur * 1.04 + 0.006
        assert c27 >= c26
    # history starting on 1 January: that year's revision happens 10 minutes in
    w2, _ = make_world(start=date(2025, 1, 1))
    s = w2.ref.catalogue.skus[0]
    assert masterdata.std_cost(w2, s.sku_id, date(2025, 1, 1)) > s.std_cost_eur - 0.005


def test_master_bootstrap():
    world, ev = make_world()
    masterdata.bootstrap_master_data(world)
    accs = [e for e in ev if isinstance(e, B2BAccountCreated)]
    assert [a.account.account_id for a in accs] == [a.account_id for a in world.ref.accounts.all]
    listed = {e.sku.sku_id for e in ev if isinstance(e, SkuListed)}
    cat = world.ref.catalogue
    for s in cat.skus:
        expect = s.launch_date - timedelta(days=60) < HIST  # listed 06:00 on the listing date
        if s.delist_date is not None and s.delist_date < HIST - timedelta(days=365):
            expect = False
        assert (s.sku_id in listed) == expect, s.sku_id
    # every SKU a retailer lists on the start date has a customer material record
    knmt = defaultdict(set)
    for e in ev:
        if isinstance(e, CustomerMaterialListed):
            knmt[e.account_id].add(e.sku_id)
            assert e.sku_id in listed
            channel = world.ref.accounts.get(e.account_id).retailer
            assert e.customer_article == cat.customer_article(channel, e.sku_id)
    for channel in world.cfg.channels.retailers():
        acc = world.ref.accounts.retailer(channel).account_id
        assort = set(cat.retailer_assortment(channel, HIST))
        assert assort <= knmt[acc]
        for sku_id in knmt[acc]:
            assert masterdata.retailer_lists(world, channel, cat.get(sku_id))
    # Cutara is not listed at the drugstores
    derma = cat.brand_of_role("derma")
    assert all(cat.get(i).brand != derma for i in knmt["KRANZ"] | knmt["MYLO"])
    fx = [e for e in ev if isinstance(e, FxRatePublished)]
    assert [(f.from_currency, f.valid_from) for f in fx] == [("CHF", date(2025, 3, 1))]


def test_master_records_back_dated_plausibly():
    from cpgsim.domain.events import ServiceMaterialListed

    world, ev = make_world()
    masterdata.bootstrap_master_data(world)
    # accounts opened years before history start, before any back-dated web customer (<= 1,760 days)
    opened = [e.opened_on for e in ev if isinstance(e, B2BAccountCreated)]
    assert opened and all(d <= HIST - timedelta(days=1800) for d in opened)
    assert opened == [masterdata.account_opened_on(world, e.account.account_id)
                      for e in ev if isinstance(e, B2BAccountCreated)]  # deterministic
    assert len(set(opened)) > 1
    (srv,) = [e for e in ev if isinstance(e, ServiceMaterialListed)]
    assert srv.service == "shipping" and srv.listed_on == min(opened)
    # materials carry their listing date; a delisting never precedes it
    listed = [e for e in ev if isinstance(e, SkuListed)]
    assert listed
    for e in listed:
        assert e.listed_on == e.sku.launch_date - timedelta(days=60) <= HIST
        if e.sku.delist_date is not None:
            assert e.sku.delist_date >= e.listed_on
    for s in world.ref.catalogue.skus:  # whole catalogue, also SKUs listed later
        if s.delist_date is not None:
            assert s.delist_date > masterdata.listing_date(s), s.sku_id


def test_master_arrivals_and_handlers():
    world, ev = make_world()
    masterdata.bootstrap_master_data(world)
    boot_listed = {e.sku.sku_id for e in ev if isinstance(e, SkuListed)}
    ev.clear()
    run(world, world.at_local(date(2026, 3, 3), 0), ("master.fx", "master.cost", "master.listing"),
        step_hours=7)
    fx = [e for e in ev if isinstance(e, FxRatePublished)]
    assert [f.valid_from for f in fx] == [date(2025 + (m > 12), (m - 1) % 12 + 1, 1) for m in range(4, 16)]
    assert all(world.local(f.at).day == 1 and world.local(f.at).minute == 5 for f in fx)
    listings = [e for e in ev if isinstance(e, SkuListed)]
    assert listings, "launches inside the year are listed 60 days ahead"
    for e in listings:
        s = world.ref.catalogue.get(e.sku.sku_id)
        assert world.local_date(e.at) == s.launch_date - timedelta(days=60) == e.listed_on
        assert e.sku.sku_id not in boot_listed
        assert e.sku.std_cost_eur == masterdata.std_cost(world, s.sku_id, world.local_date(e.at))
    revs = [e for e in ev if isinstance(e, StandardCostRevised)]
    assert revs and all(r.valid_from == date(2026, 1, 1) for r in revs)
    listed_before = boot_listed | {e.sku.sku_id for e in listings if e.at < revs[0].at}
    assert {r.sku_id for r in revs} <= listed_before
    for r in revs:
        assert r.std_cost_eur == masterdata.std_cost(world, r.sku_id, date(2026, 1, 1))


def test_master_fx_on_first_of_month_start():
    world, ev = make_world(start=date(2025, 6, 1))
    masterdata.bootstrap_master_data(world)
    assert not [e for e in ev if isinstance(e, FxRatePublished)]
    run(world, world.at_local(date(2025, 6, 1), 1), ("master.fx",))
    fx = [e for e in ev if isinstance(e, FxRatePublished)]
    assert [f.valid_from for f in fx] == [date(2025, 6, 1)]


# ---------------------------------------------------------------------------
# Retail
# ---------------------------------------------------------------------------


def test_po_schedule_and_pricing():
    assert retail.po_number("drogerie_kranz", 17, date(2025, 3, 4)) == "4500000017"
    assert retail.po_number("mylo", 17, date(2025, 3, 4)) == "MY2503-000017"
    conds, net, tax = retail.price_line(3.10, 24, 0.36, 0.10)
    assert [c.ctype for c in conds] == ["list_price", "customer_discount", "promo_discount", "tax"]
    assert conds[0].amount == 74.4 and conds[1].amount == -26.78
    assert conds[2].amount == round(-(74.4 - 26.78) * 0.10, 2)
    assert net == round(74.4 - 26.78 + conds[2].amount, 2)
    assert tax == round(net * 0.19, 2)
    world, _ = make_world()
    plan = retail.dc_plan(world)
    assert [p[1] for p in plan] == ["KRANZ-DC1", "KRANZ-DC2", "KRANZ-DC3", "MYLO-DC1", "MYLO-DC2"]
    weekdays = [next(d.weekday() for d in (HIST + timedelta(days=k) for k in range(7))
                     if retail.is_order_day(7, i, d)) for _, _, i in plan]
    assert sorted(weekdays) == [0, 1, 2, 3, 4]


def test_retail_bootstrap(sim):
    world = sim.world
    retail.bootstrap_retail(world)
    for channel, dc, _ in retail.dc_plan(world):
        inv = world.store.get("retailer_inventory", dc)
        assort = world.ref.catalogue.retailer_assortment(channel, HIST)
        assert sorted(inv.forecast) == assort
        mid = HIST + timedelta(days=3)
        for s in assort:
            season = max(category_factor(world.ref.catalogue.get(s).season_profile, mid), 0.05)
            assert inv.on_hand[s] == round(inv.forecast[s] * season * 2.0)
        assert sum(inv.on_hand.values()) > 0


@pytest.fixture
def quarter(sim):
    """Run 26 weeks of retail; collect weekly sell-out per DC and POD receipts."""
    world = sim.world
    retail.bootstrap_retail(world)
    sellout = defaultdict(lambda: defaultdict(float))  # (dc) -> week_start -> units
    lost = defaultdict(float)  # sku -> units
    receipts = defaultdict(lambda: defaultdict(float))  # dc -> week (monday) -> units
    pods: list = []
    payments: list = []
    inventory_start = {dc: sum(world.store.get("retailer_inventory", dc).on_hand.values())
                       for _, dc, _ in retail.dc_plan(world)}

    def on_event(w, ev):
        if ev.kind == "retail.sellout":
            so = w.store.get("retailer_inventory", ev.data["dc"] + retail.SELLOUT_SUFFIX)
            sellout[ev.data["dc"]][ev.data["week_start"]] += sum(so.on_hand.values())
            for s, q in so.on_order.items():
                lost[s] += q
        elif ev.kind == "retail.pod":
            d = w.store.get("delivery", ev.data["delivery_id"])
            po = w.store.get("retail_po", d.order_id)
            wk = retail.monday(w.local_date()).isoformat()
            receipts[po.dc_account][wk] += sum(l.qty for l in d.lines)
            pods.append((po.po_id, w.now))
        elif ev.kind == "retail.payment":
            payments.append((ev.data["po_id"], ev.data["billing_id"], w.now))

    run(world, world.at_local(HIST + timedelta(weeks=26), 0), RETAIL_SOURCES, on_event=on_event)
    return types.SimpleNamespace(sim=sim, sellout=sellout, lost=lost, receipts=receipts, pods=pods,
                                 payments=payments, inventory_start=inventory_start)


def test_po_lifecycle(quarter):
    sim = quarter.sim
    world = sim.world
    created = [e for e in sim.events if isinstance(e, SalesDocCreated)]
    assert len(created) == 5 * 26  # one PO per DC and week
    cal = world.ref.calendar
    for e in created:
        doc = e.doc
        loc = world.local(e.at)
        assert loc.hour == world.cfg.retail.order_hour_local and loc.minute == 0
        assert doc.doc_type == "retail" and doc.currency == "EUR" and doc.fx_rate_eur == 1.0
        assert doc.ship_to.startswith(doc.sold_to)  # KRANZ / KRANZ-DC1
        # 10, 20, ...; 900 is the freight item number, so a 90th line is 910
        assert [i.item_no for i in doc.items] == [goods_item_no(n + 1) for n in range(len(doc.items))]
        assert 900 not in [i.item_no for i in doc.items]
        for it in doc.items:
            sku = world.ref.catalogue.get(it.sku_id)
            assert it.qty % sku.units_per_case == 0 and it.qty > 0
            assert it.customer_article == world.ref.catalogue.customer_article(doc.channel, it.sku_id)
            amounts = {c.ctype: c.amount for c in it.conditions}
            assert it.net_value == round(sum(v for k, v in amounts.items() if k != "tax"), 2)
            assert it.tax == amounts["tax"] == round(it.net_value * 0.19, 2)
        if doc.channel == "drogerie_kranz":
            assert doc.external_ref.startswith("45") and len(doc.external_ref) == 10
        else:
            assert doc.external_ref.startswith("MY")
        lead = cal.working_days_between(loc.date(), doc.requested_date, "DE",
                                        world.ref.accounts.get(doc.ship_to).address.region)
        assert 2 <= lead <= 4
    # deliveries planned for the next warehouse working day after confirmation
    for d in sim.ful.created:
        assert d.kind == "retail" and cal.is_working_day(d.planned_ship_date, "DE", "DE-HE")
        assert world.local_date(d.created_at) < d.planned_ship_date
    # invoice after POD, due = billing date + terms, payment on a working day near the due date
    pod_at = dict(quarter.pods)
    by_billing = {}
    for doc, at in sim.bil.invoices:
        po_id = next(x.order_id for x in sim.ful.created if x.sales_doc_id == doc.sales_doc_id)
        assert at > pod_at[po_id]
        assert 0 <= (doc.billing_date - world.local_date(pod_at[po_id])).days <= 4
        terms = world.cfg.channels.retailers()[
            "drogerie_kranz" if doc.payer == "KRANZ" else "mylo"].payment_terms_days
        assert doc.due_date == doc.billing_date + timedelta(days=terms)
        by_billing[doc.billing_id] = doc
    assert sim.bil.cleared, "early POs are paid within the half year"
    for bid, at in sim.bil.cleared:
        doc = by_billing[bid]
        d = world.local_date(at)
        assert cal.is_working_day(d, "DE", "DE-HE")
        assert -6 <= (d - doc.due_date).days <= 6 + 4
        assert 30 <= (d - doc.billing_date).days <= 70
        # paid POs are removed together with their sales doc and invoice
        assert not world.store.has("billing", bid)
        assert not world.store.has("sales_doc", doc.sales_doc_id)
    # every open PO has a plausible status, older ones are further along
    statuses = {po.status for _, po in world.store.items("retail_po")}
    assert statuses <= {"received", "confirmed", "delivery_planned", "shipped", "delivered", "invoiced"}
    assert world.store.count("retail_po") < 5 * 12
    # shipped -> delivered_qty updates carry confirmed_qty
    changed = [e for e in sim.events if isinstance(e, SalesDocChanged) and e.header.get("status") == "in_process"]
    assert changed
    for e in changed:
        for v in e.items.values():
            assert "confirmed_qty" in v and v["delivered_qty"] == v["confirmed_qty"]


def test_cuts_and_lost_sales(quarter):
    sim = quarter.sim
    short = sim.short_sku
    cuts = [
        (e, v) for e in sim.events if isinstance(e, SalesDocChanged) and not e.header
        for v in e.items.values() if "confirmed_qty" in v
    ]
    assert cuts, "the short SKU gets confirmed below the ordered quantity"
    upc = sim.world.ref.catalogue.get(short).units_per_case
    assert all(v["confirmed_qty"] % upc == 0 for _, v in cuts)
    assert all(v["confirmed_qty"] == 2 * upc for _, v in cuts)
    # reservations are whole cases (the remainder was unreserved)
    assert sim.wh.reserved[short] % upc == 0
    assert quarter.lost[short] > 0, "persistent cuts lead to empty shelves and lost sales"


def test_promo_forward_buy_and_discount(quarter):
    sim = quarter.sim
    world = sim.world
    promo = sim.promo
    start = world.local_date(promo.start)
    fs = world.cfg.promotions.retail_funding_share
    kranz = [e.doc for e in sim.events if isinstance(e, SalesDocCreated) and e.doc.channel == "drogerie_kranz"]
    in_window, before = defaultdict(float), defaultdict(float)
    for doc in kranz:
        d = world.local_date(doc.created_at)
        window = start - timedelta(weeks=2) <= d < start
        for it in doc.items:
            pd = [c for c in it.conditions if c.ctype == "promo_discount"]
            if window and it.sku_id in promo.sku_ids:
                assert pd and math.isclose(pd[0].rate, fs * promo.discount_pct)
                in_window[it.sku_id] += it.qty
            else:
                assert not pd
            if start - timedelta(weeks=5) <= d < start - timedelta(weeks=2) and it.sku_id in promo.sku_ids:
                before[it.sku_id] += it.qty
    # two weeks of forward buying versus three weeks of normal ordering
    assert sum(in_window.values()) / 2 > 1.5 * sum(before.values()) / 3


def test_sellin_sellout_diverge_weekly_converge_quarterly(quarter):
    world = quarter.sim.world
    for channel, dc, _ in retail.dc_plan(world):
        so = quarter.sellout[dc]
        si = quarter.receipts[dc]
        weeks = sorted(so)[1:]
        ratios = [si.get(w, 0.0) / so[w] for w in weeks if so[w] > 0]
        mean = sum(ratios) / len(ratios)
        sd = math.sqrt(sum((r - mean) ** 2 for r in ratios) / len(ratios))
        assert sd > 0.03, f"{dc}: sell-in tracks sell-out too closely week to week"
        # Over the half year received units and sell-out agree within 5 %. Per quarter the
        # comparison uses confirmed quantities by order week, because a public holiday can
        # move one week's receipt across the quarter boundary.
        assert abs(sum(si.get(w, 0.0) for w in weeks) / sum(so[w] for w in weeks) - 1) < 0.05
        conf = defaultdict(float)
        for d in quarter.sim.ful.created:
            if d.ship_to_account == dc:
                conf[retail.monday(world.local_date(d.created_at)).isoformat()] += sum(l.qty for l in d.lines)
        for k0 in (1, 12):
            q = [(HIST + timedelta(weeks=k)).isoformat() for k in range(k0, k0 + 13)]
            q = [w for w in q if w in so]
            ratio = sum(conf.get(w, 0.0) for w in q) / sum(so[w] for w in q)
            assert abs(ratio - 1) < 0.05, (dc, k0, ratio)


def test_retail_partition_invariance(monkeypatch):
    """Different slice lengths give the same POs (determinism)."""
    results = []
    for step in (1.0, 13.0):
        world, ev = make_world()
        promos_mod = fake_promotions([kranz_promo(world)])
        monkeypatch.setitem(sys.modules, "cpgsim.domain.promotions", promos_mod)
        monkeypatch.setitem(sys.modules, "cpgsim.domain.demandplan", stub_demandplan(promos_mod))
        monkeypatch.setitem(sys.modules, "cpgsim.domain.warehouse", FakeWarehouse().module())
        monkeypatch.setitem(sys.modules, "cpgsim.domain.fulfilment", FakeFulfilment().module())
        monkeypatch.setitem(sys.modules, "cpgsim.domain.billing", FakeBilling().module())
        retail.bootstrap_retail(world)
        run(world, world.at_local(HIST + timedelta(weeks=6), 0), RETAIL_SOURCES, step_hours=step)
        results.append([
            (e.doc.sales_doc_id, e.doc.external_ref, e.at, [(i.sku_id, i.qty, i.net_value) for i in e.doc.items])
            for e in ev if isinstance(e, SalesDocCreated)
        ])
    assert results[0] == results[1]


def test_retail_order_scenario_factor(sim):
    world = sim.world
    world.scenarios = StubScenarios({"retail_order": 1.3})
    retail.bootstrap_retail(world)
    run(world, world.at_local(HIST + timedelta(days=7), 0), ("retail.po_cycle",))
    assert len(world.scenarios.recorded) == 5
    assert all(r[1][0] == "sales_doc" for r in world.scenarios.recorded)


# ---------------------------------------------------------------------------
# Value share (analytic, via demandplan's sizing)
# ---------------------------------------------------------------------------


def _demandplan_for_share(monkeypatch, world):
    try:
        dp = importlib.import_module("cpgsim.domain.demandplan")
        importlib.import_module("cpgsim.domain.promotions")
        dp.basket_stats(world)
        dp.retail_weekly_demand(world, "drogerie_kranz", "KRANZ-DC1",
                                world.ref.catalogue.retailer_assortment("drogerie_kranz", HIST)[0], HIST)
        return dp
    except Exception:
        promos_mod = fake_promotions([])
        monkeypatch.setitem(sys.modules, "cpgsim.domain.promotions", promos_mod)
        dp = stub_demandplan(promos_mod)
        monkeypatch.setitem(sys.modules, "cpgsim.domain.demandplan", dp)
        return dp


def _web_annual_value(world, dp, year_start: date) -> float:
    """Expected web net value over a year: daily order rate (with events) * E[basket net].

    The real demand plan sizes retail against the web value the simulation
    realises (``WEB_VALUE_REALISATION`` of ``annual_web_net_eur``), which is what
    the generated data shows; compare against that when it is available.
    """
    if hasattr(dp, "WEB_VALUE_REALISATION"):
        return dp.basket_stats(world)["annual_web_net_eur"] * dp.WEB_VALUE_REALISATION
    basket = dp.basket_stats(world)["basket_net_eur"]
    if hasattr(dp, "web_order_rate_per_day"):
        rate = lambda d: dp.web_order_rate_per_day(world, world.at_local(d, 12))  # noqa: E731
    else:  # stub: base * seasonal level of the active range * web calendar events
        from cpgsim.models.seasonality import event_factor
        cfg, cat = world.cfg, world.ref.catalogue

        def rate(d):
            act = cat.active(d)
            level = sum(s.popularity * category_factor(s.season_profile, d) for s in act) / sum(
                s.popularity for s in act)
            return cfg.channels.web.base_orders_per_day * level * event_factor(
                cfg.seasonality, d, "web", None, world.ref.calendar)
    return sum(rate(year_start + timedelta(days=i)) for i in range(364)) * basket


def test_retail_value_share(monkeypatch):
    world, _ = make_world()
    dp = _demandplan_for_share(monkeypatch, world)
    cfg = world.cfg
    cat = world.ref.catalogue
    web_value = _web_annual_value(world, dp, HIST)
    values = {}
    for channel, rc in cfg.channels.retailers().items():
        v = 0.0
        for k in range(52):
            ws = HIST + timedelta(weeks=k)
            ids = cat.retailer_assortment(channel, ws + timedelta(days=3))
            for acc in world.ref.accounts.dcs(channel):
                for sku_id in ids:
                    units = dp.retail_weekly_demand(world, channel, acc.account_id, sku_id, ws)
                    _, net, _ = retail.price_line(cat.get(sku_id).list_price_eur, 1, rc.trade_discount)
                    v += units * net
        values[channel] = v
    total = web_value + sum(values.values())
    share = {k: v / total for k, v in values.items()}
    share["web"] = web_value / total
    # enabled channels: 35 / 25 / 10 of the full business -> 50 / 35.7 / 14.3 % among them
    assert abs(share["drogerie_kranz"] - 0.35 / 0.70) < 0.04, share
    assert abs(share["mylo"] - 0.25 / 0.70) < 0.04, share
    assert abs(share["web"] - 0.10 / 0.70) < 0.03, share
    assert abs(values["drogerie_kranz"] / web_value - 3.5) < 0.35, values["drogerie_kranz"] / web_value
    assert abs(values["mylo"] / web_value - 2.5) < 0.25, values["mylo"] / web_value
