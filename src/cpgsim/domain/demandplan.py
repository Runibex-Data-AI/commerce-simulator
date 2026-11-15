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

Web weights then apply the scenario demand transfers
(``scenarios.transfers("sku_demand", ...)``: ``hero_oos_substitution`` moves
``share`` of the hero's weight to its sibling and drops the rest).

Expected web units for planning (``expected_daily_units``) add the deal-site
orders of ``promo_margin_trap`` windows (``_deal_seeker_units``), which come on
top of the regular web order rate. Expected retail units are multiplied by the
``retail_push`` factor (``retailer_overstock``), so the central warehouse
plans production for a pushing retailer's over-orders.

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
# analytic ``annual_web_net_eur`` on the default config (24-month backfill,
# web goods billed net of credit memos, shipping charges excluded): web orders
# run above base_orders_per_day (promotion, event and launch lifts, repeat
# customers' response to Black Friday / Christmas, the promo_margin_trap deal
# seekers) while repeat and subscription baskets are ~15 % smaller than the
# new-customer basket model. Phase 1 (web and two drugstore chains): 0.97.
# Phase 2 (five channels, marketing-driven acquisition): 0.94; 0.97 gave a web
# goods share of 9.85 % in 2025 and 9.78 % in the last 12 months of the default
# 24-month backfill, and 9.21 % in the acceptance run from 2025-01-06 (shorter
# history, fewer repeat customers), against 10 %. Marketing recalibration
# (influencer first-order discount, paid media) brought the acceptance run's web
# goods share down to 9.50 % at 0.94 and 300 orders a day, a realisation of about
# 0.89 against that run's retail. The default web baseline then rose to 315
# orders a day and this constant to 0.895 = 0.94 x 300 / 315: retail sizing
# (proportional to base_orders_per_day x this constant) keeps the absolute size
# it had at 300 orders a day (to 0.03 %), so the 5 % more web volume shows up as
# web share (expected about 9.9 %).
WEB_VALUE_REALISATION = 0.895
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
        self.events: dict[tuple, object] = {}  # web event factors and event windows (pure in config/calendar)
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
    if channel == "web":
        moves = _transfers(world, t, d)
        if moves:
            for src, dst, share in moves:
                if src in by_sku and dst in by_sku:
                    by_sku[dst] += share * by_sku[src]
                    by_sku[src] = 0.0
            acc = 0.0
            cum = []
            for sid in ids:
                acc += by_sku[sid]
                cum.append(acc)
    return _put(st.weights, key, _Weights(ids, cum, acc, sum_pop, by_sku))


def _transfers(world, t: datetime, d: date) -> list[tuple[int, int, float]]:
    """Web demand transfers ``(from_sku, to_sku, share)`` of the scenario windows active at ``t``.

    ``hero_oos_substitution`` moves ``share`` of the hero's expected units to
    its sibling and loses the rest (AMB-12). Retail channels get no transfer:
    retailer DC stock buffers the stock-out, and retail substitution acts on
    lost units (``retail.on_sellout``).
    """
    sc = world.scenarios
    if sc is None or not hasattr(sc, "transfers"):
        return []
    return [(src, dst, share) for _, src, dst, share in sc.transfers("sku_demand", t, world=world, channel="web", d=d)]


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


def web_event_factor(world, d: date) -> float:
    """Calendar event factor of the web channel on local date ``d`` (Black Friday, Christmas, ...)."""
    st = _state(world)
    key = ("ev", d)
    hit = st.events.get(key)
    if hit is None:
        hit = _put(st.events, key, event_factor(st.cfg.seasonality, d, "web", None, world.ref.calendar))
    return hit


def repeat_lift(world, sku_ids, d: date) -> float:
    """How strongly an existing customer is drawn to rebuy ``sku_ids`` on local date ``d``.

    Web event factor times the mean promotion lift of the SKUs; only lifts
    count (factors below 1, such as the Christmas Eve lull or a post-promo dip,
    are read as 1: the repeat stream gets its post-event dip from pull-forward,
    see ``customers``). Pure in config, calendar and the promotion calendar.
    """
    ev = max(1.0, web_event_factor(world, d))
    ids = list(sku_ids)
    if not ids:
        return ev
    f = promotions.day_factors(world, "web", d)
    promo = sum(max(1.0, f.get(int(s), 1.0)) for s in ids) / len(ids)
    return ev * promo


@dataclass(frozen=True, slots=True)
class DemandWindow:
    """A stretch of local days in which web demand is lifted (event or promotion)."""

    first: date
    last: date  # inclusive
    lift: float  # mean demand multiplier over the window
    kind: str  # "black_friday" | "christmas" | "promotion"


CHRISTMAS_RUNUP = ((12, 1), (12, 20))


def _event_windows(world, year: int) -> list[DemandWindow]:
    st = _state(world)
    key = ("evwin", year)
    hit = st.events.get(key)
    if hit is None:
        cal = world.ref.calendar
        bf = cal.black_friday(year)
        mon = bf - timedelta(days=bf.weekday())
        days = [mon + timedelta(days=i) for i in range(8)]  # Black Friday week through Cyber Monday
        bf_lift = sum(web_event_factor(world, x) for x in days) / len(days)
        (m0, d0), (m1, d1) = CHRISTMAS_RUNUP
        first = max(date(year, m0, d0), days[-1] + timedelta(days=1))
        last = date(year, m1, d1)
        xmas = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        x_lift = sum(web_event_factor(world, x) for x in xmas) / len(xmas) if xmas else 1.0
        hit = [DemandWindow(days[0], days[-1], bf_lift, "black_friday")]
        if xmas and x_lift > 1.0:
            hit.append(DemandWindow(first, last, x_lift, "christmas"))
        _put(st.events, key, hit)
    return hit


def demand_windows(
    world, sku_ids, d0: date, d1: date, known_at: datetime | None = None
) -> list[DemandWindow]:
    """Web demand windows relevant to ``sku_ids`` that start within [d0, d1], by start date.

    Calendar events (Black Friday week through Cyber Monday, Christmas run-up)
    apply to every SKU and are always known. Web promotions count when they
    cover at least one of the SKUs, actually lift demand (the Black Week code
    promotion does not: the event factor carries it) and, with ``known_at``,
    have been announced by then (nobody stocks up for a promotion they have
    not heard of; this also keeps the result causal for live scenario
    promotions). A pure function of config, calendar and the promotion
    calendar, so it may be evaluated when a future intent is planned.
    """
    out: list[DemandWindow] = []
    for y in range(d0.year, d1.year + 1):
        out.extend(w for w in _event_windows(world, y) if d0 <= w.first <= d1)
    ids = [int(s) for s in sku_ids]
    if ids:
        cat = world.ref.catalogue
        skus = [cat.get(s) for s in ids]
        for p in promotions.promotions_between(world, d0, d1, "web"):
            first = world.local_date(p.start)
            if not d0 <= first <= d1 or (known_at is not None and p.announced_at > known_at):
                continue
            if not any(promotions.is_eligible(p, s) for s in skus):
                continue
            lift = promotions.promo_lift(world, p)
            if lift > 1.0:
                out.append(DemandWindow(first, world.local_date(p.end), lift, "promotion"))
    out.sort(key=lambda w: (w.first, w.last, w.kind))
    return out


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
            for s, u in _deal_seeker_units(world, d).items():
                hit[s] = hit.get(s, 0.0) + u
        else:
            week = _retail_week(world, channel, d - timedelta(days=d.weekday()))
            # retailer_overstock: the pushing retailer's orders run ``over_order``
            # above demand, and the central warehouse plans for them (pure in the
            # scenario windows).
            push = _scen_factor(world, "retail_push", world.at_local(d, 12), channel=channel)
            hit = {s: u * push / 7.0 for s, u in week.items()}
        _put(st.expected, key, hit)
    return hit


def _deal_seeker_units(world, d: date) -> dict[int, float]:
    """Expected web units per SKU on local date ``d`` from promo_margin_trap deal seekers.

    Pure in config, catalogue and the scenario windows: the hourly arrival rate
    of ``customers.deal_seeker_rate_per_hour`` (summed over the local day, DST
    days included) times the expected deal basket, spread over the promoted
    brand's SKUs with the weights ``customers.deal_basket`` draws by (web weight
    / price ** DEAL_PRICE_SENSITIVITY; one line, a second distinct line with
    DEAL_SECOND_LINE_PROB, quantity 2 with DEAL_QTY_TWO_PROB). Like the arrival
    source, each hour's deal orders go to the first window active then.
    """
    from . import customers  # deferred: customers resolves demandplan lazily too

    wins = customers.deal_windows(world)
    if not wins:
        return {}
    t0 = world.at_local(d, 0)
    t1 = world.at_local(d + timedelta(days=1), 0)
    if not any(w["share"] > 0 and w["start"] < t1 and w["end"] > t0 for w in wins):
        return {}
    orders: dict[str, float] = {}
    t = t0
    hour = timedelta(hours=1)
    while t < t1:
        mid = t + hour / 2
        active = [w for w in wins if w["start"] <= mid < w["end"]]
        if active:
            rate = customers.deal_seeker_rate_per_hour(world, mid)
            if rate > 0:
                brand = active[0]["brand"]
                orders[brand] = orders.get(brand, 0.0) + rate
        t += hour
    if not orders:
        return {}
    cat = world.ref.catalogue
    w = _weights(world, "web", d)
    qty = 1.0 + customers.DEAL_QTY_TWO_PROB
    p2 = customers.DEAL_SECOND_LINE_PROB
    out: dict[int, float] = {}
    for brand in sorted(orders):
        pool: list[tuple[int, float]] = []
        for sid in w.ids:
            x = w.by_sku[sid]
            sku = cat.get(sid)
            if sku.brand == brand and x > 0:
                pool.append((sid, x / max(sku.rrp_eur, 0.5) ** customers.DEAL_PRICE_SENSITIVITY))
        tot = sum(x for _, x in pool)
        if tot <= 0:
            continue
        p = [(sid, x / tot) for sid, x in pool]
        # P(second line is s) = p_s * sum_{j != s} p_j / (1 - p_j) (drawn without replacement)
        odds = sum(pj / (1.0 - pj) for _, pj in p if pj < 1.0) if len(p) > 1 else 0.0
        for sid, ps in p:
            second = ps * (odds - (ps / (1.0 - ps) if ps < 1.0 else 0.0)) if len(p) > 1 else 0.0
            out[sid] = out.get(sid, 0.0) + orders[brand] * qty * (ps + p2 * second)
    return out


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
    "web_event_factor",
    "repeat_lift",
    "demand_windows",
    "DemandWindow",
]
