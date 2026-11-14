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

from ..engine.registry import bootstrap, handler
from ..engine.rng import Rng
from ..models import repurchase, seasonality
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

LOCALES = {"DE": "de_DE", "AT": "de_AT", "CH": "de_CH"}
INTENT_GROUP_DAYS = 14.0  # lines running out within this many days of the first go into one intent
MARKETING_OPT_IN = 0.46
PHONE_SHARE = 0.78
BIRTH_YEAR_SHARE = 0.62
WARM_HISTORY_DAYS = 730
WARM_SPREAD = 1.12  # stretch of the warm cohort's first intents (empirical: flat volume from day one)


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
        acquisition = pick(rng, cfg.customers.acquisition_mix)
    code = None
    codes = cfg.customers.influencer_codes
    if acquisition == "influencer" and codes:
        code = codes[min(int(rng.random() * len(codes)), len(codes) - 1)]
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
            address=copy_address(address),
        )
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
    _schedule_intent(world, c, base + timedelta(days=earliest), lines, rng)


def _schedule_intent(world, c: Customer, approx: datetime, lines: list[list[int]], rng: Rng) -> None:
    c.intent_seq += 1
    due = at_time_of_day(world, rng, world.local_date(approx))
    due = not_before_now(world, due, rng)
    world.schedule(
        "cust.intent",
        due,
        f"cust.intent:{c.customer_id}:{c.intent_seq}",
        {
            "customer_id": c.customer_id,
            "seq": c.intent_seq,
            "skus": [s for s, _ in lines],
            "qtys": [q for _, q in lines],
        },
    )


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
        "repurchase", world.now, customer_id=cid, acquisition=c.acquisition, influencer_code=c.influencer_code
    )
    p = repurchase.conversion_probability(ccfg.repurchase_prob, ccfg.repurchase_decay, c.missed_intents, mult)
    rng = world.rng("cust.intent", cid, seq)
    lines = [[int(s), int(q)] for s, q in zip(ev.data.get("skus", []), ev.data.get("qtys", []))]
    if rng.random() < p:
        basket = web.repeat_basket(world, rng, c.country, lines)
        if basket:
            web.place_order(world, c, basket, source=pick(rng, REPEAT_SOURCES))
            return
    c = world.store.mut("customer", cid)
    c.missed_intents += 1
    if c.missed_intents >= ccfg.max_missed_intents:
        c.lapsed = True
        return
    cat = world.ref.catalogue
    usage = [cat.get(s).usage_days * max(1, q) for s, q in lines] or [45.0]
    delay = ccfg.missed_retry_factor * (sum(usage) / len(usage)) * rng.lognormal_median(1.0, 0.3)
    _schedule_intent(world, c, world.now + timedelta(days=max(repurchase.MIN_INTENT_DAYS, delay)), lines, rng)


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
