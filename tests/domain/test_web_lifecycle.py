"""Unit tests for the web domain: customers, web orders, returns, subscriptions.

The other domain modules (demandplan, promotions, masterdata, warehouse,
fulfilment, billing) and the scenario manager are replaced by small stubs
installed through ``sys.modules`` for each test, so these tests exercise the
web lifecycle on its own. Emitted events are also routed through the real
writers, which catches vocabulary and payload mistakes.
"""

from __future__ import annotations

import sys
import types
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from cpgsim.config import Config, load_config
from cpgsim.domain import billing as real_billing
from cpgsim.domain import customers, subscriptions, web
from cpgsim.domain.events import (
    BillingDocCreated,
    CustomerRegistered,
    DeliveryChanged,
    DeliveryCreated,
    PaymentRecorded,
    PaymentStatusChanged,
    RefundChanged,
    RefundIssued,
    ReturnChanged,
    ReturnCreated,
    SalesDocChanged,
    SalesDocCreated,
    SubscriptionChanged,
    SubscriptionCreated,
    WebOrderLinesChanged,
    WebOrderMilestoneReached,
    WebOrderPlaced,
    WebOrderStatusChanged,
)
from cpgsim.domain.model import (
    ENTITY_KINDS,
    BillingDoc,
    BillingItem,
    Counters,
    Delivery,
    DeliveryLine,
    Promotion,
)
from cpgsim.domain.reference import build_reference
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import EventQueue
from cpgsim.engine.registry import HANDLERS
from cpgsim.engine.world import World
from cpgsim.models.pricing import split_gross
from cpgsim.writers import build_writers
from cpgsim.writers.base import Dispatcher, WriterContext

UTC = timezone.utc
START = datetime(2025, 3, 2, 23, 0, tzinfo=UTC)  # 2025-03-03 00:00 Berlin (Monday)

_REF: dict = {}


def ref_for(cfg):
    if "ref" not in _REF:
        _REF["ref"] = build_reference(cfg)
    return _REF["ref"]


# ---------------------------------------------------------------------------
# Stubs of the other domain modules
# ---------------------------------------------------------------------------


class Stubs:
    def __init__(self, h: "Harness") -> None:
        self.h = h
        self.stock: dict[int, int] = {}
        self.reserved: dict[int, int] = {}
        self.default_stock = 10**9
        self.received: list[tuple] = []
        self.cleared: list[tuple] = []
        self.effects: list[tuple] = []
        self.promo_fn = lambda world, t, sku_ids, rng: None
        self.outcome_fn = lambda d: "delivered"
        self.weights_cache: dict = {}
        self.lift_fn = lambda d: 1.0
        self.windows: list = []

    # demandplan
    def sku_weights(self, world, channel, d):
        hit = self.weights_cache.get(d)
        if hit is None:
            ids, cum, acc = [], [], 0.0
            for s in world.ref.catalogue.active(d):
                acc += s.popularity
                ids.append(s.sku_id)
                cum.append(acc)
            hit = (ids, cum)
            self.weights_cache[d] = hit
        return hit

    def web_order_rate_per_day(self, world, t):
        return world.cfg.channels.web.base_orders_per_day

    def web_event_factor(self, world, d):
        return 1.0

    def repeat_lift(self, world, sku_ids, d):
        return self.lift_fn(d)

    def demand_windows(self, world, sku_ids, d0, d1, known_at=None):
        return [w for w in self.windows if d0 <= w.first <= d1]

    # promotions
    def web_promo_for_basket(self, world, t, sku_ids, rng, country=None):
        return self.promo_fn(world, t, sku_ids, rng)

    def active_web_promos(self, world, t):
        return list(getattr(self, "active_promos", []))

    @staticmethod
    def is_eligible(promo, sku):  # as promotions.is_eligible: SKU scope, else brand scope, else sitewide
        if promo.sku_ids:
            return sku.sku_id in promo.sku_ids
        if promo.brand:
            return sku.brand == promo.brand
        return True

    # masterdata
    @staticmethod
    def fx_rate(world, currency, d):
        return 1.0 if currency == "EUR" else 1.05

    @staticmethod
    def std_cost(world, sku_id, d):
        return world.ref.catalogue.get(sku_id).std_cost_eur

    # warehouse
    def available(self, world, sku_id):
        return self.stock.get(sku_id, self.default_stock) - self.reserved.get(sku_id, 0)

    def reserve(self, world, sku_id, qty):
        g = max(0, min(qty, self.available(world, sku_id)))
        self.reserved[sku_id] = self.reserved.get(sku_id, 0) + g
        return g

    def unreserve(self, world, sku_id, qty):
        self.reserved[sku_id] = self.reserved.get(sku_id, 0) - qty

    def issue(self, world, sku_id, qty, delivery_id):
        self.reserved[sku_id] = self.reserved.get(sku_id, 0) - qty
        self.stock[sku_id] = self.stock.get(sku_id, self.default_stock) - qty

    def receive_return(self, world, sku_id, qty, return_id, resaleable, delivery_id=None):
        self.received.append((sku_id, qty, return_id, resaleable))

    # fulfilment
    def create_delivery(self, world, kind, sales_doc, lines, ship_to_account, ship_to, carrier,
                        planned_ship_date, order_id, seq_in_order=1):
        did = world.next_id("delivery")
        d = Delivery(
            delivery_id=did, kind=kind, sales_doc_id=sales_doc.sales_doc_id, ship_to_account=ship_to_account,
            ship_to=ship_to, carrier=carrier or "Paketblitz",
            lines=[DeliveryLine(i, s, q, item) for i, (s, q, item) in enumerate(lines, 1)],
            created_at=world.now, planned_ship_date=planned_ship_date, order_id=order_id,
            seq_in_order=seq_in_order,
        )
        world.store.put("delivery", did, d)
        world.emit(DeliveryCreated(at=world.now, delivery=d, sales_doc=sales_doc))
        t = max(world.at_local(planned_ship_date, 17), world.now + timedelta(hours=1))
        world.schedule("stub.ship", t, f"stub.ship:{did}", {"delivery_id": did})
        return d

    def cancel_delivery(self, world, delivery_id, reason):
        d = world.store.mut("delivery", delivery_id)
        if d.status != "open":
            return False
        d.status = "cancelled"
        for dl in d.lines:
            self.unreserve(world, dl.sku_id, dl.qty)
        world.emit(DeliveryChanged(at=world.now, delivery_id=delivery_id, status="cancelled",
                                   fields={"kind": "web", "line_nos": [dl.line_no for dl in d.lines]}))
        return True

    def on_ship(self, world, ev):
        did = ev.data["delivery_id"]
        d = world.store.mut("delivery", did)
        if d.status != "open":
            return
        d.status = "shipped"
        d.shipped_at = world.now
        d.tracking_no = f"PB{did:010d}"
        for dl in d.lines:
            self.issue(world, dl.sku_id, dl.qty, did)
        sdoc = world.store.mut("sales_doc", d.sales_doc_id)
        by_no = {it.item_no: it for it in sdoc.items}
        for dl in d.lines:
            by_no[dl.sales_item_no].delivered_qty += dl.qty
        world.emit(DeliveryChanged(at=world.now, delivery_id=did, status="shipped",
                                   fields={"kind": "web", "tracking_no": d.tracking_no, "freight_cost_eur": 4.1,
                                           "shipped_at": world.now}))
        sd = world.store.get("sales_doc", d.sales_doc_id)
        inv = self.create_invoice(world, sd, d, None, world.local_date(), world.local_date(),
                                  include_shipping=d.seq_in_order == 1)
        d.billing_id = inv.billing_id
        world.schedule("web.shipped", world.now, f"web.shipped:{did}", {"delivery_id": did})
        world.schedule("stub.deliver", world.now + timedelta(days=2), f"stub.deliver:{did}", {"delivery_id": did})

    def on_deliver(self, world, ev):
        did = ev.data["delivery_id"]
        d = world.store.mut("delivery", did)
        outcome = self.outcome_fn(d)
        if outcome == "returned_to_sender":
            world.schedule("web.delivery_exception", world.now, f"web.delivery_exception:{did}",
                           {"delivery_id": did})
        d.outcome = outcome
        d.status = outcome
        if outcome == "delivered":
            d.delivered_at = world.now
        world.schedule("web.delivery_outcome", world.now + timedelta(minutes=1),
                       f"web.delivery_outcome:{did}", {"delivery_id": did, "outcome": outcome})

    # billing
    def create_invoice(self, world, sales_doc, delivery, items, billing_date, due_date, include_shipping):
        by_item = {it.item_no: it for it in sales_doc.items}
        out = []
        for dl in delivery.lines:
            it = by_item[dl.sales_item_no]
            it.billed_qty += dl.qty
            out.append(BillingItem(
                item_no=10 * (len(out) + 1), sku_id=dl.sku_id, qty=dl.qty,
                net_value=round(it.net_value / it.qty * dl.qty, 2), tax=round(it.tax / it.qty * dl.qty, 2),
                cost=round(it.cost / it.qty * dl.qty, 2), sales_item_no=dl.sales_item_no,
                delivery_id=delivery.delivery_id, delivery_line_no=dl.line_no,
            ))
        fi = by_item.get(900)
        if include_shipping and fi is not None and not fi.billed_qty:
            fi.billed_qty = 1
            out.append(BillingItem(item_no=900, sku_id=0, qty=1, net_value=fi.net_value,
                                   tax=fi.tax, cost=0.0, sales_item_no=900,
                                   delivery_id=delivery.delivery_id))
        doc = BillingDoc(
            billing_id=world.next_id("billing"), doc_kind="invoice", sales_doc_id=sales_doc.sales_doc_id,
            payer=sales_doc.sold_to, currency=sales_doc.currency, fx_rate_eur=sales_doc.fx_rate_eur,
            billing_date=billing_date, created_at=world.now, due_date=due_date, items=out,
        )
        world.store.put("billing", doc.billing_id, doc)
        world.emit(BillingDocCreated(at=world.now, doc=doc))
        return doc

    def create_credit_memo(self, world, invoice_id, items, reason, include_shipping=False):
        inv = world.store.get("billing", invoice_id)
        by_si = {it.sales_item_no: it for it in inv.items if it.item_no != 900}
        out = []
        for si, q in items:
            ref = by_si[si]
            out.append(BillingItem(
                item_no=10 * (len(out) + 1), sku_id=ref.sku_id, qty=q,
                net_value=round(ref.net_value / ref.qty * q, 2), tax=round(ref.tax / ref.qty * q, 2),
                cost=round(ref.cost / ref.qty * q, 2), sales_item_no=si, reference_item_no=ref.item_no,
            ))
        if include_shipping:
            for it in inv.items:
                if it.item_no == 900:
                    out.append(BillingItem(item_no=900, sku_id=0, qty=1, net_value=it.net_value, tax=it.tax,
                                           cost=0.0, sales_item_no=it.sales_item_no, reference_item_no=900))
        cm = BillingDoc(
            billing_id=world.next_id("billing"), doc_kind="credit_memo", sales_doc_id=inv.sales_doc_id,
            payer=inv.payer, currency=inv.currency, fx_rate_eur=inv.fx_rate_eur,
            billing_date=world.local_date(), created_at=world.now, due_date=world.local_date(), items=out,
            reference_billing_id=invoice_id, reason=reason,
        )
        world.store.put("billing", cm.billing_id, cm)
        world.emit(BillingDocCreated(at=world.now, doc=cm))
        return cm

    def clear(self, world, billing_id, amount=None):
        self.cleared.append((billing_id, amount))

    refresh_header = staticmethod(real_billing.refresh_header)

    # scenarios
    def record_effect(self, world, scenario_id, window_id, entity_type, entity_id, effect, magnitude, detail=None):
        self.effects.append((scenario_id, window_id, entity_type, entity_id, effect, magnitude, detail))


class ScenStub:
    version = 0

    def __init__(self, stubs: Stubs) -> None:
        self.stubs = stubs
        self.factors: dict[str, float] = {}

    def factor(self, name, t, world=None, record=None, **ctx):
        return self.factors.get(name, 1.0)

    def active(self, scenario_id, t):
        return []

    def record_effect(self, *a, **k):
        self.stubs.record_effect(*a, **k)

    def scenario_promotions(self):
        return [{"window_id": "promo_margin_trap:config:0", "scenario_id": "promo_margin_trap", "index": 0}]


def _module(name: str, obj, attrs: list[str]) -> types.ModuleType:
    m = types.ModuleType(name)
    for a in attrs:
        setattr(m, a, getattr(obj, a))
    return m


class Harness:
    def __init__(self, monkeypatch, with_writers: bool = True, **overrides) -> None:
        base = {"scenarios": [], "channels": {"web": {"initial_customers": 0}}}
        cfg = load_config(**_merge(base, overrides))
        self.cfg = cfg
        self.events: list = []
        store, queue = EntityStore(), EventQueue()
        self.dispatcher = None
        self.world = World(cfg, store, queue, emit=self._emit)
        for kind, cls in ENTITY_KINDS.items():
            store.register(kind, cls)
        self.world.ref = ref_for(cfg)
        if with_writers:
            ctx = WriterContext(seed=self.world.seed, cfg=cfg, ref=self.world.ref, tz=self.world.tz)
            self.dispatcher = Dispatcher(build_writers(ctx))
        self.s = Stubs(self)
        self.world.scenarios = ScenStub(self.s)
        store.put("counters", "all", Counters())
        self.world.now = START
        self.world.history_start = START
        self.stub_handlers = {"stub.ship": self.s.on_ship, "stub.deliver": self.s.on_deliver}
        mods = {
            "demandplan": ["sku_weights", "web_order_rate_per_day", "repeat_lift", "demand_windows",
                           "web_event_factor"],
            "promotions": ["web_promo_for_basket", "is_eligible", "active_web_promos"],
            "masterdata": ["fx_rate", "std_cost"],
            "warehouse": ["available", "reserve", "unreserve", "issue", "receive_return"],
            "fulfilment": ["create_delivery", "cancel_delivery"],
            "billing": ["create_invoice", "create_credit_memo", "clear", "refresh_header"],
        }
        for name, attrs in mods.items():
            monkeypatch.setitem(sys.modules, f"cpgsim.domain.{name}", _module(f"cpgsim.domain.{name}", self.s, attrs))

    def _emit(self, e) -> None:
        self.events.append(e)
        if self.dispatcher is not None:
            self.dispatcher.dispatch(e)
            if len(self.dispatcher.batch) > 50_000:
                self.dispatcher.batch.clear()

    def run_until(self, end: datetime) -> int:
        w, q, n = self.world, self.world.queue, 0
        while True:
            due = q.peek_due()
            if due is None or due > end:
                break
            ev = q.pop()
            w.now = ev.due
            fn = self.stub_handlers.get(ev.kind) or HANDLERS[ev.kind]
            fn(w, ev)
            n += 1
        w.now = max(w.now, end)
        return n

    def run_with_arrivals(self, end: datetime, step: timedelta = timedelta(hours=1)) -> None:
        while self.world.now < end:
            nxt = min(self.world.now + step, end)
            for ev in web.new_customer_arrivals(self.world, self.world.now, nxt):
                self.world.queue.push(ev)
            self.run_until(nxt)

    def of(self, cls):
        return [e for e in self.events if isinstance(e, cls)]

    def customer(self, **kw):
        rng = self.world.rng("test.customer", self.world.store.count("customer"))
        return customers.new_customer(self.world, rng, **kw)

    def statuses(self, order_id):
        return [e.status for e in self.of(WebOrderStatusChanged) if e.order_id == order_id]


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return out


def _sku(h, brand_role="mass", max_price=None, min_price=None):
    cat = h.world.ref.catalogue
    brand = cat.brand_of_role(brand_role)
    d = h.world.local_date()
    for s in cat.active(d):
        if s.brand != brand:
            continue
        if max_price is not None and s.rrp_eur > max_price:
            continue
        if min_price is not None and s.rrp_eur < min_price:
            continue
        return s
    raise AssertionError("no sku")


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


def test_new_customer_registers_unique_email(monkeypatch):
    h = Harness(monkeypatch)
    cs = [h.customer() for _ in range(300)]
    regs = h.of(CustomerRegistered)
    assert len(regs) == 300
    emails = [r.email for r in regs]
    assert len(set(emails)) == 300
    for c, r in zip(cs, regs):
        assert h.world.store.get("customer", c.customer_id) is c
        assert r.customer.country == c.address.country
        assert r.locale.endswith(c.country)
        assert r.email.split("@")[1].endswith("example") or ".example" in r.email or "example." in r.email
        if c.acquisition == "influencer":
            assert c.influencer_code in h.cfg.customers.influencer_codes
    countries = Counter(c.country for c in cs)
    assert countries["DE"] > countries["AT"] > 0


def test_unique_email_rule():
    assert customers.unique_email("anna.berger@example.de", 3897) == "anna.berger.309@example.de"
    assert customers.unique_email("anna.berger85@example.de", 3897) == "anna.berger309@example.de"


def test_calibration_hits_target():
    cfg = load_config()
    cal = customers.repeat_calibration(cfg)
    assert 0 < cal["chain_share"] <= 1
    s = cfg.channels.web.new_customer_share
    assert cal["chain_share"] * cal["expected_chain_orders"] == pytest.approx((1 - s) / s, rel=1e-6)


# ---------------------------------------------------------------------------
# Arrivals
# ---------------------------------------------------------------------------


def test_arrivals_partition_invariant(monkeypatch):
    h = Harness(monkeypatch, with_writers=False)
    end = START + timedelta(days=2)
    whole = [e.key for e in web.new_customer_arrivals(h.world, START, end)]
    parts = []
    t = START
    while t < end:
        nxt = min(t + timedelta(minutes=37), end)
        parts += [e.key for e in web.new_customer_arrivals(h.world, t, nxt)]
        t = nxt
    assert whole == parts
    assert len(set(whole)) == len(whole)
    expected = 2 * h.cfg.channels.web.base_orders_per_day * h.cfg.channels.web.new_customer_share
    assert 0.75 * expected < len(whole) < 1.25 * expected


# ---------------------------------------------------------------------------
# Order lifecycle
# ---------------------------------------------------------------------------


def _happy_cfg():
    return {"payments": {"auth_rate": 1.0}, "fulfilment": {"cancel_before_pick": 0.0, "return_rate": 0.0}}


def test_happy_path_order_lifecycle_and_cleanup(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    c = h.customer(country="DE")
    sku = _sku(h, min_price=10.0)
    o = web.place_order(h.world, c, [(sku.sku_id, 3)], source="organic_search")
    assert o.total == o.goods_gross  # above the free-shipping threshold
    h.run_until(START + timedelta(days=10))
    assert h.statuses(o.order_id) == ["authorised", "released", "shipped", "delivered"]
    sd = h.of(SalesDocCreated)[0].doc
    item = sd.items[0]
    net, tax = split_gross(o.lines[0].gross, 0.19)
    assert (item.net_value, item.tax, item.item_no) == (net, tax, 10)
    assert [cond.ctype for cond in item.conditions] == ["list_price", "tax"]
    assert sd.sold_to == "WEB-DE" and sd.external_ref == str(o.order_id)
    caps = [e for e in h.of(PaymentStatusChanged) if e.status == "captured"]
    assert len(caps) == 1 and caps[0].amount == o.total
    assert len(h.s.cleared) == 1 and h.s.cleared[0][1] == o.total
    order = h.world.store.get("web_order", o.order_id)
    assert order.lines[0].shipped_qty == 3 and order.delivered_at is not None
    cust = h.world.store.get("customer", c.customer_id)
    assert cust.orders == 1
    # cleanup 30 days after delivery removes order, sales doc, delivery and invoice
    h.run_until(START + timedelta(days=45))
    assert h.world.store.get("web_order", o.order_id) is None
    assert h.world.store.count("sales_doc") == 0
    assert h.world.store.count("delivery") == 0
    assert h.world.store.count("billing") == 0


def test_shipping_fee_and_threshold_per_country(monkeypatch):
    """Austria pays its own fee and threshold (EUR); Germany and Switzerland their currency's."""
    h = Harness(monkeypatch, **_happy_cfg())
    w = h.cfg.channels.web
    assert (w.shipping_fee("DE"), w.free_shipping_threshold("DE")) == (w.shipping_fee_eur, w.free_shipping_eur)
    assert (w.shipping_fee("CH"), w.free_shipping_threshold("CH")) == (w.shipping_fee_chf, w.free_shipping_chf)
    assert (w.shipping_fee("AT"), w.free_shipping_threshold("AT")) == (
        w.shipping_fee_by_country["AT"], w.free_shipping_by_country["AT"])
    assert w.shipping_fee("AT") > w.shipping_fee_eur and w.free_shipping_threshold("AT") > w.free_shipping_eur
    # a basket between the German and the Austrian threshold ships free to Germany, not to Austria
    lines, _ = _basket_between(h, w.free_shipping_eur, w.free_shipping_threshold("AT"), "EUR")
    de = web.place_order(h.world, h.customer(country="DE", acquisition="direct"), lines, source="direct")
    at = web.place_order(h.world, h.customer(country="AT", acquisition="direct"), lines, source="direct")
    assert de.shipping_fee == 0.0 and at.currency == "EUR" and at.shipping_fee == w.shipping_fee("AT")
    lines, _ = _basket_between(h, w.free_shipping_threshold("AT"), 1000.0, "EUR")
    at = web.place_order(h.world, h.customer(country="AT", acquisition="direct"), lines, source="direct")
    assert at.shipping_fee == 0.0


def test_small_basket_pays_shipping_and_chf(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    c = h.customer(country="CH")
    sku = _sku(h, max_price=8.0)
    o = web.place_order(h.world, c, [(sku.sku_id, 1)], source="direct")
    assert o.currency == "CHF"
    assert o.lines[0].unit_price == sku.rrp_chf
    assert o.lines[0].tax_rate == h.cfg.channels.web.vat["CH"]
    assert o.shipping_fee == h.cfg.channels.web.shipping_fee_chf
    h.run_until(START + timedelta(days=1))
    sd = h.of(SalesDocCreated)[0].doc
    assert sd.currency == "CHF" and sd.fx_rate_eur == 1.05
    net, tax = split_gross(o.shipping_fee, h.cfg.channels.web.vat["CH"])
    assert (sd.shipping_net, sd.shipping_tax) == (net, tax)
    assert [cond.ctype for cond in sd.items[0].conditions] == ["list_price", "tax"]
    # the shipping charge is a freight item of its own: net values add up
    fr = sd.items[-1]
    assert (fr.item_no, fr.sku_id, fr.qty, fr.confirmed_qty, fr.item_type) == (900, 0, 1, 1, "freight")
    assert (fr.net_value, fr.tax) == (net, tax)
    assert [(c.ctype, c.amount) for c in fr.conditions] == [("shipping", net), ("tax", tax)]
    assert round(sd.net_value + sum(i.tax for i in sd.items), 2) == o.total
    for it in sd.items:
        assert round(sum(c.amount for c in it.conditions if c.ctype != "tax"), 2) == it.net_value
    # only the goods line is reserved and delivered
    dc = h.of(DeliveryCreated)[0].delivery
    assert [dl.sales_item_no for dl in dc.lines] == [10]


def test_payment_failure_without_retry(monkeypatch):
    h = Harness(monkeypatch, payments={"auth_rate": 0.0, "retry_share": 0.0})
    c = h.customer()
    o = web.place_order(h.world, c, [(_sku(h).sku_id, 1)], source="direct")
    h.run_until(START + timedelta(days=2))
    assert h.statuses(o.order_id) == ["payment_failed"]
    failed = [e for e in h.of(PaymentStatusChanged) if e.status == "failed"]
    assert len(failed) == 1 and failed[0].reason
    assert not h.of(SalesDocCreated)
    h.run_until(START + timedelta(days=31))
    assert h.world.store.get("web_order", o.order_id) is None


def test_payment_retry_success(monkeypatch):
    h = Harness(monkeypatch, payments={"auth_rate": 0.0, "retry_share": 1.0, "retry_success": 1.0},
                fulfilment={"cancel_before_pick": 0.0, "return_rate": 0.0})
    c = h.customer()
    o = web.place_order(h.world, c, [(_sku(h).sku_id, 1)], source="direct")
    h.run_until(START + timedelta(days=1))
    order = h.world.store.get("web_order", o.order_id)
    assert order.payment_attempts == 2 and len(order.payment_ids) == 2
    assert h.statuses(o.order_id)[:2] == ["authorised", "released"]


@pytest.mark.parametrize("auth", [0.0, 1.0])
def test_marketing_attributes_only_authorised_orders(monkeypatch, auth):
    """mkt counts confirmed orders: the order is noted when its payment is authorised, with its source."""
    from cpgsim.domain import marketing

    noted = []
    monkeypatch.setattr(marketing, "note_order",
                        lambda w, o, cu, src, new_customer: noted.append((o.order_id, w.now, src, new_customer,
                                                                          cu.customer_id)))
    h = Harness(monkeypatch, payments={"auth_rate": auth, "retry_share": 0.0},
                fulfilment={"cancel_before_pick": 0.0, "return_rate": 0.0})
    c = h.customer()
    o = web.place_order(h.world, c, [(_sku(h).sku_id, 1)], source="social")
    assert noted == []
    h.run_until(START + timedelta(days=1))
    if auth:
        (pay,) = [e for e in h.of(PaymentRecorded) if e.payment.order_id == o.order_id]
        assert noted == [(o.order_id, pay.at, "social", True, c.customer_id)]
    else:
        assert noted == [] and h.statuses(o.order_id) == ["payment_failed"]


def test_customer_cancel_before_pick(monkeypatch):
    h = Harness(monkeypatch, payments={"auth_rate": 1.0}, fulfilment={"cancel_before_pick": 1.0})
    c = h.customer()
    o = web.place_order(h.world, c, [(_sku(h).sku_id, 2)], source="direct")
    h.run_until(START + timedelta(days=5))
    assert h.statuses(o.order_id) == ["authorised", "released", "cancelled"]
    assert [e.reason for e in h.of(WebOrderStatusChanged)][-1] == "customer_cancelled"
    assert any(e.status == "voided" for e in h.of(PaymentStatusChanged))
    sdc = h.of(SalesDocChanged)[-1]
    assert sdc.header == {"status": "rejected"}
    assert sdc.items[10]["rejection_reason"] == "customer_cancelled"
    if o.shipping_fee > 0:  # the freight item is rejected with the goods
        assert sdc.items[900] == {"rejection_reason": "customer_cancelled"}
    order = h.world.store.get("web_order", o.order_id)
    assert order.lines[0].cancelled_qty == 2
    assert h.s.reserved.get(o.lines[0].sku_id, 0) == 0


def test_backorder_split_shipment(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    a, b = _sku(h, "mass"), _sku(h, "derma")
    h.s.stock[b.sku_id] = 1
    c = h.customer()
    o = web.place_order(h.world, c, [(a.sku_id, 1), (b.sku_id, 3)], source="direct")
    h.run_until(START + timedelta(hours=3))
    order = h.world.store.get("web_order", o.order_id)
    assert order.backorder == {2: 2} and len(order.delivery_ids) == 1
    h.run_until(START + timedelta(days=1, hours=12))
    h.s.stock[b.sku_id] = 100  # production receipt
    h.run_until(START + timedelta(days=10))
    order = h.world.store.get("web_order", o.order_id)
    assert len(order.delivery_ids) == 2 and not order.backorder
    st = h.statuses(o.order_id)
    assert st[:3] == ["authorised", "released", "partially_shipped"]
    assert "shipped" in st and st[-1] == "delivered"
    assert [ln.shipped_qty for ln in order.lines] == [1, 3]
    caps = [e.amount for e in h.of(PaymentStatusChanged) if e.status == "captured"]
    assert caps[-1] == pytest.approx(o.total, abs=0.02)


def test_backorder_cancelled_after_max_days(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    a, b = _sku(h, "mass"), _sku(h, "derma")
    h.s.stock[b.sku_id] = 0
    c = h.customer()
    o = web.place_order(h.world, c, [(a.sku_id, 1), (b.sku_id, 2)], source="direct")
    h.run_until(START + timedelta(days=20))
    order = h.world.store.get("web_order", o.order_id)
    assert order.lines[1].cancelled_qty == 2 and not order.backorder
    sd_changes = [e for e in h.of(SalesDocChanged) if 20 in e.items]
    assert sd_changes[-1].items[20]["rejection_reason"] == "out_of_stock"
    # the shipped part is delivered and billed, so the header completes now
    assert sd_changes[-1].header.get("status") == "completed"
    assert sd_changes[-1].header.get("delivery_status") == "full"
    assert sd_changes[-1].header.get("billing_status") == "full"
    assert h.statuses(o.order_id)[-2:] == ["shipped", "delivered"]


def test_everything_out_of_stock_cancels_order(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    a = _sku(h, "mass")
    h.s.stock[a.sku_id] = 0
    c = h.customer()
    o = web.place_order(h.world, c, [(a.sku_id, 1)], source="direct")
    h.run_until(START + timedelta(days=20))
    assert h.statuses(o.order_id)[-1] == "cancelled"
    assert any(e.status == "voided" for e in h.of(PaymentStatusChanged))


def test_lost_parcel_refund_and_credit_memo(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    h.s.outcome_fn = lambda d: "lost"
    c = h.customer()
    o = web.place_order(h.world, c, [(_sku(h, max_price=8.0).sku_id, 1)], source="direct")
    h.run_until(START + timedelta(days=10))
    assert h.statuses(o.order_id)[-2:] == ["lost", "refunded"]
    ref = h.of(RefundIssued)
    assert len(ref) == 1 and ref[0].refund.amount == o.total and ref[0].refund.reason == "lost"
    cms = [e.doc for e in h.of(BillingDocCreated) if e.doc.doc_kind == "credit_memo"]
    assert len(cms) == 1 and any(it.item_no == 900 for it in cms[0].items)
    assert round(cms[0].net_value + cms[0].tax, 2) == pytest.approx(o.total, abs=0.02)
    # no repurchase intent for a lost order
    assert not any(ev.kind == "cust.intent" for _, _, _, ev in h.world.queue._heap)


def test_returned_to_sender(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    h.s.outcome_fn = lambda d: "returned_to_sender"
    c = h.customer()
    o = web.place_order(h.world, c, [(_sku(h).sku_id, 2)], source="direct")
    h.run_until(START + timedelta(days=10))
    assert h.statuses(o.order_id)[-3:] == ["delivery_exception", "returned_to_sender", "refunded"]


def test_return_lifecycle(monkeypatch):
    h = Harness(monkeypatch, payments={"auth_rate": 1.0},
                fulfilment={"cancel_before_pick": 0.0, "return_rate": 1.0, "return_reject_rate": 0.0})
    c = h.customer()
    a, b = _sku(h, "mass", min_price=6.0), _sku(h, "seasonal", min_price=6.0)
    o = web.place_order(h.world, c, [(a.sku_id, 2), (b.sku_id, 2)], source="direct")
    h.run_until(START + timedelta(days=40))
    rc = h.of(ReturnCreated)
    assert len(rc) == 1
    r = rc[0].ret
    assert len(h.of(RefundIssued)) == 1
    assert r.reason != "allergic_reaction"  # no dermocosmetic line in the parcel
    statuses = [e.status for e in h.of(ReturnChanged)]
    assert statuses == ["label_issued", "received", "inspected", "refunded"]
    # the payment provider completes the refund a few days later
    (done,) = h.of(RefundChanged)
    issued = h.of(RefundIssued)[0]
    assert done.refund_id == issued.refund.refund_id and done.status == "completed"
    assert timedelta(0) < done.at - issued.at <= timedelta(days=7)
    assert len(h.s.received) == len(r.lines)
    ref = h.of(RefundIssued)[0].refund
    assert ref.reason == "return" and ref.return_id == r.return_id
    expected = sum(web.line_gross(o, rl.order_line_no, rl.qty) for rl in r.lines)
    assert ref.amount >= expected - 0.01
    st = h.statuses(o.order_id)
    assert st[-3:-1] == ["return_requested", "returned"] and st[-1] in ("refunded", "partially_refunded")
    cms = [e.doc for e in h.of(BillingDocCreated) if e.doc.doc_kind == "credit_memo"]
    assert len(cms) == 1 and cms[0].reason == "return"
    # cleanup waits 30 days after the refund closed the order
    closed = h.of(RefundIssued)[0].at
    assert h.world.store.get("web_order", o.order_id) is not None
    h.run_until(closed + timedelta(days=29))
    assert h.world.store.get("web_order", o.order_id) is not None
    h.run_until(closed + timedelta(days=45))
    assert h.world.store.get("web_order", o.order_id) is None
    assert h.world.store.count("web_return") == 0


def test_allergic_reaction_only_with_derma_line(monkeypatch):
    h = Harness(monkeypatch, payments={"auth_rate": 1.0},
                fulfilment={"cancel_before_pick": 0.0, "return_rate": 1.0,
                            "return_reason_mix": {"allergic_reaction": 1.0}})
    c = h.customer()
    mass, derma = _sku(h, "mass"), _sku(h, "derma")
    o1 = web.place_order(h.world, c, [(mass.sku_id, 1)], source="direct")
    o2 = web.place_order(h.world, c, [(mass.sku_id, 1), (derma.sku_id, 1)], source="direct")
    h.run_until(START + timedelta(days=30))
    by_order = {e.ret.order_id: e.ret for e in h.of(ReturnCreated)}
    assert by_order[o1.order_id].reason == "did_not_like"
    assert by_order[o2.order_id].reason == "allergic_reaction"
    assert [rl.sku_id for rl in by_order[o2.order_id].lines] == [derma.sku_id]


def test_scenario_promo_recorded_and_free_shipping(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    velmora = h.world.ref.catalogue.brand_of_role("mass")
    promo = Promotion(
        promo_id=900000, channel="web", name="Velmora 25", kind="automatic", code=None, discount_pct=0.25,
        free_shipping=True, sku_ids=[], brand=velmora, start=START, end=START + timedelta(days=10),
        announced_at=START, scenario_id="promo_margin_trap",
    )
    h.s.promo_fn = lambda world, t, sku_ids, rng: promo
    c = h.customer(country="DE")
    sku = _sku(h, "mass", max_price=8.0)
    o = web.place_order(h.world, c, [(sku.sku_id, 1)], source="direct")
    assert o.promo_id == 900000 and o.shipping_fee == 0.0
    assert o.lines[0].discount == round(sku.rrp_eur * 0.25, 2)
    assert h.of(WebOrderPlaced)[0].promotion is promo
    (eff,) = h.s.effects
    assert eff[:5] == ("promo_margin_trap", "promo_margin_trap:config:0", "web_order", o.order_id, "promo_discount")
    assert eff[5] == o.lines[0].discount
    assert eff[6]["free_shipping_waived"] == h.cfg.channels.web.shipping_fee_eur
    h.run_until(START + timedelta(days=1))
    conds = h.of(SalesDocCreated)[0].doc.items[0].conditions
    assert [cond.ctype for cond in conds] == ["list_price", "promo_discount", "tax"]
    # the condition rate is the promotion's nominal rate, not amount / base
    assert conds[1].rate == 0.25 and o.lines[0].discount_pct == 0.25


def test_austrian_shipping_fee_in_erp_and_waived_fee_in_truth(monkeypatch):
    """Austria's own fee (EUR 6.95 gross, 20 % VAT) on the ERP freight item, and as the waived amount in truth."""
    h = Harness(monkeypatch, **_happy_cfg())
    w = h.cfg.channels.web
    fee = w.shipping_fee("AT")
    net, tax = split_gross(fee, w.vat["AT"])
    assert fee == 6.95 and (net, tax) == (5.79, 1.16)
    sku = _sku(h, "mass", max_price=8.0)
    o = web.place_order(h.world, h.customer(country="AT"), [(sku.sku_id, 1)], source="direct")
    assert o.currency == "EUR" and o.shipping_fee == fee
    h.run_until(START + timedelta(days=1))
    fr = h.of(SalesDocCreated)[0].doc.items[-1]
    assert fr.item_type == "freight" and (fr.net_value, fr.tax) == (net, tax)
    assert [(c.ctype, c.amount) for c in fr.conditions] == [("shipping", net), ("tax", tax)]
    # a free-shipping promotion waives the Austrian fee; truth records it gross and net
    velmora = h.world.ref.catalogue.brand_of_role("mass")
    promo = Promotion(
        promo_id=900002, channel="web", name="Velmora 25", kind="automatic", code=None, discount_pct=0.25,
        free_shipping=True, sku_ids=[], brand=velmora, start=START, end=START + timedelta(days=10),
        announced_at=START, scenario_id="promo_margin_trap",
    )
    h.s.promo_fn = lambda world, t, sku_ids, rng: promo
    o = web.place_order(h.world, h.customer(country="AT"), [(sku.sku_id, 1)], source="direct")
    assert o.promo_id == promo.promo_id and o.shipping_fee == 0.0
    det = h.s.effects[-1][6]
    assert det["free_shipping_waived"] == fee and det["shipping_waived_eur"] == net
    assert det["freight_eur"] == round(web.expected_freight_eur(h.world, "AT"), 2)
    assert det["freight_eur"] > web.expected_freight_eur(h.world, "DE") + 1.5


def test_promo_not_applied_without_eligible_line(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    promo = Promotion(
        promo_id=100001, channel="web", name="X", kind="code", code="X10", discount_pct=0.1,
        free_shipping=False, sku_ids=[], brand="NoSuchBrand", start=START, end=START + timedelta(days=3),
        announced_at=START,
    )
    h.s.promo_fn = lambda world, t, sku_ids, rng: promo
    c = h.customer(acquisition="direct")  # no influencer code (its first-order discount would apply)
    o = web.place_order(h.world, c, [(_sku(h).sku_id, 1)], source="direct")
    assert o.promo_id is None and h.of(WebOrderPlaced)[0].promotion is None


# ---------------------------------------------------------------------------
# Influencer code first-order discount (one code per order)
# ---------------------------------------------------------------------------


def _trap_promo(h):
    return Promotion(
        promo_id=900000, channel="web", name="Velmora 25% off + free shipping", kind="automatic", code=None,
        discount_pct=0.25, free_shipping=True, sku_ids=[], brand=h.world.ref.catalogue.brand_of_role("mass"),
        start=START, end=START + timedelta(days=28), announced_at=START, scenario_id="promo_margin_trap",
    )


def _basket_promo(promo):
    """Like promotions.web_promo_for_basket: the promotion only if a basket SKU is eligible."""
    def fn(world, t, sku_ids, rng):
        cat = world.ref.catalogue
        return promo if any(Stubs.is_eligible(promo, cat.get(s)) for s in sku_ids) else None
    return fn


def _influencer_customer(h):
    c = h.customer(country="DE", acquisition="influencer")
    assert c.influencer_code in h.cfg.customers.influencer_codes
    return c


def _shop_rows(h, table, **match):
    rows = h.dispatcher.batch.table(table).inserts.values()
    return [r for r in rows if all(r.get(k) == v for k, v in match.items())]


def test_influencer_first_order_gets_the_code_discount_in_shop_and_erp(monkeypatch):
    from cpgsim.domain import marketing
    from cpgsim.writers import keys

    h = Harness(monkeypatch, **_happy_cfg())
    c = _influencer_customer(h)
    sku, other = _sku(h, "mass", min_price=12.0), _sku(h, "derma")
    o = web.place_order(h.world, c, [(sku.sku_id, 2), (other.sku_id, 1)], source="influencer")
    promo = marketing.influencer_promotion(h.world, c.influencer_code)
    assert promo.kind == "code" and promo.code == c.influencer_code and promo.discount_pct == 0.15
    assert not promo.free_shipping and promo.sku_ids == [] and promo.brand is None
    assert marketing.is_influencer_promo(promo.promo_id) and o.promo_id == promo.promo_id
    assert h.of(WebOrderPlaced)[0].promotion is promo
    for ln in o.lines:  # 15 % off every item
        assert ln.discount == round(ln.unit_price * ln.qty * 0.15, 2) and ln.discount_pct == 0.15
    assert o.shipping_fee == 0.0 and not h.s.effects  # above the threshold; no scenario truth
    # shop: order, lines and redemption carry the code and the same discount
    oid, pid = keys.shop_order_id(h.world.seed, o.order_id), keys.shop_promotion_id(h.world.seed, promo.promo_id)
    (row,) = _shop_rows(h, "shop.orders", id=oid)
    lines = _shop_rows(h, "shop.order_lines", order_id=oid)
    (red,) = _shop_rows(h, "shop.promo_redemptions", order_id=oid)
    disc = round(sum(ln.discount for ln in o.lines), 2)
    assert (row["promo_code"], row["promotion_id"]) == (c.influencer_code, pid)
    assert float(row["discount_total"]) == disc == float(sum(r["discount_amount"] for r in lines))
    assert float(row["total_amount"]) == o.total == round(o.goods_gross, 2)
    assert (red["promotion_id"], red["code"], float(red["discount_amount"])) == (pid, c.influencer_code, disc)
    # ERP: a ZINF condition per item at the nominal rate (no ZPRO); conditions add up to the net value
    h.run_until(START + timedelta(days=1))
    sd = h.of(SalesDocCreated)[0].doc
    goods = [it for it in sd.items if it.item_type != "freight"]
    for it in goods:
        assert [cond.ctype for cond in it.conditions] == ["list_price", "influencer_discount", "tax"]
        assert it.conditions[1].rate == 0.15
        assert round(sum(cond.amount for cond in it.conditions if cond.ctype != "tax"), 2) == it.net_value
    konv = list(h.dispatcher.batch.table("erp.konv").inserts.values())
    zinf = [k for k in konv if k["kschl"] == "ZINF"]
    assert len(zinf) == len(goods) and not [k for k in konv if k["kschl"] == "ZPRO"]
    assert {float(k["kbetr"]) for k in zinf} == {-15.0}
    assert round(sum(float(k["kwert"]) for k in zinf), 2) == round(
        sum(cond.amount for it in goods for cond in it.conditions if cond.ctype == "influencer_discount"), 2)
    vbap = [r for r in h.dispatcher.batch.table("erp.vbap").inserts.values() if r["matnr"] != "SHIPPING"]
    assert round(sum(float(r["netwr"]) for r in vbap), 2) == round(sum(it.net_value for it in goods), 2)
    assert round(sd.net_value + sum(i.tax for i in sd.items), 2) == o.total


def test_influencer_discount_only_on_the_first_order(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    c = _influencer_customer(h)
    sku = _sku(h, min_price=10.0)
    o1 = web.place_order(h.world, c, [(sku.sku_id, 3)], source="influencer")
    o2 = web.place_order(h.world, c, [(sku.sku_id, 3)], source="email")
    assert o1.promo_id is not None and o1.lines[0].discount > 0
    assert o2.promo_id is None and o2.lines[0].discount == 0.0 and h.of(WebOrderPlaced)[1].promotion is None


def _basket_between(h, low, high, currency):
    """A one-SKU basket whose list value lies in [low, high)."""
    for sku in h.world.ref.catalogue.active(h.world.local_date()):
        price = web.unit_price(sku, currency)
        for q in range(1, 8):
            if low <= round(price * q, 2) < high:
                return [(sku.sku_id, q)], round(price * q, 2)
    raise AssertionError("no basket")


def test_influencer_discount_never_costs_the_free_shipping(monkeypatch):
    """A basket that ships free at list prices still ships free after the code's 15 % (threshold before discount)."""
    from cpgsim.domain import marketing

    h = Harness(monkeypatch, **_happy_cfg())
    w = h.cfg.channels.web
    for country in ("DE", "AT", "CH"):
        threshold = w.free_shipping_threshold(country)
        currency = web.currency_of(country)
        lines, listed = _basket_between(h, threshold, round(threshold / 0.85, 2) - 0.01, currency)
        c = h.customer(country=country, acquisition="influencer")
        o = web.place_order(h.world, c, lines, source="influencer")
        assert marketing.is_influencer_promo(o.promo_id) and o.lines[0].discount_pct == 0.15
        assert o.goods_gross < threshold <= listed  # below the threshold after the discount ...
        assert o.shipping_fee == 0.0 and o.total == o.goods_gross < listed  # ... yet free shipping
    # a calendar promotion keeps testing the threshold on the goods after its discount (unchanged)
    lines, _ = _basket_between(h, w.free_shipping_eur, round(w.free_shipping_eur / 0.85, 2) - 0.01, "EUR")
    promo = Promotion(
        promo_id=20250302, channel="web", name="Everything 15% off", kind="code", code="ALL15-2503",
        discount_pct=0.15, free_shipping=False, sku_ids=[], brand=None, start=START,
        end=START + timedelta(days=7), announced_at=START,
    )
    h.s.promo_fn = _basket_promo(promo)
    o = web.place_order(h.world, h.customer(country="DE", acquisition="direct"), lines, source="direct")
    assert o.promo_id == promo.promo_id and o.shipping_fee == w.shipping_fee_eur


def test_influencer_discount_not_combined_with_the_margin_trap_promotion(monkeypatch):
    """One code per order: a qualifying promo_margin_trap basket keeps the trap promotion only."""
    from cpgsim.domain import marketing
    from cpgsim.writers import keys

    h = Harness(monkeypatch, **_happy_cfg())
    trap = _trap_promo(h)
    h.s.promo_fn = _basket_promo(trap)
    velmora = _sku(h, "mass", max_price=8.0)
    derma = _sku(h, "derma", max_price=12.0)
    c1, c2 = _influencer_customer(h), _influencer_customer(h)
    o1 = web.place_order(h.world, c1, [(velmora.sku_id, 1), (derma.sku_id, 1)], source="influencer")
    assert o1.promo_id == trap.promo_id and o1.shipping_fee == 0.0  # trap: free shipping, no minimum
    assert [ln.discount_pct for ln in o1.lines] == [0.25, 0.0]
    assert o1.lines[0].discount == round(velmora.rrp_eur * 0.25, 2) and o1.lines[1].discount == 0.0
    assert h.of(WebOrderPlaced)[0].promotion is trap
    (eff,) = h.s.effects  # the trap's truth as before
    assert eff[3] == o1.order_id and eff[5] == o1.lines[0].discount
    # a basket the trap promotion does not cover gets the influencer discount instead
    o2 = web.place_order(h.world, c2, [(derma.sku_id, 1)], source="influencer")
    assert marketing.is_influencer_promo(o2.promo_id) and o2.lines[0].discount_pct == 0.15
    assert o2.shipping_fee == h.cfg.channels.web.shipping_fee_eur  # no free shipping with the code
    assert len(h.s.effects) == 1
    # the offer is not carried over: c1's second order (no promotion) is at full price
    h.s.promo_fn = lambda world, t, sku_ids, rng: None
    o3 = web.place_order(h.world, c1, [(derma.sku_id, 1)], source="email")
    assert o3.promo_id is None and o3.lines[0].discount == 0.0
    # shop rows: the trap order carries the trap promotion and no voucher code
    seed = h.world.seed
    (row,) = _shop_rows(h, "shop.orders", id=keys.shop_order_id(seed, o1.order_id))
    assert (row["promo_code"], row["promotion_id"]) == (None, keys.shop_promotion_id(seed, trap.promo_id))
    (row2,) = _shop_rows(h, "shop.orders", id=keys.shop_order_id(seed, o2.order_id))
    assert row2["promo_code"] == c2.influencer_code
    h.run_until(START + timedelta(days=1))
    by_ref = {e.doc.external_ref: e.doc for e in h.of(SalesDocCreated)}
    ctypes = [cond.ctype for it in by_ref[str(o1.order_id)].items for cond in it.conditions]
    assert "promo_discount" in ctypes and "influencer_discount" not in ctypes


def test_trap_order_of_an_influencer_customer_stays_attributed_to_the_code(monkeypatch):
    """The trap promotion wins the order, but the customer and the order stay attributed to the influencer code."""
    from cpgsim.domain import marketing
    from cpgsim.engine.queue import Event
    from cpgsim.writers import keys

    h = Harness(monkeypatch, marketing={"tracking_share": 1.0}, **_happy_cfg())
    trap = _trap_promo(h)
    h.s.promo_fn = _basket_promo(trap)
    c = _influencer_customer(h)
    code = c.influencer_code
    o = web.place_order(h.world, c, [(_sku(h, "mass", max_price=8.0).sku_id, 1)], source="influencer")
    # the order carries the trap promotion only: no influencer discount, no voucher code
    assert o.promo_id == trap.promo_id and not marketing.is_influencer_promo(o.promo_id)
    assert o.lines[0].discount_pct == 0.25
    seed = h.world.seed
    oid = keys.shop_order_id(seed, o.order_id)
    (row,) = _shop_rows(h, "shop.orders", id=oid)
    assert (row["promo_code"], row["promotion_id"]) == (None, keys.shop_promotion_id(seed, trap.promo_id))
    (red,) = _shop_rows(h, "shop.promo_redemptions", order_id=oid)
    assert red["promotion_id"] == keys.shop_promotion_id(seed, trap.promo_id) and red["code"] != code
    # the customer keeps the signup source and the code it signed up with
    (cust,) = _shop_rows(h, "shop.customers", id=keys.shop_customer_id(seed, c.customer_id))
    assert (cust["signup_source"], cust["influencer_code"]) == ("influencer", code)
    # mkt: attributed to the influencer channel and the code (never to the trap's promotion or a coupon site)
    h.run_until(START + timedelta(days=1))
    d = h.world.local_date(START)
    h.world.now = h.world.at_local(d + timedelta(days=1), 6)
    marketing.on_export(h.world, Event(due=h.world.now, priority=60, kind=marketing.EXPORT_KIND,
                                       key=f"{marketing.EXPORT_KIND}:{d}", data={"date": d.isoformat()}))
    (att,) = [r for r in h.dispatcher.batch.table("mkt.attributions").inserts.values() if r["order_ref"] == oid]
    assert (att["channel"], att["influencer_code"], att["is_new_customer"]) == ("influencer", code, True)
    assert att["customer_ref"] == keys.shop_customer_id(seed, c.customer_id)
    usable = [cp for cp in marketing.active_campaigns(h.world, d)
              if cp.channel == "influencer" and cp.influencer_code == code]
    assert usable and att["campaign_id"] in {cp.campaign_id for cp in usable}


def test_influencer_discount_not_combined_with_a_regular_code_promotion(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    sku = _sku(h, min_price=10.0)
    promo = Promotion(
        promo_id=20250301, channel="web", name="Selected favourites 10% off", kind="code", code="GLOW10-2503",
        discount_pct=0.10, free_shipping=False, sku_ids=[sku.sku_id], brand=None, start=START,
        end=START + timedelta(days=7), announced_at=START,
    )
    h.s.promo_fn = _basket_promo(promo)
    o = web.place_order(h.world, _influencer_customer(h), [(sku.sku_id, 3)], source="influencer")
    assert o.promo_id == promo.promo_id and o.lines[0].discount_pct == 0.10
    (row,) = h.dispatcher.batch.table("shop.orders").inserts.values()
    assert row["promo_code"] == "GLOW10-2503"


def test_no_influencer_discount_without_a_code(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    sku = _sku(h, min_price=10.0)
    for acq in ("direct", "paid_search", "social", "organic_search"):
        c = h.customer(country="DE", acquisition=acq)
        assert c.influencer_code is None
        o = web.place_order(h.world, c, [(sku.sku_id, 3)], source=acq)
        assert o.promo_id is None and o.lines[0].discount == 0.0


def test_influencer_discount_switched_off(monkeypatch):
    from cpgsim.domain import marketing

    h = Harness(monkeypatch, marketing={"influencer_first_order_discount_pct": 0.0}, **_happy_cfg())
    c = _influencer_customer(h)
    assert marketing.influencer_promotion(h.world, c.influencer_code) is None
    o = web.place_order(h.world, c, [(_sku(h, min_price=10.0).sku_id, 3)], source="influencer")
    assert o.promo_id is None and o.lines[0].discount == 0.0
    assert not h.dispatcher.batch.table("shop.promo_redemptions").inserts
    h.run_until(START + timedelta(days=1))
    assert [cond.ctype for cond in h.of(SalesDocCreated)[0].doc.items[0].conditions] == ["list_price", "tax"]


# ---------------------------------------------------------------------------
# Repurchase intents and subscriptions
# ---------------------------------------------------------------------------


def test_repurchase_intent_converts(monkeypatch):
    h = Harness(monkeypatch, customers={"repurchase_prob": 1.0}, **_happy_cfg())
    c = h.customer()
    c.meta["chain"] = True
    o = web.place_order(h.world, c, [(_sku(h).sku_id, 1)], source="direct")
    h.run_until(START + timedelta(days=200))
    placed = [e for e in h.of(WebOrderPlaced) if e.order.customer_id == c.customer_id]
    assert len(placed) >= 3
    assert all(e.order.is_repeat for e in placed[1:])
    assert placed[1].order.lines[0].sku_id == o.lines[0].sku_id or len(placed[1].order.lines) >= 1
    gaps = [(b.order.placed_at - a.order.placed_at).days for a, b in zip(placed, placed[1:])]
    assert min(gaps) >= 3


def test_repurchase_misses_lapse(monkeypatch):
    h = Harness(monkeypatch, customers={"repurchase_prob": 0.0}, **_happy_cfg())
    c = h.customer()
    c.meta["chain"] = True
    web.place_order(h.world, c, [(_sku(h).sku_id, 1)], source="direct")
    h.run_until(START + timedelta(days=400))
    # lapsed with no open order, intent or subscription: evicted from the store
    assert not h.world.store.has("customer", c.customer_id)
    cust = c
    assert cust.lapsed and cust.missed_intents == h.cfg.customers.max_missed_intents
    assert cust.intent_seq == h.cfg.customers.max_missed_intents
    assert cust.orders == 1


def test_one_time_buyers_get_no_intent(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    share = customers.calibration(h.world)["chain_share"]
    cs = [h.customer() for _ in range(400)]
    for c in cs:
        web.place_order(h.world, c, [(_sku(h).sku_id, 1)], source="direct")
    h.run_until(START + timedelta(days=6))
    chain = sum(1 for c in cs if h.world.store.get("customer", c.customer_id).meta.get("chain"))
    assert abs(chain / 400 - share) < 0.08


def test_subscription_lifecycle(monkeypatch):
    h = Harness(
        monkeypatch,
        customers={"repurchase_prob": 1.0, "subscription_rate": 1.0, "subscription_cancel_prob": 0.25,
                   "subscription_pause_prob": 0.25},
        **_happy_cfg(),
    )
    c = h.customer()
    c.meta["chain"] = True
    web.place_order(h.world, c, [(_sku(h).sku_id, 1)], source="direct")
    h.run_until(START + timedelta(days=900))
    created = h.of(SubscriptionCreated)
    assert len(created) == 1
    sub = created[0].subscription
    sub_orders = [e for e in h.of(WebOrderPlaced) if e.order.subscription_id == sub.subscription_id]
    assert sub_orders and all(e.source == "subscription" for e in sub_orders)
    changes = h.of(SubscriptionChanged)
    assert changes[-1].status == "cancelled" and changes[-1].next_order_at is None
    for e in changes[:-1]:
        assert e.next_order_at is not None
    assert not h.world.store.has("customer", c.customer_id)  # churned, orders cleaned up: evicted
    cust = c
    assert cust.subscription_id is None and cust.lapsed
    # no intents fire while subscribed: no repeat order outside the subscription after it started
    after = [e for e in h.of(WebOrderPlaced)
             if e.order.placed_at > sub.created_at and e.order.subscription_id is None]
    assert not after


def test_warm_start(monkeypatch):
    h = Harness(monkeypatch, with_writers=False, channels={"web": {"initial_customers": 500}})
    customers.warm_start_customers(h.world)
    subscriptions.warm_start_subscriptions(h.world)
    cal = customers.calibration(h.world)
    created = [e.subscription for e in h.of(SubscriptionCreated)]
    n_sub = len(created)
    rate = sum(1 / (7 * s.interval_weeks) for s in created)
    target = cal["sub_orders_per_day"] / (1 + h.cfg.customers.subscription_pause_prob * 1.5)
    assert target <= rate < target + 1 / 28
    assert len(h.of(CustomerRegistered)) == 500 + n_sub
    regs = h.of(CustomerRegistered)
    assert all(r.at == START and r.customer.created_at < START for r in regs)
    intents = [ev for _, _, _, ev in h.world.queue._heap if ev.kind == "cust.intent"]
    assert len(intents) == 500
    ahead = sorted((ev.due - START).days for ev in intents)
    assert ahead[len(ahead) // 2] < 60 and ahead[0] >= 0
    subs = [ev for _, _, _, ev in h.world.queue._heap if ev.kind == "sub.order"]
    assert len(subs) == n_sub and max(ev.due for ev in subs) <= START + timedelta(weeks=13)


# ---------------------------------------------------------------------------
# Determinism and steady-state volume
# ---------------------------------------------------------------------------


def _run_small_business(monkeypatch, days: int, base: float, initial: int, **over):
    h = Harness(
        monkeypatch,
        with_writers=False,
        channels={"web": {"base_orders_per_day": base, "initial_customers": initial}},
        **over,
    )
    customers.warm_start_customers(h.world)
    subscriptions.warm_start_subscriptions(h.world)
    h.run_with_arrivals(START + timedelta(days=days))
    return h


def test_deterministic_replay(monkeypatch):
    def fingerprint():
        h = _run_small_business(monkeypatch, 20, 30.0, 300)
        return [(type(e).__name__, e.at, getattr(e, "order_id", None), getattr(e, "status", None))
                for e in h.events]

    assert fingerprint() == fingerprint()


def test_steady_state_volume(monkeypatch):
    base = 60.0
    days = 240
    web_default = Config().channels.web  # warm start at the default ratio (30 x base_orders_per_day)
    initial = int(web_default.initial_customers * base / web_default.base_orders_per_day)
    h = _run_small_business(monkeypatch, days, base, initial)
    per_day = Counter()
    kinds = Counter()
    for e in h.of(WebOrderPlaced):
        day = (e.at - START).days
        per_day[day] += 1
        kinds["sub" if e.order.subscription_id else ("repeat" if e.order.is_repeat else "new")] += 1
    total = sum(per_day.values()) / days
    assert 0.85 * base < total < 1.15 * base, (total, kinds)
    # roughly stationary: first and last 60 days within 20 %
    first = sum(per_day[d] for d in range(60)) / 60
    last = sum(per_day[d] for d in range(days - 60, days)) / 60
    assert abs(first - last) / base < 0.2, (first, last)
    new_share = kinds["new"] / sum(kinds.values())
    assert abs(new_share - h.cfg.channels.web.new_customer_share) < 0.07, kinds
    # memory stays bounded: orders older than ~2 months are gone
    assert h.world.store.count("web_order") < base * 75


def _roundtrip_state(world) -> None:
    """Replace every entity and queued event by its JSON round trip (as after a restart)."""
    import json

    from cpgsim.engine.codec import from_jsonable, to_jsonable

    store = world.store
    for kind, data in store._data.items():
        cls = store._kinds[kind]
        for sid in list(data):
            data[sid] = from_jsonable(cls, json.loads(json.dumps(to_jsonable(data[sid]))))
    rows = [(ev.key, ev.due, ev.priority, ev.kind, json.dumps(ev.data)) for _, _, _, ev in world.queue._heap]
    world.queue.load(rows)


def test_restart_roundtrip_is_deterministic(monkeypatch):
    def fp(h, since):
        return [(type(e).__name__, e.at, getattr(e, "order_id", None), getattr(e, "status", None),
                 getattr(e, "amount", None)) for e in h.events if e.at > since]

    over = {"fulfilment": {"return_rate": 0.2}, "customers": {"subscription_rate": 0.5}}
    cut = START + timedelta(days=12)
    a = _run_small_business(monkeypatch, 40, 30.0, 300, **over)
    b = _run_small_business(monkeypatch, 12, 30.0, 300, **over)
    _roundtrip_state(b.world)
    b.run_with_arrivals(START + timedelta(days=40))
    assert fp(a, cut) == fp(b, cut)


def test_backorder_cancel_after_partial_shipment(monkeypatch):
    """A cut item keeps its delivered part; ERP header completes; shop times are business times."""
    h = Harness(monkeypatch, **_happy_cfg())
    a, b = _sku(h, "mass"), _sku(h, "derma")
    h.s.stock[b.sku_id] = 1  # one of three units available, never replenished
    c = h.customer()
    o = web.place_order(h.world, c, [(a.sku_id, 1), (b.sku_id, 3)], source="direct")
    h.run_until(START + timedelta(days=20))
    order = h.world.store.get("web_order", o.order_id)
    assert order.lines[1].cancelled_qty == 2 and not order.backorder
    cancel = [e for e in h.of(SalesDocChanged) if 20 in e.items][-1]
    # the reduced item carries its delivered/billed quantities so its statuses are recomputed
    assert cancel.items[20] == {"confirmed_qty": 1, "delivered_qty": 1, "billed_qty": 1}
    assert cancel.header.get("status") == "completed"
    doc = h.world.store.get("sales_doc", order.sales_doc_id)
    assert (doc.status, doc.delivery_status, doc.billing_status) == ("completed", "full", "full")
    # shop: shipped and delivered effective at the parcel's goods issue and delivery scan
    (dlv,) = [h.world.store.get("delivery", d) for d in order.delivery_ids]
    by_status = {e.status: e for e in h.of(WebOrderStatusChanged) if e.order_id == o.order_id}
    assert h.statuses(o.order_id)[-2:] == ["shipped", "delivered"]
    assert by_status["shipped"].effective_at == dlv.shipped_at
    assert by_status["delivered"].effective_at == dlv.delivered_at
    assert by_status["delivered"].at > dlv.delivered_at  # recognised at the cancel


def test_everything_out_of_stock_rejects_freight_item(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    a = _sku(h, "mass", max_price=8.0)
    h.s.stock[a.sku_id] = 0
    o = web.place_order(h.world, h.customer(country="DE"), [(a.sku_id, 1)], source="direct")
    assert o.shipping_fee > 0
    h.run_until(START + timedelta(days=20))
    last = h.of(SalesDocChanged)[-1]
    assert last.header == {"status": "rejected"}
    assert last.items[900] == {"confirmed_qty": 0, "rejection_reason": "out_of_stock"}


def test_rejected_return(monkeypatch):
    h = Harness(monkeypatch, payments={"auth_rate": 1.0},
                fulfilment={"cancel_before_pick": 0.0, "return_rate": 1.0, "return_reject_rate": 1.0})
    c = h.customer()
    o = web.place_order(h.world, c, [(_sku(h, "mass", min_price=6.0).sku_id, 2)], source="direct")
    h.run_until(START + timedelta(days=40))
    assert [e.status for e in h.of(ReturnChanged)] == ["label_issued", "received", "inspected", "rejected"]
    st = h.statuses(o.order_id)
    assert st[-3:] == ["return_requested", "returned", "return_rejected"]
    assert not h.of(RefundIssued) and not h.s.received
    returned = [e.lines[1]["returned_qty"] for e in h.of(WebOrderLinesChanged)
                if e.order_id == o.order_id and 1 in e.lines and "returned_qty" in e.lines[1]]
    assert returned[-1] == 0 and max(returned) > 0
    order = h.world.store.get("web_order", o.order_id)
    assert order is None or order.lines[0].returned_qty == 0


def test_remainder_ships_after_first_parcel_returned_to_sender(monkeypatch):
    """Split order: the first parcel goes back to the sender, the backordered rest ships later.

    The order still passes 'shipped' (shop shipped_at) before 'delivered'.
    """
    h = Harness(monkeypatch, **_happy_cfg())
    a, b = _sku(h, "mass"), _sku(h, "derma")
    h.s.stock[b.sku_id] = 0
    h.s.outcome_fn = lambda d: "returned_to_sender" if d.seq_in_order == 1 else "delivered"
    c = h.customer()
    o = web.place_order(h.world, c, [(a.sku_id, 1), (b.sku_id, 1)], source="direct")
    t = START
    while "returned_to_sender" not in h.statuses(o.order_id):
        t += timedelta(minutes=30)
        assert t < START + timedelta(days=6)
        h.run_until(t)
    h.s.stock[b.sku_id] = 100
    h.run_until(START + timedelta(days=12))
    st = h.statuses(o.order_id)
    # the first parcel is refunded (all that was captured so far) before the rest ships
    assert st[st.index("returned_to_sender"):] == ["returned_to_sender", "refunded", "shipped", "delivered"], st
    order = h.world.store.get("web_order", o.order_id)
    assert [ln.shipped_qty for ln in order.lines] == [1, 1]


def test_customer_return_keeps_its_status_when_the_rest_ships():
    """Only a failed parcel is superseded by a later shipment, not a customer return or its refund."""
    o = types.SimpleNamespace(status="partially_refunded", return_ids=[], meta={"outcomes": {"7": "returned_to_sender"}})
    assert web._only_parcels_failed(o)
    o.return_ids = [3]
    assert not web._only_parcels_failed(o)
    o.status = "lost"
    assert web._only_parcels_failed(o)
    o.status, o.return_ids, o.meta["outcomes"] = "refunded", [], {"7": "delivered"}
    assert not web._only_parcels_failed(o)


def _first_parcel_returned(monkeypatch, **fulfilment):
    """Split order whose first parcel the customer returns; the second parcel is never returned."""
    from cpgsim.domain import returns as real_returns

    real = real_returns.maybe_schedule_return

    def only_first(world, order, delivery):
        if delivery.seq_in_order == 1:
            real(world, order, delivery)

    monkeypatch.setattr(real_returns, "maybe_schedule_return", only_first)
    fcfg = {"cancel_before_pick": 0.0, "return_rate": 1.0, "return_reject_rate": 0.0, "return_window_days": 1}
    fcfg.update(fulfilment)
    h = Harness(monkeypatch, payments={"auth_rate": 1.0}, fulfilment=fcfg)
    a, b = _sku(h, "mass", min_price=6.0), _sku(h, "seasonal", min_price=6.0)
    h.s.stock[b.sku_id] = 0
    o = web.place_order(h.world, h.customer(country="DE"), [(a.sku_id, 2), (b.sku_id, 1)], source="direct")
    return h, o, b


def _milestones(h, order_id):
    return {e.milestone: e for e in h.of(WebOrderMilestoneReached) if e.order_id == order_id}


def test_remainder_ships_after_customer_return_of_first_parcel(monkeypatch):
    """Return of parcel 1 refunded before the backordered rest ships: the return status stays,
    shipped_at / delivered_at are recorded at the remainder's goods issue and delivery scan,
    and the order is only partially refunded once the remainder is captured."""
    h, o, b = _first_parcel_returned(monkeypatch, backorder_max_days=40)
    t = START
    while not set(h.statuses(o.order_id)) & {"refunded", "partially_refunded"}:
        t += timedelta(hours=1)
        assert t < START + timedelta(days=30)
        h.run_until(t)
    assert "shipped" not in h.statuses(o.order_id)  # never completed before the return
    h.s.stock[b.sku_id] = 100
    h.run_until(t + timedelta(days=10))
    order = h.world.store.get("web_order", o.order_id)
    st = h.statuses(o.order_id)
    assert "shipped" not in st and "delivered" not in st, st
    assert st[-1] == "partially_refunded", st
    assert [ln.shipped_qty for ln in order.lines] == [2, 1]
    second = h.world.store.get("delivery", order.delivery_ids[1])
    ms = _milestones(h, o.order_id)
    assert set(ms) == {"shipped", "delivered"}
    assert (ms["shipped"].effective_at or ms["shipped"].at) == second.shipped_at
    assert (ms["delivered"].effective_at or ms["delivered"].at) == second.delivered_at
    # refunded_at keeps the refund's time when the status becomes partially_refunded
    by_status = {e.status: e for e in h.of(WebOrderStatusChanged) if e.order_id == o.order_id}
    if "refunded" in by_status:
        assert by_status["partially_refunded"].effective_at == by_status["refunded"].at
    assert order.refunded_total < order.captured_total


def test_backorder_cancelled_after_customer_return_of_first_parcel(monkeypatch):
    """The remainder is cancelled while the return of parcel 1 is under way: milestones only."""
    h, o, _ = _first_parcel_returned(monkeypatch, backorder_max_days=6)
    h.run_until(START + timedelta(days=30))
    order = h.world.store.get("web_order", o.order_id)
    st = h.statuses(o.order_id)
    assert "return_requested" in st and "shipped" not in st and "delivered" not in st, st
    assert st[-1] in ("partially_refunded", "refunded"), st
    assert order.lines[1].cancelled_qty == 1
    (dlv,) = [h.world.store.get("delivery", d) for d in order.delivery_ids]
    ms = _milestones(h, o.order_id)
    assert ms["shipped"].effective_at == dlv.shipped_at
    assert ms["delivered"].effective_at == dlv.delivered_at
    cancel_at = [e.at for e in h.of(WebOrderLinesChanged)
                 if e.order_id == o.order_id and 2 in e.lines and "cancelled_qty" in e.lines[2]][0]
    assert ms["shipped"].at == cancel_at and ms["delivered"].at == cancel_at
    # the return started before the cancel
    req = [e.at for e in h.of(WebOrderStatusChanged) if e.order_id == o.order_id and e.status == "return_requested"]
    assert req[0] < cancel_at


def test_no_milestone_when_status_passed_through():
    """A milestone the order already reached as a status is not recorded twice."""
    events = []
    w = types.SimpleNamespace(now=START, emit=events.append)
    o = types.SimpleNamespace(order_id=1, status="released", return_ids=[], meta={})
    web.set_status(w, o, "shipped")
    web.set_status(w, o, "return_requested")
    web._milestone(w, o, "shipped", START)
    web._milestone(w, o, "delivered", START - timedelta(hours=1))
    web._milestone(w, o, "delivered", START)
    ms = [e for e in events if isinstance(e, WebOrderMilestoneReached)]
    assert [(e.milestone, e.effective_at) for e in ms] == [("delivered", START - timedelta(hours=1))]


def test_subscription_cancel_evicts_customer_without_open_order(monkeypatch):
    """A subscription cancelled with no open order (e.g. every SKU delisted) evicts its customer;
    one cancelled right after its last order keeps the customer until that order is cleaned up."""
    h = Harness(monkeypatch, **_happy_cfg())
    sku = _sku(h)
    idle, busy = h.customer(), h.customer()
    subs = {}
    for c in (idle, busy):
        c = h.world.store.mut("customer", c.customer_id)
        subs[c.customer_id] = subscriptions._create(
            h.world, c, [sku.sku_id], [1], 4, START, START + timedelta(days=28)
        )
    web.place_order(h.world, busy, [(sku.sku_id, 1)], source="subscription",
                    subscription_id=subs[busy.customer_id].subscription_id)
    for cid, sub in subs.items():
        subscriptions._cancel(h.world, h.world.store.mut("subscription", sub.subscription_id),
                              h.world.store.get("customer", cid))
    assert not h.world.store.has("customer", idle.customer_id)
    assert h.world.store.has("customer", busy.customer_id)
    left = h.world.store.get("customer", busy.customer_id)
    assert left.lapsed and left.subscription_id is None
    h.run_until(START + timedelta(days=60))  # the order is delivered and cleaned up
    assert not h.world.store.has("customer", busy.customer_id)


def test_backorder_cancelled_after_first_parcel_returned_to_sender(monkeypatch):
    """Nothing arrived, the rest is cut: the refund status stays, shipped_at is still recorded."""
    h = Harness(monkeypatch, payments={"auth_rate": 1.0},
                fulfilment={"cancel_before_pick": 0.0, "return_rate": 0.0, "backorder_max_days": 6})
    a, b = _sku(h, "mass"), _sku(h, "derma")
    h.s.stock[b.sku_id] = 0
    h.s.outcome_fn = lambda d: "returned_to_sender"
    o = web.place_order(h.world, h.customer(), [(a.sku_id, 1), (b.sku_id, 1)], source="direct")
    h.run_until(START + timedelta(days=20))
    st = h.statuses(o.order_id)
    assert "shipped" not in st and st[-1] == "refunded", st
    order = h.world.store.get("web_order", o.order_id)
    (dlv,) = [h.world.store.get("delivery", d) for d in order.delivery_ids]
    ms = _milestones(h, o.order_id)
    assert set(ms) == {"shipped"} and ms["shipped"].effective_at == dlv.shipped_at
