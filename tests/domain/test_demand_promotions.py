"""Demand plan and promotion calendar."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import pytest

from cpgsim.config import Config
from cpgsim.domain import demandplan, promotions
from cpgsim.domain.events import PromotionDefined
from cpgsim.domain.model import ENTITY_KINDS, Promotion
from cpgsim.domain.reference import build_reference
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import EventQueue
from cpgsim.engine.registry import HANDLERS
from cpgsim.engine.rng import Rng
from cpgsim.engine.world import UTC, World
from cpgsim.models import pricing
from cpgsim.models.seasonality import category_factor, event_factor

RETAILERS = ("drogerie_kranz", "mylo", "shopnova", "belle")


class StubScenarios:
    def __init__(self, specs=None, factors=None):
        self.version = 0
        self.history_start = None
        self.specs = list(specs or [])
        self.factors = factors or {}  # name -> fn(t, **ctx) -> float

    def factor(self, name, t, world=None, record=None, **ctx):
        fn = self.factors.get(name)
        return fn(t, **ctx) if fn else 1.0

    def active(self, *a, **k):
        return []

    def record_effect(self, *a, **k):
        pass

    def scenario_promotions(self):
        return [dict(s) for s in self.specs]


_REF_CACHE: dict[str, object] = {}


def make_world(cfg: Config | None = None, scenarios=None, now: datetime | None = None):
    cfg = cfg or Config()
    events: list = []
    w = World(cfg, EntityStore(), EventQueue(), emit=events.append)
    for kind, cls in ENTITY_KINDS.items():
        w.store.register(kind, cls)
    key = cfg.fingerprint()
    if key not in _REF_CACHE:
        _REF_CACHE[key] = build_reference(cfg)
    w.ref = _REF_CACHE[key]
    w.scenarios = scenarios or StubScenarios()
    if now is not None:
        w.now = now
        w.history_start = now
    return w, events


def drain(w, until: datetime) -> int:
    n = 0
    while True:
        due = w.queue.peek_due()
        if due is None or due > until:
            break
        ev = w.queue.pop()
        w.now = ev.due
        HANDLERS[ev.kind](w, ev)
        n += 1
    return n


def no_web_promos() -> Config:
    return Config(promotions={"web_promos_per_month": 0})


def margin_trap_spec(w, start=date(2026, 3, 2), end=date(2026, 3, 29), live=False, index=1):
    return {
        "window_id": f"promo_margin_trap:config:{index}",
        "scenario_id": "promo_margin_trap",
        "brand": "Velmora",
        "discount_pct": 0.25,
        "free_shipping": True,
        "start": w.at_local(start, 0),
        "end": w.at_local(end + timedelta(days=1), 0),
        "index": index,
        "live": live,
    }


def all_promos(w, months=24, y=2025, m=1):
    out = []
    for i in range(months):
        yy, mm = y + (m - 1 + i) // 12, (m - 1 + i) % 12 + 1
        out.extend(promotions.calendar(w, yy, mm))
    return out


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def test_calendar_is_deterministic():
    w1, _ = make_world()
    w2, _ = make_world()
    a = [(p.promo_id, p.name, p.start, p.end, p.sku_ids, p.code) for p in all_promos(w1)]
    b = [(p.promo_id, p.name, p.start, p.end, p.sku_ids, p.code) for p in all_promos(w2)]
    assert a == b and len(a) > 30
    w3, _ = make_world(Config(seed=7))
    c = [(p.promo_id, p.name, p.start) for p in all_promos(w3)]
    assert c != [(x[0], x[1], x[2]) for x in a]


def test_promo_ids_unique_and_in_ranges():
    w, _ = make_world()
    ps = all_promos(w)
    ids = [p.promo_id for p in ps]
    assert len(ids) == len(set(ids))
    for p in ps:
        if p.channel == "web":
            assert 20_000_000 <= p.promo_id < 30_000_000
        else:
            assert p.channel in RETAILERS
            assert promotions.RETAIL_ID_BASE <= p.promo_id < promotions.RETAIL_ID_BASE + 30_000_000


def test_web_promo_properties():
    w, _ = make_world()
    cfg = w.cfg.promotions
    web = [p for p in all_promos(w) if p.channel == "web"]
    codes = [p.code for p in web if p.kind == "code"]
    assert len(codes) == len(set(codes))
    kinds = {p.kind for p in web}
    assert kinds == {"code", "automatic"}
    for p in web:
        local_start = w.local(p.start)
        assert (local_start.hour, local_start.minute) == (0, 0)
        assert (w.local(p.end).hour, w.local(p.end).minute) == (23, 59)
        assert p.announced_at < p.start
        assert (w.local_date(p.start) - w.local_date(p.announced_at)).days == promotions.WEB_ANNOUNCE_DAYS
        assert (p.kind == "code") == (p.code is not None)
        days = (w.local_date(p.end) - w.local_date(p.start)).days + 1
        if p.name.startswith("Black Week"):
            assert days == 8
            continue
        assert p.discount_pct in cfg.web_discount_choices
        assert days in cfg.web_promo_days
        assert p.sku_ids or p.brand
    # roughly web_promos_per_month promotions per month, plus Black Week each November
    regular = [p for p in web if not p.name.startswith("Black Week")]
    assert 24 <= len(regular) <= 80
    assert sum(1 for p in web if p.name.startswith("Black Week")) == 2


def test_black_week_covers_black_friday_to_cyber_monday():
    w, _ = make_world()
    bw = [p for p in promotions.calendar(w, 2025, 11) if p.name.startswith("Black Week")]
    assert len(bw) == 1
    p = bw[0]
    cal = w.ref.calendar
    assert w.local_date(p.start) <= cal.black_friday(2025)
    assert w.local_date(p.end) == cal.cyber_monday(2025)
    assert p.sku_ids == [] and p.brand is None and p.kind == "code"
    # no demand lift of its own (the event factor carries Black Friday)
    assert promotions._lift(w, p) == 1.0


def test_retail_promos_per_quarter():
    w, _ = make_world()
    cat = w.ref.catalogue
    for ch in RETAILERS:
        rc = getattr(w.cfg.channels, ch)
        for year in (2025, 2026):
            for q in (1, 2, 3, 4):
                start = date(year, 3 * q - 2, 1)
                end = date(year + (q == 4), 1 if q == 4 else 3 * q + 1, 1)
                ps = promotions.retail_promos_starting(w, ch, start, end)
                weeks = 0
                for p in ps:
                    sd, ld = w.local_date(p.start), w.local_date(p.end)
                    days = (ld - sd).days + 1
                    assert days in (7, 14) and sd.weekday() == 0
                    assert start <= sd and ld < end
                    assert p.discount_pct in w.cfg.promotions.retail_discount_choices
                    assert (sd - w.local_date(p.announced_at)).days == promotions.RETAIL_ANNOUNCE_DAYS
                    assort = set(cat.retailer_assortment(ch, sd))
                    assert p.sku_ids and set(p.sku_ids) <= assort
                    assert any(cat.get(s).hero for s in p.sku_ids)
                    assert p.kind == "automatic" and not p.free_shipping
                    weeks += days // 7
                assert weeks == rc.promo_weeks_per_quarter
                # no overlap
                spans = sorted((w.local_date(p.start), w.local_date(p.end)) for p in ps)
                for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
                    assert a1 < b0


def test_retail_promos_active_on_day_and_seasonal_choice():
    w, _ = make_world()
    ps = promotions.retail_promos_starting(w, "mylo", date(2025, 4, 1), date(2025, 7, 1))
    p = ps[0]
    d = w.local_date(p.start) + timedelta(days=2)
    assert p in promotions.retail_promos(w, "mylo", d)
    assert p not in promotions.retail_promos(w, "mylo", w.local_date(p.start) - timedelta(days=1))
    assert promotions.retail_promos(w, "drogerie_kranz", d) == [
        q for q in promotions.promotions_between(w, d, d, "drogerie_kranz")
    ]


def test_promotions_between_and_calendar_consistency():
    w, _ = make_world()
    start, end = date(2025, 5, 1), date(2025, 5, 31)
    between = promotions.promotions_between(w, start, end)
    for p in between:
        assert w.local_date(p.start) <= end and w.local_date(p.end) >= start
    cal_ids = {p.promo_id for p in promotions.calendar(w, 2025, 5)}
    assert cal_ids <= {p.promo_id for p in between}
    web_only = promotions.promotions_between(w, start, end, "web")
    assert all(p.channel == "web" for p in web_only)
    assert {p.promo_id for p in web_only} <= {p.promo_id for p in between}


# ---------------------------------------------------------------------------
# Eligibility, lift, dip
# ---------------------------------------------------------------------------


def _promo(**kw) -> Promotion:
    base = dict(
        promo_id=1, channel="web", name="x", kind="automatic", code=None, discount_pct=0.2,
        free_shipping=False, sku_ids=[], brand=None,
        start=datetime(2025, 1, 1, tzinfo=UTC), end=datetime(2025, 1, 8, tzinfo=UTC),
        announced_at=datetime(2024, 12, 29, tzinfo=UTC),
    )
    base.update(kw)
    return Promotion(**base)


def test_is_eligible_scopes():
    w, _ = make_world()
    cat = w.ref.catalogue
    vel = next(s for s in cat.skus if s.brand == "Velmora")
    cut = next(s for s in cat.skus if s.brand == "Cutara")
    assert promotions.is_eligible(_promo(sku_ids=[vel.sku_id]), vel)
    assert not promotions.is_eligible(_promo(sku_ids=[vel.sku_id]), cut)
    assert promotions.is_eligible(_promo(brand="Velmora"), vel)
    assert not promotions.is_eligible(_promo(brand="Velmora"), cut)
    assert promotions.is_eligible(_promo(), cut)  # sitewide


def test_lift_variants():
    w, _ = make_world()
    pc = w.cfg.promotions
    full = pricing.elasticity_lift(0.2, pc.elasticity)
    assert promotions._lift(w, _promo()) == pytest.approx(full)
    assert promotions._lift(w, _promo(kind="code", code="X20")) == pytest.approx(
        1 + (full - 1) * pc.code_redemption_share
    )
    assert promotions._lift(w, _promo(free_shipping=True)) == pytest.approx(full * promotions.FREE_SHIPPING_LIFT)
    assert promotions._lift(w, _promo(channel="mylo", free_shipping=True)) == pytest.approx(full)


def test_scenario_promo_lift_and_post_promo_dip():
    w, _ = make_world(no_web_promos())
    w.scenarios.specs = [margin_trap_spec(w)]
    cat = w.ref.catalogue
    d_in = date(2026, 3, 10)
    vel = next(s for s in cat.active(d_in) if s.brand == "Velmora")
    cut = next(s for s in cat.active(d_in) if s.brand == "Cutara")
    lift = pricing.elasticity_lift(0.25, -2.0) * promotions.FREE_SHIPPING_LIFT
    assert promotions.demand_factor(w, vel.sku_id, "web", d_in) == pytest.approx(lift)
    assert promotions.demand_factor(w, cut.sku_id, "web", d_in) == 1.0
    assert promotions.demand_factor(w, vel.sku_id, "web", date(2026, 3, 1)) == 1.0
    assert promotions.demand_factor(w, vel.sku_id, "web", date(2026, 3, 29)) == pytest.approx(lift)
    # dip after the end (29 March is the last day)
    first = promotions.demand_factor(w, vel.sku_id, "web", date(2026, 3, 30))
    expected = pricing.post_promo_dip(lift, 0.3, 0, 21, 28)
    assert first == pytest.approx(expected) and first < 1.0
    later = promotions.demand_factor(w, vel.sku_id, "web", date(2026, 4, 10))
    assert first < later < 1.0
    assert promotions.demand_factor(w, vel.sku_id, "web", date(2026, 4, 20)) == 1.0
    # retail channels are unaffected by web scenario promotions
    plain, _ = make_world(no_web_promos())
    assert promotions.day_factors(w, "mylo", d_in) == promotions.day_factors(plain, "mylo", d_in)


def test_retail_demand_factor_uses_retailer_promos():
    w, _ = make_world()
    p = promotions.retail_promos_starting(w, "drogerie_kranz", date(2025, 7, 1), date(2025, 10, 1))[0]
    d = w.local_date(p.start) + timedelta(days=1)
    lift = pricing.elasticity_lift(p.discount_pct, -2.0)
    for s in p.sku_ids:
        assert promotions.demand_factor(w, s, "drogerie_kranz", d) == pytest.approx(lift)
        if s not in {x for q in promotions.retail_promos(w, "mylo", d) for x in q.sku_ids}:
            assert promotions.demand_factor(w, s, "mylo", d) <= 1.0
    after = w.local_date(p.end) + timedelta(days=1)
    for s in p.sku_ids:
        assert promotions.demand_factor(w, s, "drogerie_kranz", after) < 1.0


def test_demand_factor_never_calls_demandplan(monkeypatch):
    w, _ = make_world()

    def boom(*a, **k):
        raise AssertionError("recursion into demandplan")

    monkeypatch.setattr(demandplan, "sku_weights", boom)
    monkeypatch.setattr(demandplan, "_weights", boom)
    for i in range(60):
        promotions.day_factors(w, "web", date(2025, 3, 1) + timedelta(days=i))
        promotions.day_factors(w, "mylo", date(2025, 3, 1) + timedelta(days=i))


# ---------------------------------------------------------------------------
# Web basket promos
# ---------------------------------------------------------------------------


def test_active_web_promos_boundaries():
    w, _ = make_world()
    p = next(p for p in all_promos(w, 6) if p.channel == "web")
    assert p in promotions.active_web_promos(w, p.start)
    assert p in promotions.active_web_promos(w, p.end)
    assert p not in promotions.active_web_promos(w, p.start - timedelta(seconds=1))
    assert p not in promotions.active_web_promos(w, p.end + timedelta(seconds=1))
    assert all(q.channel == "web" for q in promotions.active_web_promos(w, p.start))


def test_web_promo_for_basket_automatic_and_code():
    w, _ = make_world(no_web_promos())
    w.scenarios.specs = [margin_trap_spec(w)]
    cat = w.ref.catalogue
    t = w.at_local(date(2026, 3, 10), 14)
    d = date(2026, 3, 10)
    vel = [s.sku_id for s in cat.active(d) if s.brand == "Velmora"][:2]
    cut = [s.sku_id for s in cat.active(d) if s.brand == "Cutara"][:2]
    p = promotions.web_promo_for_basket(w, t, vel + cut, Rng(1, "x"))
    assert p is not None and p.promo_id == promotions.SCENARIO_ID_BASE + 1
    assert p.scenario_id == "promo_margin_trap"
    assert promotions.scenario_window_id(w, p) == "promo_margin_trap:config:1"
    assert promotions.web_promo_for_basket(w, t, cut, Rng(1, "x")) is None
    assert promotions.web_promo_for_basket(w, t, [], Rng(1, "x")) is None
    assert promotions.web_promo_for_basket(w, w.at_local(date(2026, 4, 2), 12), vel, Rng(1, "x")) is None

    # code redemption probability
    w2, _ = make_world()
    code = next(
        p for p in all_promos(w2) if p.channel == "web" and p.kind == "code" and not p.name.startswith("Black")
    )
    t2 = code.start + timedelta(hours=12)
    others = [q for q in promotions.active_web_promos(w2, t2) if q.promo_id != code.promo_id]
    skus = [s.sku_id for s in cat.active(w2.local_date(t2)) if promotions.is_eligible(code, s)
            and not any(promotions.is_eligible(q, s) for q in others)][:1]
    if not skus:
        pytest.skip("no SKU covered only by the code promotion")
    hits = sum(
        promotions.web_promo_for_basket(w2, t2, skus, Rng(5, "basket", i)) is not None for i in range(3000)
    )
    share = w2.cfg.promotions.code_redemption_share
    assert abs(hits / 3000 - share) < 0.04


def test_web_promo_for_basket_prefers_larger_value(monkeypatch):
    w, _ = make_world()
    cat = w.ref.catalogue
    s = cat.active(date(2025, 6, 1))[0]
    small = _promo(promo_id=5, discount_pct=0.10, sku_ids=[s.sku_id])
    big = _promo(promo_id=6, discount_pct=0.25, brand=s.brand)
    fs = _promo(promo_id=4, discount_pct=0.10, sku_ids=[s.sku_id], free_shipping=True)
    monkeypatch.setattr(promotions, "active_web_promos", lambda world, t: [small, big])
    assert promotions.web_promo_for_basket(w, small.start, [s.sku_id], Rng(1)) is big
    monkeypatch.setattr(promotions, "active_web_promos", lambda world, t: [small, fs])
    assert promotions.web_promo_for_basket(w, small.start, [s.sku_id], Rng(1)) is fs


def test_web_promo_for_basket_values_free_shipping_at_the_country_fee(monkeypatch):
    w, _ = make_world()
    web = w.cfg.channels.web
    s = next(x for x in w.ref.catalogue.active(date(2025, 6, 1)) if x.rrp_eur >= 10.0)
    fs = _promo(promo_id=4, discount_pct=0.10, sku_ids=[s.sku_id], free_shipping=True)
    # worth more than 10 % plus the German fee, less than 10 % plus the Austrian fee
    between = (web.shipping_fee_eur + web.shipping_fee("AT")) / 2
    mid = _promo(promo_id=5, discount_pct=0.10 + between / s.rrp_eur, sku_ids=[s.sku_id])
    monkeypatch.setattr(promotions, "active_web_promos", lambda world, t: [fs, mid])
    assert promotions.web_promo_for_basket(w, fs.start, [s.sku_id], Rng(1), "DE") is mid
    assert promotions.web_promo_for_basket(w, fs.start, [s.sku_id], Rng(1), "AT") is fs
    assert promotions.web_promo_for_basket(w, fs.start, [s.sku_id], Rng(1), "CH") is mid  # EUR fee, as before
    assert promotions.web_promo_for_basket(w, fs.start, [s.sku_id], Rng(1)) is mid


# ---------------------------------------------------------------------------
# Announcement
# ---------------------------------------------------------------------------


def _collect_arrivals(w, start, end, step):
    out = []
    t = start
    while t < end:
        e = min(t + step, end)
        for ev in promotions.announce_arrivals(w, t, e):
            assert t < ev.due <= e
            out.append((ev.due, ev.key, tuple(sorted(ev.data.items()))))
        t = e
    return out


def test_announce_arrivals_partition_invariant():
    w, _ = make_world()
    start = w.at_local(date(2025, 2, 1), 0)
    end = w.at_local(date(2025, 6, 1), 0)
    hourly = _collect_arrivals(w, start, end, timedelta(hours=1))
    w2, _ = make_world()
    daily = _collect_arrivals(w2, start, end, timedelta(days=3, hours=5))
    assert hourly == daily and len(hourly) > 8
    keys = [k for _, k, _ in hourly]
    assert len(keys) == len(set(keys))
    by_id = {p.promo_id: p for p in all_promos(w, 8, 2025, 1)}
    for due, key, _ in hourly:
        pid = int(key.split(":")[1])
        assert by_id[pid].announced_at == due


def test_announce_handler_stores_and_emits():
    w, events = make_world()
    start = w.at_local(date(2025, 2, 1), 0)
    w.now = start
    for ev in promotions.announce_arrivals(w, start, start + timedelta(days=60)):
        w.queue.push(ev)
    n = drain(w, start + timedelta(days=60))
    assert n > 0
    defined = [e for e in events if isinstance(e, PromotionDefined)]
    assert len(defined) == n
    for e in defined:
        p = e.promotion
        assert e.at == p.announced_at
        stored = w.store.get("promotion", p.promo_id)
        assert stored == p and stored is not p
        assert stored is not promotions.find(w, p.promo_id)
    # idempotent
    ev0 = promotions._announce_event(promotions._state(w).by_id[defined[0].promotion.promo_id], w.now)
    HANDLERS["promo.announce"](w, ev0)
    assert len([e for e in events if isinstance(e, PromotionDefined)]) == n


def test_announce_handler_after_restart_finds_promo():
    w, _ = make_world()
    p = promotions.retail_promos_starting(w, "mylo", date(2025, 4, 1), date(2025, 7, 1))[0]
    rec = promotions._state(w).by_id[p.promo_id]
    ev = promotions._announce_event(rec, p.announced_at)
    w2, events = make_world(now=p.announced_at)  # fresh caches
    HANDLERS["promo.announce"](w2, ev)
    assert w2.store.get("promotion", p.promo_id) == p
    assert isinstance(events[0], PromotionDefined)
    w3, _ = make_world(now=p.announced_at)
    assert promotions.find(w3, p.promo_id) == p


def test_bootstrap_announces_running_and_upcoming():
    hs_date = date(2025, 5, 14)
    cfg = Config()
    w, events = make_world(cfg)
    hs = w.at_local(hs_date, 0)
    w.now = w.history_start = hs
    w.scenarios.specs = [margin_trap_spec(w, start=date(2025, 5, 1), end=date(2025, 5, 31))]
    promotions.announce_running_promotions(w)
    scheduled = []
    while len(w.queue):
        ev = w.queue.pop()
        assert ev.due == hs
        scheduled.append(int(ev.data["promo_id"]))
    expected = {
        p.promo_id
        for p in all_promos(make_world(cfg)[0], 6, 2025, 3)
        if p.announced_at <= hs and p.end >= hs
    } | {promotions.SCENARIO_ID_BASE + 1}
    assert set(scheduled) == expected
    assert any(pid >= promotions.RETAIL_ID_BASE for pid in scheduled)  # announced 6 weeks ahead
    # arrivals after history start do not repeat them
    later = promotions.announce_arrivals(w, hs, hs + timedelta(days=50))
    assert not {int(e.data["promo_id"]) for e in later} & set(scheduled)


def test_live_scenario_promo_announced_at_window_start():
    w, _ = make_world(no_web_promos())
    spec = margin_trap_spec(w, start=date(2026, 3, 2), live=True, index=3)
    spec["start"] = w.at_local(date(2026, 3, 2), 15, 7)
    w.scenarios.specs = [spec]
    w.scenarios.version = 1
    evs = promotions.announce_arrivals(w, w.at_local(date(2026, 3, 2), 15), w.at_local(date(2026, 3, 2), 16))
    assert [e.key for e in evs if e.data["promo_id"] >= promotions.SCENARIO_ID_BASE] == ["promo.announce:900003"]
    p = promotions.find(w, 900003)
    assert p.announced_at == p.start == spec["start"]
    # active from the window start instant, not from midnight
    assert p not in promotions.active_web_promos(w, spec["start"] - timedelta(minutes=1))
    assert p in promotions.active_web_promos(w, spec["start"])


def test_scenario_version_change_refreshes():
    w, _ = make_world(no_web_promos())
    d = date(2026, 3, 10)
    vel = next(s for s in w.ref.catalogue.active(d) if s.brand == "Velmora")
    assert promotions.demand_factor(w, vel.sku_id, "web", d) == 1.0
    w.scenarios.specs = [margin_trap_spec(w)]
    w.scenarios.version += 1
    assert promotions.demand_factor(w, vel.sku_id, "web", d) > 1.5
    assert any(p.promo_id == 900001 for p in promotions.calendar(w, 2026, 3))


# ---------------------------------------------------------------------------
# Demand plan
# ---------------------------------------------------------------------------


def test_sku_weights_formula_and_caching():
    w, _ = make_world()
    cat = w.ref.catalogue
    cal = w.ref.calendar
    for ch, d in (("web", date(2025, 5, 3)), ("mylo", date(2025, 12, 3)), ("web", date(2026, 5, 2))):
        ids, cum = demandplan.sku_weights(w, ch, d)
        expected_ids = (
            [s.sku_id for s in cat.active(d)] if ch == "web" else cat.retailer_assortment(ch, d)
        )
        assert ids == expected_ids
        assert all(b >= a for a, b in zip(cum, cum[1:]))
        prev = 0.0
        base_ev = event_factor(w.cfg.seasonality, d, ch, None, cal)
        for sid, c in zip(ids, cum):
            s = cat.get(sid)
            ramp = demandplan._launch_ramp(s.launch_date, d)
            exp = (
                s.popularity * category_factor(s.season_profile, d)
                * event_factor(w.cfg.seasonality, d, ch, s.brand, cal) / base_ev
                * promotions.demand_factor(w, sid, ch, d) * ramp
            )
            assert c - prev == pytest.approx(exp, rel=1e-9)
            prev = c
        again = demandplan.sku_weights(w, ch, d)
        assert again[0] is ids and again[1] is cum


def test_mothers_day_lifts_cutara_only():
    w, _ = make_world(no_web_promos())
    md = w.ref.calendar.mothers_day(2025)
    before, normal = md - timedelta(days=3), md - timedelta(days=20)
    ids_b, cum_b = demandplan.sku_weights(w, "web", before)
    wb = demandplan._weights(w, "web", before).by_sku
    wn = demandplan._weights(w, "web", normal).by_sku
    cat = w.ref.catalogue
    cut = [s for s in ids_b if cat.get(s).brand == "Cutara" and s in wn]
    vel = [s for s in ids_b if cat.get(s).brand == "Velmora" and s in wn]
    r_cut = sum(wb[s] for s in cut) / sum(wn[s] for s in cut)
    r_vel = sum(wb[s] for s in vel) / sum(wn[s] for s in vel)
    assert r_cut / r_vel == pytest.approx(w.cfg.seasonality.mothers_day_lift, rel=0.08)


def test_launch_ramp():
    assert demandplan._launch_ramp(date(2025, 1, 1), date(2025, 1, 1)) == pytest.approx(0.3)
    assert demandplan._launch_ramp(date(2025, 1, 1), date(2025, 1, 29)) == pytest.approx(0.65)
    assert demandplan._launch_ramp(date(2025, 1, 1), date(2025, 3, 1)) == 1.0
    w, _ = make_world()
    launched = [s for s in w.ref.catalogue.skus if date(2025, 2, 1) <= s.launch_date <= date(2026, 6, 1)]
    assert launched
    s = launched[0]
    d = s.launch_date
    wt = demandplan._weights(w, "web", d).by_sku[s.sku_id]
    base = s.popularity * category_factor(s.season_profile, d) * promotions.demand_factor(w, s.sku_id, "web", d)
    base *= demandplan._brand_events(demandplan._state(w), w, "web", d)[s.brand]
    assert wt == pytest.approx(0.3 * base)


def test_scenario_factors_applied():
    target = {}
    sc = StubScenarios(factors={
        "sku_demand": lambda t, channel=None, sku_id=None: 2.0 if sku_id == target.get("sku") else 1.0,
        "web_orders": lambda t: 1.5,
    })
    w, _ = make_world(scenarios=sc)
    d = date(2025, 9, 9)
    plain_w, _ = make_world()
    sid = plain_w.ref.catalogue.active(d)[3].sku_id
    target["sku"] = sid
    a = demandplan._weights(plain_w, "web", d).by_sku[sid]
    b = demandplan._weights(w, "web", d).by_sku[sid]
    assert b == pytest.approx(2 * a)
    t = w.at_local(d, 12)
    w_ratio = demandplan._weights(w, "web", d)
    p_ratio = demandplan._weights(plain_w, "web", d)
    assert demandplan.web_order_rate_per_day(w, t) == pytest.approx(
        1.5 * demandplan.web_order_rate_per_day(plain_w, t) * (w_ratio.total / p_ratio.total)
    )


def test_web_order_rate_level_and_events():
    w, _ = make_world()
    base = w.cfg.channels.web.base_orders_per_day
    rates = [demandplan.web_order_rate_per_day(w, w.at_local(date(2025, 1, 1) + timedelta(days=i), 12))
             for i in range(365)]
    mean = sum(rates) / len(rates)
    assert 0.95 * base <= mean <= 1.25 * base
    bf = w.ref.calendar.black_friday(2025)
    r_bf = demandplan.web_order_rate_per_day(w, w.at_local(bf, 12))
    r_oct = demandplan.web_order_rate_per_day(w, w.at_local(date(2025, 10, 15), 12))
    assert r_bf > 2.2 * r_oct
    # same local day -> same rate regardless of the hour (stub scenario factor 1)
    assert demandplan.web_order_rate_per_day(w, w.at_local(bf, 1)) == r_bf


def test_basket_stats():
    w, _ = make_world()
    b = demandplan.basket_stats(w)
    assert b["lines"] == pytest.approx(2.4)
    assert b["qty"] == pytest.approx(1.1488, abs=1e-4)
    assert 3.0 < b["unit_net_eur"] < 15.0
    assert b["basket_net_eur"] == pytest.approx(b["lines"] * b["qty"] * b["unit_net_eur"])
    assert b["e_basket_net_eur"] == b["basket_net_eur"]
    assert b["annual_web_net_eur"] == pytest.approx(w.cfg.channels.web.base_orders_per_day * 365 * b["basket_net_eur"])


def test_retail_weekly_demand_sizing_and_dc_split():
    w, _ = make_world()
    cat = w.ref.catalogue
    b = demandplan.basket_stats(w)
    for ch in RETAILERS:
        rc = getattr(w.cfg.channels, ch)
        dcs = [a.account_id for a in w.ref.accounts.dcs(ch)]
        weights = demandplan.dc_weights(w, ch)
        assert sorted(weights) == sorted(dcs) and sum(weights.values()) == pytest.approx(1.0)
        value = 0.0
        for wk in range(52):
            ws = date(2025, 1, 6) + timedelta(weeks=wk)
            for sid in cat.retailer_assortment(ch, ws + timedelta(days=3)):
                units = sum(demandplan.retail_weekly_demand(w, ch, dc, sid, ws) for dc in dcs)
                assert units == pytest.approx(demandplan._retail_week(w, ch, ws)[sid])
                value += units * cat.get(sid).list_price_eur * (1 - rc.trade_discount)
        # sized against the realised web value; the reference-year lift is divided out
        target = b["annual_web_net_eur"] * demandplan.WEB_VALUE_REALISATION * rc.share / w.cfg.channels.web.share
        assert 0.93 * target < value < 1.07 * target
        assert demandplan.retail_weekly_demand(w, ch, "NOPE", cat.retailer_assortment(ch, date(2025, 3, 5))[0],
                                               date(2025, 3, 3)) == 0.0
    # sun care sells far more in July than in January
    sun = next(s for s in cat.retailer_assortment("mylo", date(2025, 7, 10))
               if cat.get(s).season_profile == "sun" and s in cat.retailer_assortment("mylo", date(2025, 1, 9)))
    dc = demandplan.dc_weights(w, "mylo")
    first_dc = sorted(dc)[0]
    jul = demandplan.retail_weekly_demand(w, "mylo", first_dc, sun, date(2025, 7, 7))
    jan = demandplan.retail_weekly_demand(w, "mylo", first_dc, sun, date(2025, 1, 6))
    assert jul > 5 * jan


def test_expected_daily_units():
    w, _ = make_world()
    cat = w.ref.catalogue
    d = date(2025, 6, 11)
    b = demandplan.basket_stats(w)
    web_total = sum(demandplan.expected_daily_units(w, s.sku_id, d, "web") for s in cat.active(d))
    rate = demandplan.web_order_rate_per_day(w, w.at_local(d, 12))
    assert web_total == pytest.approx(rate * b["lines"] * b["qty"])
    sid = cat.retailer_assortment("mylo", d)[0]
    ws = d - timedelta(days=d.weekday())
    mylo = sum(demandplan.retail_weekly_demand(w, "mylo", dc, sid, ws) for dc in demandplan.dc_weights(w, "mylo")) / 7
    assert demandplan.expected_daily_units(w, sid, d, "mylo") == pytest.approx(mylo)
    total = demandplan.expected_daily_units(w, sid, d)
    parts = sum(demandplan.expected_daily_units(w, sid, d, ch) for ch in ("web",) + RETAILERS)
    assert total == pytest.approx(parts) and total > 0
    # SKUs not listed at a retailer have no retail demand there
    unlisted = next(s.sku_id for s in cat.active(d) if s.sku_id not in set(cat.retailer_assortment("mylo", d)))
    assert demandplan.expected_daily_units(w, unlisted, d, "mylo") == 0.0


def test_hot_path_performance():
    w, _ = make_world()
    t0 = time.perf_counter()
    start = date(2025, 1, 1)
    for i in range(730):
        d = start + timedelta(days=i)
        for ch in ("web",) + RETAILERS:
            demandplan.sku_weights(w, ch, d)
        demandplan.web_order_rate_per_day(w, w.at_local(d, 12))
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    d = date(2025, 8, 1)
    t = w.at_local(d, 18)
    for _ in range(100_000):
        demandplan.sku_weights(w, "web", d)
        promotions.demand_factor(w, 5, "web", d)
    for _ in range(20_000):
        demandplan.web_order_rate_per_day(w, t)
        promotions.active_web_promos(w, t)
    warm = time.perf_counter() - t0
    assert cold < 10.0 and warm < 5.0


def test_disabled_retailer_has_no_demand():
    cfg = Config()
    cfg.channels.shopnova.enabled = cfg.channels.belle.enabled = False
    w, _ = make_world(cfg)
    d = date(2025, 6, 11)
    sid = w.ref.catalogue.active(d)[0].sku_id
    assert demandplan.expected_daily_units(w, sid, d, "shopnova") == 0.0
    assert demandplan.retail_weekly_demand(w, "belle", "BELLE-DC1", sid, d) == 0.0
    assert promotions.retail_promos(w, "shopnova", d) == []
    assert promotions.demand_factor(w, sid, "shopnova", d) == 1.0


def test_retail_push_scales_planned_retail_demand_only():
    """retailer_overstock: the warehouse plans the pushing retailer's over-order (expected_daily_units)."""
    sc = StubScenarios(factors={"retail_push": lambda t, channel=None, **k: 1.35 if channel == "mylo" else 1.0})
    w, _ = make_world(scenarios=sc)
    plain, _ = make_world()
    d = date(2025, 9, 10)
    sids = plain.ref.catalogue.retailer_assortment("mylo", d)[:5]
    for sid in sids:
        base = demandplan.expected_daily_units(plain, sid, d, "mylo")
        assert base > 0
        assert demandplan.expected_daily_units(w, sid, d, "mylo") == pytest.approx(1.35 * base)
        for ch in ("web", "drogerie_kranz"):
            assert demandplan.expected_daily_units(w, sid, d, ch) == pytest.approx(
                demandplan.expected_daily_units(plain, sid, d, ch))
        total = demandplan.expected_daily_units(w, sid, d)
        assert total == pytest.approx(demandplan.expected_daily_units(plain, sid, d) + 0.35 * base)
