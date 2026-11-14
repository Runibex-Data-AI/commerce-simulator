"""Expected consumer demand per SKU, channel and day.

Everything here is a pure function of seed, config, catalogue, calendar, the
promotion calendar and the scenario windows (never of entity state), so web
arrival rates may be built on it. Results are cached per
``(channel, date, scenario cache key)``; the scenario key changes whenever the
set of scenario windows changes.

SKU weight on a local date ``d`` for a channel::

    popularity
    * seasonality.category_factor(profile, d)
    * brand-specific calendar event factor (e.g. Mother's Day for Cutara)
    * promotions.demand_factor(sku, channel, d)       # lift / post-promo dip
    * launch ramp (0.3 -> 1.0 over the first 8 weeks after launch_date)
    * scenarios.factor("sku_demand", noon of d, channel=..., sku_id=...)

Sizing of retail demand: a retailer's annual sell-in value is the web annual
net value estimate times ``share_r / share_web``; it is converted to units with
the popularity-weighted net trade price of the retailer's assortment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..models import baskets
from ..models.seasonality import category_factor, event_factor
from . import promotions
from .catalogue import REFERENCE_DATE

LAUNCH_RAMP_DAYS = 56
LAUNCH_RAMP_START = 0.3
DAYS_PER_YEAR = 365.0
WEEKS_PER_YEAR = 365.0 / 7.0
_CACHE_LIMIT = 6000
# Retail sizing is calibrated against the web value the simulation actually
# realises, so the channel value shares in the generated data match the config
# (share_r / share_web). Realised web goods net value per year divided by the
# analytic ``annual_web_net_eur`` on the default config (24-month backfill):
# web orders run ~11 % above base_orders_per_day (promotion, event and launch
# lifts) while repeat and subscription baskets are ~15 % smaller than the
# new-customer basket model, net 0.94.
WEB_VALUE_REALISATION = 0.94
_DC_WEIGHT_SPREAD = 0.5  # DC weights are 0.75 + 0.5 * u before normalising


@dataclass(slots=True)
class _Weights:
    ids: list[int]
    cum: list[float]
    total: float
    sum_pop: float  # sum of popularity of the same SKUs
    by_sku: dict[int, float]


class _State:
    def __init__(self, world) -> None:
        self.cfg = world.cfg
        self.weights: dict[tuple, _Weights] = {}
        self.web_rate: dict[tuple, float] = {}
        self.expected: dict[tuple, dict[int, float]] = {}
        self.retail_week: dict[tuple, dict[int, float]] = {}
        self.brand_event: dict[tuple, dict[str, float]] = {}
        self.retail_annual: dict[str, tuple[float, float]] = {}
        self.dc_weights: dict[str, dict[str, float]] = {}
        self.basket: dict | None = None
        self.retailers: tuple[str, ...] = tuple(sorted(world.cfg.channels.retailers()))


def _state(world) -> _State:
    st = world.__dict__.get("_demandplan_state")
    if st is None or st.cfg is not world.cfg:
        st = _State(world)
        world.__dict__["_demandplan_state"] = st
    return st


def _put(cache: dict, key, value):
    if len(cache) > _CACHE_LIMIT:
        cache.clear()
    cache[key] = value
    return value


def _scen_key(world) -> tuple:
    return promotions._version(world)


def _scen_factor(world, name: str, t: datetime, **ctx) -> float:
    sc = world.scenarios
    if sc is None:
        return 1.0
    return float(sc.factor(name, t, **ctx))


def _launch_ramp(launch: date, d: date) -> float:
    days = (d - launch).days
    if days >= LAUNCH_RAMP_DAYS:
        return 1.0
    if days <= 0:
        return LAUNCH_RAMP_START
    return LAUNCH_RAMP_START + (1.0 - LAUNCH_RAMP_START) * days / LAUNCH_RAMP_DAYS


def _brand_events(st: _State, world, channel: str, d: date) -> dict[str, float]:
    key = (channel, d)
    hit = st.brand_event.get(key)
    if hit is None:
        scfg = st.cfg.seasonality
        cal = world.ref.calendar
        base = event_factor(scfg, d, channel, None, cal)
        hit = {}
        for b in sorted(world.ref.catalogue.brand_roles):
            hit[b] = event_factor(scfg, d, channel, b, cal) / base if base > 0 else 1.0
        _put(st.brand_event, key, hit)
    return hit


def _channel_skus(world, channel: str, d: date):
    cat = world.ref.catalogue
    if channel == "web":
        return cat.active(d)
    return [cat.get(i) for i in cat.retailer_assortment(channel, d)]


def _weights(world, channel: str, d: date) -> _Weights:
    st = _state(world)
    key = (channel, d, _scen_key(world))
    hit = st.weights.get(key)
    if hit is not None:
        return hit
    skus = _channel_skus(world, channel, d)
    brand_ev = _brand_events(st, world, channel, d)
    promo = promotions.day_factors(world, channel, d)
    t = world.at_local(d, 12)
    ids: list[int] = []
    cum: list[float] = []
    by_sku: dict[int, float] = {}
    acc = 0.0
    sum_pop = 0.0
    for s in skus:
        w = s.popularity * category_factor(s.season_profile, d) * brand_ev.get(s.brand, 1.0)
        w *= promo.get(s.sku_id, 1.0)
        if (d - s.launch_date).days < LAUNCH_RAMP_DAYS:
            w *= _launch_ramp(s.launch_date, d)
        w *= _scen_factor(world, "sku_demand", t, channel=channel, sku_id=s.sku_id)
        w = max(w, 0.0)
        acc += w
        sum_pop += s.popularity
        ids.append(s.sku_id)
        cum.append(acc)
        by_sku[s.sku_id] = w
    return _put(st.weights, key, _Weights(ids, cum, acc, sum_pop, by_sku))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sku_weights(world, channel: str, d: date) -> tuple[list[int], list[float]]:
    """Active SKUs of ``channel`` on local date ``d`` and their CUMULATIVE weights.

    The lists are cached and shared: callers must not modify them.
    """
    w = _weights(world, channel, d)
    return w.ids, w.cum


def web_order_rate_per_day(world, t: datetime) -> float:
    """Expected total web orders per day at the local date of ``t`` (before the intraday shape)."""
    st = _state(world)
    d = world.local_date(t)
    key = (d, _scen_key(world))
    base = st.web_rate.get(key)
    if base is None:
        cfg = st.cfg
        w = _weights(world, "web", d)
        level = w.total / w.sum_pop if w.sum_pop > 0 else 0.0
        base = cfg.channels.web.base_orders_per_day * level * event_factor(
            cfg.seasonality, d, "web", None, world.ref.calendar
        )
        _put(st.web_rate, key, base)
    return base * _scen_factor(world, "web_orders", t)


def basket_stats(world) -> dict:
    """Analytic web basket statistics (cached).

    Keys: ``lines`` (E[lines] = 1 + extra mean), ``qty`` (E[qty per line]),
    ``unit_net_eur`` (popularity-weighted RRP net of German VAT over the SKUs
    active on the catalogue reference date), ``basket_net_eur`` (E[basket net
    EUR] = lines * qty * unit_net_eur) and ``annual_web_net_eur``
    (base_orders_per_day * 365 * basket_net_eur). Aliases ``e_lines``,
    ``e_qty``, ``e_basket_net_eur`` hold the same values.
    """
    st = _state(world)
    if st.basket is None:
        cfg = st.cfg
        web = cfg.channels.web
        cat = world.ref.catalogue
        skus = cat.active(REFERENCE_DATE) or list(cat.skus)
        vat = web.vat.get("DE", 0.19)
        pop = sum(s.popularity for s in skus)
        unit_net = sum(s.popularity * s.rrp_eur / (1.0 + vat) for s in skus) / pop if pop > 0 else 0.0
        lines = 1.0 + web.basket_extra_lines_mean
        p2 = web.qty_two_prob
        qty = 1.0 + p2 + 2.0 * p2 * baskets.QTY_THREE_SHARE
        basket_net = lines * qty * unit_net
        st.basket = {
            "lines": lines,
            "qty": qty,
            "unit_net_eur": unit_net,
            "basket_net_eur": basket_net,
            "annual_web_net_eur": web.base_orders_per_day * DAYS_PER_YEAR * basket_net,
            "e_lines": lines,
            "e_qty": qty,
            "e_basket_net_eur": basket_net,
        }
    return dict(st.basket)


def _retailer_cfg(world, channel: str):
    rc = getattr(world.cfg.channels, channel, None)
    if rc is None or channel == "web":
        raise ValueError(f"unknown retailer channel {channel!r}")
    return rc


def _retail_annual(st: _State, world, channel: str) -> tuple[float, float]:
    """(annual consumer units, popularity sum of the reference assortment) for a retailer."""
    hit = st.retail_annual.get(channel)
    if hit is None:
        cfg = st.cfg
        rc = _retailer_cfg(world, channel)
        web_share = cfg.channels.web.share
        value = (
            basket_stats(world)["annual_web_net_eur"] * WEB_VALUE_REALISATION * rc.share / web_share
            if web_share > 0 else 0.0
        )
        skus = _channel_skus(world, channel, REFERENCE_DATE)
        pop = sum(s.popularity for s in skus)
        price = (
            sum(s.popularity * s.list_price_eur * (1.0 - rc.trade_discount) for s in skus) / pop
            if pop > 0 else 0.0
        )
        units = value / price if price > 0 else 0.0
        lift = _retail_lift(world, channel, pop) if pop > 0 else 1.0
        hit = (units / lift if lift > 0 else units, pop)
        st.retail_annual[channel] = hit
    return hit


def _retail_lift(world, channel: str, ref_pop: float) -> float:
    """Mean weekly demand level of a retailer over the reference year, relative to popularity.

    Seasonality, brand events, the retailer's own promotions and launch ramps
    lift the weekly weights above plain popularity on average (about 8 %);
    dividing the annual units by this level makes the realised annual sell-in
    hit the sized value. Scenario factors are left out, so the result never
    depends on scenario windows (it is cached for the whole run).
    """
    scfg = world.cfg.seasonality
    cal = world.ref.calendar
    st = _state(world)
    d0 = date(REFERENCE_DATE.year, 1, 1)
    d0 += timedelta(days=(7 - d0.weekday()) % 7)  # first Monday of the reference year
    acc = 0.0
    for k in range(52):
        ws = d0 + timedelta(weeks=k)
        d = ws + timedelta(days=3)
        ev = sum(event_factor(scfg, ws + timedelta(days=i), channel, None, cal) for i in range(7)) / 7.0
        brand_ev = _brand_events(st, world, channel, d)
        promo = promotions.day_factors(world, channel, d)
        tot = 0.0
        for s in _channel_skus(world, channel, d):
            w = s.popularity * category_factor(s.season_profile, d) * brand_ev.get(s.brand, 1.0)
            w *= promo.get(s.sku_id, 1.0)
            if (d - s.launch_date).days < LAUNCH_RAMP_DAYS:
                w *= _launch_ramp(s.launch_date, d)
            tot += max(w, 0.0)
        acc += ev * tot / ref_pop
    return acc / 52.0


def dc_weights(world, channel: str) -> dict[str, float]:
    """Deterministic share of a retailer's demand per DC account (sums to 1)."""
    st = _state(world)
    hit = st.dc_weights.get(channel)
    if hit is None:
        dcs = [a.account_id for a in world.ref.accounts.dcs(channel)]
        raw = {
            a: 1.0 - _DC_WEIGHT_SPREAD / 2 + _DC_WEIGHT_SPREAD * world.rng("demandplan.dc_weight", channel, a).random()
            for a in dcs
        }
        tot = sum(raw.values())
        hit = {a: v / tot for a, v in raw.items()}
        st.dc_weights[channel] = hit
    return hit


def _retail_week(world, channel: str, week_start: date) -> dict[int, float]:
    """``{sku_id: mean weekly consumer units}`` for the whole retailer (all DCs)."""
    st = _state(world)
    key = (channel, week_start, _scen_key(world))
    hit = st.retail_week.get(key)
    if hit is None:
        units, ref_pop = _retail_annual(st, world, channel)
        scfg = st.cfg.seasonality
        cal = world.ref.calendar
        ev = sum(event_factor(scfg, week_start + timedelta(days=i), channel, None, cal) for i in range(7)) / 7.0
        w = _weights(world, channel, week_start + timedelta(days=3))
        k = units / WEEKS_PER_YEAR * ev / ref_pop if ref_pop > 0 else 0.0
        hit = {s: k * x for s, x in w.by_sku.items()}
        _put(st.retail_week, key, hit)
    return hit


def retail_weekly_demand(world, channel: str, dc_account: str, sku_id: int, week_start: date) -> float:
    """Mean hidden consumer demand (units) at one DC region for the week starting ``week_start``.

    0 for an unknown DC, a disabled retailer or a SKU the retailer does not list.
    """
    if channel not in _state(world).retailers:
        return 0.0
    share = dc_weights(world, channel).get(dc_account, 0.0)
    if share <= 0:
        return 0.0
    return _retail_week(world, channel, week_start).get(sku_id, 0.0) * share


def _expected_map(world, channel: str, d: date) -> dict[int, float]:
    st = _state(world)
    key = (channel, d, _scen_key(world))
    hit = st.expected.get(key)
    if hit is None:
        if channel == "web":
            b = basket_stats(world)
            w = _weights(world, "web", d)
            rate = web_order_rate_per_day(world, world.at_local(d, 12))
            k = rate * b["lines"] * b["qty"] / w.total if w.total > 0 else 0.0
            hit = {s: k * x for s, x in w.by_sku.items()}
        else:
            week = _retail_week(world, channel, d - timedelta(days=d.weekday()))
            hit = {s: u / 7.0 for s, u in week.items()}
        _put(st.expected, key, hit)
    return hit


def expected_daily_units(world, sku_id: int, d: date, channel: str | None = None) -> float:
    """Expected units per day for planning. ``channel`` None = web plus all enabled retailers."""
    st = _state(world)
    if channel is not None:
        if channel != "web" and channel not in st.retailers:
            return 0.0
        return _expected_map(world, channel, d).get(sku_id, 0.0)
    total = _expected_map(world, "web", d).get(sku_id, 0.0)
    for ch in st.retailers:
        total += _expected_map(world, ch, d).get(sku_id, 0.0)
    return total


__all__ = [
    "sku_weights",
    "web_order_rate_per_day",
    "expected_daily_units",
    "retail_weekly_demand",
    "basket_stats",
    "dc_weights",
]
