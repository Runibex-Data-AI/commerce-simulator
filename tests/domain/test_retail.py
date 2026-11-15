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
        self.paid: list[tuple[int, float | None, str | None]] = []

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

        def clear(world, billing_id, amount=None, status=None):
            self.cleared.append((billing_id, world.now))
            self.paid.append((billing_id, amount, status))

        mod.create_invoice = create_invoice
        mod.clear = clear
        return mod


class FakeFeeds:
    """Captures the sell-out handed to ``domain.feeds`` (weekly and daily)."""

    def __init__(self) -> None:
        self.weeks: list[tuple] = []  # (channel, dc, week_start, sold, lost, demand, on_hand)
        self.days: list[tuple] = []  # (channel, dc, day, sold, demand, on_hand, on_order)

    def module(self) -> types.ModuleType:
        mod = types.ModuleType("cpgsim.domain.feeds")

        def record_sellout_week(world, channel, dc, week_start, sold, lost, demand, on_hand):
            self.weeks.append((channel, dc, week_start, dict(sold), dict(lost), dict(demand), dict(on_hand)))

        def record_marketplace_day(world, channel, dc, day, sold, demand, on_hand, on_order):
            self.days.append((channel, dc, day, dict(sold), dict(demand), dict(on_hand), dict(on_order)))

        mod.record_sellout_week = record_sellout_week
        mod.record_marketplace_day = record_marketplace_day
        return mod


class FakeDeductions:
    """Captures the retail calls into ``domain.deductions``; takes ``take`` EUR per payment."""

    def __init__(self, take: float = 0.0) -> None:
        self.take = take
        self.sellout: list[tuple] = []
        self.pods: list[tuple] = []
        self.cuts: list[tuple] = []
        self.payments: list[tuple] = []

    def module(self) -> types.ModuleType:
        mod = types.ModuleType("cpgsim.domain.deductions")

        def record_sellout(world, channel, dc, first_day, days, sold):
            self.sellout.append((channel, dc, first_day, days, dict(sold)))

        def record_pod(world, po_id, channel, dc, delivery_id, lines):
            self.pods.append((po_id, channel, dc, delivery_id, list(lines)))

        def record_cut(world, po_id, channel, dc, ordered_units, cut_units, cut_value_net):
            self.cuts.append((po_id, channel, dc, ordered_units, cut_units, cut_value_net))

        def take_on_payment(world, channel, billing_id, gross):
            t = min(self.take, gross)
            self.payments.append((channel, billing_id, gross, t))
            return t

        mod.record_sellout = record_sellout
        mod.record_pod = record_pod
        mod.record_cut = record_cut
        mod.take_on_payment = take_on_payment
        return mod


def sellout_by_week(feeds: FakeFeeds) -> dict:
    """``{dc: {week_start iso: units sold}}`` from the captured feeds calls (days summed into weeks)."""
    out: dict = defaultdict(lambda: defaultdict(float))
    for _, dc, ws, sold, *_ in feeds.weeks:
        out[dc][ws.isoformat()] += sum(sold.values())
    for _, dc, day, sold, *_ in feeds.days:
        out[dc][retail.monday(day).isoformat()] += sum(sold.values())
    return out


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
    ful, bil, feeds, ded = FakeFulfilment(), FakeBilling(), FakeFeeds(), FakeDeductions()
    install_fakes(monkeypatch, promos_mod, wh, ful, bil, feeds, ded)
    return types.SimpleNamespace(world=world, events=collected, promo=promo, wh=wh, ful=ful,
                                 bil=bil, feeds=feeds, ded=ded, short_sku=short_sku)


def install_fakes(monkeypatch, promos_mod, wh, ful, bil, feeds, ded) -> None:
    monkeypatch.setitem(sys.modules, "cpgsim.domain.promotions", promos_mod)
    monkeypatch.setitem(sys.modules, "cpgsim.domain.demandplan", stub_demandplan(promos_mod))
    monkeypatch.setitem(sys.modules, "cpgsim.domain.warehouse", wh.module())
    monkeypatch.setitem(sys.modules, "cpgsim.domain.fulfilment", ful.module())
    monkeypatch.setitem(sys.modules, "cpgsim.domain.billing", bil.module())
    monkeypatch.setitem(sys.modules, "cpgsim.domain.feeds", feeds.module())
    monkeypatch.setitem(sys.modules, "cpgsim.domain.deductions", ded.module())


RETAIL_SOURCES = ("retail.sellout", "retail.po_cycle")
WEEKLY_DCS = ("KRANZ-DC1", "KRANZ-DC2", "KRANZ-DC3", "MYLO-DC1", "MYLO-DC2", "BELLE-DC1", "BELLE-DC2")
DAILY_DCS = ("SHOPNOVA-DC1", "SHOPNOVA-DC2")
CHANNEL_OF_PAYER = {"KRANZ": "drogerie_kranz", "MYLO": "mylo", "SHOPNOVA": "shopnova", "BELLE": "belle"}


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
        # unmapped articles (chaos defect, about 5 %) have no customer-material record
        from cpgsim import chaos
        unmapped = {s for s in assort if chaos.is_unmapped(world, channel, s)}
        assert assort - unmapped <= knmt[acc] and not unmapped & knmt[acc]
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
    assert retail.po_number("shopnova", 17, date(2025, 3, 4)) == "SNV-PO-0000017"
    assert retail.po_number("belle", 17, date(2025, 3, 4)) == "BEL/2025/00017"
    conds, net, tax = retail.price_line(3.10, 24, 0.36, 0.10)
    assert [c.ctype for c in conds] == ["list_price", "customer_discount", "promo_discount", "tax"]
    assert conds[0].amount == 74.4 and conds[1].amount == -26.78
    assert conds[2].amount == round(-(74.4 - 26.78) * 0.10, 2)
    assert net == round(74.4 - 26.78 + conds[2].amount, 2)
    assert tax == round(net * 0.19, 2)
    world, _ = make_world()
    plan = retail.dc_plan(world)
    assert [p[1] for p in plan] == ["KRANZ-DC1", "KRANZ-DC2", "KRANZ-DC3", "MYLO-DC1", "MYLO-DC2",
                                    "SHOPNOVA-DC1", "SHOPNOVA-DC2", "BELLE-DC1", "BELLE-DC2"]
    retailers = world.cfg.channels.retailers()
    week = [HIST + timedelta(days=k) for k in range(14)]
    days = {dc: [d.weekday() for d in week if retail.is_order_day(retailers[ch], i, d)] for ch, dc, i in plan}
    # weekly cycles on weekday index % 5 (phase-1 DCs unchanged), Shopnova on Mon, Wed, Fri
    assert [days[dc][0] for dc in WEEKLY_DCS] == [1, 2, 3, 4, 0, 3, 4]
    assert all(len(days[dc]) == 2 for dc in WEEKLY_DCS)
    assert days["SHOPNOVA-DC1"] == days["SHOPNOVA-DC2"] == [0, 2, 4, 0, 2, 4]
    assert retail.is_order_day(7, 1, HIST + timedelta(days=1))  # phase-1 call form still works
    assert retail._cycle_days(retailers["shopnova"]) == pytest.approx(7 / 3)
    assert retail._cycle_days(retailers["belle"]) == 7.0


def test_retail_bootstrap(sim):
    world = sim.world
    retail.bootstrap_retail(world)
    retailers = world.cfg.channels.retailers()
    for channel, dc, _ in retail.dc_plan(world):
        inv = world.store.get("retailer_inventory", dc)
        assort = world.ref.catalogue.retailer_assortment(channel, HIST)
        assert sorted(inv.forecast) == assort
        mid = HIST + timedelta(days=3)
        # one seasonal order cycle (Shopnova: 7/3 days) plus a flat safety week; no calendar event in March
        cycle = (7 / 3 if channel == "shopnova" else 7.0) / 7
        assert retail._cycle_weeks(retailers[channel]) == pytest.approx(cycle)
        for s in assort:
            season = max(category_factor(world.ref.catalogue.get(s).season_profile, mid), 0.05)
            assert inv.on_hand[s] == round(inv.forecast[s] * (season * cycle + 1.0))
        assert sum(inv.on_hand.values()) > 0


@pytest.fixture
def quarter(sim):
    """Run 26 weeks of retail; collect weekly sell-out per DC and POD receipts."""
    world = sim.world
    retail.bootstrap_retail(world)
    receipts = defaultdict(lambda: defaultdict(float))  # dc -> week (monday) -> units
    pods: list = []
    payments: list = []
    inventory_start = {dc: sum(world.store.get("retailer_inventory", dc).on_hand.values())
                       for _, dc, _ in retail.dc_plan(world)}

    def on_event(w, ev):
        if ev.kind == "retail.pod":
            d = w.store.get("delivery", ev.data["delivery_id"])
            po = w.store.get("retail_po", d.order_id)
            wk = retail.monday(w.local_date()).isoformat()
            receipts[po.dc_account][wk] += sum(l.qty for l in d.lines)
            pods.append((po.po_id, w.now))
        elif ev.kind == "retail.payment":
            payments.append((ev.data["po_id"], ev.data["billing_id"], w.now))

    run(world, world.at_local(HIST + timedelta(weeks=26), 0), RETAIL_SOURCES, on_event=on_event)
    lost = defaultdict(float)  # sku -> units
    for _, _, _, _, lost_m, _, _ in sim.feeds.weeks:
        for s, q in lost_m.items():
            lost[s] += q
    for _, _, _, sold, demand, _, _ in sim.feeds.days:
        for s, q in demand.items():
            lost[s] += q - sold.get(s, 0)
    return types.SimpleNamespace(sim=sim, sellout=sellout_by_week(sim.feeds), lost=lost, receipts=receipts,
                                 pods=pods, payments=payments, inventory_start=inventory_start)


def test_po_lifecycle(quarter):
    sim = quarter.sim
    world = sim.world
    created = [e for e in sim.events if isinstance(e, SalesDocCreated)]
    per_dc = defaultdict(int)
    for e in created:
        per_dc[e.doc.ship_to] += 1
    assert all(per_dc[dc] == 26 for dc in WEEKLY_DCS)  # one PO per DC and week
    assert all(per_dc[dc] == 3 * 26 for dc in DAILY_DCS)  # Mon, Wed, Fri
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
        assert doc.external_ref == retail.po_number(doc.channel, doc.order_id, loc.date())
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
        assert 0 <= (doc.billing_date - world.local_date(pod_at[po_id])).days <= 5  # Easter: Fri + Mon off
        terms = world.cfg.channels.retailers()[CHANNEL_OF_PAYER[doc.payer]].payment_terms_days
        assert doc.due_date == doc.billing_date + timedelta(days=terms)
        by_billing[doc.billing_id] = doc
    assert sim.bil.cleared, "early POs are paid within the half year"
    for bid, at in sim.bil.cleared:
        doc = by_billing[bid]
        d = world.local_date(at)
        assert cal.is_working_day(d, "DE", "DE-HE")
        assert -6 <= (d - doc.due_date).days <= 6 + 4
        terms = world.cfg.channels.retailers()[CHANNEL_OF_PAYER[doc.payer]].payment_terms_days
        assert terms - 6 <= (d - doc.billing_date).days <= terms + 10
        # paid POs are removed together with their sales doc and invoice
        assert not world.store.has("billing", bid)
        assert not world.store.has("sales_doc", doc.sales_doc_id)
    # every open PO has a plausible status, older ones are further along
    statuses = {po.status for _, po in world.store.items("retail_po")}
    assert statuses <= {"received", "confirmed", "delivery_planned", "shipped", "delivered", "invoiced"}
    assert world.store.count("retail_po") < 7 * 12 + 2 * 3 * 7
    # handoffs: one POD and one cut record per PO; payments clear the invoice at gross minus deductions
    assert len(sim.ded.pods) == len(pod_at) and {p[0] for p in sim.ded.pods} == set(pod_at)
    assert len(sim.ded.cuts) == len(created)
    gross = {doc.billing_id: round(doc.net_value + doc.tax, 2) for doc, _ in sim.bil.invoices}
    assert len(sim.ded.payments) == len(sim.bil.paid) == len(sim.bil.cleared)
    for (channel, bid, g, taken), (bid2, paid, status) in zip(sim.ded.payments, sim.bil.paid):
        assert bid == bid2 and g == gross[bid] and paid == round(g - taken, 2) and status == "cleared"
        assert channel == CHANNEL_OF_PAYER[by_billing[bid].payer]
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
        feeds = FakeFeeds()
        install_fakes(monkeypatch, promos_mod, FakeWarehouse(), FakeFulfilment(), FakeBilling(), feeds,
                      FakeDeductions())
        retail.bootstrap_retail(world)
        run(world, world.at_local(HIST + timedelta(weeks=6), 0), RETAIL_SOURCES, step_hours=step)
        results.append(([
            (e.doc.sales_doc_id, e.doc.external_ref, e.at, [(i.sku_id, i.qty, i.net_value) for i in e.doc.items])
            for e in ev if isinstance(e, SalesDocCreated)
        ], feeds.weeks, feeds.days))
    assert results[0] == results[1]
    assert results[0][2], "daily marketplace sell-out is covered"


def test_retail_order_scenario_factor(sim):
    world = sim.world
    world.scenarios = StubScenarios({"retail_order": 1.3})
    retail.bootstrap_retail(world)
    run(world, world.at_local(HIST + timedelta(days=7), 0), ("retail.po_cycle",))
    assert len(world.scenarios.recorded) == 7 + 2 * 3  # weekly DCs once, Shopnova FCs three times
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
    # all five channels: 35 / 25 / 20 / 10 / 10 of the business
    for ch, target in (("drogerie_kranz", 0.35), ("mylo", 0.25), ("shopnova", 0.20), ("belle", 0.10),
                       ("web", 0.10)):
        assert abs(share[ch] - target) < 0.02, share
        if ch != "web":
            assert abs(values[ch] / web_value - target / 0.10) < 0.1 * target / 0.10, (ch, values[ch] / web_value)


# ---------------------------------------------------------------------------
# Phase 2: daily sell-out, event-aware targets, push mode (design 2.2, 2.4, 5.7)
# ---------------------------------------------------------------------------


def _fire(world, kind: str, at: datetime, data: dict) -> None:
    from cpgsim.engine.queue import Event

    world.now = at
    from cpgsim.engine.registry import HANDLERS as _H

    _H[kind](world, Event(due=at, kind=kind, key=f"{kind}:test", data=data))


def test_daily_sellout_adds_up_to_the_weekly_draw(sim):
    world = sim.world
    retail.bootstrap_retail(world)
    dc = "SHOPNOVA-DC1"
    inv = world.store.mut("retailer_inventory", dc)
    for s in inv.on_hand:
        inv.on_hand[s] = 10**6  # nothing is censored
    ws = HIST + timedelta(weeks=1)  # a Monday in March: no calendar event
    dp = sys.modules["cpgsim.domain.demandplan"]
    g = retail._week_noise(world, dc, ws)
    # g: mean-1 log-normal per SKU, spread demand_noise_cv
    vals = list(g.values())
    m = sum(vals) / len(vals)
    sd = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
    assert m == pytest.approx(1.0, abs=0.03)
    assert sd == pytest.approx(world.cfg.retail.demand_noise_cv, rel=0.15)

    sku0 = sorted(inv.on_hand)[0]
    fc0 = inv.forecast[sku0]
    for k in range(7):
        day = ws + timedelta(days=k)
        _fire(world, "retail.sellout", world.at_local(day + timedelta(days=1), 0, 30),
              {"channel": "shopnova", "dc": dc, "week_start": ws.isoformat(), "day": day.isoformat(), "days": 1})
        if k == 0:
            # forecast: daily EWMA of sold / (level factor x dow share), alpha_day = 1-(1-a)^(1/7)
            a = 1 - (1 - world.cfg.retail.forecast_alpha) ** (1 / 7)
            pf = retail._level_factor(world, "shopnova", sku0, ws) * retail._dow_share(world, day)
            sold = sim.feeds.days[-1][3].get(sku0, 0)
            assert inv.forecast[sku0] == pytest.approx(round(a * sold / pf + (1 - a) * fc0, 3))
    assert [c[2] for c in sim.feeds.days] == [ws + timedelta(days=k) for k in range(7)]
    want = defaultdict(int)
    for _, _, _, sold, demand, on_hand, on_order in sim.feeds.days:
        assert sold == demand  # uncensored
        assert sorted(on_hand) == sorted(on_order) == sorted(retail._listed(world, "shopnova", ws))
        for s, q in demand.items():
            want[s] += q
    assert not sim.feeds.weeks
    assert [(c[1], c[2], c[3]) for c in sim.ded.sellout][:2] == [(dc, ws, 1), (dc, ws + timedelta(days=1), 1)]
    listed = sorted(retail._listed(world, "shopnova", ws))
    for s in listed:
        mean = dp.retail_weekly_demand(world, "shopnova", dc, s, ws) * g[s]
        assert abs(want[s] - mean) <= 7  # seven stochastic roundings
    total = sum(dp.retail_weekly_demand(world, "shopnova", dc, s, ws) * g[s] for s in listed)
    assert sum(want[s] for s in listed) == pytest.approx(total, rel=0.02)


def test_window_demand_is_event_aware_and_tapers_with_the_listing(sim):
    world = sim.world
    cat = world.ref.catalogue
    from cpgsim.models.seasonality import marketplace_sale_week

    lo, hi = marketplace_sale_week(2025)
    sku = cat.get(sorted(retail._listed(world, "shopnova", lo))[0])
    before = retail._coverage_days(world, lo - timedelta(days=7), 7.0)
    into = retail._coverage_days(world, lo - timedelta(days=3), 7.0)
    w_before = retail._window_demand(world, "shopnova", sku, 70.0, before)
    w_into = retail._window_demand(world, "shopnova", sku, 70.0, into)
    lift = world.cfg.seasonality.marketplace_sale_lift
    season = [retail._season(world, sku.sku_id, d) for d, _ in into]
    expected = 70.0 / 7 * sum(s * (lift if d >= lo else 1.0) for s, (d, _) in zip(season, into))
    assert w_into == pytest.approx(expected)
    assert w_into > 2 * w_before
    # the retailers do not have the marketplace sale
    kranz_sku = cat.get(sorted(retail._listed(world, "drogerie_kranz", lo))[0])
    kr = retail._window_demand(world, "drogerie_kranz", kranz_sku, 70.0, into)
    assert kr == pytest.approx(70.0 / 7 * sum(retail._season(world, kranz_sku.sku_id, d) for d, _ in into))
    # fractional last day
    assert retail._coverage_days(world, lo, 7 / 3) == [(lo, 1.0), (lo + timedelta(days=1), 1.0),
                                                      (lo + timedelta(days=2), pytest.approx(1 / 3))]

    # a SKU whose listing ends: no demand (and no safety stock) on the days after its last listed day
    hit = None
    for ch in ("drogerie_kranz", "mylo", "belle", "shopnova"):
        for k in range(0, 600, 7):
            d = HIST + timedelta(days=k)
            gone = retail._listed(world, ch, d) - retail._listed(world, ch, d + timedelta(days=7))
            if gone:
                hit = (ch, d, min(gone))
                break
        if hit:
            break
    assert hit, "some SKU leaves a retailer range within the horizon"
    ch, d, sid = hit
    last = max(x for x in (d + timedelta(days=j) for j in range(8)) if sid in retail._listed(world, ch, x))
    after = retail._coverage_days(world, last + timedelta(days=1), 14.0)
    assert retail._window_demand(world, ch, cat.get(sid), 50.0, after) == 0.0
    assert retail._safety_stock(world, ch, sid, 50.0, after) == 0.0
    span = retail._coverage_days(world, last - timedelta(days=2), 7.0)
    assert 0 < retail._window_demand(world, ch, cat.get(sid), 50.0, span) < retail._window_demand(
        world, ch, cat.get(sid), 50.0, retail._coverage_days(world, last - timedelta(days=9), 7.0)) + 1e-9
    assert retail._safety_stock(world, ch, sid, 49.0, span) == pytest.approx(49.0 * 3 / 7)


def _mylo_order_day(world, start: date) -> date:
    rc = world.cfg.channels.mylo
    idx = next(i for c, dc, i in retail.dc_plan(world) if dc == "MYLO-DC1")
    return next(start + timedelta(days=k) for k in range(8) if retail.is_order_day(rc, idx, start + timedelta(days=k)))


def test_push_mode_backlog_and_recording(sim):
    world = sim.world
    world.scenarios = StubScenarios({"retail_push": 1.3})
    retail.bootstrap_retail(world)
    cat = world.ref.catalogue
    mylo = cat.retailer_assortment("mylo", HIST)
    short = sorted(mylo, key=lambda i: (-cat.get(i).popularity, i))[0]
    sim.wh.short.clear()
    sim.wh.short[short] = 0  # the warehouse cannot deliver it at all
    d = _mylo_order_day(world, HIST)
    _fire(world, "retail.po_cycle", world.at_local(d, 6), {"channel": "mylo", "dc": "MYLO-DC1"})
    (po_id,) = [p for p, _ in world.store.items("retail_po")]
    po = world.store.get("retail_po", po_id)
    doc = world.store.get("sales_doc", po.sales_doc_id)
    assert world.scenarios.recorded == [("retail_push", ("sales_doc", po.sales_doc_id),
                                         {"channel": "mylo", "dc": "MYLO-DC1"})]
    q_short = next(l.qty for l in po.lines if l.sku_id == short)
    _fire(world, "retail.confirm", world.at_local(d, 11), {"po_id": po.po_id})
    inv = world.store.get("retailer_inventory", "MYLO-DC1")
    assert inv.push_backlog == {short: float(q_short)}
    assert inv.on_order[short] == 0
    # the cut handed to deductions: ordered and cut units, net value of the cut share
    (cut,) = [c for c in sim.ded.cuts if c[0] == po.po_id]
    item = next(i for i in doc.items if i.sku_id == short)
    assert cut[1:5] == ("mylo", "MYLO-DC1", sum(l.qty for l in po.lines), q_short)
    assert cut[5] == pytest.approx(item.net_value, abs=0.01)

    # the backlog rides on the next PO (on top of cycle demand x push), then is gone
    d2 = _mylo_order_day(world, d + timedelta(days=1))
    world_b, _ = make_world(scenarios=StubScenarios({"retail_push": 1.3}))
    retail.bootstrap_retail(world_b)
    inv_b = world_b.store.mut("retailer_inventory", "MYLO-DC1")
    inv_b.on_hand = dict(inv.on_hand)
    inv_b.on_order = dict(inv.on_order)
    inv_b.forecast = dict(inv.forecast)
    _fire(world, "retail.po_cycle", world.at_local(d2, 6), {"channel": "mylo", "dc": "MYLO-DC1"})
    _fire(world_b, "retail.po_cycle", world_b.at_local(d2, 6), {"channel": "mylo", "dc": "MYLO-DC1"})
    new = [x for _, x in world.store.items("retail_po") if x.po_id != po.po_id]
    (po_b,) = [x for _, x in world_b.store.items("retail_po")]
    qa = {l.sku_id: l.qty for l in new[0].lines}
    qb = {l.sku_id: l.qty for l in po_b.lines}
    assert qa[short] > qb.get(short, 0) and qa[short] - qb.get(short, 0) >= 0.5 * q_short
    assert {s: q for s, q in qa.items() if s != short} == {s: q for s, q in qb.items() if s != short}
    assert short not in world.store.get("retailer_inventory", "MYLO-DC1").push_backlog

    # push ends: an open backlog is dropped and orders follow the target again
    inv = world.store.mut("retailer_inventory", "MYLO-DC1")
    inv.push_backlog[short] = 99.0
    world.scenarios.factors = {}
    d3 = _mylo_order_day(world, d2 + timedelta(days=1))
    _fire(world, "retail.po_cycle", world.at_local(d3, 6), {"channel": "mylo", "dc": "MYLO-DC1"})
    assert world.store.get("retailer_inventory", "MYLO-DC1").push_backlog == {}
