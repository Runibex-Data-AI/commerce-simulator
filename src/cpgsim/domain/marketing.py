"""Marketing: campaign calendar, spend-driven acquisition and daily exports (phase 2).

Spend flights of the paid channels raise new-customer acquisition through a
pure, normalised multiplier (the arrival source stays pure and the annual
calibration intact), shift the acquisition mix and the influencer-code pick,
and every tracked web order is attributed. See the phase-2 design, section 5.5.

Public API (called by ``domain/web.py`` and ``domain/customers.py``)::

    new_customer_multiplier(world, t) -> float    # in [1/MULTIPLIER_MAX, MULTIPLIER_MAX]
    acquisition_mix(world, t) -> dict[str, float]
    pick_influencer_code(world, rng, t) -> str | None   # exactly one rng.random() draw
    note_order(world, order, customer, source, new_customer) -> None

Campaign calendar (pure)
------------------------

**Regular** campaigns depend only on seed, config, the year and the regular web
promotion calendar (built on a scenario-free view of the world), never on
scenario windows. Per calendar quarter (rng ``("mkt.campaigns", channel, year,
quarter[, code])``):

* ``paid_search``: two always-on campaigns (brand 40 %, generic 60 % of the
  always-on budget) plus seasonal pushes (sun care 1 April - 31 July, gifting
  November) at ``U(flight_intensity) x (1 - always_on_share) x B``.
* ``social``: an always-on retargeting campaign plus ``Poisson(flights_per_quarter)``
  flights of ``U(flight_days)`` days at ``U(flight_intensity) x (1 - always_on_share) x B``.
* ``influencer``: one always-on campaign per code and year (Q1 calendar) plus
  ``Poisson(flights_per_quarter)`` collaboration flights per code and quarter;
  a flight costs the code's fixed fee, spread over its days.
* ``email``: one newsletter per month (3 days) and one campaign per regular web
  promotion (``EM-P<promo_id>``); each costs ``newsletter_cost_eur``.
* ``affiliate``: an always-on coupon-site campaign per quarter (listing fee).

``B`` (the channel's baseline daily budget) = expected new customers per day
through the channel at ``base_orders_per_day`` times ``cac_eur``; an
influencer code gets an equal share of the influencer budget.

**Scenario** campaigns: per ``promo_margin_trap`` window an email campaign and
an affiliate deal-site campaign (``EM-X<index>`` / ``AF-X<index>``, ``promo_ref``
= the scenario promotion). The deal-site campaign is paid by commission
(``deal_site_commission_pct`` of attributed net revenue). ``influencer_cohort_churn``
creates no campaign: it scales the plan of the code's campaigns (factor
``mkt_spend``); each boosted campaign gets a truth ``spend_boost`` row and a
``daily_budget_eur`` update while the window runs (``_spend_boosts``).

Campaign events (arrival source ``mkt.campaigns``, priority 15):
``CampaignDefined`` at ``defined_at`` (status ``active`` with no separate active
step if ``defined_at >= start 00:05``, else ``planned`` followed by
``CampaignStatusChanged(active)`` at start 00:05), ``CampaignStatusChanged(ended)``
at end + 1 day 00:05. Campaigns defined before the history start that are still
running are defined by a bootstrap. Spend and attribution use a campaign only
from ``max(active_at, defined_at)``.

Spend to acquisition (pure)
---------------------------

Per paid channel ``c`` (``paid_search``, ``social``, influencer per code):
``plan0_c(d)`` = sum of the active regular campaigns' daily budgets,
``plan_c(d)`` = ``plan0_c(d) x factor("mkt_spend", noon(d), channel, influencer_code)``.
``flight_c = plan_c / baseline_c(Y)`` with ``baseline_c(Y)`` the mean of
``plan0_c`` over calendar year ``Y``. ``M(d) = sum mix_c x flight_c ** beta``
(unpaid channels have flight 1), clamped to ``[1/MULTIPLIER_CAP, MULTIPLIER_CAP]``,
then divided by the rate-weighted yearly mean ``K(Y)`` of the scenario-free
``m0`` (weights: the expected new-customer rate of each day without marketing
and without scenarios). So without scenarios every calendar year keeps the
phase-1 expected new-customer volume.

Daily export (arrival source ``mkt.export``, 04:00-05:00 local, priority 60)
---------------------------------------------------------------------------

For the previous local day: ``CampaignSpendReported`` (one row per usable
campaign), ``SessionsReported`` (channel x country x device) and
``OrdersAttributed`` (one row per tracked web order, single-touch last click),
then the ``MarketingDay`` accumulator is removed. ``note_order`` fills the
accumulator when an order's payment is authorised (tracking draw
``("mkt.track", order_id)``, campaign draw ``("mkt.attrib", order_id)``).

Channel words in ``mkt``: the shop's traffic sources, except that the shop's
``deal_site`` source is the ``affiliate`` channel, so ``mkt.campaigns``,
``mkt.attributions`` and ``mkt.sessions_daily`` share one vocabulary. Coupon
sites take the last click of ``COUPON_INTERCEPT_SHARE`` of the tracked promoted
orders from organic search and direct traffic (rng ``("mkt.coupon",
order_id)``); those orders are attributed to the quarter's coupon-site
campaign. Affiliate sessions come from affiliate clicks only. The deal-site
campaign is paid by commission, so its clicks are derived from the deal
traffic it drives (``clicks >= sessions >= orders``), not from spend, and
``platform_conversions`` never exceed clicks.
"""

from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

from ..engine.queue import Event
from ..engine.registry import arrival_source, bootstrap, handler
from ..engine.world import World
from .customers import _Lazy, lazy
from .events import (
    AttributionRow,
    CampaignDefined,
    CampaignSpendReported,
    CampaignSpendRow,
    CampaignStatusChanged,
    InfluencerCodeRegistered,
    OrdersAttributed,
    SessionsReported,
    SessionsRow,
)
from .model import Campaign, MarketingDay

demandplan = lazy("demandplan")
promotions = lazy("promotions")
masterdata = lazy("masterdata")
customers = lazy("customers")
keys = _Lazy("cpgsim.writers.keys")

MULTIPLIER_CAP = 1.6
MULTIPLIER_MAX = 2.0  # hard upper bound of the returned value (clamp after normalisation); web lam_max covers it

PAID_CHANNELS = ("paid_search", "social", "influencer")  # channels whose spend drives acquisition
MKT_DAY_KIND = "mkt_day"
CAMPAIGN_KIND = "mkt.campaign"
EXPORT_KIND = "mkt.export"
CAMPAIGN_PRIORITY = 15
EXPORT_PRIORITY = 60
SCENARIO_ID_BASE = 900_000  # scenario promotion ids (promotions.SCENARIO_ID_BASE)
TRAP_SCENARIO = "promo_margin_trap"
CHURN_SCENARIO = "influencer_cohort_churn"

DEFINE_DAYS = 14  # campaigns are set up two weeks before they start
DEFINE_HOUR = 9
STATUS_MINUTE = 5  # status changes at 00:05 local
PAID_SEARCH_BRAND_SHARE = 0.4  # brand vs generic split of the always-on paid-search budget
MAX_FLIGHTS = 6  # cap of the Poisson flight count per quarter
NEWSLETTER_DAYS = 3
COUPON_DAILY_EUR = 60.0  # always-on coupon-site listing fee
DEAL_BASKET_NET_EUR = 6.0  # planning estimate of a deal-site basket (budget of the CPA campaign)
OPEN_ENDED_PROMO_DAYS = 28  # = promo_margin_trap.OPEN_ENDED_PROMO_DAYS
INFLUENCER_PLATFORMS = {"short_video": 0.45, "photo_feed": 0.35, "blog": 0.12, "podcast": 0.08}
_HANDLE_PATTERNS = ("{b}", "{b}.glow", "{b}_skin", "the.{b}", "{b}.beauty", "{b}.daily")
WARM_HISTORY_DAYS = 730  # customers.WARM_HISTORY_DAYS: oldest warm-start registration

# Reporting of non-paid channels (CPM in EUR per 1000 impressions or sends, click-through rate).
OTHER_MEDIA = {"email": (1.2, 0.025), "affiliate": (2.5, 0.006)}
SPEND_SIGMA = 0.08
CLICK_SIGMA = 0.10
SESSION_SIGMA = 0.10
CELL_SIGMA = 0.05
CLICK_TO_SESSION = (0.80, 0.92)
USERS_PER_SESSION = (1.25, 1.45)  # sessions / users
PLATFORM_OVERCOUNT = (1.1, 1.6)  # ad platform conversions / attributed orders
PAID_ORGANIC_SHARE = 0.15  # paid channels: sessions not from a campaign click (share of expected sessions)
NEW_USER_SHARE = {
    "organic_search": 0.45, "paid_search": 0.50, "social": 0.60, "influencer": 0.65,
    "marketplace_spillover": 0.70, "direct": 0.25, "email": 0.05, "app": 0.02, "affiliate": 0.75,
}
#: mkt channel of a shop traffic source (identity unless listed).
MKT_CHANNEL = {"deal_site": "affiliate"}
COUPON_SOURCES = ("organic_search", "direct")  # promoted orders whose last click a coupon site can take
COUPON_INTERCEPT_SHARE = 0.15
COUPON_SOURCE = "coupon_site"  # internal attribution source of coupon-site clicks
_SOCIAL_THEMES = {
    1: "New year skin", 2: "Winter rescue", 3: "Spring refresh", 4: "Spring glow", 5: "Sun ready",
    6: "Summer skin", 7: "Summer skin", 8: "After sun", 9: "Autumn repair", 10: "Autumn repair",
    11: "Gift guide", 12: "Gift guide",
}
_CACHE_LIMIT = 4000
_DAY = timedelta(days=1)


# ---------------------------------------------------------------------------
# State and helpers
# ---------------------------------------------------------------------------


class _BaseView(World):
    """Scenario-free view of a world: same seed, config, reference data and time zone.

    Used for everything that must not depend on scenario windows (the regular
    calendar, ``base(d)`` and ``K(Y)``). It has its own demand-plan and
    promotion caches.
    """

    def __init__(self, world) -> None:  # noqa: D107 - deliberately not calling World.__init__
        self.cfg = world.cfg
        self.seed = int(world.seed)
        self.tz = world.tz
        self.ref = world.ref
        self.store = None
        self.queue = None
        self._emit = None
        self.now = world.now
        self.history_start = world.history_start
        self.scenarios = None
        self.mode = "backfill"


@dataclass(slots=True)
class _Day:
    """Marketing state of one local day (with scenario factors)."""

    mult: float
    mix: dict[str, float]
    codes: list[str]  # sorted
    code_cum: list[float]  # cumulative pick weights of ``codes``
    plan: dict[str, float]  # channel key -> plan (EUR/day) with scenario factors


@dataclass(slots=True)
class _Year:
    campaigns: list[Campaign]
    by_id: dict[str, Campaign]
    due: dict[date, list[tuple[datetime, str, Campaign]]]


@dataclass(slots=True)
class _Scen:
    campaigns: list[Campaign]
    by_id: dict[str, Campaign]


class _State:
    def __init__(self, world) -> None:
        self.cfg = world.cfg
        self.ref = world.ref
        self.view = _BaseView(world)
        self.years: dict[int, _Year] = {}
        self.plan0: dict[int, dict[str, list[float]]] = {}
        self.baseline: dict[int, dict[str, float]] = {}
        self.k: dict[int, float] = {}
        self.days: dict[tuple, _Day] = {}
        self.active: dict[tuple, list[Campaign]] = {}
        self.scen: tuple[Any, _Scen] | None = None
        self.terms: dict[str, dict[str, Any]] = {}
        self.expected: dict[tuple, dict[str, float]] = {}


def _state(world) -> _State:
    st = world.__dict__.get("_marketing_state")
    if st is None or st.cfg is not world.cfg or st.ref is not world.ref:
        st = _State(world)
        world.__dict__["_marketing_state"] = st
    return st


def _version(world) -> tuple:
    """Scenario cache key (identical to ``promotions._version``; the module may be stubbed in tests)."""
    sc = world.scenarios
    if sc is None:
        return (0, None)
    return (getattr(sc, "version", 0), getattr(sc, "history_start", None))


def _bounded(cache: dict) -> dict:
    if len(cache) > _CACHE_LIMIT:
        cache.clear()
    return cache


def _noon(world, d: date) -> datetime:
    return world.at_local(d, 12)


def _quarter_bounds(year: int, q: int) -> tuple[date, date]:
    """First and last day of a calendar quarter."""
    first = date(year, 3 * q - 2, 1)
    last_month = 3 * q
    return first, date(year, last_month, _calendar.monthrange(year, last_month)[1])


def _mix(cfg) -> dict[str, float]:
    mix = {k: float(v) for k, v in cfg.customers.acquisition_mix.items() if v > 0}
    total = sum(mix.values())
    return {k: v / total for k, v in mix.items()} if total > 0 else {}


def _codes(cfg) -> list[str]:
    return sorted(cfg.customers.influencer_codes)


def _code_share(cfg) -> float:
    n = len(cfg.customers.influencer_codes)
    return 1.0 / n if n else 0.0


def _paid(cfg) -> dict[str, Any]:
    return {c: cfg.marketing.paid[c] for c in PAID_CHANNELS if c in cfg.marketing.paid}


def baseline_budget(cfg, channel: str) -> float:
    """Baseline daily budget ``B`` of a paid channel (EUR): expected new customers per day x CAC."""
    pc = _paid(cfg).get(channel)
    if pc is None:
        return 0.0
    w = cfg.channels.web
    return w.base_orders_per_day * w.new_customer_share * _mix(cfg).get(channel, 0.0) * pc.cac_eur


def _key(channel: str, code: str | None) -> str:
    return f"influencer:{code}" if channel == "influencer" else channel


def _paid_keys(cfg) -> list[str]:
    out = []
    for c in _paid(cfg):
        if c == "influencer":
            out.extend(_key(c, code) for code in _codes(cfg))
        else:
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Influencer terms (pure)
# ---------------------------------------------------------------------------


def influencer_terms(world, code: str) -> dict[str, Any]:
    """Handle, platform, commission and fixed fee of an influencer code (rng ``("mkt.influencer", code)``)."""
    st = _state(world)
    hit = st.terms.get(code)
    if hit is not None:
        return hit
    cfg = world.cfg
    rng = world.rng("mkt.influencer", code)
    plats = sorted(INFLUENCER_PLATFORMS)
    x = rng.random() * sum(INFLUENCER_PLATFORMS.values())
    platform, acc = plats[-1], 0.0
    for p in plats:
        acc += INFLUENCER_PLATFORMS[p]
        if x < acc:
            platform = p
            break
    stem = "".join(ch for ch in code.lower() if ch.isalpha()) or "creator"
    handle = _HANDLE_PATTERNS[rng.randrange(len(_HANDLE_PATTERNS))].format(b=stem)[:40]
    commission = round(cfg.marketing.influencer_commission_pct * rng.uniform(0.75, 1.25) * 2) / 2
    pc = _paid(cfg).get("influencer")
    fee = 0.0
    if pc is not None:
        per_quarter = (1.0 - pc.always_on_share) * baseline_budget(cfg, "influencer") * _code_share(cfg) * 91.0
        fee = per_quarter / max(pc.flights_per_quarter, 0.5) * rng.uniform(0.8, 1.25)
        fee = max(50.0, round(fee / 50.0) * 50.0)
    hit = {
        "code": code, "handle": handle, "platform": platform, "commission_pct": float(commission),
        "fixed_fee_eur": float(fee), "valid_offset_days": WARM_HISTORY_DAYS + rng.randint(30, 400),
    }
    st.terms[code] = hit
    return hit


# ---------------------------------------------------------------------------
# Regular calendar (pure in seed, config, year and the regular web promotions)
# ---------------------------------------------------------------------------


def _campaign(world, cid, name, channel, objective, start, end, budget, defined_at=None, **kw) -> Campaign:
    if defined_at is None:
        defined_at = world.at_local(start - timedelta(days=DEFINE_DAYS), DEFINE_HOUR)
    return Campaign(
        campaign_id=cid, name=name[:80], channel=channel, objective=objective, start=start, end=end,
        daily_budget_eur=round(max(budget, 0.0), 2), created_on=world.local_date(defined_at),
        defined_at=defined_at, **kw,
    )


def _numbered(world, channel: str, year: int, q: int, specs: list[tuple]) -> list[Campaign]:
    """Regular campaigns of one channel and quarter, numbered in (start, name) order."""
    out = []
    for seq, (start, name, objective, end, budget, kw) in enumerate(sorted(specs, key=lambda s: (s[0], s[1])), 1):
        cid = keys.mkt_campaign_id(channel, year, q, seq)
        out.append(_campaign(world, cid, name, channel, objective, start, end, budget, **kw))
    return out


def _gen_quarter(world, year: int, q: int) -> list[Campaign]:
    cfg = world.cfg
    paid = _paid(cfg)
    first, last = _quarter_bounds(year, q)
    q_days = (last - first).days + 1
    out: list[Campaign] = []

    pc = paid.get("paid_search")
    b = baseline_budget(cfg, "paid_search")
    if pc is not None and b > 0:
        rng = world.rng("mkt.campaigns", "paid_search", year, q)
        aon = pc.always_on_share * b
        specs = [
            (first, f"Brand search {year} Q{q}", "acquisition", last, aon * PAID_SEARCH_BRAND_SHARE, {}),
            (first, f"Generic search {year} Q{q}", "acquisition", last, aon * (1 - PAID_SEARCH_BRAND_SHARE), {}),
        ]
        push = (1.0 - pc.always_on_share) * b
        if q == 2:
            specs.append((date(year, 4, 1), f"Sun care search push {year}", "promotion", date(year, 7, 31),
                          rng.uniform(*pc.flight_intensity) * push, {}))
        if q == 4:
            specs.append((date(year, 11, 1), f"Gifting search push {year}", "promotion", date(year, 11, 30),
                          rng.uniform(*pc.flight_intensity) * push, {}))
        out += _numbered(world, "paid_search", year, q, specs)

    pc = paid.get("social")
    b = baseline_budget(cfg, "social")
    if pc is not None and b > 0:
        rng = world.rng("mkt.campaigns", "social", year, q)
        specs = [(first, f"Retargeting always-on {year} Q{q}", "retention", last, pc.always_on_share * b, {})]
        n = min(rng.poisson(pc.flights_per_quarter), MAX_FLIGHTS)
        for k in range(1, n + 1):
            start = first + timedelta(days=rng.randrange(q_days))
            days = rng.randint(*pc.flight_days)
            intensity = rng.uniform(*pc.flight_intensity)
            specs.append((start, f"Social flight {_SOCIAL_THEMES[start.month]} {year} #{q}{k}", "acquisition",
                          start + timedelta(days=days - 1), intensity * (1.0 - pc.always_on_share) * b, {}))
        out += _numbered(world, "social", year, q, specs)

    pc = paid.get("influencer")
    b = baseline_budget(cfg, "influencer") * _code_share(cfg)
    if pc is not None and b > 0:
        specs = []
        for code in _codes(cfg):
            kw = {"influencer_code": code}
            if q == 1:
                specs.append((date(year, 1, 1), f"{code} always-on {year}", "acquisition", date(year, 12, 31),
                              pc.always_on_share * b, kw))
            rng = world.rng("mkt.campaigns", "influencer", year, q, code)
            fee = influencer_terms(world, code)["fixed_fee_eur"]
            n = min(rng.poisson(pc.flights_per_quarter), MAX_FLIGHTS)
            for k in range(1, n + 1):
                start = first + timedelta(days=rng.randrange(q_days))
                days = rng.randint(*pc.flight_days)
                specs.append((start, f"{code} collaboration {year} Q{q} #{k}", "acquisition",
                              start + timedelta(days=days - 1), fee / days, kw))
        out += _numbered(world, "influencer", year, q, specs)

    rng = world.rng("mkt.campaigns", "email", year, q)
    cost = cfg.marketing.newsletter_cost_eur
    specs = []
    for m in range(3 * q - 2, 3 * q + 1):
        send = date(year, m, 2 + rng.randrange(9))
        specs.append((send, f"Newsletter {_calendar.month_name[m]} {year}", "retention",
                      send + timedelta(days=NEWSLETTER_DAYS - 1), cost / NEWSLETTER_DAYS, {}))
    out += _numbered(world, "email", year, q, specs)

    out += _numbered(world, "affiliate", year, q,
                     [(first, f"Coupon sites {year} Q{q}", "acquisition", last, COUPON_DAILY_EUR, {})])
    return out


def _web_promotions(view, year: int) -> list:
    """Regular web promotions starting in ``year`` (scenario-free view; empty if promotions is stubbed)."""
    fn = getattr(promotions, "promotions_between", None)
    if fn is None:
        return []
    out = []
    for p in fn(view, date(year, 1, 1), date(year, 12, 31), "web"):
        if p.scenario_id is None and view.local_date(p.start).year == year:
            out.append(p)
    return sorted(out, key=lambda p: (p.start, p.promo_id))


def _promo_emails(world, view, year: int) -> list[Campaign]:
    cost = world.cfg.marketing.newsletter_cost_eur
    out = []
    for p in _web_promotions(view, year):
        start, end = world.local_date(p.start), world.local_date(p.end)
        days = (end - start).days + 1
        defined = max(world.at_local(start - timedelta(days=DEFINE_DAYS), DEFINE_HOUR), p.announced_at)
        out.append(_campaign(world, keys.mkt_promo_campaign_id(p.promo_id), f"Promotion email: {p.name}",
                             "email", "promotion", start, end, cost / days, defined_at=defined,
                             promo_id=p.promo_id))
    return out


def active_at(world, c: Campaign) -> datetime:
    return world.at_local(c.start, 0, STATUS_MINUTE)


def ended_at(world, c: Campaign) -> datetime:
    return world.at_local(c.end + _DAY, 0, STATUS_MINUTE)


def usable_from(world, c: Campaign) -> datetime:
    """Spend and attribution use a campaign from this instant on."""
    return max(active_at(world, c), c.defined_at)


def _due_events(world, c: Campaign) -> list[tuple[datetime, str]]:
    out = [(c.defined_at, "defined")]
    act = active_at(world, c)
    if c.defined_at < act:
        out.append((act, "active"))
    out.append((ended_at(world, c), "ended"))
    return out


def _year(world, year: int) -> _Year:
    st = _state(world)
    hit = st.years.get(year)
    if hit is None:
        camps: list[Campaign] = []
        for q in range(1, 5):
            camps += _gen_quarter(world, year, q)
        camps += _promo_emails(world, st.view, year)
        camps.sort(key=lambda c: (c.start, c.campaign_id))
        due: dict[date, list[tuple[datetime, str, Campaign]]] = {}
        for c in camps:
            for t, step in _due_events(world, c):
                due.setdefault(world.local_date(t), []).append((t, step, c))
        hit = _Year(camps, {c.campaign_id: c for c in camps}, due)
        st.years[year] = hit
    return hit


def regular_campaigns(world, year: int) -> list[Campaign]:
    """Regular campaigns starting in calendar ``year`` (pure, scenario-independent), sorted by start, id."""
    return list(_year(world, year).campaigns)


# ---------------------------------------------------------------------------
# Scenario campaigns (one per promo_margin_trap window and kind)
# ---------------------------------------------------------------------------


def _scen_key(world) -> tuple:
    """Cache key of everything built from the scenario campaigns.

    ``_version`` plus the ``visible_from`` bounds of the trap windows: a config
    window introduced after the fresh start may get its ``known_at`` without a
    change of its start (and so of the version), which moves ``defined_at``.
    """
    sc = world.scenarios
    v = _version(world)
    if sc is None or not hasattr(sc, "visible_from") or not hasattr(sc, "windows"):
        return v
    return v + tuple(sc.visible_from(w) for w in sc.windows(TRAP_SCENARIO))


def _scenario(world) -> _Scen:
    st = _state(world)
    v = _scen_key(world)
    if st.scen is not None and st.scen[0] == v:
        return st.scen[1]
    sc = world.scenarios
    camps: list[Campaign] = []
    specs = sc.scenario_promotions() if sc is not None and hasattr(sc, "scenario_promotions") else []
    cfg = world.cfg
    for spec in specs or []:
        if spec.get("scenario_id") != TRAP_SCENARIO or spec.get("start") is None or "index" not in spec:
            continue
        s_at = spec["start"]
        e_at = spec.get("end") or s_at + timedelta(days=OPEN_ENDED_PROMO_DAYS)
        if e_at <= s_at:
            continue
        start = world.local_date(s_at)
        end = max(start, world.local_date(e_at - timedelta(microseconds=1)))
        index = int(spec["index"])
        defined = world.at_local(start - timedelta(days=DEFINE_DAYS), DEFINE_HOUR)
        win = sc.window(spec["window_id"]) if hasattr(sc, "window") and spec.get("window_id") else None
        vf = sc.visible_from(win) if win is not None and hasattr(sc, "visible_from") else None
        if vf is not None and vf > defined:
            defined = vf
        brand = spec.get("brand") or "Everything"
        pct = int(round(float(spec.get("discount_pct") or 0.0) * 100))
        days = (end - start).days + 1
        kw = {"promo_id": SCENARIO_ID_BASE + index, "scenario_id": TRAP_SCENARIO, "window_id": spec.get("window_id")}
        camps.append(_campaign(world, keys.mkt_scenario_campaign_id("email", index),
                               f"Promotion email: {brand} {pct}% off", "email", "promotion", start, end,
                               cfg.marketing.newsletter_cost_eur / days, defined_at=defined, **kw))
        budget = (float(spec.get("deal_seeker_share") or 0.0) * cfg.channels.web.base_orders_per_day
                  * DEAL_BASKET_NET_EUR * cfg.marketing.deal_site_commission_pct / 100.0)
        camps.append(_campaign(world, keys.mkt_scenario_campaign_id("affiliate", index),
                               f"Deal sites: {brand} {pct}% off", "affiliate", "acquisition", start, end,
                               budget, defined_at=defined, **kw))
    camps.sort(key=lambda c: (c.start, c.campaign_id))
    out = _Scen(camps, {c.campaign_id: c for c in camps})
    st.scen = (v, out)
    return out


def scenario_campaigns(world) -> list[Campaign]:
    """Campaigns of the current scenario windows (``promo_margin_trap``), sorted by start, id."""
    return list(_scenario(world).campaigns)


def find_campaign(world, campaign_id: str, year: int | None = None) -> Campaign | None:
    hit = _scenario(world).by_id.get(campaign_id)
    if hit is not None:
        return hit
    years = [year] if year is not None else []
    if campaign_id.startswith("EM-P"):
        try:
            years.append(int(campaign_id[4:]) // 10000)
        except ValueError:
            pass
    elif len(campaign_id) > 7 and campaign_id[3:7].isdigit():
        years.append(int(campaign_id[3:7]))
    for y in years:
        hit = _year(world, int(y)).by_id.get(campaign_id)
        if hit is not None:
            return hit
    return None


def active_campaigns(world, d: date) -> list[Campaign]:
    """Regular and scenario campaigns running on local day ``d`` (start <= d <= end), sorted by id."""
    st = _state(world)
    key = (d, _scen_key(world))
    hit = st.active.get(key)
    if hit is None:
        hit = [c for y in (d.year - 1, d.year) for c in _year(world, y).campaigns if c.start <= d <= c.end]
        hit += [c for c in _scenario(world).campaigns if c.start <= d <= c.end]
        hit.sort(key=lambda c: c.campaign_id)
        _bounded(st.active)[key] = hit
    return hit


# ---------------------------------------------------------------------------
# Plans, flights and the acquisition multiplier
# ---------------------------------------------------------------------------


def _year_days(year: int) -> list[date]:
    d0 = date(year, 1, 1)
    return [d0 + timedelta(days=i) for i in range((date(year + 1, 1, 1) - d0).days)]


def _plan0(world, year: int) -> dict[str, list[float]]:
    """Scenario-free plan per paid channel key and day of ``year`` (EUR/day)."""
    st = _state(world)
    hit = st.plan0.get(year)
    if hit is None:
        cfg = world.cfg
        n = len(_year_days(year))
        d0 = date(year, 1, 1)
        hit = {k: [0.0] * n for k in _paid_keys(cfg)}
        for y in (year - 1, year):
            for c in _year(world, y).campaigns:
                if c.channel not in PAID_CHANNELS:
                    continue
                k = _key(c.channel, c.influencer_code)
                arr = hit.get(k)
                if arr is None:
                    continue
                lo = max((c.start - d0).days, 0)
                hi = min((c.end - d0).days, n - 1)
                for i in range(lo, hi + 1):
                    arr[i] += c.daily_budget_eur
        st.plan0[year] = hit
    return hit


def _baseline(world, year: int) -> dict[str, float]:
    st = _state(world)
    hit = st.baseline.get(year)
    if hit is None:
        hit = {k: (sum(v) / len(v) if v else 0.0) for k, v in _plan0(world, year).items()}
        st.baseline[year] = hit
    return hit


def _flight_terms(cfg, flights: dict[str, float]) -> tuple[float, dict[str, float], list[float]]:
    """Raw multiplier M, acquisition weights and influencer code weights from flights per key."""
    beta = cfg.marketing.response_elasticity
    mix = _mix(cfg)
    weights: dict[str, float] = {}
    total = 0.0
    codes = _codes(cfg)
    share = _code_share(cfg)
    code_w = [share * flights.get(_key("influencer", code), 1.0) ** beta for code in codes]
    for c, m in mix.items():
        if c == "influencer" and "influencer" in _paid(cfg) and codes:
            w = m * sum(code_w)
        elif c in _paid(cfg) and c != "influencer":
            w = m * flights.get(c, 1.0) ** beta
        else:
            w = m
        weights[c] = w
        total += w
    for c, v in cfg.customers.acquisition_mix.items():  # keep zero-weight keys as configured
        weights.setdefault(c, float(v))
    return total, weights, code_w


def _clamp(x: float) -> float:
    return min(max(x, 1.0 / MULTIPLIER_CAP), MULTIPLIER_CAP)


def _flights0(world, d: date) -> dict[str, float]:
    plan = _plan0(world, d.year)
    base = _baseline(world, d.year)
    i = (d - date(d.year, 1, 1)).days
    return {k: (plan[k][i] / base[k] if base.get(k, 0.0) > 0 else 1.0) for k in plan}


def base_rate(world, d: date) -> float:
    """Expected new customers per day on ``d`` without marketing and without scenario factors."""
    view = _state(world).view
    return demandplan.web_order_rate_per_day(view, _noon(view, d)) * world.cfg.channels.web.new_customer_share


def normaliser(world, year: int) -> float:
    """``K(Y)``: rate-weighted yearly mean of the clamped scenario-free multiplier (cached by year)."""
    st = _state(world)
    hit = st.k.get(year)
    if hit is None:
        num = den = 0.0
        for d in _year_days(year):
            b = base_rate(world, d)
            m0, _, _ = _flight_terms(world.cfg, _flights0(world, d))
            num += b * _clamp(m0)
            den += b
        hit = num / den if den > 0 else 1.0
        st.k[year] = hit if hit > 0 else 1.0
    return st.k[year]


def _day(world, d: date) -> _Day:
    st = _state(world)
    key = (d, _version(world))
    hit = st.days.get(key)
    if hit is not None:
        return hit
    cfg = world.cfg
    f0 = _flights0(world, d)
    base = _baseline(world, d.year)
    sc = world.scenarios
    noon = _noon(world, d)
    flights: dict[str, float] = {}
    plan: dict[str, float] = {}
    for k, v in f0.items():
        channel, _, code = k.partition(":")
        f = 1.0
        if sc is not None:
            f = float(sc.factor("mkt_spend", noon, channel=channel, influencer_code=code or None))
        flights[k] = v * f
        plan[k] = v * base.get(k, 0.0) * f
    m, weights, code_w = _flight_terms(cfg, flights)
    mult = _clamp(m) / normaliser(world, d.year)
    mult = min(max(mult, 1.0 / MULTIPLIER_MAX), MULTIPLIER_MAX)
    cum, acc = [], 0.0
    for w in code_w:
        acc += w
        cum.append(acc)
    hit = _Day(mult=mult, mix=weights, codes=_codes(cfg), code_cum=cum, plan=plan)
    _bounded(st.days)[key] = hit
    return hit


def new_customer_multiplier(world, t: datetime) -> float:
    """Multiplier on the new-customer arrival rate at ``t`` (marketing spend flights)."""
    return _day(world, world.local_date(t)).mult


def acquisition_mix(world, t: datetime) -> dict[str, float]:
    """Acquisition channel weights for a customer registering at ``t`` (unnormalised)."""
    return _day(world, world.local_date(t)).mix


def pick_influencer_code(world, rng, t: datetime) -> str | None:
    """Influencer code of a customer acquired through an influencer at ``t``.

    Weighted by ``share_code x flight_code ** beta`` in sorted code order;
    always exactly one ``rng.random()`` draw.
    """
    u = rng.random()
    day = _day(world, world.local_date(t))
    if not day.codes:
        return None
    total = day.code_cum[-1]
    if total <= 0:
        return day.codes[min(int(u * len(day.codes)), len(day.codes) - 1)]
    x = u * total
    for code, c in zip(day.codes, day.code_cum):
        if x < c:
            return code
    return day.codes[-1]


def campaign_plan(world, c: Campaign, d: date) -> float:
    """Planned spend of a campaign on ``d`` (daily budget x scenario spend factor)."""
    if c.channel not in PAID_CHANNELS or world.scenarios is None:
        return c.daily_budget_eur
    f = world.scenarios.factor("mkt_spend", _noon(world, d), channel=c.channel, influencer_code=c.influencer_code)
    return c.daily_budget_eur * float(f)


# ---------------------------------------------------------------------------
# Orders: tracking and attribution
# ---------------------------------------------------------------------------

_CAMPAIGN_CHANNEL = {"paid_search": "paid_search", "social": "social", "influencer": "influencer",
                     "email": "email", "deal_site": "affiliate", COUPON_SOURCE: "affiliate"}


def _candidates(world, source: str, code: str | None, t: datetime) -> list[Campaign]:
    ch = _CAMPAIGN_CHANNEL.get(source)
    if ch is None:
        return []
    out = []
    for c in active_campaigns(world, world.local_date(t)):
        if c.channel != ch or usable_from(world, c) > t:
            continue
        if ch == "influencer" and c.influencer_code != code:
            continue
        if source == "deal_site" and c.scenario_id != TRAP_SCENARIO:
            continue
        if source == COUPON_SOURCE and c.scenario_id is not None:
            continue  # coupon sites: the regular always-on campaign
        out.append(c)
    return out


def attribute(world, order_id: int, source: str, code: str | None, t: datetime) -> str | None:
    """Campaign of a tracked order (last click; weighted by plan, rng ``("mkt.attrib", order_id)``)."""
    cands = _candidates(world, source, code, t)
    if not cands:
        return None
    d = world.local_date(t)
    weights = [max(campaign_plan(world, c, d), 0.0) for c in cands]
    total = sum(weights)
    u = world.rng("mkt.attrib", order_id).random()
    if total <= 0:
        return cands[min(int(u * len(cands)), len(cands) - 1)].campaign_id
    x, acc = u * total, 0.0
    for c, w in zip(cands, weights):
        acc += w
        if x < acc:
            return c.campaign_id
    return cands[-1].campaign_id


def _device(cfg, u: float) -> str:
    mix = cfg.marketing.device_mix
    devs = sorted(k for k, v in mix.items() if v > 0)
    x, acc = u * sum(mix[k] for k in devs), 0.0
    for k in devs:
        acc += mix[k]
        if x < acc:
            return k
    return devs[-1]


def note_order(world, order, customer, source: str, new_customer: bool) -> None:
    """A web order was confirmed (payment authorised, at ``world.now``): accumulate it for the daily export.

    Orders whose payment fails are never noted, so mkt counts confirmed orders
    (later cancellations stay attributed, as in a real last-click export). The
    attribution date is the local confirmation date.

    Rng ``("mkt.track", order_id)``: first draw tracking (``tracking_share``),
    second draw the device. Accumulator ``MarketingDay`` (id = local date):
    ``by_channel["channel|country"] = [orders, revenue_eur, new_customers]`` and
    one attribution entry per order ``[order_id, customer_id, channel,
    campaign_id, code, is_new, revenue_eur, net_eur, country, device]``.
    """
    if order is None:
        return
    cfg = world.cfg
    t = world.now
    rng = world.rng("mkt.track", order.order_id)
    if not rng.random() < cfg.marketing.tracking_share:
        return
    device = _device(cfg, rng.random())
    d = world.local_date(t)
    fx = masterdata.fx_rate(world, order.currency, d) if order.currency != "EUR" else 1.0
    revenue = round(order.total * fx, 2)
    vat = cfg.channels.web.vat.get(order.country, 0.19)
    net = round(revenue / (1.0 + vat), 2)
    code = getattr(customer, "influencer_code", None) if source == "influencer" else None
    attr_source = source
    if (source in COUPON_SOURCES and getattr(order, "promo_id", None) is not None
            and world.rng("mkt.coupon", order.order_id).random() < COUPON_INTERCEPT_SHARE):
        attr_source = COUPON_SOURCE
    campaign_id = attribute(world, order.order_id, attr_source, code, t)
    channel = "affiliate" if attr_source == COUPON_SOURCE else MKT_CHANNEL.get(source, source)
    did = d.isoformat()
    md = world.store.get(MKT_DAY_KIND, did)
    if md is None:
        md = world.store.put(MKT_DAY_KIND, did, MarketingDay(day=d))
    else:
        md = world.store.mut(MKT_DAY_KIND, did)
    k = f"{channel}|{order.country}"
    agg = md.by_channel.setdefault(k, [0, 0.0, 0])
    agg[0] += 1
    agg[1] = round(agg[1] + revenue, 2)
    agg[2] += 1 if new_customer else 0
    md.attributions.append([
        order.order_id, order.customer_id, channel, campaign_id, code, bool(new_customer), revenue, net,
        order.country, device,
    ])


# ---------------------------------------------------------------------------
# Expected traffic (pure)
# ---------------------------------------------------------------------------


def expected_orders(world, d: date) -> dict[str, float]:
    """Expected web orders per source on ``d`` (new customers, repeat intents, deal seekers)."""
    st = _state(world)
    key = (d, _version(world))
    hit = st.expected.get(key)
    if hit is not None:
        return hit
    cfg = world.cfg
    w = cfg.channels.web
    s = w.new_customer_share
    noon = _noon(world, d)
    rate = demandplan.web_order_rate_per_day(world, noon)
    day = _day(world, d)
    out: dict[str, float] = {}
    total = sum(v for v in day.mix.values() if v > 0)
    new = rate * s * day.mult
    if total > 0:
        for c, v in day.mix.items():
            if v > 0:
                out[c] = out.get(c, 0.0) + new * v / total
    cal = customers.calibration(world)
    repeat_base = w.base_orders_per_day * (1.0 - s)
    sub_share = min(max(cal.get("sub_orders_per_day", 0.0) / repeat_base, 0.0), 1.0) if repeat_base > 0 else 0.0
    repeat = rate * (1.0 - s) * (1.0 - sub_share)
    rs = customers.REPEAT_SOURCES
    rtot = sum(rs.values())
    for c, v in rs.items():
        out[c] = out.get(c, 0.0) + repeat * v / rtot
    deal = 0.0
    for win in customers.deal_windows(world):
        if win["start"] <= noon < win["end"]:
            deal += win["share"] * w.base_orders_per_day
    if deal > 0:
        out["deal_site"] = out.get("deal_site", 0.0) + deal
    _bounded(st.expected)[key] = out
    return out


# ---------------------------------------------------------------------------
# Arrival sources, bootstrap and handlers
# ---------------------------------------------------------------------------


def _campaign_event(c: Campaign, due: datetime, step: str, year: int | None) -> Event:
    return Event(
        due=due, kind=CAMPAIGN_KIND, key=f"{CAMPAIGN_KIND}:{c.campaign_id}:{step}",
        data={"campaign_id": c.campaign_id, "step": step, "year": year, "end": c.end.isoformat()},
        priority=CAMPAIGN_PRIORITY,
    )


@arrival_source("mkt.campaigns")
def campaign_arrivals(world, start: datetime, end: datetime) -> list[Event]:
    """Campaign definitions and status changes due in ``(start, end]`` (pure in the calendar)."""
    out: list[Event] = []
    d, d1 = world.local_date(start), world.local_date(end)
    while d <= d1:
        for y in (d.year - 1, d.year, d.year + 1):
            for t, step, c in _year(world, y).due.get(d, ()):
                if start < t <= end:
                    out.append(_campaign_event(c, t, step, y))
        d += _DAY
    for c in _scenario(world).campaigns:
        for t, step in _due_events(world, c):
            if start < t <= end:
                out.append(_campaign_event(c, t, step, None))
    out.sort(key=lambda e: (e.due, e.key))
    return out


def export_due(world, d: date) -> datetime:
    """When the export of local day ``d`` runs: the next day, 04:00-05:00 local."""
    rng = world.rng("mkt.sessions", "export", d.isoformat())
    m = rng.randrange(60)
    return world.at_local(d + _DAY, 4, m, rng.randrange(60))


@arrival_source(EXPORT_KIND)
def export_arrivals(world, start: datetime, end: datetime) -> list[Event]:
    """Daily marketing exports due in ``(start, end]`` (for days from the history start on)."""
    out: list[Event] = []
    hs_d = world.local_date(world.history_start)
    d, d1 = world.local_date(start) - _DAY, world.local_date(end)
    while d < d1:
        if d >= hs_d:
            due = export_due(world, d)
            if start < due <= end:
                out.append(Event(due=due, kind=EXPORT_KIND, key=f"{EXPORT_KIND}:{d:%Y%m%d}",
                                 data={"date": d.isoformat()}, priority=EXPORT_PRIORITY))
        d += _DAY
    return out


def _define(world, c: Campaign) -> None:
    status = "active" if c.defined_at >= active_at(world, c) or active_at(world, c) <= world.now else "planned"
    world.emit(CampaignDefined(at=world.now, campaign=replace(c), status=status))


@bootstrap(order=16)
def bootstrap_campaigns(world) -> None:
    """At history start: register the influencer codes and define running or upcoming campaigns."""
    hs = world.now
    hd = world.local_date(hs)
    for code in _codes(world.cfg):
        tm = influencer_terms(world, code)
        world.emit(InfluencerCodeRegistered(
            at=hs, code=code, handle=tm["handle"], audience_platform=tm["platform"],
            commission_pct=tm["commission_pct"], fixed_fee_eur=tm["fixed_fee_eur"],
            valid_from=hd - timedelta(days=tm["valid_offset_days"]),
        ))
    seen: set[str] = set()
    camps = [c for y in (hd.year - 1, hd.year, hd.year + 1) for c in _year(world, y).campaigns]
    camps += _scenario(world).campaigns
    for c in sorted(camps, key=lambda c: (c.defined_at, c.campaign_id)):
        if c.campaign_id in seen or c.defined_at > hs or c.end < hd:
            continue
        seen.add(c.campaign_id)
        _define(world, c)


@handler(CAMPAIGN_KIND)
def on_campaign(world, ev) -> None:
    d = ev.data
    cid, step = d["campaign_id"], d["step"]
    c = find_campaign(world, cid, d.get("year"))
    if step == "defined":
        if c is not None:
            _define(world, c)
    elif step == "active":
        world.emit(CampaignStatusChanged(at=world.now, campaign_id=cid, status="active"))
    elif step == "ended":
        end = c.end if c is not None else date.fromisoformat(d["end"])
        world.emit(CampaignStatusChanged(at=world.now, campaign_id=cid, status="ended", end_date=end))


def _media(cfg, channel: str) -> tuple[float, float]:
    pc = cfg.marketing.paid.get(channel)
    if pc is not None:
        return pc.cpm_eur, pc.ctr
    return OTHER_MEDIA.get(channel, (2.0, 0.01))


def _deal_clicks(world, d: date, n_campaigns: int) -> float:
    """Clicks of one commission-paid deal-site campaign on ``d``: the deal traffic it drives (pure).

    Expected deal sessions (expected deal orders x tracking share / conversion)
    divided by the mean click-to-session share, split over the usable deal-site
    campaigns; so clicks >= sessions >= orders on the channel.
    """
    mc = world.cfg.marketing
    conv = mc.conversion_rates.get("deal_site", 0.0)
    if conv <= 0 or n_campaigns <= 0:
        return 0.0
    sessions = expected_orders(world, d).get("deal_site", 0.0) * mc.tracking_share / conv
    return sessions / (sum(CLICK_TO_SESSION) / 2.0) / n_campaigns


def _spend_rows(world, d: date, md: MarketingDay | None) -> tuple[list[CampaignSpendRow], dict[str, int]]:
    cfg = world.cfg
    end_of_day = world.at_local(d + _DAY, 0)
    orders: dict[str, int] = {}
    net: dict[str, float] = {}
    for a in (md.attributions if md is not None else ()):
        if a[3] is not None:
            orders[a[3]] = orders.get(a[3], 0) + 1
            net[a[3]] = net.get(a[3], 0.0) + float(a[7])
    rows: list[CampaignSpendRow] = []
    clicks_by_channel: dict[str, int] = {}
    usable = [c for c in active_campaigns(world, d) if usable_from(world, c) < end_of_day]
    n_deal = sum(1 for c in usable if c.scenario_id == TRAP_SCENARIO and c.channel == "affiliate")
    for c in usable:
        rng = world.rng("mkt.spend", c.campaign_id, d.isoformat())
        noise = rng.lognormvariate(0.0, SPEND_SIGMA)
        click_noise = rng.lognormvariate(0.0, CLICK_SIGMA)
        over = rng.uniform(*PLATFORM_OVERCOUNT)
        if c.scenario_id == TRAP_SCENARIO and c.channel == "affiliate":
            spend = cfg.marketing.deal_site_commission_pct / 100.0 * net.get(c.campaign_id, 0.0)
        else:
            spend = campaign_plan(world, c, d) * noise
            if c.channel == "influencer" and c.influencer_code:
                spend += influencer_terms(world, c.influencer_code)["commission_pct"] / 100.0 * net.get(
                    c.campaign_id, 0.0)
        spend = round(spend, 2)
        cpm, ctr = _media(cfg, c.channel)
        if c.scenario_id == TRAP_SCENARIO and c.channel == "affiliate":
            # paid per order: reach follows the traffic the deal listing drives, not the commission
            clicks = max(int(round(_deal_clicks(world, d, n_deal) * click_noise)), orders.get(c.campaign_id, 0))
            impressions = int(round(clicks / ctr)) if ctr > 0 else clicks
        else:
            impressions = int(round(spend / cpm * 1000.0)) if cpm > 0 else 0
            clicks = int(round(impressions * ctr * click_noise))
        conv = min(int(round(orders.get(c.campaign_id, 0) * over)), clicks)
        rows.append(CampaignSpendRow(c.campaign_id, d, impressions, clicks, spend, conv))
        clicks_by_channel[c.channel] = clicks_by_channel.get(c.channel, 0) + clicks
    return rows, clicks_by_channel


def _session_rows(world, d: date, md: MarketingDay | None, clicks: dict[str, int]) -> list[SessionsRow]:
    cfg = world.cfg
    mc = cfg.marketing
    rng = world.rng("mkt.sessions", d.isoformat())
    exp = expected_orders(world, d)
    countries = sorted(k for k, v in cfg.channels.web.country_mix.items() if v > 0)
    c_tot = sum(cfg.channels.web.country_mix[k] for k in countries)
    devices = sorted(k for k, v in mc.device_mix.items() if v > 0)
    d_tot = sum(mc.device_mix[k] for k in devices)
    cells: dict[tuple[str, str, str], list] = {}
    for a in (md.attributions if md is not None else ()):
        cell = cells.setdefault((a[2], a[8], a[9]), [0, 0.0, 0])
        cell[0] += 1
        cell[1] += float(a[6])
        cell[2] += 1 if a[5] else 0
    paid = _paid(cfg)
    rows: list[SessionsRow] = []
    for src in sorted(mc.conversion_rates):
        conv = mc.conversion_rates[src]
        ch = MKT_CHANNEL.get(src, src)
        expected_sessions = exp.get(src, 0.0) * mc.tracking_share / conv if conv > 0 else 0.0
        if ch in paid:
            u = rng.uniform(*CLICK_TO_SESSION)
            noise = rng.lognormvariate(0.0, SESSION_SIGMA)
            sessions = clicks.get(ch, 0) * u + PAID_ORGANIC_SHARE * expected_sessions * noise
        elif ch == "affiliate":  # every affiliate session is a click on a coupon or deal listing
            sessions = clicks.get(ch, 0) * rng.uniform(*CLICK_TO_SESSION)
        else:
            sessions = expected_sessions * rng.lognormvariate(0.0, SESSION_SIGMA)
        for country in countries:
            for dev in devices:
                share = cfg.channels.web.country_mix[country] / c_tot * mc.device_mix[dev] / d_tot
                n_s = int(round(sessions * share * rng.lognormvariate(0.0, CELL_SIGMA)))
                ratio = rng.uniform(*USERS_PER_SESSION)
                o, rev, new = cells.get((ch, country, dev), (0, 0.0, 0))
                n_s = max(n_s, o)
                users = int(round(n_s / ratio))
                new_users = max(new, int(round(users * NEW_USER_SHARE.get(ch, 0.3))))
                users = max(users, new_users)
                n_s = max(n_s, users)
                rows.append(SessionsRow(ch, country, dev, n_s, users, new_users, o, round(rev, 2)))
    return rows


def _spend_usable(world, c: Campaign, d: date) -> bool:
    """Does ``c`` get a spend row in the export of local day ``d``?"""
    return (c.start <= d <= c.end and d >= world.local_date(world.history_start)
            and usable_from(world, c) < world.at_local(d + _DAY, 0))


def _spend_boosts(world, d: date) -> None:
    """influencer_cohort_churn: truth and budget updates of the boosted campaigns, in the export of ``d``.

    Every influencer campaign of the window's code is boosted (factor
    ``mkt_spend``) on the days whose noon lies in the window. On the first
    exported day a campaign is boosted, it gets one ``campaign`` spend_boost
    truth row and a budget update to ``daily_budget_eur x boost``; on the first
    exported day after the window, while the campaign still runs, the budget
    goes back. Each (window, campaign) start and end matches exactly one export
    date and export keys are unique, so every row is written once, also across
    restarts.
    """
    sc = world.scenarios
    if sc is None or not hasattr(sc, "windows"):
        return
    wins = sc.windows(CHURN_SCENARIO)
    if not wins:
        return
    prev = d - _DAY
    local_today = world.local_date(world.now)
    for w in wins:
        code = w.params.get("influencer_code")
        on, was = w.contains(_noon(world, d)), w.contains(_noon(world, prev))
        if not on and not was:
            continue
        boost = float(w.params.get("acquisition_boost", 1.0))
        cands = {c.campaign_id: c for day in (prev, d) for c in active_campaigns(world, day)
                 if c.channel == "influencer" and c.influencer_code == code}
        for cid in sorted(cands):
            c = cands[cid]
            now_b = on and _spend_usable(world, c, d)
            prev_b = was and _spend_usable(world, c, prev)
            status = "active" if local_today <= c.end else "ended"
            if now_b and not prev_b:
                sc.record_effect(world, CHURN_SCENARIO, w.window_id, "campaign", cid, "spend_boost", boost,
                                 {"influencer_code": code, "export_date": d.isoformat(),
                                  "daily_budget_eur": c.daily_budget_eur,
                                  "boosted_budget_eur": round(c.daily_budget_eur * boost, 2)})
                world.emit(CampaignStatusChanged(at=world.now, campaign_id=cid, status=status,
                                                 daily_budget_eur=round(c.daily_budget_eur * boost, 2)))
            elif prev_b and not now_b and _spend_usable(world, c, d):
                world.emit(CampaignStatusChanged(at=world.now, campaign_id=cid, status=status,
                                                 daily_budget_eur=c.daily_budget_eur))


@handler(EXPORT_KIND)
def on_export(world, ev) -> None:
    """Daily marketing export for the previous local day; removes its accumulator."""
    d = date.fromisoformat(ev.data["date"])
    did = d.isoformat()
    md = world.store.get(MKT_DAY_KIND, did)
    spend, clicks = _spend_rows(world, d, md)
    if spend:
        world.emit(CampaignSpendReported(at=world.now, rows=spend))
    world.emit(SessionsReported(at=world.now, session_date=d, rows=_session_rows(world, d, md, clicks)))
    if md is not None and md.attributions:
        rows = [
            AttributionRow(order_id=int(a[0]), customer_id=int(a[1]), attribution_date=d, channel=a[2],
                           campaign_id=a[3], influencer_code=a[4], is_new_customer=bool(a[5]),
                           revenue_eur=float(a[6]))
            for a in md.attributions
        ]
        world.emit(OrdersAttributed(at=world.now, rows=rows))
    _spend_boosts(world, d)
    if md is not None:
        world.store.remove(MKT_DAY_KIND, did)


__all__ = [
    "MULTIPLIER_CAP",
    "MULTIPLIER_MAX",
    "new_customer_multiplier",
    "acquisition_mix",
    "pick_influencer_code",
    "note_order",
    "regular_campaigns",
    "scenario_campaigns",
    "active_campaigns",
    "find_campaign",
    "normaliser",
    "base_rate",
    "expected_orders",
    "influencer_terms",
]
