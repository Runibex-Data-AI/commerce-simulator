"""Web customers: registration, the warm-start cohort and repurchase intents.

Public API (wave 2 contract):

* ``new_customer(world, rng, acquisition=None) -> Customer``
* ``schedule_repurchase(world, customer, order)``

Event kinds owned here:

* ``cust.intent:{customer_id}:{intent_seq}``: a customer runs out of the
  products of an earlier order. Converts into a repeat order with
  ``models.repurchase.conversion_probability`` (decaying with the customer's
  cumulative missed intents); a miss schedules a retry after
  ``missed_retry_factor`` of the usage time; ``max_missed_intents`` misses lapse
  the customer. Only the latest intent of a customer is live (``intent_seq``).
* ``cust.deal_order:{bucket}:{i}`` (arrival source ``cust.deal_seekers``):
  first-time deal-site customers while a ``promo_margin_trap`` promotion with
  ``deal_seeker_share`` runs (see ``deal_basket``, ``deal_return_rate``,
  ``cherry_pick`` and the scenario module).

Existing customers respond to events and promotions (Black Friday week
``black_friday_lift``, Christmas run-up ``christmas_lift``, web promotions on
their products), so total web orders, not only first orders, carry the lifts:

* **Conversion**: the intent's conversion probability is multiplied by
  ``1 + EVENT_CONVERSION_ELASTICITY * (lift - 1)`` (at most
  ``EVENT_CONVERSION_CAP``), ``lift = demandplan.repeat_lift`` on the day the
  intent fires (``event_conversion_multiplier``).
* **Pull-forward**: when an intent is planned, every demand window that
  starts after today and at most ``PULL_FORWARD_DAYS`` before the intent date
  may move it into the window (``_pull_forward``, probability
  ``PULL_FORWARD_SHARE * (1 - 1 / lift)``). Decided from the pure window
  calendar at planning time, so it is restart-safe. A customer who buys early
  still has stock: the next intent is pushed back by the days bought early
  (``carry_days``), which, with the intents taken out of the following
  weeks, makes the post-event dip.

Calibrated on the default config and the reduced e2e config: Black Friday week
about 2.4-2.5x the three weeks before, 1-20 December about 1.55x
(``tests/e2e/test_statistics.py``). Subscriptions keep their fixed intervals.

Calibration (see ``repeat_calibration``): the repeat stream is a branching
process seeded by new customers. With the default intent parameters a customer
who enters the repurchase chain places about 3.3 further orders on average
(intents plus, for the 8 % who subscribe at their first repeat order, about
20 subscription orders). To make repeat + subscription orders equal
``(1 - new_customer_share) / new_customer_share`` per new customer, only a share
``chain_share`` of first-time buyers ever enter the chain (the rest are
one-time buyers: gift purchases, trials). The share is solved analytically from
config at start-up, so steady-state volume stays close to
``base_orders_per_day`` whatever the intent parameters.

Warm start (bootstrap order 40): ``initial_customers`` existing customers with
a pending intent drawn from the forward-recurrence distribution of the intent
delay, so the repeat stream is near steady state from the first day.
Subscriptions get their own warm start in ``subscriptions``.
"""

from __future__ import annotations

import importlib
import re
from datetime import date, datetime, timedelta
from typing import Any

from ..engine.queue import Event
from ..engine.registry import arrival_source, bootstrap, handler, on_load
from ..engine.rng import Rng
from ..models import arrivals, repurchase, seasonality
from . import people
from .events import CustomerRegistered
from .model import Address, Customer, WebOrder


# ---------------------------------------------------------------------------
# Lazy access to sibling modules (avoids import cycles; tests can stub them
# through sys.modules)
# ---------------------------------------------------------------------------


class _Lazy:
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(importlib.import_module(self._name), attr)


def lazy(module: str) -> _Lazy:
    """Proxy for ``cpgsim.domain.<module>`` resolved at attribute access."""
    return _Lazy(f"cpgsim.domain.{module}")


web = lazy("web")
demandplan = lazy("demandplan")
marketing = lazy("marketing")
chaos = _Lazy("cpgsim.chaos")

LOCALES = {"DE": "de_DE", "AT": "de_AT", "CH": "de_CH"}
INTENT_GROUP_DAYS = 14.0  # lines running out within this many days of the first go into one intent
MARKETING_OPT_IN = 0.46
PHONE_SHARE = 0.78
BIRTH_YEAR_SHARE = 0.62
WARM_HISTORY_DAYS = 730
WARM_SPREAD = 1.12  # stretch of the warm cohort's first intents (empirical: flat volume from day one)
# Response of existing customers to events and promotions (calibrated so total
# web orders hit black_friday_lift / christmas_lift; see on_intent, _pull_forward).
EVENT_CONVERSION_ELASTICITY = 1.2
EVENT_CONVERSION_CAP = 2.5
PULL_FORWARD_SHARE = 0.45
PULL_FORWARD_DAYS = 35


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def pick(rng: Rng, mix: dict[str, float]) -> str:
    """Weighted draw from a ``{key: weight}`` mix, keys in sorted order (one draw)."""
    keys = sorted(k for k, w in mix.items() if w > 0)
    total = sum(mix[k] for k in keys)
    x = rng.random() * total
    acc = 0.0
    for k in keys:
        acc += mix[k]
        if x < acc:
            return k
    return keys[-1]


def cache(world, name: str) -> dict:
    """Per-world scratch cache for values derived purely from config/reference data."""
    d = world.__dict__.setdefault("_web_domain_cache", {})
    return d.setdefault(name, {})


def b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n <= 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def unique_email(email: str, customer_id: int) -> str:
    """Make ``people.person`` e-mails unique: digits replaced by / suffix of the id in base 36."""
    local, _, domain = email.partition("@")
    suffix = b36(customer_id)
    if any(ch.isdigit() for ch in local):
        local = re.sub(r"\d+", "", local).rstrip("._") + suffix
    else:
        local = f"{local}.{suffix}"
    return f"{local}@{domain}"


def copy_address(a: Address) -> Address:
    return Address(a.country, a.region, a.postcode, a.city, a.street, a.house_number)


def hour_cum(world) -> list[float]:
    c = cache(world, "hour_cum")
    if "v" not in c:
        acc, out = 0.0, []
        for w in seasonality.hour_weights(world.cfg.seasonality):
            acc += w
            out.append(acc)
        c["v"] = out
    return c["v"]


def at_time_of_day(world, rng: Rng, d: date) -> datetime:
    """A local moment on date ``d`` with the hour drawn from the web hour-of-day weights."""
    h = rng.weighted_index(hour_cum(world))
    h = min(h, 23)
    return world.at_local(d, h, int(rng.random() * 60), int(rng.random() * 60))


def not_before_now(world, t: datetime, rng: Rng) -> datetime:
    if t <= world.now:
        return world.now + timedelta(minutes=5 + rng.random() * 55)
    return t


def has_active_subscription(world, c: Customer) -> bool:
    if c.subscription_id is None:
        return False
    sub = world.store.get("subscription", c.subscription_id)
    return sub is not None and sub.status in ("active", "paused")


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def repeat_calibration(cfg) -> dict[str, Any]:
    """Analytic calibration of the repeat stream (pure function of config).

    ``delta``: probability an order ends delivered (payment, cancellation and
    carrier losses), which is when the next intent is scheduled. Intent states
    ``m`` = missed intents so far (cumulative), ``p_m`` the conversion
    probability. ``H_m``: expected further orders of a customer with an intent
    pending at state m who has already decided against a subscription;
    ``G_m``: the same before the first repeat order (whose delivery triggers the
    one-off subscription decision, ``S`` = expected subscription orders).
    ``chain_share`` = target repeat orders per new customer / (delta * G_0).
    """
    c, w, p, f = cfg.customers, cfg.channels.web, cfg.payments, cfg.fulfilment
    s = min(max(w.new_customer_share, 0.01), 1.0)
    auth = p.auth_rate + (1 - p.auth_rate) * p.retry_share * p.retry_success
    rts = f.exception_rate * (1 - f.exception_redelivered_share)
    delta = auth * (1 - f.cancel_before_pick) * max(0.0, 1 - f.lost_rate - rts)
    big_m = max(1, c.max_missed_intents)
    pm = [repurchase.conversion_probability(c.repurchase_prob, c.repurchase_decay, m) for m in range(big_m)]
    sigma = c.subscription_rate
    cancel = max(c.subscription_cancel_prob, 0.02)
    sub_orders = 1.0 / cancel
    h = [0.0] * (big_m + 1)
    g = [0.0] * (big_m + 1)
    fr = [0.0] * (big_m + 1)
    for m in reversed(range(big_m)):
        h[m] = (pm[m] + (1 - pm[m]) * h[m + 1]) / max(1e-9, 1 - pm[m] * delta)
        g[m] = pm[m] * (1 + delta * (sigma * sub_orders + (1 - sigma) * h[m])) + (1 - pm[m]) * g[m + 1]
        fr[m] = pm[m] + (1 - pm[m]) * fr[m + 1]
    target = (1 - s) / s
    chain_share = 1.0 if delta * g[0] <= 0 else min(1.0, target / (delta * g[0]))
    # Stationary mix of pending intents by missed count (visit counts per chain).
    weights, reach = [], 1.0
    for m in range(big_m):
        weights.append(reach / max(1e-9, 1 - pm[m] * delta))
        reach *= (1 - pm[m]) / max(1e-9, 1 - pm[m] * delta)
    new_per_day = w.base_orders_per_day * s
    subs_per_day = new_per_day * chain_share * delta * fr[0] * delta * sigma
    intervals = c.subscription_intervals or [8]
    mean_interval_days = 7.0 * sum(intervals) / len(intervals)
    life_days = sub_orders * mean_interval_days * (1 + c.subscription_pause_prob * 1.5)
    return {
        "delta": delta,
        "p": pm,
        "chain_share": chain_share,
        "expected_chain_orders": delta * g[0],
        "target_repeat_per_new": target,
        "m_weights": weights,
        "subs_per_day": subs_per_day,
        "sub_orders": sub_orders,
        "sub_stationary": subs_per_day * life_days,
        "sub_orders_per_day": subs_per_day * sub_orders,
    }


def calibration(world) -> dict[str, Any]:
    c = cache(world, "calibration")
    if "v" not in c:
        c["v"] = repeat_calibration(world.cfg)
    return c["v"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def new_customer(
    world,
    rng: Rng,
    acquisition: str | None = None,
    *,
    created_at: datetime | None = None,
    country: str | None = None,
) -> Customer:
    """Register a new web customer (stores it and emits ``CustomerRegistered`` at ``world.now``).

    ``created_at`` (warm start only) back-dates the shop registration date.
    """
    cfg = world.cfg
    cid = world.next_id("customer")
    if country is None:
        country = pick(rng, cfg.channels.web.country_mix)
    if acquisition is None:
        acquisition = pick(rng, marketing.acquisition_mix(world, world.now))
    code = None
    if acquisition == "influencer":
        code = marketing.pick_influencer_code(world, rng, world.now)
    prng = world.rng("customer.person", cid)
    address = world.ref.geo.sample_address(prng, country)
    domains = cfg.customers.email_domains.get(country) or None
    person = people.person(prng.getrandbits(63), country, domains)
    c = Customer(
        customer_id=cid,
        country=country,
        address=address,
        created_at=created_at or world.now,
        acquisition=acquisition,
        influencer_code=code,
    )
    world.store.put("customer", cid, c)
    world.emit(
        CustomerRegistered(
            at=world.now,
            customer=c,
            first_name=person["first_name"],
            last_name=person["last_name"],
            email=unique_email(person["email"], cid),
            phone=person["phone"] if prng.random() < PHONE_SHARE else None,
            birth_year=person["birth_year"] if prng.random() < BIRTH_YEAR_SHARE else None,
            marketing_opt_in=prng.random() < MARKETING_OPT_IN,
            locale=LOCALES.get(country, "de_DE"),
            # the shop's record (chaos may garble it); the entity keeps the true address, so wms ships to it
            address=chaos.shop_address(world, cid, copy_address(address)),
        )
    )
    chaos.on_customer_registered(world, c)
    if code and world.scenarios is not None:
        # lifetime factor: records the customers acquired with a code while a cohort window is open
        world.scenarios.factor(
            "repurchase", world.now, world=world, record=("customer", cid), customer_id=cid,
            acquisition=acquisition, influencer_code=code, acquired_at=c.created_at,
        )
    return c


def schedule_repurchase(world, customer: Customer, order: WebOrder) -> None:
    """Schedule the next repurchase intent after the first delivery of ``order``.

    First-time buyers enter the repurchase chain with probability
    ``chain_share`` (calibration); customers with an active subscription get no
    intents. The intent fires when the earliest line runs out and carries the
    lines that run out within 14 days of it.
    """
    c = world.store.mut("customer", customer.customer_id)
    if c.lapsed or has_active_subscription(world, c):
        return
    if not c.meta.get("chain"):
        if c.meta.get("one_time"):
            return
        if world.rng("cust.chain", c.customer_id).random() < calibration(world)["chain_share"]:
            c.meta["chain"] = True
        else:
            c.meta["one_time"] = True
            return
    cat = world.ref.catalogue
    shape = world.cfg.customers.repurchase_shape
    rng = world.rng("cust.repurchase", c.customer_id, order.order_id)
    delays: list[tuple[float, int, int]] = []
    for ln in order.lines:
        q = ln.qty - ln.cancelled_qty
        if q <= 0:
            continue
        sku = cat.get(ln.sku_id)
        delays.append((repurchase.intent_delay_days(rng, sku.usage_days, q, shape), ln.sku_id, q))
    if not delays:
        return
    earliest = min(d for d, _, _ in delays)
    lines = [[s, q] for d, s, q in delays if d <= earliest + INTENT_GROUP_DAYS]
    base = order.delivered_at or world.now
    carry = int(c.meta.pop("carry_days", 0) or 0)
    _schedule_intent(world, c, base + timedelta(days=earliest + carry), lines, rng)


def _pull_forward(world, rng: Rng, due_d: date, skus: list[int]) -> date:
    """Move a planned intent into an earlier demand window, or keep its date.

    Every web demand window (Black Friday week through Cyber Monday, the
    Christmas run-up, an already announced web promotion covering one of the
    intent's SKUs) that starts after today and at most ``PULL_FORWARD_DAYS``
    before the intent's date gets one draw, earliest first: with probability
    ``PULL_FORWARD_SHARE * (1 - 1 / lift)`` the customer stocks up early and
    the intent moves to a day of that window (weighted by the day's event
    factor). The window calendar is a pure function of config, calendar and
    the promotion calendar, so this is decided when the intent is planned and
    the result is in the queue (restart-safe). The intents taken out of the
    following weeks are the post-event dip.
    """
    today = world.local_date()
    lo = today + timedelta(days=1)
    hi = due_d - timedelta(days=1)
    if hi < lo:
        return due_d
    first = max(lo, due_d - timedelta(days=PULL_FORWARD_DAYS))
    for w in demandplan.demand_windows(world, skus, first, hi, known_at=world.now):
        if not rng.random() < PULL_FORWARD_SHARE * (1.0 - 1.0 / max(1.0, w.lift)):
            continue
        last = min(w.last, hi)
        days = [w.first + timedelta(days=i) for i in range((last - w.first).days + 1)]
        weights, acc = [], 0.0
        for x in days:
            acc += max(1.0, demandplan.web_event_factor(world, x))
            weights.append(acc)
        return days[min(rng.weighted_index(weights), len(days) - 1)]
    return due_d


def _schedule_intent(world, c: Customer, approx: datetime, lines: list[list[int]], rng: Rng) -> None:
    c.intent_seq += 1
    planned = world.local_date(approx)
    due_d = _pull_forward(world, rng, planned, [s for s, _ in lines])
    due = at_time_of_day(world, rng, due_d)
    due = not_before_now(world, due, rng)
    data = {
        "customer_id": c.customer_id,
        "seq": c.intent_seq,
        "skus": [s for s, _ in lines],
        "qtys": [q for _, q in lines],
    }
    if due_d < planned:
        data["early_days"] = (planned - due_d).days  # stock still left when buying early
    world.schedule("cust.intent", due, f"cust.intent:{c.customer_id}:{c.intent_seq}", data)


def event_conversion_multiplier(world, skus: list[int]) -> float:
    """Multiplier on an intent's conversion probability from today's events and promotions.

    ``1 + EVENT_CONVERSION_ELASTICITY * (lift - 1)``, capped at
    ``EVENT_CONVERSION_CAP``, where ``lift = demandplan.repeat_lift`` (web event
    factor times the mean promotion lift of the intent's SKUs).
    """
    lift = demandplan.repeat_lift(world, skus, world.local_date())
    if lift <= 1.0:
        return 1.0
    return min(EVENT_CONVERSION_CAP, 1.0 + EVENT_CONVERSION_ELASTICITY * (lift - 1.0))


REPEAT_SOURCES = {
    "email": 0.34, "direct": 0.28, "organic_search": 0.16, "paid_search": 0.10, "social": 0.07, "app": 0.05,
}


@handler("cust.intent")
def on_intent(world, ev) -> None:
    cid = int(ev.data["customer_id"])
    seq = int(ev.data["seq"])
    c = world.store.get("customer", cid)
    if c is None or c.lapsed or c.intent_seq != seq or has_active_subscription(world, c):
        return
    ccfg = world.cfg.customers
    mult = world.scenarios.factor(
        "repurchase", world.now, customer_id=cid, acquisition=c.acquisition, influencer_code=c.influencer_code,
        acquired_at=c.meta.get("acquired_at", c.created_at),
    )
    lines = [[int(s), int(q)] for s, q in zip(ev.data.get("skus", []), ev.data.get("qtys", []))]
    mult *= event_conversion_multiplier(world, [s for s, _ in lines])
    p = repurchase.conversion_probability(ccfg.repurchase_prob, ccfg.repurchase_decay, c.missed_intents, mult)
    rng = world.rng("cust.intent", cid, seq)
    if rng.random() < p:
        basket = cherry_pick(world, rng, web.repeat_basket(world, rng, c.country, lines))
        if basket:
            # A linked duplicate record may take over the person (chaos); it may evict ``c``.
            buyer = chaos.repeat_customer(world, c)
            early = int(ev.data.get("early_days", 0))
            if early > 0:
                # Bought ahead of running out: the stock left over pushes the
                # next intent back by the same number of days.
                world.store.mut("customer", buyer.customer_id).meta["carry_days"] = early
            web.place_order(world, buyer, basket, source=pick(rng, REPEAT_SOURCES))
            return
    c = world.store.mut("customer", cid)
    c.missed_intents += 1
    if c.missed_intents >= ccfg.max_missed_intents:
        c.lapsed = True
        maybe_evict(world, c)
        return
    cat = world.ref.catalogue
    usage = [cat.get(s).usage_days * max(1, q) for s, q in lines] or [45.0]
    delay = ccfg.missed_retry_factor * (sum(usage) / len(usage)) * rng.lognormal_median(1.0, 0.3)
    _schedule_intent(world, c, world.now + timedelta(days=max(repurchase.MIN_INTENT_DAYS, delay)), lines, rng)


# ---------------------------------------------------------------------------
# Eviction: customers who can never act again leave the entity store
# ---------------------------------------------------------------------------
#
# A customer acts only through an open web order (deliveries, returns), a
# pending repurchase intent or a subscription. New business never comes from a
# customer without them: first-time buyers are new customers (new ids from
# ``world.next_id``, so an evicted id is never re-created), repeat orders come
# from intents and subscriptions. Such a customer is removed from
# ``world.store`` (``sim.entity_state``); the shop rows stay. Every handler
# that reads a customer tolerates a missing one or only runs while one of
# those three exists. New code (e.g. phase-2 GDPR erasure or duplicate
# customers) must not look customers up in the store by id: pick them from
# open orders, intents or subscriptions, or keep its own state.

OPEN_ORDERS = "open_orders"  # meta: web orders of this customer still in the store
EVICTION_FLAG = "customer_eviction_v1"  # counters: open-order counts are maintained


def order_opened(c: Customer) -> None:
    """``place_order`` (customer already marked mutated): one more open order."""
    c.meta[OPEN_ORDERS] = int(c.meta.get(OPEN_ORDERS, 0)) + 1


def order_closed(world, customer_id: int | None) -> None:
    """The web order left the store (``web.cleanup``): one fewer; evict if terminal."""
    if customer_id is None or not world.store.has("customer", customer_id):
        return
    c = world.store.mut("customer", customer_id)
    n = int(c.meta.get(OPEN_ORDERS, 0)) - 1
    if n > 0:
        c.meta[OPEN_ORDERS] = n
    else:
        c.meta.pop(OPEN_ORDERS, None)
    maybe_evict(world, c)


def can_act_again(world, c: Customer) -> bool:
    if int(c.meta.get(OPEN_ORDERS, 0)) > 0:
        return True
    if c.subscription_id is not None:
        return True
    # A pending intent of a lapsed customer is a no-op (on_intent returns early).
    return (
        not c.lapsed
        and c.intent_seq > 0
        and world.is_scheduled(f"cust.intent:{c.customer_id}:{c.intent_seq}")
    )


def maybe_evict(world, c: Customer) -> None:
    """Remove ``c`` from the store if it can never act again (deterministic: pure in state)."""
    if not can_act_again(world, c):
        world.store.remove("customer", c.customer_id)


@on_load(order=60)
def track_open_orders(world) -> None:
    """Once per simulation: count open orders per customer and evict terminal customers.

    Runs at the fresh start (nothing to count) and on the first resume of a
    state written before customers were evicted.
    """
    cnt = world.store.get("counters", "all")
    if cnt is None or cnt.values.get(EVICTION_FLAG):
        return
    counts: dict[int, int] = {}
    for _, o in world.store.items("web_order"):
        if o.customer_id is not None:
            counts[o.customer_id] = counts.get(o.customer_id, 0) + 1
    for _, c in list(world.store.items("customer")):
        n = counts.get(c.customer_id, 0)
        if int(c.meta.get(OPEN_ORDERS, 0)) != n:
            c = world.store.mut("customer", c.customer_id)
            if n:
                c.meta[OPEN_ORDERS] = n
            else:
                c.meta.pop(OPEN_ORDERS, None)
        maybe_evict(world, c)
    world.store.mut("counters", "all").values[EVICTION_FLAG] = 1


# ---------------------------------------------------------------------------
# Deal seekers (promo_margin_trap)
# ---------------------------------------------------------------------------

DEAL_SOURCE = "deal_site"
DEAL_SECOND_LINE_PROB = 0.15
DEAL_QTY_TWO_PROB = 0.1
DEAL_LAM_HEADROOM = 2.5  # intraday peak (hour x day-of-week) is about 2.36x the daily mean
DEAL_MAX_SHARE = 3.0  # = promo_margin_trap.MAX_DEAL_SEEKER_SHARE (more in overlapping windows is clipped)
DEAL_PRICE_SENSITIVITY = 2.0  # deal SKU weight = web weight / price ** this


def deal_windows(world) -> list[dict[str, Any]]:
    """Scenario promotions that bring deal seekers: ``{start, end, share, brand, return_rate, window_id}``.

    Pure in the scenario windows (cached per ``world.scenarios.version``).
    ``end`` is exclusive.
    """
    sc = world.scenarios
    if sc is None or not hasattr(sc, "scenario_promotions"):
        return []
    c = cache(world, "deal_windows")
    ver = (getattr(sc, "version", 0), getattr(sc, "history_start", None))
    if c.get("ver") != ver:
        out = []
        for spec in sc.scenario_promotions() or []:
            share = float(spec.get("deal_seeker_share") or 0.0)
            cherry = float(spec.get("cherry_pick_share") or 0.0)
            if (share <= 0 and cherry <= 0) or not spec.get("brand"):
                continue
            out.append({
                "start": spec["start"], "end": spec["end"], "share": share, "brand": spec["brand"],
                "return_rate": float(spec.get("deal_seeker_return_rate") or 0.0),
                "cherry_pick": cherry, "window_id": spec.get("window_id"),
            })
        c["ver"], c["v"] = ver, out
    return c["v"]


def deal_seeker_rate_per_hour(world, t: datetime) -> float:
    share = sum(w["share"] for w in deal_windows(world) if w["start"] <= t < w["end"])
    if share <= 0:
        return 0.0
    w = world.cfg.channels.web
    return w.base_orders_per_day * share / 24.0 * seasonality.intraday_factor(world.cfg.seasonality, world.local(t))


@arrival_source("cust.deal_seekers")
def deal_seeker_arrivals(world, start: datetime, end: datetime):
    """First-time deal-site customers while a promotion with ``deal_seeker_share`` runs (NHPP)."""
    wins = [w for w in deal_windows(world) if w["start"] < end and w["end"] > start]
    if not wins:
        return []
    # Constant bound (config only), so the candidates never depend on which
    # windows exist: switching a window on live cannot change other windows' arrivals.
    lam_max = world.cfg.channels.web.base_orders_per_day * DEAL_MAX_SHARE / 24.0 * DEAL_LAM_HEADROOM
    out = []
    for t, i, bucket in arrivals.thinned_arrivals(
        world.seed, "cust.deal", start, end, lambda x: deal_seeker_rate_per_hour(world, x), lam_max
    ):
        brand = next((w["brand"] for w in wins if w["start"] <= t < w["end"]), None)
        if brand is not None:
            out.append(Event(due=t, kind="cust.deal_order", key=f"cust.deal_order:{bucket}:{i}",
                             data={"brand": brand}))
    return out


def deal_basket(world, rng: Rng, brand: str) -> list[list[int]]:
    """One line of ``brand`` (sometimes two), quantity 1 (sometimes 2), no top-up.

    SKUs are drawn by today's web weight divided by the squared unit price: deal
    seekers go for the cheap bestsellers that deal posts feature.
    """
    d = world.local_date()
    cat = world.ref.catalogue
    sku_ids, cum = demandplan.sku_weights(world, "web", d)
    pool, weights, prev = [], [], 0.0
    for s, cw in zip(sku_ids, cum):
        wt, prev = cw - prev, cw
        sku = cat.get(s)
        if sku.brand == brand and wt > 0:
            pool.append(s)
            weights.append(wt / max(sku.rrp_eur, 0.5) ** DEAL_PRICE_SENSITIVITY)
    lines: list[list[int]] = []
    n = 2 if rng.random() < DEAL_SECOND_LINE_PROB else 1
    for _ in range(min(n, len(pool))):
        acc, cum_w = 0.0, []
        for wt in weights:
            acc += wt
            cum_w.append(acc)
        i = min(rng.weighted_index(cum_w), len(pool) - 1)
        lines.append([pool.pop(i), 2 if rng.random() < DEAL_QTY_TWO_PROB else 1])
        weights.pop(i)
    return lines


@handler("cust.deal_order")
def on_deal_order(world, ev) -> None:
    rng = world.rng("cust.deal_order", ev.key)
    lines = deal_basket(world, rng, str(ev.data["brand"]))
    if not lines:
        return
    c = new_customer(world, rng, acquisition=DEAL_SOURCE)
    c.meta["one_time"] = True  # came for the deal, never enters the repurchase chain
    c.meta["deal"] = True
    o = web.place_order(world, c, lines, source=DEAL_SOURCE)
    world.store.mut("web_order", o.order_id).meta["deal"] = True


def deal_return_rate(world, order: WebOrder) -> float | None:
    """Return probability of a deal seeker's parcel, or None for every other order."""
    if not order.meta.get("deal"):
        return None
    for w in deal_windows(world):
        if w["start"] <= order.placed_at < w["end"]:
            return w["return_rate"]
    return None


def cherry_pick(world, rng: Rng, lines: list[list[int]]) -> list[list[int]]:
    """Free shipping with no minimum: some shoppers buy only the promoted brand.

    While a scenario promotion with ``cherry_pick_share`` runs, a basket that
    mixes lines of the promoted brand with other lines is cut down to the
    promoted lines with that probability (the rest is bought later or
    elsewhere). Outside such a window the basket is returned unchanged and no
    random draw is made.
    """
    if not lines:
        return lines
    wins = [w for w in deal_windows(world) if w["cherry_pick"] > 0 and w["start"] <= world.now < w["end"]]
    if not wins:
        return lines
    cat = world.ref.catalogue
    for w in wins:
        keep = [ln for ln in lines if cat.get(int(ln[0])).brand == w["brand"]]
        if keep and len(keep) < len(lines):
            return keep if rng.random() < w["cherry_pick"] else lines
    return lines


# ---------------------------------------------------------------------------
# Warm start
# ---------------------------------------------------------------------------


@bootstrap(order=40)
def warm_start_customers(world) -> None:
    """Pre-existing customers with pending intents, so repeats start at steady state."""
    n = int(world.cfg.channels.web.initial_customers)
    if n <= 0:
        return
    start = world.now
    d0 = world.local_date(start)
    cal = calibration(world)
    shape = world.cfg.customers.repurchase_shape
    retry = world.cfg.customers.missed_retry_factor
    max_m = world.cfg.customers.max_missed_intents
    m_cum, acc = [], 0.0
    for wgt in cal["m_weights"]:
        acc += wgt
        m_cum.append(acc)
    cat = world.ref.catalogue
    for i in range(n):
        rng = world.rng("cust.warm", i)
        lines = web.draw_lines(world, rng, d0)
        if not lines:
            continue
        m = min(rng.weighted_index(m_cum), max_m - 1)
        # Interval to the pending intent: length-biased (forward recurrence) draw.
        if m == 0 or rng.random() < 0.5:
            xs = [
                max(repurchase.MIN_INTENT_DAYS,
                    rng.gamma_mean(cat.get(s).usage_days * q * (shape + 1) / shape, shape + 1))
                for s, q in lines
            ]
            x = min(xs)
        else:
            usage = sum(cat.get(s).usage_days * q for s, q in lines) / len(lines)
            x = max(repurchase.MIN_INTENT_DAYS, retry * usage * rng.lognormal_median(1.0, 0.3) * 1.1)
        u = rng.random()
        ahead = max(0.02, u * x * WARM_SPREAD)
        last_order = start - timedelta(days=max(3.0, (1 - u) * x + 3.0))
        span = max(1.0, WARM_HISTORY_DAYS - (start - last_order).days)
        created = last_order - timedelta(days=rng.random() * span)
        c = new_customer(world, rng, created_at=created)
        c.orders = 1 + rng.poisson(1.2 + m * 0.8)
        c.missed_intents = m
        c.last_order_at = last_order
        c.last_skus = [s for s, _ in lines]
        c.meta["chain"] = True
        if c.orders >= 2:
            c.meta["sub_decided"] = True
        group = [[s, q] for s, q in lines]
        _schedule_intent(world, c, start + timedelta(days=ahead), group, rng)
