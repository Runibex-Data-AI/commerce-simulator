"""Promotion calendar: web promotions, retailer promotions and scenario promotions.

The calendar is a pure function of seed, config, the catalogue and the
scenario windows (``world.scenarios.scenario_promotions()``). It never reads
entity state, so the demand functions built on it may be used by arrival
sources.

* **Web promotions**: ``Poisson(web_promos_per_month)`` per calendar month
  (capped), each starting on a random day of that month and running
  ``web_promo_days`` days, ``web_discount_choices`` off, a code promotion with
  probability ``code_share`` (else automatic), occasionally with free shipping.
  Scope: a brand, a category (SKU list), or a handful of SKUs, chosen with a
  bias towards in-season products. November additionally has a sitewide
  "Black Week" code promotion from the Monday of Black Friday week to Cyber
  Monday (no demand lift of its own: the Black Friday event factor already
  carries it). Ids: ``year * 10000 + month * 100 + i``.
* **Retailer promotions**: per enabled retailer and quarter,
  ``promo_weeks_per_quarter`` promotion weeks split into promotions of one or
  two weeks (Monday to Sunday), on hero SKUs plus in-season SKUs of the
  retailer's assortment, ``retail_discount_choices`` off. Promotion weeks lie
  in weeks ``retail_forward_buy_weeks + 1`` to ``n - 1`` of the retail quarter
  (ISO weeks, ``models.retail_calendar``), so a forward buy, the promotion
  and its replenishment order fall in the same retail quarter.
  Ids: ``60_000_000 + year * 10000 + channel index * 1000 + quarter * 100 + i``.
* **Scenario promotions** (e.g. ``promo_margin_trap``) from the scenario
  manager, web channel, automatic. Ids: ``900_000 + index``.

Dates: regular promotions start at 00:00 local on their first day and end at
23:59:59 local on their last day (``Promotion.end`` is inclusive). Scenario
windows end exclusive at their window end; the Promotion's ``end`` is one
second earlier so ``start <= t <= end`` holds for every promotion.

Announcement: event ``promo.announce:{promo_id}`` at ``announced_at`` (web: 3
days before the start, 10:00-11:59 local; retailer: 6 weeks before, 09:00
local; scenario: at the window start if switched on live, else 3 days before).
The handler stores the Promotion (kind ``promotion``) and emits
``PromotionDefined``. Promotions announced before the history start that are
still running or upcoming are announced at the history start by a bootstrap.
"""

from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Iterable

from ..engine.queue import Event
from ..engine.registry import arrival_source, bootstrap, handler
from ..models import pricing
from ..models.retail_calendar import quarter_weeks, retail_quarter
from ..models.seasonality import category_factor
from .events import PromotionChanged, PromotionDefined
from .model import Promotion, Sku

RETAIL_ID_BASE = 60_000_000
SCENARIO_ID_BASE = 900_000
RETAIL_CHANNEL_INDEX = {"drogerie_kranz": 1, "mylo": 2, "shopnova": 3, "belle": 4}

WEB_ANNOUNCE_DAYS = 3
RETAIL_ANNOUNCE_DAYS = 42
SCENARIO_ANNOUNCE_DAYS = 3
SCENARIO_DEFAULT_DAYS = 28  # provisional length of an open-ended scenario promotion
MAX_WEB_PROMOS_PER_MONTH = 5
FREE_SHIPPING_PROB = 0.15
FREE_SHIPPING_LIFT = 1.05  # demand bump from free shipping on top of the price lift
BLACK_WEEK_INDEX = 90
BLACK_WEEK_DISCOUNT = 0.20
RETAIL_MAX_WEEKS = 2
RETAIL_QUARTER_TAIL_WEEKS = 1  # last weeks of a retail quarter without promotion weeks
SEASONAL_FACTOR_MIN = 1.25  # category factor above which a SKU counts as in season
_CACHE_LIMIT = 6000

_SCOPE_BRAND = 0.35  # P(brand scope); then category up to 0.60; else selected SKUs
_SCOPE_CATEGORY = 0.60
_ROLE_WEIGHT = {"mass": 0.5, "derma": 0.3, "seasonal": 0.2}
_CATEGORY_WORDS = {
    "face_care": ("FACE", "Face care"),
    "body_care": ("BODY", "Body care"),
    "cleansing": ("CLEAN", "Cleansing"),
    "hand_care": ("HANDS", "Hand care"),
    "lip_care": ("LIPS", "Lip care"),
    "sun_care": ("SUN", "Sun care"),
    "after_sun": ("AFTERSUN", "After sun"),
    "gift_set": ("GIFT", "Gift sets"),
}
_SKU_WORDS = ("GLOW", "FAVES", "SKIN", "CARE", "TREAT")

_DAY = timedelta(days=1)
_SECOND = timedelta(seconds=1)


@dataclass(slots=True)
class _Rec:
    """A promotion plus derived values used by the lookups."""

    promo: Promotion
    start_d: date  # first local day
    last_d: date  # last local day (inclusive)
    skus: frozenset[int] | None  # explicit SKU scope, else None (brand / sitewide)
    year: int  # calendar month the promotion starts in (event data)
    month: int


class _State:
    def __init__(self, world) -> None:
        cfg = world.cfg
        self.cfg = cfg
        self.web_months: dict[tuple[int, int], list[_Rec]] = {}
        self.retail_q: dict[tuple[str, int, int], list[_Rec]] = {}
        self.by_id: dict[int, _Rec] = {}
        self.announce: dict[date, list[_Rec]] = {}
        self.scen_version: object = object()
        self.scen: list[_Rec] = []
        self.scen_by_id: dict[int, _Rec] = {}
        self.scen_window: dict[int, str | None] = {}
        self.day_factor: dict[tuple, dict[int, float]] = {}
        self.day_active: dict[tuple, list[_Rec]] = {}
        self.cal: dict[tuple, list[Promotion]] = {}
        pc = cfg.promotions
        self.dip_days = int(pc.dip_weeks) * 7
        max_days = max(list(pc.web_promo_days) + [RETAIL_MAX_WEEKS * 7, 8])
        self.lookback = max_days + self.dip_days + 2  # days back a promo can still matter
        self.retailers = dict(cfg.channels.retailers())


def _state(world) -> _State:
    st = world.__dict__.get("_promotions_state")
    if st is None or st.cfg is not world.cfg:
        st = _State(world)
        world.__dict__["_promotions_state"] = st
    return st


def _version(world) -> tuple:
    """Cache key part for everything that depends on the scenario windows."""
    sc = world.scenarios
    if sc is None:
        return (0, None)
    return (getattr(sc, "version", 0), getattr(sc, "history_start", None))


def _bounded(cache: dict) -> dict:
    if len(cache) > _CACHE_LIMIT:
        cache.clear()
    return cache


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _month_iter(d0: date, d1: date) -> Iterable[tuple[int, int]]:
    y, m = d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _quarter(month: int) -> int:
    return (month - 1) // 3 + 1


def _weighted_sample(rng, items: list, weights: list[float], k: int) -> list:
    """``k`` distinct items, weighted, without replacement (deterministic for an rng)."""
    items = list(items)
    weights = [max(w, 0.0) for w in weights]
    out = []
    for _ in range(min(k, len(items))):
        total = sum(weights)
        if total <= 0:
            break
        x = rng.random() * total
        acc = 0.0
        idx = len(items) - 1
        for i, w in enumerate(weights):
            acc += w
            if x < acc:
                idx = i
                break
        out.append(items.pop(idx))
        weights.pop(idx)
    return out


def _add(st: _State, world, promo: Promotion, skus: Iterable[int] | None, year: int, month: int) -> _Rec:
    rec = _Rec(
        promo=promo,
        start_d=world.local_date(promo.start),
        last_d=world.local_date(promo.end),
        skus=frozenset(skus) if skus else None,
        year=year,
        month=month,
    )
    st.by_id[promo.promo_id] = rec
    st.announce.setdefault(world.local_date(promo.announced_at), []).append(rec)
    return rec


def _pct(x: float) -> int:
    return int(round(x * 100))


def _gen_web_month(st: _State, world, year: int, month: int) -> list[_Rec]:
    cfg = st.cfg
    pc = cfg.promotions
    cat = world.ref.catalogue
    rng = world.rng("promo.web", year, month)
    n_days = _calendar.monthrange(year, month)[1]
    n = min(rng.poisson(pc.web_promos_per_month), MAX_WEB_PROMOS_PER_MONTH)
    recs: list[_Rec] = []
    codes: set[str] = set()
    for i in range(n):
        start_d = date(year, month, rng.randint(1, n_days))
        days = int(rng.choice(list(pc.web_promo_days)))
        discount = float(rng.choice(list(pc.web_discount_choices)))
        is_code = rng.random() < pc.code_share
        free_ship = rng.random() < FREE_SHIPPING_PROB
        scope_u = rng.random()
        active = cat.active(start_d)
        if not active:
            continue
        brand: str | None = None
        sku_ids: list[int] = []
        if scope_u < _SCOPE_BRAND:
            brands = sorted({s.brand for s in active})
            weights = [_ROLE_WEIGHT.get(cat.brand_roles.get(b, ""), 0.2) for b in brands]
            brand = _weighted_sample(rng, brands, weights, 1)[0]
            word, label = brand.upper(), brand
        elif scope_u < _SCOPE_CATEGORY:
            by_cat: dict[str, float] = {}
            for s in active:
                by_cat[s.category] = by_cat.get(s.category, 0.0) + s.popularity * category_factor(
                    s.season_profile, start_d
                )
            cats = sorted(by_cat)
            category = _weighted_sample(rng, cats, [by_cat[c] for c in cats], 1)[0]
            sku_ids = [s.sku_id for s in active if s.category == category]
            word, label = _CATEGORY_WORDS.get(category, (category.upper(), category))
        else:
            k = rng.randint(3, 8)
            chosen = _weighted_sample(
                rng, active, [s.popularity * category_factor(s.season_profile, start_d) for s in active], k
            )
            sku_ids = sorted(s.sku_id for s in chosen)
            word, label = _SKU_WORDS[rng.randrange(len(_SKU_WORDS))], "Selected favourites"
        last_d = start_d + timedelta(days=days - 1)
        code = None
        if is_code:
            code = f"{word}{_pct(discount)}-{year % 100:02d}{month:02d}"
            suffix = 0
            while code in codes:
                suffix += 1
                code = f"{word}{_pct(discount)}-{year % 100:02d}{month:02d}{chr(64 + suffix)}"
            codes.add(code)
        name = f"{label} {_pct(discount)}% off" + (" + free shipping" if free_ship else "")
        ann_d = start_d - timedelta(days=WEB_ANNOUNCE_DAYS)
        minute = rng.randrange(120)
        announced = world.at_local(ann_d, 10 + minute // 60, minute % 60)
        promo = Promotion(
            promo_id=year * 10000 + month * 100 + i,
            channel="web",
            name=name,
            kind="code" if is_code else "automatic",
            code=code,
            discount_pct=discount,
            free_shipping=free_ship,
            sku_ids=sku_ids,
            brand=brand,
            start=world.at_local(start_d, 0),
            end=world.at_local(last_d + _DAY, 0) - _SECOND,
            announced_at=announced,
        )
        recs.append(_add(st, world, promo, sku_ids, year, month))
    if month == 11:
        bf = world.ref.calendar.black_friday(year)
        mon = bf - timedelta(days=bf.weekday())
        last_d = mon + timedelta(days=7)  # through Cyber Monday
        promo = Promotion(
            promo_id=year * 10000 + month * 100 + BLACK_WEEK_INDEX,
            channel="web",
            name=f"Black Week {_pct(BLACK_WEEK_DISCOUNT)}% off everything",
            kind="code",
            code=f"BLACKWEEK{year % 100:02d}",
            discount_pct=BLACK_WEEK_DISCOUNT,
            free_shipping=False,
            sku_ids=[],
            brand=None,
            start=world.at_local(mon, 0),
            end=world.at_local(last_d + _DAY, 0) - _SECOND,
            announced_at=world.at_local(mon - timedelta(days=WEB_ANNOUNCE_DAYS), 10),
        )
        recs.append(_add(st, world, promo, None, year, month))
    recs.sort(key=lambda r: (r.promo.start, r.promo.promo_id))
    return recs


def _gen_retail_quarter(st: _State, world, channel: str, year: int, q: int) -> list[_Rec]:
    rc = st.retailers[channel]
    pc = st.cfg.promotions
    cat = world.ref.catalogue
    rng = world.rng("promo.retail", channel, year, q)
    q_start = date(year, 3 * q - 2, 1)
    q_end = date(year + (q == 4), 1 if q == 4 else 3 * q + 1, 1)  # exclusive
    first_mon = q_start + timedelta(days=(7 - q_start.weekday()) % 7)
    # Promotion weeks lie inside the retailer's own retail quarter (ISO weeks),
    # clear of its edges: not in the first ``retail_forward_buy_weeks`` weeks
    # (the forward buy is ordered in the same quarter) and not in the last
    # RETAIL_QUARTER_TAIL_WEEKS weeks (the promotion and its replenishment
    # order fall in the same quarter). Retailers plan promotion slots per
    # retail quarter; it also keeps sell-in and sell-out of a quarter aligned.
    n_weeks = len(quarter_weeks(year, q))
    lo_idx = int(pc.retail_forward_buy_weeks) + 1
    hi_idx = n_weeks - RETAIL_QUARTER_TAIL_WEEKS
    mondays = []
    m = first_mon
    while m + timedelta(days=6) < q_end:
        iso_week = m.isocalendar()[1]
        if retail_quarter(m) == (year, q) and lo_idx <= iso_week - 13 * (q - 1) <= hi_idx:
            mondays.append(m)
        m += timedelta(days=7)
    taken: set[int] = set()  # week indexes used (incl. one week buffer)
    weeks_left = int(rc.promo_weeks_per_quarter)
    recs: list[_Rec] = []
    try:
        ret_name = world.ref.accounts.retailer(channel).name
    except KeyError:
        ret_name = channel
    i = 0
    while weeks_left > 0 and i < 20:
        dur = RETAIL_MAX_WEEKS if weeks_left >= RETAIL_MAX_WEEKS and rng.random() < 0.5 else 1
        slots = [
            k for k in range(len(mondays) - dur + 1)
            if all(j not in taken for j in range(k - 1, k + dur + 1))
        ]
        if not slots:
            break
        k = slots[rng.randrange(len(slots))]
        for j in range(k, k + dur):
            taken.add(j)
        start_d = mondays[k]
        last_d = start_d + timedelta(days=7 * dur - 1)
        assort = [cat.get(s) for s in cat.retailer_assortment(channel, start_d)]
        mid = start_d + timedelta(days=3)
        heroes = [s for s in assort if s.hero]
        hero_pick = _weighted_sample(rng, heroes, [s.popularity for s in heroes], rng.randint(2, 3))
        seasonal = [
            s for s in assort
            if not s.hero and category_factor(s.season_profile, mid) >= SEASONAL_FACTOR_MIN
        ]
        if not seasonal:
            seasonal = sorted((s for s in assort if not s.hero), key=lambda s: (-s.popularity, s.sku_id))[:10]
        seas_pick = _weighted_sample(
            rng, seasonal, [s.popularity * category_factor(s.season_profile, mid) for s in seasonal],
            rng.randint(1, 3),
        )
        sku_ids = sorted({s.sku_id for s in hero_pick + seas_pick})
        discount = float(rng.choice(list(pc.retail_discount_choices)))
        iso_week = start_d.isocalendar()[1]
        promo = Promotion(
            promo_id=RETAIL_ID_BASE + year * 10000 + RETAIL_CHANNEL_INDEX.get(channel, 9) * 1000 + q * 100 + i,
            channel=channel,
            name=f"{ret_name} promotion week {iso_week:02d}/{start_d.isocalendar()[0]}",
            kind="automatic",
            code=None,
            discount_pct=discount,
            free_shipping=False,
            sku_ids=sku_ids,
            brand=None,
            start=world.at_local(start_d, 0),
            end=world.at_local(last_d + _DAY, 0) - _SECOND,
            announced_at=world.at_local(start_d - timedelta(days=RETAIL_ANNOUNCE_DAYS), 9),
        )
        if sku_ids:
            recs.append(_add(st, world, promo, sku_ids, start_d.year, start_d.month))
        weeks_left -= dur
        i += 1
    recs.sort(key=lambda r: (r.promo.start, r.promo.promo_id))
    return recs


def _web_month(st: _State, world, year: int, month: int) -> list[_Rec]:
    recs = st.web_months.get((year, month))
    if recs is None:
        recs = _gen_web_month(st, world, year, month)
        st.web_months[(year, month)] = recs
    return recs


def _retail_quarter(st: _State, world, channel: str, year: int, q: int) -> list[_Rec]:
    key = (channel, year, q)
    recs = st.retail_q.get(key)
    if recs is None:
        recs = _gen_retail_quarter(st, world, channel, year, q) if channel in st.retailers else []
        st.retail_q[key] = recs
    return recs


def _to_dt(world, x, end: bool = False) -> datetime | None:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x
    if isinstance(x, date):  # local date: start at 00:00; end exclusive at 00:00 of the next day
        return world.at_local(x + _DAY, 0) if end else world.at_local(x, 0)
    if isinstance(x, str):
        return _to_dt(world, datetime.fromisoformat(x) if "T" in x or " " in x else date.fromisoformat(x), end)
    raise TypeError(f"unsupported datetime value {x!r}")


def _scenario_recs(st: _State, world) -> list[_Rec]:
    v = _version(world)  # specs also depend on the manager's history start
    if st.scen_version == v:
        return st.scen
    sc = world.scenarios
    specs = list(sc.scenario_promotions()) if sc is not None and hasattr(sc, "scenario_promotions") else []
    recs: list[_Rec] = []
    by_id: dict[int, _Rec] = {}
    windows: dict[int, str | None] = {}
    for pos, spec in enumerate(specs):
        start = _to_dt(world, spec.get("start"))
        if start is None:
            continue
        end_x = _to_dt(world, spec.get("end"), end=True)
        if end_x is None:
            end_x = start + timedelta(days=SCENARIO_DEFAULT_DAYS)
        end = end_x - _SECOND
        if end < start:
            continue
        index = spec.get("index", pos)
        pid = SCENARIO_ID_BASE + int(index)
        brand = spec.get("brand")
        discount = float(spec.get("discount_pct") or 0.0)
        free_ship = bool(spec.get("free_shipping", False))
        live = bool(spec.get("live", False))
        announced = start if live else start - timedelta(days=SCENARIO_ANNOUNCE_DAYS)
        floor = _to_dt(world, spec.get("announce_not_before"))
        if floor is not None and announced < floor:
            # Window introduced by a config change behind the clock: announce
            # as soon as it is known (an announcement in the past would be lost).
            announced = floor
        label = brand or "Everything"
        name = f"{label} {_pct(discount)}% off" + (" + free shipping" if free_ship else "")
        promo = Promotion(
            promo_id=pid,
            channel="web",
            name=name,
            kind="automatic",
            code=None,
            discount_pct=discount,
            free_shipping=free_ship,
            sku_ids=[],
            brand=brand,
            start=start,
            end=end,
            announced_at=announced,
            scenario_id=spec.get("scenario_id"),
        )
        sd = world.local_date(start)
        rec = _Rec(promo, sd, world.local_date(end), None, sd.year, sd.month)
        recs.append(rec)
        by_id[pid] = rec
        windows[pid] = spec.get("window_id")
    recs.sort(key=lambda r: (r.promo.start, r.promo.promo_id))
    st.scen, st.scen_by_id, st.scen_window, st.scen_version = recs, by_id, windows, v
    return recs


def _recs_starting(
    st: _State, world, channel: str | None, d0: date, d1: date, scen_from: date | None = None
) -> list[_Rec]:
    """Promotions of ``channel`` (None = all) whose first day lies in [d0, d1].

    With ``scen_from``, scenario promotions (which may be long) are included
    whenever they start by ``d1`` and their last day plus the dip period
    reaches ``scen_from``.
    """
    out: list[_Rec] = []
    months = list(_month_iter(d0, d1))
    scen: list[_Rec] = []
    if channel in (None, "web"):
        for y, m in months:
            out.extend(_web_month(st, world, y, m))
        for r in _scenario_recs(st, world):
            if d0 <= r.start_d <= d1:
                out.append(r)
            elif scen_from is not None and r.start_d <= d1 and r.last_d + timedelta(days=st.dip_days) >= scen_from:
                scen.append(r)
    chans = sorted(st.retailers) if channel is None else ([channel] if channel != "web" else [])
    for ch in chans:
        seen: set[tuple[int, int]] = set()
        for y, m in months:
            q = _quarter(m)
            if (y, q) in seen:
                continue
            seen.add((y, q))
            out.extend(_retail_quarter(st, world, ch, y, q))
    res = [r for r in out if d0 <= r.start_d <= d1] + scen
    res.sort(key=lambda r: (r.promo.start, r.promo.promo_id))
    return res


def _active_day(st: _State, world, channel: str, d: date) -> list[_Rec]:
    v = _version(world) if channel == "web" else None
    key = (channel, d, v)
    hit = st.day_active.get(key)
    if hit is None:
        hit = [
            r for r in _recs_starting(st, world, channel, d - timedelta(days=st.lookback), d, scen_from=d)
            if r.last_d >= d
        ]
        _bounded(st.day_active)[key] = hit
    return hit


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def calendar(world, year: int, month: int) -> list[Promotion]:
    """All promotions (web, retailer, scenario) starting in that month, sorted by start then id."""
    st = _state(world)
    key = (year, month, _version(world))
    hit = st.cal.get(key)
    if hit is None:
        last = date(year, month, _calendar.monthrange(year, month)[1])
        hit = [r.promo for r in _recs_starting(st, world, None, date(year, month, 1), last)]
        _bounded(st.cal)[key] = hit
    return list(hit)


def promotions_between(world, start: date, end: date, channel: str | None = None) -> list[Promotion]:
    """Promotions running on at least one local day of [start, end] (inclusive), sorted by start, id."""
    st = _state(world)
    recs = _recs_starting(st, world, channel, start - timedelta(days=st.lookback), end, scen_from=start)
    return [r.promo for r in recs if r.last_d >= start]


def active_web_promos(world, t: datetime) -> list[Promotion]:
    """Web promotions (incl. scenario promotions) running at instant ``t``."""
    st = _state(world)
    return [
        r.promo for r in _active_day(st, world, "web", world.local_date(t))
        if r.promo.start <= t <= r.promo.end
    ]


def is_eligible(promo: Promotion, sku: Sku) -> bool:
    """SKU scope if the promotion lists SKUs, else brand scope, else sitewide."""
    if promo.sku_ids:
        return sku.sku_id in promo.sku_ids
    if promo.brand:
        return sku.brand == promo.brand
    return True


def web_promo_for_basket(world, t: datetime, sku_ids: list[int], rng) -> Promotion | None:
    """Best applicable web promotion for a basket, or None.

    Automatic promotions apply whenever at least one basket SKU is eligible.
    Code promotions are used with probability ``code_redemption_share``: one
    draw from ``rng`` per applicable code promotion, in promo id order. Among
    usable promotions the one with the largest value wins (discount times the
    eligible SKUs' RRP plus the shipping fee if it waives shipping; ties to the
    lower promo id).
    """
    promos = active_web_promos(world, t)
    if not promos or not sku_ids:
        return None
    cfg = world.cfg
    cat = world.ref.catalogue
    skus = [cat.get(s) for s in sku_ids]
    share = cfg.promotions.code_redemption_share
    fee = cfg.channels.web.shipping_fee_eur
    best: tuple[float, int] | None = None
    best_p: Promotion | None = None
    for p in sorted(promos, key=lambda p: p.promo_id):
        eligible = [s for s in skus if is_eligible(p, s)]
        if not eligible:
            continue
        if p.kind == "code" and not rng.random() < share:
            continue
        value = p.discount_pct * sum(s.rrp_eur for s in eligible) + (fee if p.free_shipping else 0.0)
        key = (-value, p.promo_id)
        if best is None or key < best:
            best, best_p = key, p
    return best_p


def promo_lift(world, promo: Promotion) -> float:
    """Demand multiplier on the covered SKUs while ``promo`` runs (1.0 for Black Week)."""
    return _lift(world, promo)


def _is_black_week(promo: Promotion) -> bool:
    pid = promo.promo_id
    return promo.channel == "web" and 10_000_000 <= pid < RETAIL_ID_BASE and pid % 100 == BLACK_WEEK_INDEX


def _lift(world, promo: Promotion) -> float:
    """Demand multiplier while the promotion runs.

    The Black Week promotion gets no lift of its own: it is the mechanism
    behind the configured Black Friday event factor, which already lifts
    web demand, so stacking both would overshoot ``black_friday_lift``.
    """
    if _is_black_week(promo):
        return 1.0
    pc = world.cfg.promotions
    lift = pricing.elasticity_lift(min(max(promo.discount_pct, 0.0), 0.95), pc.elasticity)
    if promo.channel == "web":
        if promo.free_shipping:
            lift *= FREE_SHIPPING_LIFT
        if promo.kind == "code":
            lift = 1.0 + (lift - 1.0) * pc.code_redemption_share
    return lift


def _scope_ids(world, rec: _Rec, channel: str, d: date) -> Iterable[int]:
    if rec.skus is not None:
        return rec.skus
    cat = world.ref.catalogue
    if channel == "web":
        skus = cat.active(d)
    else:
        skus = [cat.get(i) for i in cat.retailer_assortment(channel, d)]
    p = rec.promo
    return [s.sku_id for s in skus if is_eligible(p, s)]


def day_factors(world, channel: str, d: date) -> dict[int, float]:
    """``{sku_id: demand factor}`` for every SKU whose factor on ``d`` is not 1 (cached)."""
    st = _state(world)
    v = _version(world) if channel == "web" else None
    key = (channel, d, v)
    hit = st.day_factor.get(key)
    if hit is not None:
        return hit
    pc = world.cfg.promotions
    lifts: dict[int, float] = {}
    dips: dict[int, float] = {}
    for rec in _recs_starting(st, world, channel, d - timedelta(days=st.lookback), d, scen_from=d):
        if d > rec.last_d + timedelta(days=st.dip_days):
            continue
        lift = _lift(world, rec.promo)
        if lift <= 1.0:
            continue  # no lift (e.g. Black Week): neither lift nor dip
        ids = _scope_ids(world, rec, channel, d)
        if d <= rec.last_d:
            for s in ids:
                if lift > lifts.get(s, 0.0):
                    lifts[s] = lift
        else:
            f = pricing.post_promo_dip(
                lift, pc.pull_forward_share, (d - rec.last_d).days - 1, st.dip_days,
                (rec.last_d - rec.start_d).days + 1,
            )
            if f != 1.0:
                for s in ids:
                    dips[s] = dips.get(s, 1.0) * f
    out = {s: max(f, pricing.MIN_DIP_FACTOR) for s, f in dips.items() if s not in lifts}
    out.update(lifts)
    _bounded(st.day_factor)[key] = out
    return out


def demand_factor(world, sku_id: int, channel: str, d: date) -> float:
    """Promotion lift while a promotion of ``channel`` covers the SKU on ``d``; post-promo dip after; else 1."""
    return day_factors(world, channel, d).get(sku_id, 1.0)


def retail_promos(world, channel: str, d: date) -> list[Promotion]:
    """Retailer promotions of ``channel`` running on local day ``d``."""
    return [r.promo for r in _active_day(_state(world), world, channel, d)]


def retail_promos_starting(world, channel: str, start: date, end: date) -> list[Promotion]:
    """Retailer promotions of ``channel`` whose first day lies in [start, end) (half-open)."""
    if end <= start:
        return []
    st = _state(world)
    return [r.promo for r in _recs_starting(st, world, channel, start, end - _DAY)]


# ---------------------------------------------------------------------------
# Announcement: arrival source, bootstrap, handler
# ---------------------------------------------------------------------------


def _announce_event(rec: _Rec, due: datetime) -> Event:
    p = rec.promo
    return Event(
        due=due,
        kind="promo.announce",
        key=f"promo.announce:{p.promo_id}",
        data={"promo_id": p.promo_id, "year": rec.year, "month": rec.month, "channel": p.channel},
    )


def _ensure_announcements(st: _State, world, d: date) -> list[_Rec]:
    """Regular promotions announced on local day ``d`` (generates the months they start in)."""
    hi = d + timedelta(days=RETAIL_ANNOUNCE_DAYS + 7)
    for y, m in _month_iter(d, hi):
        _web_month(st, world, y, m)
        for ch in sorted(st.retailers):
            _retail_quarter(st, world, ch, y, _quarter(m))
    return st.announce.get(d, [])


@arrival_source("promo.announce")
def announce_arrivals(world, start: datetime, end: datetime) -> list[Event]:
    st = _state(world)
    out: list[Event] = []
    d, d1 = world.local_date(start), world.local_date(end)
    while d <= d1:
        for rec in _ensure_announcements(st, world, d):
            a = rec.promo.announced_at
            if start < a <= end:
                out.append(_announce_event(rec, a))
        d += _DAY
    for rec in _scenario_recs(st, world):
        a = rec.promo.announced_at
        if start < a <= end:
            out.append(_announce_event(rec, a))
    out.sort(key=lambda e: (e.due, e.key))
    return out


@bootstrap(order=15)
def announce_running_promotions(world) -> None:
    """At history start: announce promotions announced earlier that are still running or upcoming."""
    st = _state(world)
    hs = world.now
    hd = world.local_date(hs)
    _ensure_announcements(st, world, hd)
    recs = _recs_starting(st, world, None, hd - timedelta(days=st.lookback), hd + timedelta(days=RETAIL_ANNOUNCE_DAYS + 7))
    seen: set[int] = set()
    for rec in recs + _scenario_recs(st, world):
        p = rec.promo
        if p.promo_id in seen or p.announced_at > hs or p.end < hs:
            continue
        seen.add(p.promo_id)
        key = f"promo.announce:{p.promo_id}"
        if not world.is_scheduled(key):
            ev = _announce_event(rec, hs)
            world.schedule(ev.kind, hs, key, ev.data)


def find(world, promo_id: int, year: int | None = None, month: int | None = None) -> Promotion | None:
    """The calendar Promotion with this id (generating its month if needed), or None."""
    rec = _find_rec(_state(world), world, int(promo_id), year, month)
    return rec.promo if rec else None


def scenario_window_id(world, promo: Promotion) -> str | None:
    """Scenario window id behind a scenario promotion (for ``record_effect``), else None."""
    if promo.scenario_id is None:
        return None
    st = _state(world)
    _scenario_recs(st, world)
    return st.scen_window.get(promo.promo_id)


def _find_rec(st: _State, world, pid: int, year: int | None, month: int | None) -> _Rec | None:
    if SCENARIO_ID_BASE <= pid < SCENARIO_ID_BASE + 1_000_000:
        _scenario_recs(st, world)
        return st.scen_by_id.get(pid)
    rec = st.by_id.get(pid)
    if rec is not None:
        return rec
    if pid >= RETAIL_ID_BASE:
        rem = pid - RETAIL_ID_BASE
        y, ch_idx, q = rem // 10000, (rem // 1000) % 10, (rem // 100) % 10
        for ch, idx in RETAIL_CHANNEL_INDEX.items():
            if idx == ch_idx and 1 <= q <= 4:
                _retail_quarter(st, world, ch, y, q)
    else:
        y, m = pid // 10000, (pid // 100) % 100
        if 1 <= m <= 12:
            _web_month(st, world, y, m)
    if year is not None and month is not None:
        _web_month(st, world, int(year), int(month))
        for ch in sorted(st.retailers):
            _retail_quarter(st, world, ch, int(year), _quarter(int(month)))
    return st.by_id.get(pid)


def _copy(p: Promotion) -> Promotion:
    return replace(p, sku_ids=list(p.sku_ids))


@handler("promo.announce")
def on_announce(world, ev) -> None:
    pid = int(ev.data["promo_id"])
    if world.store.has("promotion", pid):
        return
    st = _state(world)
    rec = _find_rec(st, world, pid, ev.data.get("year"), ev.data.get("month"))
    if not rec:
        return  # e.g. a scenario promotion withdrawn before its announcement
    p = _copy(rec.promo)
    world.store.put("promotion", pid, p)
    world.emit(PromotionDefined(at=world.now, promotion=_copy(p)))


def scenario_window_closed(world, window_id: str) -> None:
    """A live scenario window was closed (``scen.close``): end its announced promotion.

    Called from the scenario manager's close handler, so it runs once, at the
    close instant, in event order (restart safe). The calendar record already
    carries the new end (the close is visible to ``scenario_promotions()``);
    the stored Promotion and ``shop.promotions`` (``PromotionChanged``) follow.
    A promotion withdrawn at the instant it started is marked withdrawn.
    Promotions not announced yet need nothing: the announcement uses the
    rebuilt record.
    """
    sc = world.scenarios
    w = sc.window(window_id) if sc is not None else None
    if w is None:
        return
    pid = SCENARIO_ID_BASE + int(w.index)
    if not world.store.has("promotion", pid):
        return
    st = _state(world)
    _scenario_recs(st, world)
    rec = st.scen_by_id.get(pid)
    p = world.store.get("promotion", pid)
    withdrawn = rec is None
    new_end = p.start if withdrawn else rec.promo.end
    if new_end == p.end and not withdrawn:
        return
    p = world.store.mut("promotion", pid)
    p.end = new_end
    world.emit(PromotionChanged(at=world.now, promotion=_copy(p), withdrawn=withdrawn))


__all__ = [
    "calendar",
    "promotions_between",
    "active_web_promos",
    "web_promo_for_basket",
    "is_eligible",
    "demand_factor",
    "promo_lift",
    "day_factors",
    "retail_promos",
    "retail_promos_starting",
    "find",
    "scenario_window_id",
]
