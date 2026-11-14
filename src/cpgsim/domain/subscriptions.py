"""Web subscriptions: start, recurring orders, pause and cancellation.

Public API (wave 2 contract)::

    maybe_start_subscription(world, customer, order) -> None

A customer decides once, at the delivery of their first repeat order, whether
to subscribe (``subscription_rate``, i.e. "about 8 % of repeat customers").
Strongly seasonal products (season profiles ``gift``, ``sun``, ``after_sun``)
never go on a subscription; a customer who decides to subscribe but whose
order holds only such products subscribes at a later repeat order instead.
The warm start skips them too.
Subscribers get no repurchase intents while the subscription is active or
paused; a cancelled subscription ends the relationship (the calibration in
``customers.repeat_calibration`` counts it that way).

Event kinds owned here and their timing:

* ``sub.order:{subscription_id}:{n}``: every ``interval_weeks`` at 06:00-08:00
  local -> ``web.place_order(..., source="subscription")``. After each order the
  subscription is cancelled with ``subscription_cancel_prob`` or paused with
  ``subscription_pause_prob`` (one or two intervals, the next order resumes it),
  otherwise the next order is planned. ``SubscriptionChanged`` always carries
  the current ``next_order_at``.

Warm start (bootstrap order 41): the stationary number of active
subscriptions implied by config (``repeat_calibration()["sub_stationary"]``),
held by additional pre-existing customers, so subscription orders run at
steady state from the first day.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..engine.registry import bootstrap, handler
from ..engine.rng import Rng
from .customers import calibration, lazy
from .events import SubscriptionChanged, SubscriptionCreated
from .model import Customer, Subscription, WebOrder

web = lazy("web")
customers = lazy("customers")

MAX_SUB_LINES = 3
PREFERRED_INTERVAL_PROB = 0.7
# Strongly seasonal products are never put on a subscription: nobody has gift
# sets or sun lotion delivered every few weeks all year round.
NO_SUBSCRIPTION_PROFILES = frozenset({"gift", "sun", "after_sun"})


def subscribable(world, sku_id: int) -> bool:
    """True if the SKU may be part of a subscription (not strongly seasonal)."""
    return world.ref.catalogue.get(sku_id).season_profile not in NO_SUBSCRIPTION_PROFILES


def _next_order_time(world, after: datetime, weeks: int, rng: Rng) -> datetime:
    d = world.local_date(after) + timedelta(weeks=weeks)
    t = world.at_local(d, 6) + timedelta(minutes=rng.random() * 120)
    if t <= world.now:
        t = world.now + timedelta(hours=1 + rng.random() * 2)
    return t


def _choose_interval(world, rng: Rng, sku_ids: list[int], qtys: list[int]) -> int:
    choices = sorted(world.cfg.customers.subscription_intervals or [8])
    cat = world.ref.catalogue
    usage_weeks = sum(cat.get(s).usage_days * q for s, q in zip(sku_ids, qtys)) / max(1, len(sku_ids)) / 7.0
    preferred = min(choices, key=lambda w: (abs(w - usage_weeks), w))
    if rng.random() < PREFERRED_INTERVAL_PROB:
        return preferred
    return choices[min(int(rng.random() * len(choices)), len(choices) - 1)]


def _create(
    world,
    c: Customer,
    sku_ids: list[int],
    qtys: list[int],
    interval: int,
    created_at: datetime,
    next_at: datetime,
    orders_placed: int = 0,
) -> Subscription:
    sid = world.next_id("subscription")
    sub = Subscription(
        subscription_id=sid,
        customer_id=c.customer_id,
        sku_ids=list(sku_ids),
        qtys=list(qtys),
        interval_weeks=interval,
        status="active",
        created_at=created_at,
        next_order_at=next_at,
        orders_placed=orders_placed,
    )
    world.store.put("subscription", sid, sub)
    c.subscription_id = sid
    world.emit(SubscriptionCreated(at=world.now, subscription=sub))
    n = orders_placed + 1
    world.schedule("sub.order", next_at, f"sub.order:{sid}:{n}", {"subscription_id": sid, "n": n})
    return sub


def maybe_start_subscription(world, customer: Customer, order: WebOrder) -> None:
    """One-off subscription decision at the first delivered repeat order."""
    c = world.store.mut("customer", customer.customer_id)
    if c.meta.get("sub_decided") or c.subscription_id is not None:
        return
    rng = world.rng("sub.decide", c.customer_id)
    if rng.random() >= world.cfg.customers.subscription_rate:
        c.meta["sub_decided"] = True
        return
    lines = [
        (ln.sku_id, ln.qty) for ln in order.lines if ln.qty - ln.cancelled_qty > 0 and subscribable(world, ln.sku_id)
    ]
    if not lines:
        # Only seasonal products in this order: the customer who wants a
        # subscription takes it at a later repeat order (same decision draw).
        return
    c.meta["sub_decided"] = True
    lines = sorted(lines, key=lambda x: (-world.ref.catalogue.get(x[0]).popularity, x[0]))[:MAX_SUB_LINES]
    sku_ids = [s for s, _ in lines]
    qtys = [q for _, q in lines]
    interval = _choose_interval(world, rng, sku_ids, qtys)
    _create(world, c, sku_ids, qtys, interval, world.now, _next_order_time(world, world.now, interval, rng))


def _current_lines(world, sub: Subscription) -> list[list[int]]:
    """Subscription lines for today; delisted SKUs are swapped for the nearest active sibling."""
    d = world.local_date()
    cat = world.ref.catalogue
    out: list[list[int]] = []
    seen: set[int] = set()
    for s, q in zip(sub.sku_ids, sub.qtys):
        sku = cat.get(s)
        if not cat.is_active(sku, d):
            sib = cat.siblings(s, d)
            if not sib:
                continue
            s = sib[0].sku_id
        if s not in seen:
            seen.add(s)
            out.append([s, q])
    return out


@handler("sub.order")
def on_sub_order(world, ev) -> None:
    sid = int(ev.data["subscription_id"])
    n = int(ev.data["n"])
    sub = world.store.get("subscription", sid)
    if sub is None or sub.status == "cancelled" or sub.orders_placed + 1 != n:
        return
    sub = world.store.mut("subscription", sid)
    c = world.store.get("customer", sub.customer_id)
    ccfg = world.cfg.customers
    rng = world.rng("sub.order", sid, n)
    lines = _current_lines(world, sub)
    if c is None or not lines:
        _cancel(world, sub, c)
        return
    if [s for s, _ in lines] != sub.sku_ids:
        sub.sku_ids = [s for s, _ in lines]
        sub.qtys = [q for _, q in lines]
    web.place_order(world, c, lines, source="subscription", subscription_id=sid)
    sub.orders_placed = n
    sub.paused_until = None
    x = rng.random()
    if x < ccfg.subscription_cancel_prob:
        _cancel(world, sub, c)
        return
    if x < ccfg.subscription_cancel_prob + ccfg.subscription_pause_prob:
        weeks = sub.interval_weeks * (2 + int(rng.random() * 2))
        nxt = _next_order_time(world, world.now, weeks, rng)
        sub.status = "paused"
        sub.paused_until = nxt
    else:
        nxt = _next_order_time(world, world.now, sub.interval_weeks, rng)
        sub.status = "active"
    sub.next_order_at = nxt
    world.emit(SubscriptionChanged(at=world.now, subscription_id=sid, status=sub.status, next_order_at=nxt))
    world.schedule("sub.order", nxt, f"sub.order:{sid}:{n + 1}", {"subscription_id": sid, "n": n + 1})


def _cancel(world, sub: Subscription, c: Customer | None) -> None:
    sub.status = "cancelled"
    sub.next_order_at = None
    sub.paused_until = None
    world.emit(
        SubscriptionChanged(at=world.now, subscription_id=sub.subscription_id, status="cancelled", next_order_at=None)
    )
    if c is not None and c.subscription_id == sub.subscription_id:
        c = world.store.mut("customer", c.customer_id)
        c.subscription_id = None
        c.lapsed = True  # churned with the subscription; customer record stays
    world.store.remove("subscription", sub.subscription_id)


# ---------------------------------------------------------------------------
# Warm start
# ---------------------------------------------------------------------------


@bootstrap(order=41)
def warm_start_subscriptions(world) -> None:
    """Pre-existing active subscriptions whose orders run at the stationary rate.

    Subscriptions are added until their order rate (sum of 1 / interval)
    reaches ``repeat_calibration()["sub_orders_per_day"]`` (corrected for
    pauses), so the result does not depend on how intervals are chosen.
    """
    cal = calibration(world)
    pause = world.cfg.customers.subscription_pause_prob
    target = cal["sub_orders_per_day"] / (1 + pause * 1.5)
    if target <= 0 or world.cfg.channels.web.initial_customers <= 0:
        return
    start = world.now
    d0 = world.local_date(start)
    rate = 0.0
    i = -1
    while rate < target and i < 1_000_000:
        i += 1
        rng = world.rng("sub.warm", i)
        lines = [ln for ln in web.draw_lines(world, rng, d0) if subscribable(world, ln[0])][:MAX_SUB_LINES]
        if not lines:
            continue
        sku_ids = [s for s, _ in lines]
        qtys = [q for _, q in lines]
        interval = _choose_interval(world, rng, sku_ids, qtys)
        rate += 1.0 / (7.0 * interval)
        # Age of the subscription: uniform over a typical lifetime (stationary renewal).
        life_days = cal["sub_orders"] * interval * 7.0
        age_days = rng.random() * min(life_days, 1400.0)
        sub_created = start - timedelta(days=max(1.0, age_days))
        cust_created = sub_created - timedelta(days=30 + rng.random() * 330)
        c = customers.new_customer(world, rng, created_at=cust_created)
        placed = int(age_days // (interval * 7))
        c.orders = 2 + placed + rng.poisson(1.0)
        c.meta["chain"] = True
        c.meta["sub_decided"] = True
        c.last_skus = list(sku_ids)
        ahead_days = rng.random() * interval * 7.0
        next_at = world.at_local(world.local_date(start + timedelta(days=ahead_days)), 6) + timedelta(
            minutes=rng.random() * 120
        )
        if next_at <= start:
            next_at = start + timedelta(hours=6 + rng.random() * 2)
        c.last_order_at = next_at - timedelta(weeks=interval)
        if c.last_order_at < sub_created:
            c.last_order_at = sub_created
        _create(world, c, sku_ids, qtys, interval, sub_created, next_at, orders_placed=placed)
