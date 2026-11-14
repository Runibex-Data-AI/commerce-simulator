"""Web shop: arrivals, baskets, pricing, payment, release to ERP, backorders,
cancellation, shipment and delivery hand-back.

Public API (wave 2 contract)::

    place_order(world, customer, lines, source, subscription_id=None) -> WebOrder

Event kinds owned here and their timing:

* ``web.new_order:{bucket}:{i}`` (arrival source ``web.new_customers``): NHPP of
  first-time customers, rate = demandplan.web_order_rate_per_day *
  new_customer_share / 24 * intraday factor * scenario factor.
* ``web.payment:{order_id}:{attempt}``: 5-90 s after placing; authorised with
  ``auth_rate``; a failure is retried (attempt 2, 3-60 min later) with
  ``retry_share`` and succeeds with ``retry_success``; otherwise
  ``payment_failed``.
* ``web.release:{order_id}``: lognormal(``release_delay_minutes``, 0.6) after
  authorisation: SalesDoc + SalesDocCreated, stock reservation, delivery for
  the reserved lines, backorder for the rest, maybe a customer cancellation.
* ``web.customer_cancel:{order_id}``: 5-180 min after release (probability
  ``cancel_before_pick``); succeeds only while every delivery is still open.
* ``web.backorder:{order_id}:{n}``: daily 14:30-15:30 local, up to
  ``backorder_max_days`` tries, then the remainder is cancelled.
* ``web.shipped:{delivery_id}`` (scheduled by fulfilment): shipped quantities,
  status, payment capture; schedules ``web.settle:{billing_id}`` two working
  days later (``billing.clear``).
* ``web.delivery_exception:{delivery_id}`` / ``web.delivery_outcome:{delivery_id}``
  (fulfilment): status; delivered -> repurchase intent, return decision,
  subscription offer; returned_to_sender / lost ->
  ``web.refund_undelivered:{delivery_id}`` 1-3 days later (refund + credit memo).
* ``web.cleanup:{order_id}:{n}``: 30 days after the final state, removes the
  order and its sales doc, deliveries, billing docs and returns from the store
  (rescheduled weekly while a return or refund is still open).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..engine.queue import Event
from ..engine.registry import arrival_source, handler
from ..engine.rng import Rng
from ..models import arrivals, baskets, payments, seasonality
from ..models.pricing import split_gross
from .customers import cache, copy_address, lazy, pick
from .events import (
    PaymentRecorded,
    PaymentStatusChanged,
    SalesDocChanged,
    SalesDocCreated,
    WebOrderLinesChanged,
    WebOrderPlaced,
    WebOrderStatusChanged,
)
from .model import FREIGHT_ITEM_NO, Condition, Customer, OrderLine, Payment, SalesDoc, SalesItem, WebOrder

customers = lazy("customers")
demandplan = lazy("demandplan")
promotions = lazy("promotions")
masterdata = lazy("masterdata")
warehouse = lazy("warehouse")
fulfilment = lazy("fulfilment")
billing = lazy("billing")
returns = lazy("returns")
subscriptions = lazy("subscriptions")

SCENARIO_PROMO_BASE = 900_000
NEW_ORDER_LAM_MAX_FACTOR = 12.0
REPEAT_KEEP_PROB = 0.85
REPEAT_EXTRA_PROB = 0.45
REPEAT_EXTRA_MEAN = 0.5
SAME_METHOD_PROB = 0.78
CHEAP_CANDIDATES = 30
CLEANUP_AFTER_DAYS = 30
CLEANUP_RETRY_DAYS = 7
RELEASE_SIGMA = 0.6
FAIL_REASONS = {"declined": 0.55, "insufficient_funds": 0.25, "authentication_failed": 0.20}

# Statuses after which shipping updates no longer change the order status.
_LATE_STATUSES = frozenset(
    {
        "delivered", "returned_to_sender", "lost", "return_requested", "returned", "return_rejected",
        "partially_refunded", "refunded",
    }
)
_DEAD_STATUSES = frozenset({"cancelled", "payment_failed"})
# A parcel of the order did not arrive; later parcels of the same order still ship.
_UNDELIVERED = frozenset({"returned_to_sender", "lost"})


def _only_parcels_failed(o: WebOrder) -> bool:
    """The late status comes only from a parcel that did not arrive (and its refund).

    Then a later parcel of the same order (a backordered remainder) still moves
    the order on to 'shipped' and 'delivered'; a customer return keeps its status.
    """
    if o.status in _UNDELIVERED:
        return True
    return (
        o.status in ("partially_refunded", "refunded")
        and not o.return_ids
        and any(v in _UNDELIVERED for v in o.meta.get("outcomes", {}).values())
    )
_IN_FLIGHT = frozenset({"released", "partially_shipped", "shipped", "delivery_exception"})


# ---------------------------------------------------------------------------
# Arrivals of first-time customers
# ---------------------------------------------------------------------------


def new_customer_rate_per_hour(world, t: datetime) -> float:
    w = world.cfg.channels.web
    return (
        demandplan.web_order_rate_per_day(world, t)
        * w.new_customer_share
        / 24.0
        * seasonality.intraday_factor(world.cfg.seasonality, world.local(t))
        * world.scenarios.factor("web_new_customers", t)
    )


@arrival_source("web.new_customers")
def new_customer_arrivals(world, start: datetime, end: datetime):
    w = world.cfg.channels.web
    lam_max = w.base_orders_per_day * w.new_customer_share / 24.0 * NEW_ORDER_LAM_MAX_FACTOR
    out = []
    for t, i, bucket in arrivals.thinned_arrivals(
        world.seed, "web.new", start, end, lambda x: new_customer_rate_per_hour(world, x), lam_max
    ):
        out.append(Event(due=t, kind="web.new_order", key=f"web.new_order:{bucket}:{i}", data={}))
    return out


@handler("web.new_order")
def on_new_order(world, ev) -> None:
    rng = world.rng("web.new_order", ev.key)
    c = customers.new_customer(world, rng)
    lines = customers.cherry_pick(world, rng, draw_lines(world, rng, world.local_date(), c.country, topup=True))
    if lines:
        place_order(world, c, lines, source=c.acquisition)


# ---------------------------------------------------------------------------
# Baskets
# ---------------------------------------------------------------------------


def currency_of(country: str) -> str:
    return "CHF" if country == "CH" else "EUR"


def unit_price(sku, currency: str) -> float:
    return sku.rrp_chf if currency == "CHF" else sku.rrp_eur


def _weights(world, d: date) -> tuple[list[int], list[float]]:
    return demandplan.sku_weights(world, "web", d)


def _cheap_candidates(world, d: date, currency: str, sku_ids: list[int]) -> list[tuple[int, float]]:
    c = cache(world, "cheap")
    key = (d, currency, world.scenarios.version)
    hit = c.get(key)
    if hit is None:
        if len(c) > 64:
            c.clear()
        cat = world.ref.catalogue
        cands = sorted(((unit_price(cat.get(s), currency), s) for s in sku_ids))
        hit = [(s, p) for p, s in cands[:CHEAP_CANDIDATES]]
        c[key] = hit
    return hit


def free_shipping_promo_covers(world, sku_ids) -> bool:
    """True if an automatic free-shipping web promotion running now covers one of ``sku_ids``.

    Such a basket ships free whatever its value, so the shopper has no reason
    to top it up to the free-shipping threshold.
    """
    promos = [p for p in promotions.active_web_promos(world, world.now) if p.free_shipping and p.kind == "automatic"]
    if not promos:
        return False
    cat = world.ref.catalogue
    skus = [cat.get(int(s)) for s in sku_ids]
    return any(promotions.is_eligible(p, s) for p in promos for s in skus)


def _topup(world, rng: Rng, d: date, currency: str, lines: list[list[int]], sku_ids: list[int]) -> None:
    w = world.cfg.channels.web
    cat = world.ref.catalogue
    if free_shipping_promo_covers(world, [s for s, _ in lines]):
        return
    threshold = w.free_shipping_chf if currency == "CHF" else w.free_shipping_eur
    gross = round(sum(unit_price(cat.get(s), currency) * q for s, q in lines), 2)
    in_basket = {s for s, _ in lines}
    pool = [c for c in _cheap_candidates(world, d, currency, sku_ids) if c[0] not in in_basket]
    # Shoppers top up with something they want, not always the single cheapest
    # item: offer a few cheap SKUs drawn by today's (seasonal) weight, then take
    # the cheapest of those that clears the threshold.
    cands = _sample_by_weight(world, rng, d, pool, TOPUP_OFFER)
    add = baskets.threshold_topup(rng, gross, threshold, w.threshold_topup_prob, cands)
    if add is not None:
        lines.append([add[0], add[1]])


TOPUP_OFFER = 4


def _sample_by_weight(world, rng: Rng, d: date, pool: list, k: int) -> list:
    """Up to ``k`` distinct candidates from ``pool`` drawn by today's web SKU weight."""
    if len(pool) <= k:
        return pool
    wmap = _weight_map(world, d)
    items = list(pool)
    weights = [wmap.get(c[0], 0.0) for c in items]
    out = []
    for _ in range(k):
        total = sum(weights)
        if total <= 0:
            break
        x = rng.random() * total
        acc = 0.0
        for i, wt in enumerate(weights):
            acc += wt
            if x < acc:
                break
        out.append(items.pop(i))
        weights.pop(i)
    return out


def _weight_map(world, d: date) -> dict[int, float]:
    c = cache(world, "weight_map")
    key = (d, world.scenarios.version)
    m = c.get(key)
    if m is None:
        if len(c) > 8:
            c.clear()
        sku_ids, cum = _weights(world, d)
        m = {}
        prev = 0.0
        for sid, cw in zip(sku_ids, cum):
            m[sid] = cw - prev
            prev = cw
        c[key] = m
    return m


def draw_lines(world, rng: Rng, d: date, country: str = "DE", topup: bool = False) -> list[list[int]]:
    """A fresh basket ``[[sku_id, qty], ...]`` by popularity and affinity (optionally topped up)."""
    w = world.cfg.channels.web
    sku_ids, cum = _weights(world, d)
    if not sku_ids:
        return []
    drawn = baskets.draw_basket(
        rng, sku_ids, cum, world.ref.catalogue.affinity, w.basket_extra_lines_mean, w.qty_two_prob
    )
    lines = [[s, q] for s, q in drawn]
    if topup and lines:
        _topup(world, rng, d, currency_of(country), lines, sku_ids)
    return lines


def repeat_basket(world, rng: Rng, country: str, intent_lines: list[list[int]]) -> list[list[int]]:
    """Basket of a repeat order: intent SKUs (each kept with p 0.85, delisted ones
    replaced by the nearest active sibling) plus occasional extra lines."""
    d = world.local_date()
    cat = world.ref.catalogue
    w = world.cfg.channels.web
    sku_ids, cum = _weights(world, d)
    if not sku_ids:
        return []
    active = cache(world, "active_sets")
    key = (d, world.scenarios.version)
    act = active.get(key)
    if act is None:
        if len(active) > 16:
            active.clear()
        act = frozenset(sku_ids)
        active[key] = act
    lines: list[list[int]] = []
    seen: set[int] = set()
    for s, q in intent_lines:
        # Seasonal products are rebought far less out of season (nobody restocks
        # sun lotion in December): scale the keep probability by the SKU's
        # seasonal factor today, capped at 1.
        keep_p = REPEAT_KEEP_PROB * min(1.0, _season_today(world, cat.get(s).season_profile, d))
        if not rng.random() < keep_p:
            continue
        if s not in act:
            sib = [x.sku_id for x in cat.siblings(s, d) if x.sku_id in act]
            if not sib:
                continue
            s = sib[0]
        if s in seen:
            continue
        seen.add(s)
        lines.append([s, q])
    # A customer who keeps none of the intent SKUs still came back to shop:
    # the basket is then drawn from today's (seasonal) popularity below.
    if not lines or rng.random() < REPEAT_EXTRA_PROB:
        extra = baskets.draw_basket(rng, sku_ids, cum, cat.affinity, REPEAT_EXTRA_MEAN, w.qty_two_prob)
        for s, q in extra:
            if s not in seen:
                seen.add(s)
                lines.append([s, q])
    if lines:
        _topup(world, rng, d, currency_of(country), lines, sku_ids)
    return lines


def _season_today(world, profile: str, d: date) -> float:
    c = cache(world, "season_factor")
    key = (profile, d)
    v = c.get(key)
    if v is None:
        if len(c) > 64:
            c.clear()
        v = c[key] = seasonality.category_factor(profile, d)
    return v


# ---------------------------------------------------------------------------
# Placing orders
# ---------------------------------------------------------------------------


def _scenario_window_id(world, promo) -> str | None:
    c = cache(world, "scen_promo_windows")
    ver = world.scenarios.version
    if c.get("ver") != ver:
        m = {}
        for spec in world.scenarios.scenario_promotions() or []:
            if "index" in spec:
                m[int(spec["index"])] = spec.get("window_id")
        c["ver"], c["map"] = ver, m
    wid = c["map"].get(promo.promo_id - SCENARIO_PROMO_BASE)
    if wid is None and promo.scenario_id:
        ws = world.scenarios.active(promo.scenario_id, world.now)
        if ws:
            wid = ws[0].window_id
    return wid


def place_order(
    world, customer: Customer, lines, source: str, subscription_id: int | None = None
) -> WebOrder:
    """Price and place a web order for ``lines`` ``[(sku_id, qty), ...]``.

    Gross prices in the order currency (rrp_eur, rrp_chf for CH), best web
    promotion for the basket, shipping fee below the free-shipping threshold
    (waived by free-shipping promotions). Emits ``WebOrderPlaced`` and
    schedules the payment.
    """
    cfg = world.cfg
    w = cfg.channels.web
    c = world.store.mut("customer", customer.customer_id)
    customers.order_opened(c)
    now = world.now
    oid = world.next_id("web_order")
    rng = world.rng("web.order", oid)
    country = c.country
    currency = currency_of(country)
    vat = w.vat[country]
    cat = world.ref.catalogue

    merged: dict[int, int] = {}
    for s, q in lines:
        s, q = int(s), int(q)
        if q > 0:
            merged[s] = merged.get(s, 0) + q
    sku_ids = list(merged)
    promo = promotions.web_promo_for_basket(world, now, sku_ids, rng) if sku_ids else None

    olines: list[OrderLine] = []
    for i, s in enumerate(sku_ids, start=1):
        sku = cat.get(s)
        q = merged[s]
        price = unit_price(sku, currency)
        disc = pct = 0.0
        if promo is not None and promo.discount_pct > 0 and promotions.is_eligible(promo, sku):
            disc = round(price * q * promo.discount_pct, 2)
            pct = promo.discount_pct if disc > 0 else 0.0
        olines.append(
            OrderLine(line_no=i, sku_id=s, qty=q, unit_price=price, discount=disc, tax_rate=vat, discount_pct=pct)
        )
    goods = round(sum(ln.gross for ln in olines), 2)
    threshold = w.free_shipping_chf if currency == "CHF" else w.free_shipping_eur
    fee = w.shipping_fee_chf if currency == "CHF" else w.shipping_fee_eur
    nominal_fee = round(fee, 2) if goods < threshold else 0.0
    shipping, waived = nominal_fee, 0.0
    if promo is not None and promo.free_shipping and nominal_fee > 0:
        shipping, waived = 0.0, nominal_fee
    discount_total = round(sum(ln.discount for ln in olines), 2)
    applied = promo if promo is not None and (discount_total > 0 or waived > 0) else None

    is_repeat = c.orders > 0
    prev = c.meta.get("pm")
    if prev and rng.random() < SAME_METHOD_PROB:
        method = prev
    else:
        method = payments.draw_method(rng, cfg.payments.mix.get(country) or cfg.payments.mix["DE"])

    order = WebOrder(
        order_id=oid,
        customer_id=c.customer_id,
        country=country,
        currency=currency,
        placed_at=now,
        ship_to=copy_address(c.address),
        lines=olines,
        shipping_fee=shipping,
        payment_method=method,
        promo_id=applied.promo_id if applied is not None else None,
        subscription_id=subscription_id,
        is_repeat=is_repeat,
    )
    if applied is not None and applied.scenario_id:
        order.scenario_tags.append(applied.scenario_id)
    c.orders += 1
    c.last_order_at = now
    c.last_skus = list(sku_ids)
    c.meta["pm"] = method
    world.store.put("web_order", oid, order)
    world.emit(WebOrderPlaced(at=now, order=order, customer=c, source=source, promotion=applied))

    if applied is not None and applied.scenario_id:
        world.scenarios.record_effect(
            world,
            applied.scenario_id,
            _scenario_window_id(world, applied),
            "web_order",
            oid,
            "promo_discount",
            discount_total,
            detail={
                "promo_id": applied.promo_id,
                "discount_pct": applied.discount_pct,
                "free_shipping_waived": waived,
                "goods_gross": goods,
                "currency": currency,
                "eligible_lines": sum(1 for ln in olines if ln.discount > 0),
                "deal_seeker": bool(c.meta.get("deal")),
                **contribution_estimate(world, order, waived),
            },
        )

    world.schedule(
        "web.payment",
        now + timedelta(seconds=5 + rng.random() * 85),
        f"web.payment:{oid}:1",
        {"order_id": oid, "attempt": 1},
    )
    return order


def expected_freight_eur(world, country: str) -> float:
    """Carrier-share-weighted parcel freight cost (EUR) for a country (what fulfilment will pay on average)."""
    num = den = 0.0
    for ccfg in world.cfg.carriers.parcel.values():
        share = float(ccfg.share.get(country, 0.0))
        if share > 0 and ccfg.cost_eur:
            num += share * float(ccfg.cost_eur.get(country, max(ccfg.cost_eur.values())))
            den += share
    return num / den if den > 0 else 0.0


def contribution_estimate(world, order: WebOrder, shipping_waived: float) -> dict[str, float]:
    """Contribution of an order at the moment it is placed, in EUR (truth detail of promoted orders).

    ``net_revenue_eur`` (goods after discount plus the shipping fee charged,
    net of VAT), ``cogs_eur`` (standard cost), ``freight_eur`` (expected
    parcel cost for the country; the actual cost is in ``wms``/``erp`` once
    shipped), ``discount_eur`` and ``shipping_waived_eur`` (net of VAT) and
    ``contribution_eur`` = net revenue - cogs - freight, before returns.
    """
    d = world.local_date()
    fx = masterdata.fx_rate(world, order.currency, d)
    vat = world.cfg.channels.web.vat[order.country]
    goods_net = sum(split_gross(ln.gross, vat)[0] for ln in order.lines)
    ship_net = split_gross(order.shipping_fee, vat)[0]
    disc_net = sum(split_gross(ln.discount, vat)[0] for ln in order.lines)
    waived_net = split_gross(shipping_waived, vat)[0]
    cogs = sum(masterdata.std_cost(world, ln.sku_id, d) * ln.qty for ln in order.lines)
    freight = expected_freight_eur(world, order.country)
    net = (goods_net + ship_net) * fx
    return {
        "net_revenue_eur": round(net, 2),
        "cogs_eur": round(cogs, 2),
        "freight_eur": round(freight, 2),
        "discount_eur": round(disc_net * fx, 2),
        "shipping_waived_eur": round(waived_net * fx, 2),
        "contribution_eur": round(net - cogs - freight, 2),
    }


# ---------------------------------------------------------------------------
# Helpers shared with returns / subscriptions
# ---------------------------------------------------------------------------


def set_status(
    world, o: WebOrder, status: str, reason: str | None = None, at: datetime | None = None
) -> None:
    """Set and emit an order status (no event if unchanged). ``o`` must be mutable.

    ``at``: business time of the transition when it happened before now
    (``WebOrderStatusChanged.effective_at``).
    """
    if o.status == status:
        return
    o.status = status
    eff = at if at is not None and at != world.now else None
    world.emit(
        WebOrderStatusChanged(at=world.now, order_id=o.order_id, status=status, reason=reason, effective_at=eff)
    )


def line_gross(o: WebOrder, line_no: int, qty: int) -> float:
    """Gross value of ``qty`` units of an order line (discount pro rata)."""
    ln = o.lines[line_no - 1]
    if ln.qty <= 0:
        return 0.0
    return round(ln.unit_price * qty - ln.discount * qty / ln.qty, 2)


def invoice_of_delivery(world, o: WebOrder, delivery_id: int) -> int | None:
    inv = o.meta.get("inv", {}).get(str(delivery_id))
    if inv is not None:
        return int(inv)
    d = world.store.get("delivery", delivery_id)
    return d.billing_id if d is not None else None


def open_work(o: WebOrder, delta: int, now: datetime | None = None) -> None:
    """Track open follow-up work (pending returns / refunds) that blocks cleanup.

    Closing work (``delta < 0``) with ``now`` records it as the order's latest
    state change; the cleanup waits ``CLEANUP_AFTER_DAYS`` after it.
    """
    o.meta["open"] = max(0, int(o.meta.get("open", 0)) + delta)
    if delta < 0 and now is not None:
        o.meta["last_close"] = now.isoformat()


def schedule_settlement(world, billing_id: int, amount: float, rng: Rng) -> None:
    cal = world.ref.calendar
    d = cal.add_working_days(world.local_date(), 2, "DE")
    t = world.at_local(d, 9, int(rng.random() * 60)) + timedelta(hours=rng.random() * 7)
    key = f"web.settle:{billing_id}"
    if t <= world.now:
        t = world.now + timedelta(hours=1)
    if not world.is_scheduled(key):
        world.schedule("web.settle", t, key, {"billing_id": billing_id, "amount": round(amount, 2)})


def _deliveries_final(o: WebOrder) -> bool:
    outcomes = o.meta.get("outcomes", {})
    cancelled = o.meta.get("cancelled_dlv", [])
    return all(str(d) in outcomes or d in cancelled for d in o.delivery_ids)


def maybe_final(world, o: WebOrder) -> None:
    """Schedule the cleanup once the order reached a final state."""
    if o.meta.get("cleanup"):
        return
    final = o.status in _DEAD_STATUSES or (bool(o.delivery_ids) and not o.backorder and _deliveries_final(o))
    if not final:
        return
    o.meta["cleanup"] = 1
    world.schedule(
        "web.cleanup",
        world.now + timedelta(days=CLEANUP_AFTER_DAYS),
        f"web.cleanup:{o.order_id}:1",
        {"order_id": o.order_id, "n": 1},
    )


def _void_payment(world, o: WebOrder) -> None:
    pid = o.meta.get("auth_payment")
    if pid is not None and not o.meta.get("voided"):
        o.meta["voided"] = True
        world.emit(PaymentStatusChanged(at=world.now, payment_id=int(pid), status="voided", amount=None))


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


def _provider_ref(world, payment_id: int, method: str) -> str:
    r = world.rng("web.provider_ref", payment_id)
    prefix = "KS" if method == "invoice_bnpl" else "PL"
    return f"{prefix}-{r.getrandbits(40):010X}"


@handler("web.payment")
def on_payment(world, ev) -> None:
    oid = int(ev.data["order_id"])
    attempt = int(ev.data["attempt"])
    o = world.store.get("web_order", oid)
    if o is None or o.status != "created":
        return
    o = world.store.mut("web_order", oid)
    pcfg = world.cfg.payments
    rng = world.rng("web.payment", oid, attempt)
    ok = rng.random() < (pcfg.auth_rate if attempt == 1 else pcfg.retry_success)
    pid = world.next_id("payment")
    o.payment_attempts = attempt
    o.payment_ids.append(pid)
    pay = Payment(
        payment_id=pid,
        order_id=oid,
        attempt=attempt,
        method=o.payment_method,
        amount=o.total,
        currency=o.currency,
        status="authorised" if ok else "pending",
        created_at=world.now,
        provider_ref=_provider_ref(world, pid, o.payment_method),
    )
    world.emit(PaymentRecorded(at=world.now, payment=pay))
    if ok:
        o.meta["auth_payment"] = pid
        set_status(world, o, "authorised")
        delay = min(rng.lognormal_median(world.cfg.fulfilment.release_delay_minutes, RELEASE_SIGMA), 360.0)
        world.schedule(
            "web.release", world.now + timedelta(minutes=max(1.0, delay)), f"web.release:{oid}", {"order_id": oid}
        )
        return
    world.emit(PaymentStatusChanged(at=world.now, payment_id=pid, status="failed", reason=pick(rng, FAIL_REASONS)))
    if attempt == 1 and rng.random() < pcfg.retry_share:
        world.schedule(
            "web.payment",
            world.now + timedelta(minutes=3 + rng.random() * 57),
            f"web.payment:{oid}:2",
            {"order_id": oid, "attempt": 2},
        )
        return
    set_status(world, o, "payment_failed", reason="payment_failed")
    maybe_final(world, o)


# ---------------------------------------------------------------------------
# Release to ERP and warehouse
# ---------------------------------------------------------------------------


def _sales_item(world, o: WebOrder, ln: OrderLine, d: date) -> SalesItem:
    vat = ln.tax_rate
    list_gross = round(ln.unit_price * ln.qty, 2)
    list_net, _ = split_gross(list_gross, vat)
    net, tax = split_gross(ln.gross, vat)
    unit_net, _ = split_gross(ln.unit_price, vat)
    conds = [Condition("list_price", unit_net, list_net)]
    if ln.discount > 0:
        # The condition rate is the promotion's nominal rate (like a condition
        # record); the amount is the cent-rounded discount actually granted.
        pct = ln.discount_pct or (ln.discount / list_gross if list_gross else 0.0)
        conds.append(Condition("promo_discount", round(pct, 4), round(net - list_net, 2)))
    cost = round(masterdata.std_cost(world, ln.sku_id, d) * ln.qty, 2)
    return SalesItem(
        item_no=ln.line_no * 10,
        sku_id=ln.sku_id,
        qty=ln.qty,
        confirmed_qty=ln.qty,
        net_value=net,
        tax=tax,
        cost=cost,
        conditions=conds,
    )


def _planned_ship_date(world) -> date:
    wh = world.cfg.company.warehouse
    cal = world.ref.calendar
    loc = world.local()
    d = loc.date()
    if cal.is_working_day(d, wh.country, wh.region) and loc.hour < 8:
        return d
    return cal.next_working_day(d, wh.country, wh.region)


def _create_delivery(world, o: WebOrder, doc: SalesDoc, lines: list[tuple[int, int, int]]) -> None:
    acct = world.ref.accounts.web_collective(o.country)
    dlv = fulfilment.create_delivery(
        world,
        "web",
        doc,
        lines,
        acct.account_id,
        o.ship_to,
        None,
        _planned_ship_date(world),
        o.order_id,
        len(o.delivery_ids) + 1,
    )
    o.delivery_ids.append(dlv.delivery_id)


@handler("web.release")
def on_release(world, ev) -> None:
    oid = int(ev.data["order_id"])
    o = world.store.get("web_order", oid)
    if o is None or o.status != "authorised":
        return
    o = world.store.mut("web_order", oid)
    now = world.now
    d = world.local_date()
    acct = world.ref.accounts.web_collective(o.country)
    items = [_sales_item(world, o, ln, d) for ln in o.lines]
    vat = world.cfg.channels.web.vat[o.country]
    ship_net, ship_tax = split_gross(o.shipping_fee, vat)
    for it in items:
        it.conditions.append(Condition("tax", o.lines[it.item_no // 10 - 1].tax_rate, it.tax))
    if ship_net > 0 and items:
        # The shipping charge is a freight item of its own (non-stock service
        # material), so item, header, pricing and invoice values all agree.
        items.append(
            SalesItem(
                item_no=FREIGHT_ITEM_NO,
                sku_id=0,
                qty=1,
                confirmed_qty=1,
                net_value=ship_net,
                tax=ship_tax,
                cost=0.0,
                conditions=[Condition("shipping", ship_net, ship_net), Condition("tax", vat, ship_tax)],
                item_type="freight",
            )
        )
    sid = world.next_id("sales_doc")
    doc = SalesDoc(
        sales_doc_id=sid,
        doc_type="web",
        sold_to=acct.account_id,
        ship_to=acct.account_id,
        currency=o.currency,
        fx_rate_eur=masterdata.fx_rate(world, o.currency, d),
        created_at=now,
        requested_date=d,
        external_ref=str(oid),
        items=items,
        channel="web",
        order_id=oid,
        shipping_net=ship_net,
        shipping_tax=ship_tax,
    )
    world.store.put("sales_doc", sid, doc)
    world.emit(SalesDocCreated(at=now, doc=doc))
    o.sales_doc_id = sid
    o.released_at = now
    set_status(world, o, "released")

    reserved: list[tuple[int, int, int]] = []
    for ln in o.lines:
        granted = int(warehouse.reserve(world, ln.sku_id, ln.qty))
        if granted > 0:
            reserved.append((ln.sku_id, granted, ln.line_no * 10))
        if granted < ln.qty:
            o.backorder[ln.line_no] = ln.qty - granted
    if reserved:
        _create_delivery(world, o, doc, reserved)
    rng = world.rng("web.release", oid)
    if o.backorder:
        _schedule_backorder(world, o, 1, rng)
    if rng.random() < world.cfg.fulfilment.cancel_before_pick:
        world.schedule(
            "web.customer_cancel",
            now + timedelta(minutes=5 + rng.random() * 175),
            f"web.customer_cancel:{oid}",
            {"order_id": oid},
        )


def _schedule_backorder(world, o: WebOrder, n: int, rng: Rng) -> None:
    d = world.local_date() + timedelta(days=1)
    t = world.at_local(d, 14, 30) + timedelta(minutes=rng.random() * 60)
    world.schedule("web.backorder", t, f"web.backorder:{o.order_id}:{n}", {"order_id": o.order_id, "n": n})


@handler("web.backorder")
def on_backorder(world, ev) -> None:
    oid = int(ev.data["order_id"])
    n = int(ev.data["n"])
    o = world.store.get("web_order", oid)
    if o is None or not o.backorder or o.status in _DEAD_STATUSES:
        return
    o = world.store.mut("web_order", oid)
    doc = world.store.mut("sales_doc", o.sales_doc_id)
    reserved: list[tuple[int, int, int]] = []
    for line_no in sorted(o.backorder):
        need = o.backorder[line_no]
        ln = o.lines[line_no - 1]
        granted = int(warehouse.reserve(world, ln.sku_id, need))
        if granted > 0:
            reserved.append((ln.sku_id, granted, line_no * 10))
            if granted >= need:
                del o.backorder[line_no]
            else:
                o.backorder[line_no] = need - granted
    if reserved:
        _create_delivery(world, o, doc, reserved)
    if not o.backorder:
        return
    if n < world.cfg.fulfilment.backorder_max_days:
        _schedule_backorder(world, o, n + 1, world.rng("web.backorder", oid, n))
        return
    _cancel_backorder(world, o, doc)


def _freight_item(doc: SalesDoc) -> SalesItem | None:
    for it in doc.items:
        if it.item_type == "freight":
            return it
    return None


def _last_shipment_times(world, o: WebOrder) -> tuple[datetime | None, datetime | None]:
    """Latest goods issue and latest successful delivery over the order's deliveries."""
    shipped = delivered = None
    for did in o.delivery_ids:
        d = world.store.get("delivery", did)
        if d is None:
            continue
        if d.shipped_at is not None and (shipped is None or d.shipped_at > shipped):
            shipped = d.shipped_at
        if d.outcome == "delivered" and d.delivered_at is not None and (delivered is None or d.delivered_at > delivered):
            delivered = d.delivered_at
    return shipped, delivered


def _cancel_backorder(world, o: WebOrder, doc: SalesDoc) -> None:
    """Cancel the quantities still waiting for stock.

    Kept items get the reduced confirmed quantity together with their
    delivered/billed quantities (so the ERP item statuses are recomputed) and
    the header statuses are refreshed: a document whose shipped part is
    delivered and billed is completed now. If nothing shipped, the whole
    document (freight item included) is rejected.
    """
    line_changes: dict[int, dict[str, Any]] = {}
    item_changes: dict[int, dict[str, Any]] = {}
    by_no = {it.item_no: it for it in doc.items}
    for line_no in sorted(o.backorder):
        rem = o.backorder[line_no]
        ln = o.lines[line_no - 1]
        ln.cancelled_qty += rem
        line_changes[line_no] = {"cancelled_qty": ln.cancelled_qty}
        item = by_no[line_no * 10]
        kept = ln.qty - ln.cancelled_qty
        if kept > 0:
            item.confirmed_qty = kept
            item_changes[item.item_no] = {
                "confirmed_qty": kept,
                "delivered_qty": item.delivered_qty,
                "billed_qty": item.billed_qty,
            }
        else:
            item.confirmed_qty = 0
            item.rejection_reason = "out_of_stock"
            item_changes[item.item_no] = {"confirmed_qty": 0, "rejection_reason": "out_of_stock"}
    o.backorder = {}
    nothing = not o.delivery_ids
    if nothing:
        fi = _freight_item(doc)
        if fi is not None:
            fi.confirmed_qty = 0
            fi.rejection_reason = "out_of_stock"
            item_changes[fi.item_no] = {"confirmed_qty": 0, "rejection_reason": "out_of_stock"}
        doc.status = "rejected"
        header: dict[str, Any] = {"status": "rejected"}
    else:
        header = billing.refresh_header(doc)
    world.emit(WebOrderLinesChanged(at=world.now, order_id=o.order_id, lines=line_changes))
    world.emit(SalesDocChanged(at=world.now, sales_doc_id=doc.sales_doc_id, header=header, items=item_changes))
    if nothing:
        set_status(world, o, "cancelled", reason="out_of_stock")
        _void_payment(world, o)
        maybe_final(world, o)
        return
    # While a backorder was outstanding the order could not reach 'shipped'
    # (web.shipped and the delivery outcomes keep it 'partially_shipped'), so
    # the complete shipment is recognised now, effective at the last goods
    # issue; 'delivered' is effective at the last successful delivery.
    shipped_t, delivered_t = _last_shipment_times(world, o)
    if _deliveries_final(o):
        if "delivered" in o.meta.get("outcomes", {}).values() and o.status in _IN_FLIGHT:
            set_status(world, o, "shipped", at=shipped_t)
            set_status(world, o, "delivered", at=delivered_t)
    elif o.status == "partially_shipped" and all(
        ln.shipped_qty + ln.cancelled_qty >= ln.qty for ln in o.lines
    ):
        set_status(world, o, "shipped", at=shipped_t)
    maybe_final(world, o)


@handler("web.customer_cancel")
def on_customer_cancel(world, ev) -> None:
    oid = int(ev.data["order_id"])
    o = world.store.get("web_order", oid)
    if o is None or o.status != "released":
        return
    dels = [world.store.get("delivery", d) for d in o.delivery_ids]
    if any(d is None or d.status != "open" for d in dels):
        return  # too late: already picked
    o = world.store.mut("web_order", oid)
    for d in dels:
        if not fulfilment.cancel_delivery(world, d.delivery_id, "customer_cancelled"):
            return  # cannot happen after the status check; leave the order untouched
        o.meta.setdefault("cancelled_dlv", []).append(d.delivery_id)
    o.backorder = {}
    doc = world.store.mut("sales_doc", o.sales_doc_id)
    line_changes: dict[int, dict[str, Any]] = {}
    item_changes: dict[int, dict[str, Any]] = {}
    for ln, item in zip(o.lines, doc.items):
        ln.cancelled_qty = ln.qty
        line_changes[ln.line_no] = {"cancelled_qty": ln.qty}
        item.rejection_reason = "customer_cancelled"
        item_changes[item.item_no] = {"rejection_reason": "customer_cancelled"}
    fi = _freight_item(doc)
    if fi is not None:
        fi.rejection_reason = "customer_cancelled"
        item_changes[fi.item_no] = {"rejection_reason": "customer_cancelled"}
    doc.status = "rejected"
    world.emit(WebOrderLinesChanged(at=world.now, order_id=oid, lines=line_changes))
    world.emit(
        SalesDocChanged(at=world.now, sales_doc_id=doc.sales_doc_id, header={"status": "rejected"}, items=item_changes)
    )
    set_status(world, o, "cancelled", reason="customer_cancelled")
    _void_payment(world, o)
    maybe_final(world, o)


# ---------------------------------------------------------------------------
# Shipment, settlement and delivery outcomes (handed back by fulfilment)
# ---------------------------------------------------------------------------


@handler("web.shipped")
def on_shipped(world, ev) -> None:
    did = int(ev.data["delivery_id"])
    dlv = world.store.get("delivery", did)
    if dlv is None or dlv.order_id is None:
        return
    o = world.store.get("web_order", dlv.order_id)
    if o is None:
        return
    o = world.store.mut("web_order", dlv.order_id)
    changes: dict[int, dict[str, Any]] = {}
    for dl in dlv.lines:
        line_no = dl.sales_item_no // 10
        ln = o.lines[line_no - 1]
        ln.shipped_qty += dl.qty
        changes[line_no] = {"shipped_qty": ln.shipped_qty}
    world.emit(WebOrderLinesChanged(at=world.now, order_id=o.order_id, lines=changes))
    complete = not o.backorder and all(ln.shipped_qty + ln.cancelled_qty >= ln.qty for ln in o.lines)
    # An earlier parcel returned to sender or lost (and refunded) does not stop
    # the rest of the order: shipping the remainder makes the order (partially)
    # shipped again, so it reaches 'shipped' before a later 'delivered'.
    if _only_parcels_failed(o) or (o.status not in _LATE_STATUSES and o.status not in _DEAD_STATUSES):
        set_status(world, o, "shipped" if complete else "partially_shipped")

    rng = world.rng("web.shipped", did)
    bid = dlv.billing_id
    amount = 0.0
    bill = world.store.get("billing", bid) if bid is not None else None
    if bill is not None:
        amount = round(bill.net_value + bill.tax, 2)
        o.billing_ids.append(bid)
        o.meta.setdefault("inv", {})[str(did)] = bid
    else:
        amount = sum(line_gross(o, dl.sales_item_no // 10, dl.qty) for dl in dlv.lines)
        if dlv.seq_in_order == 1:
            amount += o.shipping_fee
        amount = round(amount, 2)
    o.captured_total = round(o.captured_total + amount, 2)
    pid = o.meta.get("auth_payment")
    if pid is not None:
        world.emit(PaymentStatusChanged(at=world.now, payment_id=int(pid), status="captured", amount=o.captured_total))
    if bill is not None:
        schedule_settlement(world, bid, amount, rng)


@handler("web.settle")
def on_settle(world, ev) -> None:
    bid = int(ev.data["billing_id"])
    if world.store.get("billing", bid) is None:
        return
    billing.clear(world, bid, ev.data.get("amount"))


@handler("web.delivery_exception")
def on_delivery_exception(world, ev) -> None:
    did = int(ev.data["delivery_id"])
    dlv = world.store.get("delivery", did)
    if dlv is None or dlv.order_id is None:
        return
    o = world.store.get("web_order", dlv.order_id)
    if o is None or o.status not in ("shipped", "partially_shipped"):
        return
    o = world.store.mut("web_order", dlv.order_id)
    set_status(world, o, "delivery_exception")


@handler("web.delivery_outcome")
def on_delivery_outcome(world, ev) -> None:
    did = int(ev.data["delivery_id"])
    dlv = world.store.get("delivery", did)
    if dlv is None or dlv.order_id is None:
        return
    outcome = ev.data.get("outcome") or dlv.outcome
    o = world.store.get("web_order", dlv.order_id)
    if o is None:
        return
    o = world.store.mut("web_order", dlv.order_id)
    outcomes = o.meta.setdefault("outcomes", {})
    if str(did) in outcomes:
        return
    outcomes[str(did)] = outcome
    all_final = not o.backorder and _deliveries_final(o)
    if outcome == "delivered":
        first = not o.meta.get("first_delivered")
        if first:
            o.meta["first_delivered"] = True
            o.delivered_at = dlv.delivered_at or world.now
        if all_final:
            if _only_parcels_failed(o) or o.status not in (
                "return_requested", "returned", "return_rejected", "partially_refunded", "refunded"
            ):
                set_status(world, o, "delivered")
        elif o.status == "delivery_exception":
            set_status(world, o, "partially_shipped" if o.backorder else "shipped")
        c = world.store.get("customer", o.customer_id)
        if c is not None:
            if first:
                customers.schedule_repurchase(world, c, o)
                if o.is_repeat and o.subscription_id is None:
                    subscriptions.maybe_start_subscription(world, c, o)
            returns.maybe_schedule_return(world, o, dlv)
    elif outcome in ("returned_to_sender", "lost"):
        if o.status not in ("partially_refunded", "refunded"):
            set_status(world, o, outcome)
        open_work(o, +1)
        rng = world.rng("web.undelivered", did)
        days = 1 + int(rng.random() * 3)
        t = world.at_local(world.local_date() + timedelta(days=days), 9) + timedelta(hours=rng.random() * 8)
        world.schedule(
            "web.refund_undelivered", t, f"web.refund_undelivered:{did}", {"delivery_id": did, "outcome": outcome}
        )
    maybe_final(world, o)


@handler("web.refund_undelivered")
def on_refund_undelivered(world, ev) -> None:
    did = int(ev.data["delivery_id"])
    outcome = ev.data.get("outcome", "lost")
    dlv = world.store.get("delivery", did)
    if dlv is None or dlv.order_id is None:
        return
    o = world.store.get("web_order", dlv.order_id)
    if o is None:
        return
    o = world.store.mut("web_order", dlv.order_id)
    items = [(dl.sales_item_no, dl.qty) for dl in dlv.lines if dl.qty > 0]
    first = dlv.seq_in_order == 1
    amount = sum(line_gross(o, dl.sales_item_no // 10, dl.qty) for dl in dlv.lines)
    if first:
        amount += o.shipping_fee
    returns.refund(
        world, o, round(amount, 2), outcome, credit_items=items,
        include_shipping=first and o.shipping_fee > 0, delivery_id=did,
    )
    open_work(o, -1, world.now)
    maybe_final(world, o)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


@handler("web.cleanup")
def on_cleanup(world, ev) -> None:
    oid = int(ev.data["order_id"])
    n = int(ev.data["n"])
    o = world.store.get("web_order", oid)
    if o is None:
        return
    retry_at = None
    if int(o.meta.get("open", 0)) > 0:
        retry_at = world.now + timedelta(days=CLEANUP_RETRY_DAYS)
    elif o.meta.get("last_close"):
        due = datetime.fromisoformat(o.meta["last_close"]) + timedelta(days=CLEANUP_AFTER_DAYS)
        if due > world.now:
            retry_at = due
    if retry_at is not None:
        world.schedule("web.cleanup", retry_at, f"web.cleanup:{oid}:{n + 1}", {"order_id": oid, "n": n + 1})
        return
    store = world.store
    for did in o.delivery_ids:
        store.remove("delivery", did)
    for bid in o.billing_ids:
        store.remove("billing", bid)
    for rid in o.return_ids:
        store.remove("web_return", rid)
    if o.sales_doc_id is not None:
        store.remove("sales_doc", o.sales_doc_id)
    for pid in o.payment_ids:
        store.remove("payment", pid)
    store.remove("web_order", oid)
    customers.order_closed(world, o.customer_id)  # may evict a customer who can never act again
