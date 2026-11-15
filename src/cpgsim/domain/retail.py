"""Retail channels: hidden consumer demand and DC inventory, purchase orders by EDI.

Every enabled retailer channel (the drugstore chains Kranz and Mylo, the
marketplace Shopnova in its 1P vendor model and the perfumery Belle) has a few
distribution centres or fulfilment centres (``accounts.dcs(channel)``). Per DC
the module keeps a :class:`~cpgsim.domain.model.RetailerInventory` (store id =
DC account id): units on hand, units on order, a smoothed weekly forecast
*level* per SKU (``forecast``: units per week without promotion, season and
calendar-event effects; the planning system projects it with
``seasonality.category_factor`` and the channel's event calendar, as a buyer
with last year's curves would) and ``push_backlog`` (``retailer_overstock``).
None of this is visible directly; what shows is the sell-in (POs, sales
documents, deliveries, invoices, payments) and, through ``domain.feeds``, the
retailers' sell-out reports.

Rhythm per DC (local time)

=====================  ======================================================
sell-out 00:30         ``retail.sellout``. Weekly channels (Kranz, Mylo,
                       Belle) on Monday for last week: consumers buy the
                       week's hidden demand (``demandplan.retail_weekly_demand``
                       times log-normal noise with ``demand_noise_cv``), capped
                       by on hand; the rest is lost. Daily channels
                       (Shopnova) every day for the previous day: the week's
                       demand times one weekly noise factor per SKU, spread by
                       the normalised day-of-week shape and the day's event
                       factor. The forecast level EWMA (``forecast_alpha``;
                       daily ``1 - (1 - alpha) ** (1/7)``) learns from sell-out
                       divided by the promotion, season and event factors; a
                       sold-out period counts as at least the forecast
                       (censored demand). The period's sell-out goes to
                       ``feeds`` and ``deductions``.
order day 05:00        ``retail.po_cycle``. ``order_weekdays`` (Shopnova Mon,
                       Wed, Fri) or every ``order_cycle_days`` on weekday
                       ``i % 5`` (``i`` = 1-based DC number over all enabled
                       retailers' DCs, in account order). Order-up-to: the
                       expected demand over the coverage window (``cycle +
                       lead + safety`` weeks, from the first day whose sales
                       are not yet booked), evaluated per day as ``level *
                       season(day) * event(day) / 7`` (0 on days the SKU is
                       not listed), plus forward buy, minus on hand and on
                       order, times ``lognormal(0, forecast_error_sd)`` and
                       the ``retail_order`` scenario factor, rounded up to
                       cases. Forward buy: expected promo uplift of the
                       retailer's promotions starting within
                       ``retail_forward_buy_weeks``; those lines get an
                       off-invoice ``promo_discount`` of
                       ``retail_funding_share * discount``. While the
                       ``retail_push`` factor ``p`` is not 1 the DC orders
                       ``cycle demand * p + uplift + push_backlog`` whatever its
                       stock (retailer_overstock). Creates a RetailPO and its
                       SalesDoc (``SalesDocCreated`` = received by EDI).
+2..8 h (working)      ``retail.confirm``: ``warehouse.reserve`` per line, cut
                       to whole cases (cuts -> ``SalesDocChanged``), delivery
                       planned with ``fulfilment.create_delivery(kind="retail")``
                       for the next warehouse working day;
                       ``deductions.record_cut``.
ship (fulfilment)      ``retail.shipped``: delivered_qty on the sales items,
                       sales doc in process, PO shipped.
POD (fulfilment)       ``retail.pod``: on hand += qty, on order -= qty, PO
                       delivered, ``deductions.record_pod``; invoice 0-1 days
                       later in the evening batch.
invoice                ``retail.invoice``: ``billing.create_invoice`` with due
                       date = billing date + payment terms; PO invoiced,
                       ``invoice_gross`` kept on the PO.
due +/- jitter         ``retail.payment``: the retailer short-pays its open
                       deductions (``deductions.take_on_payment``) and pays the
                       rest; ``billing.clear`` with status cleared; PO, sales
                       doc and invoice entities are removed.
=====================  ======================================================

Because orders replenish towards a target around true demand, sell-in and
sell-out differ from week to week (order noise, case rounding, cycles, forward
buys, lost sales) but their cumulative totals stay within the inventory swing,
so they converge over a retail quarter.

Scenario hooks: ``retail_order`` (factor on every order line), ``retail_push``
and the cut phase of ``retailer_overstock`` (truth ``sales_doc`` retail_push
and ``retailer_dc_week`` post_overstock_cut), and ``hero_oos_substitution``
(``substitution_share`` of the hero's lost units moves to the sibling at the
DC; truth ``retailer_dc_week`` substituted_units and ``sales_doc``
hero_oos_cut).
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import math
from datetime import date, datetime, timedelta

from ..engine.queue import Event
from ..engine.registry import arrival_source, bootstrap, handler
from ..models.delays import next_working_moment
from ..models.pricing import elasticity_lift
from ..models.seasonality import category_factor, dow_weights, event_factor
from .events import SalesDocChanged, SalesDocCreated
from .model import Condition, RetailerInventory, RetailPO, RetailPOLine, SalesDoc, SalesItem, goods_item_no

RETAIL_VAT = 0.19
SELLOUT_HOUR, SELLOUT_MINUTE = 0, 30
ORDER_EPOCH = date(2024, 1, 1)  # a Monday: anchors order-day arithmetic
CONFIRM_WINDOW = (6, 18)  # local hours in which order desk confirmations happen
INVOICE_BATCH_HOUR = 19.0  # evening billing run, 19:00-21:00
PAYMENT_HOURS = (9.0, 15.0)
MIN_PROMO_FACTOR = 0.2
MIN_SEASON_FACTOR = 0.05
_CACHE_LIMIT = 20000


def _api(name: str):
    """Another domain module's public API (looked up at call time, so tests can swap it)."""
    return importlib.import_module(f"cpgsim.domain.{name}")


def _scenario_api(name: str):
    """A scenario module's helpers (lazy: the scenario package imports domain modules)."""
    return importlib.import_module(f"cpgsim.scenarios.{name}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def dc_plan(world) -> list[tuple[str, str, int]]:
    """``(channel, dc_account_id, index)`` for every DC of an enabled retailer (index 1-based)."""
    retailers = world.cfg.channels.retailers()
    out: list[tuple[str, str, int]] = []
    i = 0
    for acc in world.ref.accounts.all:
        if acc.kind == "retailer_dc" and acc.retailer in retailers:
            i += 1
            out.append((acc.retailer, acc.account_id, i))
    return out


def is_order_day(rc, index: int, d: date) -> bool:
    """DC ``index`` orders on ``d``?

    ``rc`` is the retailer config (or, as in phase 1, a cycle length in days).
    With ``order_weekdays`` the DC orders on those weekdays; otherwise every
    ``order_cycle_days`` days, weekly cycles on weekday ``index % 5``.
    """
    if isinstance(rc, int):
        cycle_days, weekdays = rc, None
    else:
        cycle_days, weekdays = rc.order_cycle_days, rc.order_weekdays
    if weekdays:
        return d.weekday() in weekdays
    cycle = max(int(cycle_days), 1)
    return (d.toordinal() - ORDER_EPOCH.toordinal() - index % 5) % cycle == 0


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _local_dates(world, start: datetime, end: datetime) -> list[date]:
    d = world.local(start).date()
    d1 = world.local(end).date()
    out = []
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


def _wh_region(world) -> str:
    return world.cfg.company.warehouse.region


def _cycle_days(rc) -> float:
    """Mean days between two orders of a DC (``7 / len(order_weekdays)`` or ``order_cycle_days``)."""
    if rc.order_weekdays:
        return 7.0 / len(rc.order_weekdays)
    return float(max(int(rc.order_cycle_days), 1))


def _cycle_weeks(rc) -> float:
    return _cycle_days(rc) / 7.0


def _lead_weeks(world) -> float:
    lo, hi = world.cfg.retail.lead_days
    return ((lo + hi) / 2.0 + 1.0) / 7.0  # confirmation + ship next working day + pallet lead


def _is_daily(rc) -> bool:
    return rc.sellout_frequency == "daily"


def _promo_factor(world, sku_id: int, channel: str, d: date) -> float:
    f = _api("promotions").demand_factor(world, sku_id, channel, d)
    return max(float(f), MIN_PROMO_FACTOR)


def _season(world, sku_id: int, d: date) -> float:
    profile = world.ref.catalogue.get(sku_id).season_profile
    return max(category_factor(profile, d), MIN_SEASON_FACTOR)


def _cache(world) -> dict:
    c = world.__dict__.get("_retail_cache")
    if c is None or c.get("cfg") is not world.cfg:
        c = {"cfg": world.cfg}
        world.__dict__["_retail_cache"] = c
    elif len(c) > _CACHE_LIMIT:
        c.clear()
        c["cfg"] = world.cfg
    return c


def _ev(world, channel: str, brand: str | None, d: date) -> float:
    """Calendar event factor of ``channel`` (and ``brand``: Mother's Day) on local date ``d``."""
    c = _cache(world)
    key = ("ev", channel, brand, d)
    hit = c.get(key)
    if hit is None:
        hit = c[key] = event_factor(world.cfg.seasonality, d, channel, brand, world.ref.calendar)
    return hit


def _week_ev(world, channel: str, brand: str | None, ws: date) -> float:
    """The event factor inside ``retail_weekly_demand`` for the week from ``ws``.

    Mean channel factor over Mon..Sun times the brand's own event ratio at mid-week.
    """
    c = _cache(world)
    key = ("wev", channel, brand, ws)
    hit = c.get(key)
    if hit is None:
        mean = sum(_ev(world, channel, None, ws + timedelta(days=i)) for i in range(7)) / 7.0
        mid = ws + timedelta(days=3)
        base = _ev(world, channel, None, mid)
        ratio = _ev(world, channel, brand, mid) / base if brand is not None and base > 0 else 1.0
        hit = c[key] = mean * ratio
    return hit


def _level_factor(world, channel: str, sku_id: int, ws: date) -> float:
    """Promotion x season x event factor of the week from ``ws`` (weekly demand / level)."""
    mid = ws + timedelta(days=3)
    brand = world.ref.catalogue.get(sku_id).brand
    return (_promo_factor(world, sku_id, channel, mid) * _season(world, sku_id, mid)
            * max(_week_ev(world, channel, brand, ws), MIN_PROMO_FACTOR))


def _baseline_level(world, channel: str, dc: str, sku_id: int, week_start: date) -> float:
    """Planned weekly demand without promotion, season and event effects (the buyer's starting level)."""
    m = _api("demandplan").retail_weekly_demand(world, channel, dc, sku_id, week_start)
    return m / _level_factor(world, channel, sku_id, week_start)


def _dow_share(world, d: date) -> float:
    """Share of a week's demand that falls on ``d``'s weekday (sums to 1 over Mon..Sun)."""
    return dow_weights(world.cfg.seasonality)[d.weekday()] / 7.0


def _listed(world, channel: str, d: date) -> frozenset[int]:
    c = _cache(world)
    key = ("listed", channel, d)
    hit = c.get(key)
    if hit is None:
        hit = c[key] = frozenset(world.ref.catalogue.retailer_assortment(channel, d))
    return hit


def _coverage_days(world, first: date, last_excl: float) -> list[tuple[date, float]]:
    """``(day, weight)`` for the days from ``first`` over ``last_excl`` days (last day fractional)."""
    out = []
    full = int(math.floor(last_excl))
    for k in range(full):
        out.append((first + timedelta(days=k), 1.0))
    frac = last_excl - full
    if frac > 1e-9:
        out.append((first + timedelta(days=full), frac))
    return out


def _window_demand(world, channel: str, sku, level: float, days: list[tuple[date, float]]) -> float:
    """Expected units over ``days``: ``sum(level * season(day) * event(day) / 7)``, 0 when not listed."""
    tot = 0.0
    for d, wgt in days:
        if sku.sku_id not in _listed(world, channel, d):
            continue
        tot += wgt * _season(world, sku.sku_id, d) * _ev(world, channel, sku.brand, d)
    return level * tot / 7.0


def _safety_stock(world, channel: str, sku_id: int, level: float, days: list[tuple[date, float]]) -> float:
    """Safety stock: the deseasonalised weekly level over the safety days on which the SKU is listed.

    Sized on the level rather than the seasonal rate, so the stock a DC holds
    swings with the season only by its cycle and lead-time stock; that keeps
    sell-in and sell-out of a retail quarter within a few per cent also for a
    retailer with a large sun-care share (Belle). Gift sets still taper out
    before their listing ends.
    """
    listed = sum(w for d, w in days if sku_id in _listed(world, channel, d))
    return level * listed / 7.0


def po_number(channel: str, po_id: int, d: date) -> str:
    """The retailer's own PO number format (stable, unique per PO)."""
    if channel == "drogerie_kranz":
        return f"45{po_id:08d}"
    if channel == "mylo":
        return f"MY{d:%y%m}-{po_id:06d}"
    if channel == "shopnova":
        return f"SNV-PO-{po_id:07d}"
    if channel == "belle":
        return f"BEL/{d:%Y}/{po_id:05d}"
    return f"PO{po_id:08d}"


def price_line(list_price: float, qty: int, trade_discount: float,
               promo_rate: float = 0.0) -> tuple[list[Condition], float, float]:
    """Pricing conditions, net value and tax (EUR) for one retail PO line.

    list price -> customer discount (trade discount) -> off-invoice promo
    discount on the discounted value -> 19 % VAT on net.
    """
    gross = round(list_price * qty, 2)
    cd = round(-gross * trade_discount, 2)
    conds = [
        Condition("list_price", list_price, gross),
        Condition("customer_discount", trade_discount, cd),
    ]
    net = gross + cd
    if promo_rate > 0:
        pd = round(-net * promo_rate, 2)
        conds.append(Condition("promo_discount", round(promo_rate, 4), pd))
        net += pd
    net = round(net, 2)
    tax = round(net * RETAIL_VAT, 2)
    conds.append(Condition("tax", RETAIL_VAT, tax))
    return conds, net, tax


def _lognormal_mean1(rng, cv: float) -> float:
    if cv <= 0:
        rng.gauss(0.0, 1.0)
        return 1.0
    s2 = math.log(1.0 + cv * cv)
    return math.exp(math.sqrt(s2) * rng.gauss(0.0, 1.0) - s2 / 2.0)


def _week_noise(world, dc: str, ws: date) -> dict[int, float]:
    """Weekly demand noise per SKU of a daily-reporting DC (``retail.sellout.week``).

    One mean-1 log-normal factor per SKU and week, drawn in catalogue order, so
    each SKU's weekly demand has the same noise as at the weekly retailers; it
    is re-derived (pure) on every day of the week.
    """
    c = _cache(world)
    key = ("wnoise", dc, ws)
    hit = c.get(key)
    if hit is None:
        cv = world.cfg.retail.demand_noise_cv
        rng = world.rng("retail.sellout.week", dc, ws.isoformat())
        hit = c[key] = {s.sku_id: _lognormal_mean1(rng, cv) for s in world.ref.catalogue.skus}
    return hit


def _push_factor(world, channel: str, dc: str) -> float:
    sc = world.scenarios
    if sc is None:
        return 1.0
    return float(sc.factor("retail_push", world.now, channel=channel, dc=dc))


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@bootstrap(order=30)
def bootstrap_retail(world) -> None:
    retailers = world.cfg.channels.retailers()
    cat = world.ref.catalogue
    d = world.local_date()
    ws = monday(d)
    safety = world.cfg.retail.safety_weeks
    for channel, dc, _ in dc_plan(world):
        # Average steady state of the order-up-to policy just before a sell-out:
        # about one order cycle plus the safety stock (the lead time is in transit).
        cycle = _cycle_weeks(retailers[channel])
        inv = RetailerInventory(dc_account=dc, retailer=channel)
        for sku_id in cat.retailer_assortment(channel, d):
            level = round(_baseline_level(world, channel, dc, sku_id, ws), 3)
            inv.forecast[sku_id] = level
            brand = cat.get(sku_id).brand
            rate = _season(world, sku_id, ws + timedelta(days=3)) * _week_ev(world, channel, brand, ws)
            inv.on_hand[sku_id] = int(round(level * (rate * cycle + safety)))
        world.store.put("retailer_inventory", dc, inv)


# ---------------------------------------------------------------------------
# Arrival sources (pure: seed, config, time)
# ---------------------------------------------------------------------------


@arrival_source("retail.sellout")
def sellout_arrivals(world, start: datetime, end: datetime):
    retailers = world.cfg.channels.retailers()
    out = []
    plan = None
    for d in _local_dates(world, start, end):
        t = world.at_local(d, SELLOUT_HOUR, SELLOUT_MINUTE)
        if not start < t <= end:
            continue
        plan = plan or dc_plan(world)
        ws = (d - timedelta(days=7)).isoformat()
        day = d - timedelta(days=1)
        for channel, dc, _ in plan:
            if _is_daily(retailers[channel]):
                data = {"channel": channel, "dc": dc, "week_start": monday(day).isoformat(),
                        "day": day.isoformat(), "days": 1}
            elif d.weekday() == 0:
                data = {"channel": channel, "dc": dc, "week_start": ws}
            else:
                continue
            out.append(Event(due=t, kind="retail.sellout", key=f"retail.sellout:{dc}:{d:%Y%m%d}",
                             data=data, priority=20))
    return out


@arrival_source("retail.po_cycle")
def po_cycle_arrivals(world, start: datetime, end: datetime):
    retailers = world.cfg.channels.retailers()
    hour = world.cfg.retail.order_hour_local
    out = []
    plan = None
    for d in _local_dates(world, start, end):
        t = world.at_local(d, hour)
        if not start < t <= end:
            continue
        plan = plan or dc_plan(world)
        for channel, dc, idx in plan:
            if is_order_day(retailers[channel], idx, d):
                out.append(Event(due=t, kind="retail.po_cycle", key=f"retail.po_cycle:{dc}:{d:%Y%m%d}",
                                 data={"channel": channel, "dc": dc}, priority=30))
    return out


# ---------------------------------------------------------------------------
# Sell-out (hidden consumer demand)
# ---------------------------------------------------------------------------


@handler("retail.sellout")
def on_sellout(world, ev) -> None:
    channel, dc = ev.data["channel"], ev.data["dc"]
    if not world.store.has("retailer_inventory", dc):
        return
    inv: RetailerInventory = world.store.mut("retailer_inventory", dc)
    ws = date.fromisoformat(ev.data["week_start"])
    days = int(ev.data.get("days", 7))
    if days == 1:
        first = date.fromisoformat(ev.data["day"])
        mid = first
        sold_m, lost_m, want_m = _sell_day(world, inv, channel, dc, ws, first)
    else:
        first = ws
        mid = ws + timedelta(days=3)
        sold_m, lost_m, want_m = _sell_week(world, inv, channel, dc, ws)
    assortment = _listed(world, channel, mid)

    _substitute(world, inv, channel, dc, first, mid, sold_m, lost_m, want_m)

    on_hand_m = {s: int(inv.on_hand.get(s, 0)) for s in sorted(assortment)}
    if days == 1:
        on_order_m = {s: int(inv.on_order.get(s, 0)) for s in sorted(assortment)}
        _api("feeds").record_marketplace_day(world, channel, dc, first, sold_m, want_m, on_hand_m, on_order_m)
    else:
        _api("feeds").record_sellout_week(world, channel, dc, ws, sold_m, lost_m, want_m, on_hand_m)
    _api("deductions").record_sellout(world, channel, dc, first, days, sold_m)

    # Forget SKUs the DC no longer lists once nothing is left or on its way.
    coming = _listed(world, channel, world.local_date() + timedelta(days=3))
    for sku_id in sorted(set(inv.on_hand) | set(inv.forecast)):
        if sku_id in coming:
            continue
        if inv.on_hand.get(sku_id, 0) <= 0 and inv.on_order.get(sku_id, 0) <= 0:
            inv.on_hand.pop(sku_id, None)
            inv.on_order.pop(sku_id, None)
            inv.forecast.pop(sku_id, None)
            inv.push_backlog.pop(sku_id, None)


def _sell_week(world, inv: RetailerInventory, channel: str, dc: str, ws: date):
    """One week of consumer demand at a weekly-reporting DC (phase-1 draws, unchanged order)."""
    rcfg = world.cfg.retail
    dp = _api("demandplan")
    mid = ws + timedelta(days=3)
    assortment = _listed(world, channel, mid)
    alpha = rcfg.forecast_alpha
    rng = world.rng("retail.sellout", dc, ws.isoformat())
    sold_m: dict[int, int] = {}
    lost_m: dict[int, int] = {}
    want_m: dict[int, int] = {}
    for sku_id in sorted(inv.on_hand):
        noise = _lognormal_mean1(rng, rcfg.demand_noise_cv)
        u = rng.random()
        fc = inv.forecast.get(sku_id)
        if sku_id in assortment:
            mean = dp.retail_weekly_demand(world, channel, dc, sku_id, ws)
            pf = _level_factor(world, channel, sku_id, ws)
        else:  # delisted or out of season: remaining stock sells off at the last forecast
            pf = _season(world, sku_id, mid)
            mean = (fc or 0.0) * pf
        x = max(mean, 0.0) * noise
        want = int(x) + (1 if u < x - int(x) else 0)
        oh = int(inv.on_hand.get(sku_id, 0))
        sold = min(oh, want)
        inv.on_hand[sku_id] = oh - sold
        if fc is not None and sku_id in assortment:
            obs = float(sold) if sold < oh or want == 0 else max(float(sold), fc * pf)
            inv.forecast[sku_id] = round(alpha * obs / pf + (1.0 - alpha) * fc, 3)
        if want:
            want_m[sku_id] = want
            sold_m[sku_id] = sold
            if want > sold:
                lost_m[sku_id] = want - sold
    return sold_m, lost_m, want_m


def _sell_day(world, inv: RetailerInventory, channel: str, dc: str, ws: date, day: date):
    """One day of consumer demand at a daily-reporting DC (marketplace fulfilment centre).

    mean = weekly demand x weekly noise g(sku) x dow_share(day) x ev(day) / mean ev of
    the week; the forecast learns a weekly level from ``sold / dow_share`` net of the
    same promotion, season and event factors.
    """
    rcfg = world.cfg.retail
    dp = _api("demandplan")
    mid = ws + timedelta(days=3)
    assortment = _listed(world, channel, mid)
    alpha = 1.0 - (1.0 - rcfg.forecast_alpha) ** (1.0 / 7.0)
    share = _dow_share(world, day)
    ev_day = _ev(world, channel, None, day)
    ev_week = _week_ev(world, channel, None, ws)
    ev_rel = ev_day / ev_week if ev_week > 0 else 1.0
    gmap = _week_noise(world, dc, ws)
    rng = world.rng("retail.sellout.day", dc, day.isoformat())
    sold_m: dict[int, int] = {}
    lost_m: dict[int, int] = {}
    want_m: dict[int, int] = {}
    for sku_id in sorted(inv.on_hand):
        u = rng.random()
        fc = inv.forecast.get(sku_id)
        g = gmap.get(sku_id, 1.0)
        if sku_id in assortment:
            weekly = dp.retail_weekly_demand(world, channel, dc, sku_id, ws)
            pf = _level_factor(world, channel, sku_id, ws) * share * ev_rel
            mean = weekly * share * ev_rel
        else:
            pf = _season(world, sku_id, day) * share
            mean = (fc or 0.0) * pf
        x = max(mean, 0.0) * g
        want = int(x) + (1 if u < x - int(x) else 0)
        oh = int(inv.on_hand.get(sku_id, 0))
        sold = min(oh, want)
        inv.on_hand[sku_id] = oh - sold
        if fc is not None and sku_id in assortment and pf > 0:
            obs = sold / pf
            if not (sold < oh or want == 0):
                obs = max(obs, fc)
            inv.forecast[sku_id] = round(alpha * obs + (1.0 - alpha) * fc, 3)
        if want:
            want_m[sku_id] = want
            sold_m[sku_id] = sold
            if want > sold:
                lost_m[sku_id] = want - sold
    return sold_m, lost_m, want_m


def _substitute(world, inv: RetailerInventory, channel: str, dc: str, period_start: date, mid: date,
                sold_m: dict[int, int], lost_m: dict[int, int], want_m: dict[int, int]) -> None:
    """hero_oos_substitution: ``share`` of the hero's lost units try to buy the sibling (no draws)."""
    if world.scenarios is None:
        return
    hero_mod = _scenario_api("hero_oos_substitution")
    for b in hero_mod.retail_blocks(world, channel, mid):
        lost_h = int(lost_m.get(b.sku_id, 0))
        extra = int(round(b.share * lost_h))
        if extra <= 0:
            continue
        sib = b.sibling_id
        left = int(inv.on_hand.get(sib, 0))
        got = min(extra, max(left, 0))
        if got:
            inv.on_hand[sib] = left - got
        want_m[sib] = want_m.get(sib, 0) + extra
        sold_m[sib] = sold_m.get(sib, 0) + got
        if extra > got:
            lost_m[sib] = lost_m.get(sib, 0) + extra - got
        world.scenarios.record_effect(
            world, hero_mod.SCENARIO_ID, b.window_id, "retailer_dc_week", f"{dc}:{period_start.isoformat()}",
            "substituted_units", extra,
            {"hero": b.sku_id, "sibling": sib, "hero_lost": lost_h, "sibling_sold_extra": got,
             "channel": channel},
        )


# ---------------------------------------------------------------------------
# Purchase order cycle
# ---------------------------------------------------------------------------


@handler("retail.po_cycle")
def on_po_cycle(world, ev) -> None:
    channel, dc = ev.data["channel"], ev.data["dc"]
    if not world.store.has("retailer_inventory", dc):
        return
    rc = world.cfg.channels.retailers()[channel]
    rcfg = world.cfg.retail
    pcfg = world.cfg.promotions
    cat = world.ref.catalogue
    promotions = _api("promotions")
    today = world.local_date()
    ws = monday(today)
    inv: RetailerInventory = world.store.mut("retailer_inventory", dc)

    fwd_end = today + timedelta(weeks=pcfg.retail_forward_buy_weeks)
    promos = sorted(promotions.retail_promos_starting(world, channel, today, fwd_end),
                    key=lambda p: p.promo_id)
    cycle_days = _cycle_days(rc)
    # Coverage: the stock must last until the next delivery (cycle + lead, per day
    # with season and calendar events) plus the safety stock (safety weeks of the
    # deseasonalised level, on the days the SKU is still listed).
    stock_days = cycle_days + _lead_weeks(world) * 7.0
    window = _coverage_days(world, today, stock_days)
    safety_window = _coverage_days(world, today + timedelta(days=math.ceil(stock_days)), rcfg.safety_weeks * 7.0)
    cycle_window = _coverage_days(world, today, cycle_days)
    f_sc = float(world.scenarios.factor("retail_order", world.now, channel=channel, dc=dc))
    push = _push_factor(world, channel, dc)
    if push == 1.0 and inv.push_backlog:
        inv.push_backlog.clear()  # the push window ended
    rng = world.rng("retail.po", dc, today.isoformat())

    lines: list[tuple[int, int, object]] = []  # (sku_id, qty, promo or None)
    expected_units = 0.0
    for sku_id in cat.retailer_assortment(channel, today):
        z = rng.gauss(0.0, rcfg.forecast_error_sd)
        level = inv.forecast.get(sku_id)
        if level is None:  # newly listed: start from the plan
            level = round(_baseline_level(world, channel, dc, sku_id, ws), 3)
            inv.forecast[sku_id] = level
        sku = cat.get(sku_id)
        uplift = 0.0
        promo = None
        for p in promos:
            if not promotions.is_eligible(p, sku):
                continue
            days = max((p.end - p.start).total_seconds() / 86400.0, 1.0)
            p_mid = world.local_date(p.start) + timedelta(days=days / 2.0)
            uplift += (level * _season(world, sku_id, p_mid) * _ev(world, channel, sku.brand, p_mid)
                       * (elasticity_lift(p.discount_pct, pcfg.elasticity) - 1.0) * days / 7.0)
            if promo is None or p.discount_pct > promo.discount_pct:
                promo = p
        cycle_demand = _window_demand(world, channel, sku, level, cycle_window)
        expected_units += cycle_demand
        if push != 1.0:
            backlog = float(inv.push_backlog.pop(sku_id, 0.0))
            need = cycle_demand * push + uplift + backlog
        else:
            target = (_window_demand(world, channel, sku, level, window)
                      + _safety_stock(world, channel, sku_id, level, safety_window) + uplift)
            need = target - inv.on_hand.get(sku_id, 0) - inv.on_order.get(sku_id, 0)
        if need <= 0:
            continue
        q = need * math.exp(z) * f_sc
        upc = max(int(sku.units_per_case), 1)
        qty = int(math.ceil(q / upc - 1e-9)) * upc
        if qty > 0:
            lines.append((sku_id, qty, promo))

    _record_cut_phase(world, channel, dc, today, sum(q for _, q, _ in lines), expected_units)
    if not lines:
        return

    accounts = world.ref.accounts
    dc_acc = accounts.get(dc)
    po_id = world.next_id("retail_po")
    sales_doc_id = world.next_id("sales_doc")
    r2 = world.rng("retail.po.terms", po_id)
    lead = r2.randint(*rcfg.lead_days)
    requested = world.ref.calendar.add_working_days(today, lead, "DE", dc_acc.address.region)
    number = po_number(channel, po_id, today)
    masterdata = _api("masterdata")

    po_lines: list[RetailPOLine] = []
    items: list[SalesItem] = []
    promo_ids: set[int] = set()
    for n, (sku_id, qty, promo) in enumerate(lines, start=1):
        sku = cat.get(sku_id)
        article = cat.customer_article(channel, sku_id)
        rate = 0.0
        if promo is not None:
            rate = pcfg.retail_funding_share * promo.discount_pct
            promo_ids.add(promo.promo_id)
        conds, net, tax = price_line(sku.list_price_eur, qty, rc.trade_discount, rate)
        po_lines.append(RetailPOLine(line_no=n, sku_id=sku_id, customer_article=article, qty=qty))
        items.append(
            SalesItem(
                item_no=goods_item_no(n),  # 900 is the freight item number
                sku_id=sku_id,
                qty=qty,
                confirmed_qty=qty,  # requested; cuts follow at confirmation
                net_value=net,
                tax=tax,
                cost=round(masterdata.std_cost(world, sku_id, today) * qty, 2),
                conditions=conds,
                customer_article=article,
            )
        )
        inv.on_order[sku_id] = inv.on_order.get(sku_id, 0) + qty

    po = RetailPO(
        po_id=po_id,
        retailer=channel,
        dc_account=dc,
        po_number=number,
        created_at=world.now,
        requested_delivery=requested,
        lines=po_lines,
        status="received",
        sales_doc_id=sales_doc_id,
        promo_ids=sorted(promo_ids),
    )
    doc = SalesDoc(
        sales_doc_id=sales_doc_id,
        doc_type="retail",
        sold_to=accounts.retailer(channel).account_id,
        ship_to=dc,
        currency="EUR",
        fx_rate_eur=masterdata.fx_rate(world, "EUR", today),
        created_at=world.now,
        requested_date=requested,
        external_ref=number,
        items=items,
        channel=channel,
        order_id=po_id,
    )
    world.store.put("retail_po", po_id, po)
    world.store.put("sales_doc", sales_doc_id, doc)
    world.emit(SalesDocCreated(at=world.now, doc=doc))
    if f_sc != 1.0:  # record the planted effect against the sales document
        world.scenarios.factor("retail_order", world.now, world=world,
                               record=("sales_doc", sales_doc_id), channel=channel, dc=dc)
    if push != 1.0:
        world.scenarios.factor("retail_push", world.now, world=world,
                               record=("sales_doc", sales_doc_id), channel=channel, dc=dc)

    t = world.now + timedelta(hours=r2.uniform(*rcfg.confirm_hours))
    t = next_working_moment(world.ref.calendar, world.local(t), "DE", _wh_region(world),
                            CONFIRM_WINDOW[0], CONFIRM_WINDOW[1])
    world.schedule("retail.confirm", t, f"retail.confirm:{po_id}", {"po_id": po_id})


def _record_cut_phase(world, channel: str, dc: str, today: date, ordered: int, expected: float) -> None:
    """retailer_overstock cut phase: one truth row per DC and order day (ordered / expected units)."""
    if world.scenarios is None:
        return
    mod = _scenario_api("retailer_overstock")
    w = mod.cut_phase(world, channel, world.now)
    if w is None:
        return
    world.scenarios.record_effect(
        world, mod.SCENARIO_ID, w.window_id, "retailer_dc_week", f"{dc}:{today.isoformat()}",
        "post_overstock_cut", ordered / max(expected, 1.0),
        {"channel": channel, "ordered_units": int(ordered), "expected_units": round(expected, 1)},
    )


# ---------------------------------------------------------------------------
# Confirmation and delivery planning
# ---------------------------------------------------------------------------


@handler("retail.confirm")
def on_confirm(world, ev) -> None:
    po_id = int(ev.data["po_id"])
    if not world.store.has("retail_po", po_id):
        return
    po: RetailPO = world.store.mut("retail_po", po_id)
    doc: SalesDoc = world.store.mut("sales_doc", po.sales_doc_id)
    inv: RetailerInventory | None = (
        world.store.mut("retailer_inventory", po.dc_account)
        if world.store.has("retailer_inventory", po.dc_account) else None
    )
    warehouse = _api("warehouse")
    cat = world.ref.catalogue
    push = _push_factor(world, po.retailer, po.dc_account)
    changes: dict[int, dict] = {}
    dlines: list[tuple[int, int, int]] = []
    ordered_units = cut_units = 0
    cut_value = 0.0
    cut_by_sku: dict[int, int] = {}
    for line, item in zip(po.lines, doc.items):
        granted = int(warehouse.reserve(world, line.sku_id, line.qty))
        upc = max(int(cat.get(line.sku_id).units_per_case), 1)
        keep = (granted // upc) * upc
        if keep < granted:
            warehouse.unreserve(world, line.sku_id, granted - keep)
        line.confirmed_qty = keep
        item.confirmed_qty = keep
        ordered_units += int(line.qty)
        if keep != line.qty:
            cut = int(line.qty) - keep
            cut_units += cut
            cut_value += item.net_value * cut / line.qty if line.qty else 0.0
            cut_by_sku[line.sku_id] = cut_by_sku.get(line.sku_id, 0) + cut
            ch: dict = {"confirmed_qty": keep}
            if keep == 0:
                item.rejection_reason = "out_of_stock"
                ch["rejection_reason"] = "out_of_stock"
            changes[item.item_no] = ch
            if inv is not None:
                left = inv.on_order.get(line.sku_id, 0) - cut
                inv.on_order[line.sku_id] = max(left, 0)
                if push != 1.0:  # warehouse cuts do not reduce the over-order
                    inv.push_backlog[line.sku_id] = inv.push_backlog.get(line.sku_id, 0.0) + cut
        if keep > 0:
            dlines.append((line.sku_id, keep, item.item_no))

    _api("deductions").record_cut(world, po.po_id, po.retailer, po.dc_account, ordered_units, cut_units,
                                  round(cut_value, 2))
    _record_hero_cuts(world, doc.sales_doc_id, po, cut_by_sku)

    if not dlines:
        doc.status = "rejected"
        po.status = "cancelled"
        world.emit(SalesDocChanged(at=world.now, sales_doc_id=doc.sales_doc_id,
                                   header={"status": "rejected"}, items=changes))
        world.store.remove("retail_po", po_id)
        world.store.remove("sales_doc", doc.sales_doc_id)
        return

    po.status = "confirmed"
    if changes:
        world.emit(SalesDocChanged(at=world.now, sales_doc_id=doc.sales_doc_id, items=changes))

    region = _wh_region(world)
    planned = world.ref.calendar.next_working_day(world.local_date(), "DE", region)
    dc_acc = world.ref.accounts.get(po.dc_account)
    delivery = _api("fulfilment").create_delivery(
        world, "retail", doc, dlines, po.dc_account, dataclasses.replace(dc_acc.address),
        None, planned, po_id, 1,
    )
    po.delivery_id = delivery.delivery_id
    po.status = "delivery_planned"


def _record_hero_cuts(world, sales_doc_id: int, po: RetailPO, cut_by_sku: dict[int, int]) -> None:
    """hero_oos_substitution: one truth row per sales document with hero units cut in a block."""
    if not cut_by_sku or world.scenarios is None:
        return
    mod = _scenario_api("hero_oos_substitution")
    for sku_id in sorted(cut_by_sku):
        b = mod.active_block(world, sku_id, world.now)
        if b is None:
            continue
        world.scenarios.record_effect(
            world, mod.SCENARIO_ID, b.window_id, "sales_doc", sales_doc_id, "hero_oos_cut", cut_by_sku[sku_id],
            {"hero": sku_id, "channel": po.retailer, "dc": po.dc_account, "po_id": po.po_id},
        )


# ---------------------------------------------------------------------------
# Shipment, proof of delivery, invoice, payment
# ---------------------------------------------------------------------------


def _po_for_delivery(world, delivery_id: int):
    d = world.store.get("delivery", delivery_id)
    if d is None or d.order_id is None or not world.store.has("retail_po", d.order_id):
        return None, None
    return d, world.store.mut("retail_po", d.order_id)


@handler("retail.shipped")
def on_shipped(world, ev) -> None:
    d, po = _po_for_delivery(world, int(ev.data["delivery_id"]))
    if po is None:
        return
    doc: SalesDoc = world.store.mut("sales_doc", po.sales_doc_id)
    by_item = {i.item_no: i for i in doc.items}
    shipped: dict[int, int] = {}
    for line in d.lines:
        shipped[line.sales_item_no] = shipped.get(line.sales_item_no, 0) + int(line.qty)
    items: dict[int, dict] = {}
    for item_no in sorted(shipped):
        item = by_item[item_no]
        item.delivered_qty = shipped[item_no]
        items[item_no] = {"delivered_qty": item.delivered_qty, "confirmed_qty": item.confirmed_qty}
    doc.status = "in_process"
    world.emit(SalesDocChanged(at=world.now, sales_doc_id=doc.sales_doc_id,
                               header={"status": "in_process"}, items=items))
    po.status = "shipped"


@handler("retail.pod")
def on_pod(world, ev) -> None:
    d, po = _po_for_delivery(world, int(ev.data["delivery_id"]))
    if po is None:
        return
    if world.store.has("retailer_inventory", po.dc_account):
        inv: RetailerInventory = world.store.mut("retailer_inventory", po.dc_account)
        for line in d.lines:
            q = int(line.qty)
            inv.on_hand[line.sku_id] = int(inv.on_hand.get(line.sku_id, 0)) + q
            inv.on_order[line.sku_id] = max(inv.on_order.get(line.sku_id, 0) - q, 0)
    po.status = "delivered"
    _api("deductions").record_pod(world, po.po_id, po.retailer, po.dc_account, d.delivery_id,
                                  [(line.sku_id, int(line.qty)) for line in d.lines])

    rng = world.rng("retail.invoice", po.po_id)
    lo, hi = world.cfg.retail.invoice_after_pod_days
    cal = world.ref.calendar
    bd = world.local_date() + timedelta(days=rng.randint(lo, hi))
    bd = cal.next_working_day(bd, "DE", _wh_region(world), include_today=True)
    t = world.at_local(bd, 0) + timedelta(hours=INVOICE_BATCH_HOUR + 2.0 * rng.random())
    t = max(t, world.now + timedelta(minutes=30))
    world.schedule("retail.invoice", t, f"retail.invoice:{po.po_id}",
                   {"po_id": po.po_id, "delivery_id": d.delivery_id})


@handler("retail.invoice")
def on_invoice(world, ev) -> None:
    po_id = int(ev.data["po_id"])
    if not world.store.has("retail_po", po_id):
        return
    po: RetailPO = world.store.mut("retail_po", po_id)
    doc = world.store.get("sales_doc", po.sales_doc_id)
    delivery = world.store.get("delivery", po.delivery_id)
    rc = world.cfg.channels.retailers()[po.retailer]
    bd = world.local_date()
    due = bd + timedelta(days=rc.payment_terms_days)
    inv_doc = _api("billing").create_invoice(world, doc, delivery, None, bd, due, False)
    po.billing_id = inv_doc.billing_id
    po.invoice_gross = round(inv_doc.net_value + inv_doc.tax, 2)
    po.status = "invoiced"
    if world.store.has("sales_doc", po.sales_doc_id):
        world.store.mut("sales_doc", po.sales_doc_id).status = "completed"
    world.emit(SalesDocChanged(at=world.now, sales_doc_id=po.sales_doc_id,
                               header={"status": "completed"}))
    if delivery is not None:
        world.store.remove("delivery", delivery.delivery_id)

    rng = world.rng("retail.payment", po_id)
    j = int(world.cfg.retail.payment_days_jitter)
    pay = due + timedelta(days=rng.randint(-j, j))
    pay = world.ref.calendar.next_working_day(pay, "DE", _wh_region(world), include_today=True)
    lo, hi = PAYMENT_HOURS
    t = world.at_local(pay, 0) + timedelta(hours=lo + (hi - lo) * rng.random())
    t = max(t, world.now + timedelta(days=1))
    world.schedule("retail.payment", t, f"retail.payment:{po_id}",
                   {"po_id": po_id, "billing_id": inv_doc.billing_id})


def _clear_invoice(world, billing_id: int, paid: float | None, status: str | None) -> None:
    """``billing.clear`` with the explicit status (section 5.4).

    Falls back to the phase-1 signature while ``billing.clear`` has no ``status``
    keyword (the paid amount then derives the same status as long as nothing is
    short-paid).
    """
    clear = _api("billing").clear
    try:
        params = inspect.signature(clear).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        params = {}
    if "status" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        clear(world, billing_id, paid, status=status)
    else:
        clear(world, billing_id, paid)


@handler("retail.payment")
def on_payment(world, ev) -> None:
    po_id = int(ev.data["po_id"])
    if not world.store.has("retail_po", po_id):
        return
    po: RetailPO = world.store.mut("retail_po", po_id)
    billing_id = int(ev.data.get("billing_id") or po.billing_id)
    gross = po.invoice_gross
    taken = _api("deductions").take_on_payment(world, po.retailer, billing_id, gross) if gross else 0.0
    _clear_invoice(world, billing_id, round(gross - taken, 2) if gross else None,
                   "cleared" if gross else None)
    po.status = "paid"
    world.store.remove("retail_po", po_id)
    world.store.remove("sales_doc", po.sales_doc_id)
    world.store.remove("billing", billing_id)
