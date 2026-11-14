"""Marketing (phase 2, WP4): campaign calendar, spend-driven acquisition, daily exports."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from cpgsim.config import Config
from cpgsim.domain import marketing as mk
from cpgsim.domain import promotions, web
from cpgsim.domain.events import (
    CampaignDefined,
    CampaignSpendReported,
    CampaignStatusChanged,
    InfluencerCodeRegistered,
    OrdersAttributed,
    PlantedEffect,
    SessionsReported,
)
from cpgsim.domain.model import ENTITY_KINDS, Counters
from cpgsim.domain.reference import build_reference
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import Event, EventQueue
from cpgsim.engine.registry import ARRIVAL_SOURCES, HANDLERS
from cpgsim.engine.rng import Rng
from cpgsim.engine.world import World
from cpgsim.scenarios import ScenarioManager
from cpgsim.writers import keys

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
HS = datetime(2025, 1, 6, tzinfo=BERLIN).astimezone(UTC)  # Monday 00:00 local
SOURCES = ("mkt.campaigns", "mkt.export")
# Paid-channel CPMs calibrated so that mean paid conversion matches conversion_rates (see the WP4 report).
CALIBRATED_PAID = {"paid_search": {"cpm_eur": 21.5}, "social": {"cpm_eur": 3.0}, "influencer": {"cpm_eur": 8.5}}

_REF: dict = {}


def local(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=BERLIN).astimezone(UTC)


def make_cfg(scenarios=None, **kw) -> Config:
    data = {"scenarios": scenarios or []}
    data.update(kw)
    return Config.model_validate(data)


def make_world(cfg: Config | None = None, hs: datetime = HS, overrides=None):
    cfg = cfg or make_cfg()
    out: list = []
    store = EntityStore()
    for kind, cls in ENTITY_KINDS.items():
        store.register(kind, cls)
    store.put("counters", "all", Counters())
    world = World(cfg, store, EventQueue(), emit=out.append)
    key = tuple(sorted(cfg.channels.retailers()))
    if key not in _REF:
        _REF[key] = build_reference(cfg)
    world.ref = _REF[key]
    world.scenarios = ScenarioManager(cfg)
    if overrides:
        world.scenarios.load_overrides(overrides)
    world.now = hs
    world.history_start = hs
    world.scenarios.attach(world)
    return world, out


def run(world, until: datetime, step_hours: float = 6.0, sources=SOURCES) -> None:
    """Mini runner: arrival sources per slice, then drain the queue."""
    t = world.now
    step = timedelta(hours=step_hours)
    while t < until:
        end = min(t + step, until)
        for name in sorted(sources):
            for ev in ARRIVAL_SOURCES[name](world, t, end):
                assert t < ev.due <= end
                world.queue.push(ev)
        while (due := world.queue.peek_due()) is not None and due <= end:
            ev = world.queue.pop()
            world.now = ev.due
            HANDLERS[ev.kind](world, ev)
        world.now = end
        t = end


def fake_order(oid: int, at: datetime, country: str = "DE", total: float = 40.0, cid: int | None = None):
    return SimpleNamespace(order_id=oid, customer_id=cid or 1000 + oid, country=country,
                           currency="CHF" if country == "CH" else "EUR", total=total, placed_at=at)


def schedule_orders(world, monkeypatch, orders: list[tuple[datetime, str, str | None, bool, str]]) -> None:
    """Queue synthetic web orders ``(at, source, influencer_code, new_customer, country)``."""

    def on_order(w, ev):
        d = ev.data
        o = fake_order(d["oid"], w.now, d["country"], d["total"])
        mk.note_order(w, o, SimpleNamespace(influencer_code=d["code"]), d["source"], d["new"])

    monkeypatch.setitem(HANDLERS, "test.order", on_order)
    for i, (at, source, code, new, country) in enumerate(orders, start=1):
        world.queue.push(Event(due=at, kind="test.order", key=f"test.order:{i:06d}",
                               data={"oid": i, "source": source, "code": code, "new": new, "country": country,
                                     "total": 20.0 + (i * 7) % 60}, priority=50))


def campaign_tuple(c):
    return (c.campaign_id, c.name, c.channel, c.objective, c.start, c.end, c.daily_budget_eur, c.defined_at,
            c.influencer_code, c.promo_id, c.scenario_id, c.window_id)


TRAP = {"id": "promo_margin_trap", "start": "2025-03-03", "end": "2025-03-30"}
CHURN = {"id": "influencer_cohort_churn", "influencer_code": "GLOWWITHMIA", "repurchase_multiplier": 0.3,
         "acquisition_boost": 2.5, "start": "2025-02-03", "end": "2025-03-30"}


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def test_calendar_is_pure_and_ids_follow_the_contract():
    a, _ = make_world()
    b, _ = make_world()
    for y in (2025, 2026):
        ca = [campaign_tuple(c) for c in mk.regular_campaigns(a, y)]
        assert ca == [campaign_tuple(c) for c in mk.regular_campaigns(b, y)]
        camps = mk.regular_campaigns(a, y)
        assert len({c.campaign_id for c in camps}) == len(camps)
        by_cq: dict[tuple, list] = defaultdict(list)
        for c in camps:
            assert len(c.campaign_id) <= 30 and len(c.name) <= 80
            assert c.channel in keys.MKT_PREFIX and c.objective in ("acquisition", "promotion", "retention")
            assert c.start <= c.end and c.daily_budget_eur >= 0
            assert c.created_on == a.local_date(c.defined_at) and c.defined_at < mk.active_at(a, c)
            m = re.fullmatch(r"([A-Z]{2})-(\d{4})Q(\d)-(\d{2})", c.campaign_id)
            if m is None:
                assert re.fullmatch(r"EM-P\d+", c.campaign_id) and c.promo_id is not None
                continue
            assert keys.MKT_PREFIX[c.channel] == m.group(1)
            assert int(m.group(2)) == c.start.year == y and int(m.group(3)) == (c.start.month - 1) // 3 + 1
            by_cq[(c.channel, int(m.group(3)))].append(c)
        for (ch, q), cs in by_cq.items():
            ordered = sorted(cs, key=lambda c: (c.start, c.name))
            assert [int(c.campaign_id[-2:]) for c in ordered] == list(range(1, len(cs) + 1)), (ch, q)
        chans = Counter(c.channel for c in camps)
        assert chans["paid_search"] >= 8 + 2 and chans["social"] >= 4 and chans["affiliate"] == 4
        # one always-on campaign per influencer code and year, plus collaboration flights
        codes = a.cfg.customers.influencer_codes
        always = [c for c in camps if c.channel == "influencer" and c.start == date(y, 1, 1)
                  and c.end == date(y, 12, 31)]
        assert sorted(c.influencer_code for c in always) == sorted(codes)
        # every regular web promotion gets its email campaign (EM-P<promo_id>), defined at announcement
        web_promos = [p for p in promotions.promotions_between(a, date(y, 1, 1), date(y, 12, 31), "web")
                      if p.scenario_id is None and a.local_date(p.start).year == y]
        emails = {c.promo_id: c for c in camps if c.campaign_id.startswith("EM-P")}
        assert set(emails) == {p.promo_id for p in web_promos}
        for p in web_promos:
            c = emails[p.promo_id]
            assert c.campaign_id == keys.mkt_promo_campaign_id(p.promo_id)
            assert c.defined_at == max(a.at_local(c.start - timedelta(days=14), 9), p.announced_at)


def test_regular_ids_unchanged_when_a_scenario_window_is_added():
    plain, _ = make_world()
    with_trap, _ = make_world(make_cfg([TRAP, CHURN]))
    for y in (2025, 2026):
        assert [campaign_tuple(c) for c in mk.regular_campaigns(plain, y)] == [
            campaign_tuple(c) for c in mk.regular_campaigns(with_trap, y)]
    assert mk.scenario_campaigns(plain) == []
    sc = mk.scenario_campaigns(with_trap)
    assert [c.campaign_id for c in sc] == ["AF-X000000", "EM-X000000"]
    for c in sc:
        assert c.promo_id == 900_000 and c.scenario_id == "promo_margin_trap"
        assert (c.start, c.end) == (date(2025, 3, 3), date(2025, 3, 30))
        assert c.defined_at == with_trap.at_local(date(2025, 2, 17), 9)  # config window: start - 14 d
    # a live switch adds campaigns without renumbering anything
    live, _ = make_world(overrides=[(7, "promo_margin_trap", "enable", local(2025, 5, 5, 14, 30), "{}")])
    assert [campaign_tuple(c) for c in mk.regular_campaigns(live, 2025)] == [
        campaign_tuple(c) for c in mk.regular_campaigns(plain, 2025)]
    ids = [c.campaign_id for c in mk.scenario_campaigns(live)]
    assert ids == ["AF-X001007", "EM-X001007"]


# ---------------------------------------------------------------------------
# Spend -> acquisition
# ---------------------------------------------------------------------------


def _weighted_mean(world, year: int) -> float:
    num = den = 0.0
    d = date(year, 1, 1)
    while d.year == year:
        b = mk.base_rate(world, d)
        num += b * mk.new_customer_multiplier(world, world.at_local(d, 12))
        den += b
        d += timedelta(days=1)
    return num / den


def test_rate_weighted_multiplier_mean_is_one_in_every_calendar_year():
    world, _ = make_world()
    for y in (2025, 2026, 2027):  # a 24-month history from 2025-01-06 touches three calendar years
        assert 0.8 <= mk.normaliser(world, y) <= 1.25
        assert abs(_weighted_mean(world, y) - 1.0) < 0.005, y
    vals = [mk.new_customer_multiplier(world, world.at_local(date(2025, 1, 1) + timedelta(days=i), 12))
            for i in range(730)]
    assert all(1 / mk.MULTIPLIER_MAX <= v <= mk.MULTIPLIER_MAX for v in vals)
    assert max(vals) - min(vals) > 0.1  # flights do move acquisition


def test_hourly_new_customer_rate_stays_below_the_thinning_bound():
    world, _ = make_world()
    w = world.cfg.channels.web
    lam_max = w.base_orders_per_day * w.new_customer_share / 24.0 * web.NEW_ORDER_LAM_MAX_FACTOR
    t, end, worst = HS, HS + timedelta(days=730), 0.0
    while t < end:
        worst = max(worst, web.new_customer_rate_per_hour(world, t) / lam_max)
        t += timedelta(hours=1)
    assert worst < 1.0, worst


def test_multiplier_cache_includes_the_scenario_version():
    d = date(2025, 3, 10)
    t = local(2025, 3, 10, 12)
    world, _ = make_world()
    before = mk.new_customer_multiplier(world, t)
    mix_before = dict(mk.acquisition_mix(world, t))
    rows = [(1, "influencer_cohort_churn", "enable", local(2025, 2, 1, 10), '{"influencer_code": "GLOWWITHMIA"}')]
    v0 = world.scenarios.version
    world.scenarios.load_overrides(rows)
    assert world.scenarios.version != v0
    after = mk.new_customer_multiplier(world, t)
    assert after > before and mk.acquisition_mix(world, t)["influencer"] > mix_before["influencer"]
    fresh, _ = make_world(overrides=rows)
    assert mk.new_customer_multiplier(fresh, t) == after
    assert mk.acquisition_mix(fresh, t) == mk.acquisition_mix(world, t)
    # K(Y) and the baselines are scenario-free
    assert mk.normaliser(fresh, 2025) == mk.normaliser(world, 2025)
    # the boosted code is picked more often
    base_world, _ = make_world()
    picks = Counter(mk.pick_influencer_code(fresh, Rng(3, "p", i), t) for i in range(3000))
    plain = Counter(mk.pick_influencer_code(base_world, Rng(3, "p", i), t) for i in range(3000))
    assert picks["GLOWWITHMIA"] > 1.3 * plain["GLOWWITHMIA"], (picks, plain)
    assert d == world.local_date(t)


def test_pick_influencer_code_is_weighted_with_exactly_one_draw():
    world, _ = make_world()
    t = local(2025, 5, 12, 12)
    day = mk._day(world, world.local_date(t))
    codes = sorted(world.cfg.customers.influencer_codes)
    assert day.codes == codes
    for i in range(200):
        a, b = Rng(5, "x", i), Rng(5, "x", i)
        u = b.random()
        got = mk.pick_influencer_code(world, a, t)
        x = u * day.code_cum[-1]
        assert got == next(c for c, cum in zip(codes, day.code_cum) if x < cum)
        assert a.random() == b.random()


# ---------------------------------------------------------------------------
# Campaign events
# ---------------------------------------------------------------------------


def test_bootstrap_registers_codes_and_defines_running_campaigns():
    world, out = make_world()
    mk.bootstrap_campaigns(world)
    codes = [e for e in out if isinstance(e, InfluencerCodeRegistered)]
    assert sorted(e.code for e in codes) == sorted(world.cfg.customers.influencer_codes)
    for e in codes:
        assert e.valid_from < date(2023, 1, 6) and len(e.handle) <= 40 and e.fixed_fee_eur > 0
        assert e.audience_platform in mk.INFLUENCER_PLATFORMS and 5 <= e.commission_pct <= 20
    defined = {e.campaign.campaign_id: e for e in out if isinstance(e, CampaignDefined)}
    hd = date(2025, 1, 6)
    for e in defined.values():
        c = e.campaign
        assert c.defined_at <= HS and c.end >= hd
        assert e.status == ("active" if mk.active_at(world, c) <= HS else "planned")
    # running quarter campaigns are active; a campaign starting within 14 days is planned
    assert defined["PS-2025Q1-01"].status == "active"
    soon = [e for e in defined.values() if hd < e.campaign.start <= hd + timedelta(days=14)]
    assert soon and all(e.status == "planned" for e in soon)
    # then the source emits only their 'active' step, never a second definition
    out.clear()
    run(world, HS + timedelta(days=20), sources=("mkt.campaigns",))
    redefined = [e for e in out if isinstance(e, CampaignDefined) and e.campaign.campaign_id in defined]
    assert redefined == []
    act = {e.campaign_id for e in out if isinstance(e, CampaignStatusChanged) and e.status == "active"}
    assert {e.campaign.campaign_id for e in soon} <= act
    assert "PS-2025Q1-01" not in act


def test_every_campaign_is_defined_once_before_its_status_changes():
    world, out = make_world(make_cfg([TRAP]))
    mk.bootstrap_campaigns(world)
    run(world, HS + timedelta(days=400), step_hours=11)
    seq: dict[str, list] = defaultdict(list)
    for e in out:
        if isinstance(e, CampaignDefined):
            seq[e.campaign.campaign_id].append(("defined", e.status, e.at))
        elif isinstance(e, CampaignStatusChanged):
            seq[e.campaign_id].append((e.status, e.end_date, e.at))
    assert "EM-X000000" in seq and "AF-X000000" in seq
    for cid, steps in seq.items():
        assert steps[0][0] == "defined", (cid, steps)
        kinds = [s[0] for s in steps]
        assert kinds.count("defined") == 1 and kinds.count("active") <= 1 and kinds.count("ended") <= 1
        assert [s[2] for s in steps] == sorted(s[2] for s in steps)
        if steps[0][1] == "active":
            assert "active" not in kinds
        elif steps[-1][0] == "ended":
            assert kinds == ["defined", "active", "ended"], (cid, kinds)
    # spend and attribution reference only defined campaigns
    defined = {cid for cid in seq}
    spent = {r.campaign_id for e in out if isinstance(e, CampaignSpendReported) for r in e.rows}
    assert spent and spent <= defined


def test_live_window_campaign_is_defined_active_without_an_active_step(monkeypatch):
    switch = local(2025, 5, 5, 14, 30)
    world, out = make_world(overrides=[(7, "promo_margin_trap", "enable", switch, "{}")])
    mk.bootstrap_campaigns(world)
    schedule_orders(world, monkeypatch, [
        (local(2025, 5, 5, 10), "deal_site", None, True, "DE"),  # before the switch: no campaign usable
        (local(2025, 5, 5, 18), "deal_site", None, True, "DE"),
        (local(2025, 5, 6, 9), "email", None, False, "AT"),
    ])
    run(world, local(2025, 6, 10))
    mine = [e for e in out if (isinstance(e, CampaignDefined) and e.campaign.campaign_id == "AF-X001007")
            or (isinstance(e, CampaignStatusChanged) and e.campaign_id == "AF-X001007")]
    assert isinstance(mine[0], CampaignDefined) and mine[0].status == "active" and mine[0].at == switch
    assert [e.status for e in mine[1:]] == ["ended"]
    assert mine[1].end_date == date(2025, 6, 2)  # open-ended live window: 28-day promotion
    attributed = {r.order_id: r for e in out if isinstance(e, OrdersAttributed) for r in e.rows}
    tracked = [oid for oid in (1, 2, 3) if Rng(world.seed, "mkt.track", oid).random() < 0.92]
    if 1 in tracked:
        assert attributed[1].campaign_id is None
    if 2 in tracked:
        assert attributed[2].campaign_id == "AF-X001007"


# ---------------------------------------------------------------------------
# Orders and exports
# ---------------------------------------------------------------------------


def test_tracking_share_and_attribution_rules():
    world, _ = make_world()
    t = local(2025, 2, 12, 15)
    world.now = t
    n = 4000
    for i in range(1, n + 1):
        src = ("paid_search", "influencer", "organic_search", "email", "subscription")[i % 5]
        mk.note_order(world, fake_order(i, t, ("DE", "AT", "CH")[i % 3]),
                      SimpleNamespace(influencer_code="LENA10"), src, i % 2 == 0)
    md = world.store.get("mkt_day", "2025-02-12")
    share = len(md.attributions) / n
    assert abs(share - 0.92) < 3 * (0.92 * 0.08 / n) ** 0.5 + 0.002
    active = {c.campaign_id: c for c in mk.active_campaigns(world, date(2025, 2, 12))}
    for oid, cid, ch, camp, code, is_new, rev, net, country, device in md.attributions:
        assert device in world.cfg.marketing.device_mix
        if ch in ("organic_search", "subscription"):
            assert camp is None and code is None
        else:
            assert camp in active and mk.usable_from(world, active[camp]) <= t
            assert active[camp].channel == ch
        if ch == "influencer":
            assert code == "LENA10" and active[camp].influencer_code == "LENA10"
        assert net < rev
        if country == "CH":  # converted to EUR at the month's rate
            fx = __import__("cpgsim.domain.masterdata", fromlist=["fx_rate"]).fx_rate(world, "CHF", date(2025, 2, 12))
            assert rev == round(40.0 * fx, 2)
        else:
            assert rev == 40.0
    totals = sum(v[0] for v in md.by_channel.values())
    assert totals == len(md.attributions)


def _orders_for(world, d: date, rng: Rng, scale: float = 1.0) -> list[tuple]:
    """Synthetic orders of a day drawn from the expected order counts per source."""
    exp = mk.expected_orders(world, d)
    out = []
    for src in sorted(exp):
        for _ in range(rng.poisson(exp[src] * scale)):
            h = rng.randrange(7, 23)
            code = None
            if src == "influencer":
                code = mk.pick_influencer_code(world, rng, world.at_local(d, h))
            out.append((world.at_local(d, h, rng.randrange(60)), src, code, rng.random() < 0.5,
                        ("DE", "DE", "DE", "AT", "CH")[rng.randrange(5)]))
    return sorted(out)


def test_export_is_consistent_with_noted_orders(monkeypatch):
    cfg = make_cfg([TRAP])
    world, out = make_world(cfg, hs=local(2025, 3, 2))
    mk.bootstrap_campaigns(world)
    rng = Rng(1, "orders")
    orders = []
    for i in range(4):
        orders += _orders_for(world, date(2025, 3, 2) + timedelta(days=i), rng)
    schedule_orders(world, monkeypatch, orders)
    run(world, local(2025, 3, 6, 6))
    # the day before history start is never exported; days 2-5 March are
    exports = [e for e in out if isinstance(e, SessionsReported)]
    assert [e.session_date for e in exports] == [date(2025, 3, d) for d in (2, 3, 4, 5)]
    assert world.store.count("mkt_day") == 0  # every accumulator is removed by its export
    attributed = [r for e in out if isinstance(e, OrdersAttributed) for r in e.rows]
    tracked = [i for i in range(1, len(orders) + 1) if Rng(world.seed, "mkt.track", i).random() < 0.92]
    assert sorted(r.order_id for r in attributed if r.attribution_date < date(2025, 3, 6)) == [
        i for i in tracked if orders[i - 1][0] < local(2025, 3, 6)]
    defined = {e.campaign.campaign_id for e in out if isinstance(e, CampaignDefined)}
    assert {r.campaign_id for r in attributed if r.campaign_id} <= defined
    by_day_channel: dict[tuple, list] = defaultdict(lambda: [0, 0.0, 0])
    for r in attributed:
        agg = by_day_channel[(r.attribution_date, r.channel)]
        agg[0] += 1
        agg[1] += r.revenue_eur
        agg[2] += r.is_new_customer
    for e in exports:
        assert len(e.rows) == len(cfg.marketing.conversion_rates) * 3 * 3  # channels x countries x devices
        assert len({(r.channel, r.country, r.device) for r in e.rows}) == len(e.rows)
        per: dict[str, list] = defaultdict(lambda: [0, 0.0, 0])
        for r in e.rows:
            assert r.sessions >= r.users >= r.new_users >= 0 and r.sessions >= r.orders
            per[r.channel][0] += r.orders
            per[r.channel][1] += r.revenue_eur
            per[r.channel][2] += r.new_users
        assert "deal_site" not in per  # mkt calls the shop's deal_site source the affiliate channel
        for ch in {mk.MKT_CHANNEL.get(src, src) for src in cfg.marketing.conversion_rates}:
            want = by_day_channel.get((e.session_date, ch), [0, 0.0, 0])
            assert per[ch][0] == want[0] and abs(per[ch][1] - want[1]) < 0.05 and per[ch][2] >= want[2]
    # spend: one row per usable campaign and day; the deal-site campaign is paid by commission
    spend = [(r, e.at) for e in out if isinstance(e, CampaignSpendReported) for r in e.rows]
    assert len({(r.campaign_id, r.spend_date) for r, _ in spend}) == len(spend)
    for r, _ in spend:
        assert r.spend_eur >= 0 and r.impressions >= 0 and r.clicks >= 0
        if r.campaign_id == "AF-X000000":
            dsum = sum(round(x.revenue_eur / (1 + cfg.channels.web.vat[o[4]]), 2)
                       for x, o in ((x, orders[x.order_id - 1]) for x in attributed)
                       if x.campaign_id == r.campaign_id and x.attribution_date == r.spend_date)
            assert r.spend_eur == pytest.approx(cfg.marketing.deal_site_commission_pct / 100 * dsum, abs=0.02)
    assert any(r.campaign_id == "AF-X000000" and r.spend_eur > 0 for r, _ in spend)


def test_affiliate_metrics_are_consistent(monkeypatch):
    """Clicks >= sessions >= orders on the affiliate channel; conversions never exceed clicks.

    The commission-paid deal-site campaign takes its reach from the deal traffic,
    the regular coupon-site campaign gets the last click of some promoted orders,
    and campaigns, attributions and sessions use one channel word (affiliate).
    """
    cfg = make_cfg([TRAP])
    world, out = make_world(cfg, hs=local(2025, 2, 24))
    mk.bootstrap_campaigns(world)
    rng = Rng(3, "orders")
    orders = []
    for i in range(30):  # 24 Feb - 25 Mar: a week before and most of the trap window
        orders += _orders_for(world, date(2025, 2, 24) + timedelta(days=i), rng)
    monkeypatch.setattr(mk, "COUPON_INTERCEPT_SHARE", 1.0)  # every promoted organic/direct order
    promoted = {i for i in range(1, len(orders) + 1) if i % 3 == 0}

    def on_order(w, ev):
        d = ev.data
        o = fake_order(d["oid"], w.now, d["country"], d["total"])
        o.promo_id = 123 if d["oid"] in promoted else None
        mk.note_order(w, o, SimpleNamespace(influencer_code=d["code"]), d["source"], d["new"])

    schedule_orders(world, monkeypatch, orders)
    monkeypatch.setitem(HANDLERS, "test.order", on_order)
    run(world, local(2025, 3, 26, 6))
    camps = {e.campaign.campaign_id: e.campaign for e in out if isinstance(e, CampaignDefined)}
    attributed = [r for e in out if isinstance(e, OrdersAttributed) for r in e.rows]
    assert {r.channel for r in attributed} >= {"affiliate"} and "deal_site" not in {r.channel for r in attributed}
    for r in attributed:
        if r.campaign_id:
            assert camps[r.campaign_id].channel == r.channel
    coupon = [r for r in attributed if r.campaign_id and camps[r.campaign_id].scenario_id is None
              and r.channel == "affiliate"]
    deal = [r for r in attributed if r.campaign_id == "AF-X000000"]
    assert coupon and deal
    src = {i: orders[i - 1][1] for i in range(1, len(orders) + 1)}
    assert all(src[r.order_id] in mk.COUPON_SOURCES and r.order_id in promoted for r in coupon)
    spend = [r for e in out if isinstance(e, CampaignSpendReported) for r in e.rows]
    clicks: dict[date, int] = defaultdict(int)
    for r in spend:
        assert r.platform_conversions <= r.clicks, r
        if camps[r.campaign_id].channel == "affiliate":
            clicks[r.spend_date] += r.clicks
    for r in spend:
        if r.campaign_id == "AF-X000000":
            n = sum(1 for a in deal if a.attribution_date == r.spend_date)
            assert r.clicks >= n and r.impressions >= r.clicks
    for e in out:
        if isinstance(e, SessionsReported):
            aff = [r for r in e.rows if r.channel == "affiliate"]
            assert aff and not [r for r in e.rows if r.channel == "deal_site"]
            assert sum(r.orders for r in aff) <= sum(r.sessions for r in aff) <= max(clicks[e.session_date], 1) * 1.05


def test_conversion_varies_and_matches_conversion_rates_with_calibrated_media(monkeypatch):
    cfg = make_cfg(marketing={"paid": {k: {**Config().marketing.paid[k].model_dump(), **v}
                                       for k, v in CALIBRATED_PAID.items()}})
    world, out = make_world(cfg, hs=local(2025, 1, 1))
    mk.bootstrap_campaigns(world)
    rng = Rng(2, "orders")
    days = 365  # a whole year: flights move conversion, the yearly mean is calibrated
    orders = []
    for i in range(days):
        orders += _orders_for(world, date(2025, 1, 1) + timedelta(days=i), rng)
    schedule_orders(world, monkeypatch, orders)
    run(world, local(2025, 1, 1) + timedelta(days=days, hours=6), step_hours=24)
    conv: dict[str, list[float]] = defaultdict(list)
    for e in out:
        if isinstance(e, SessionsReported):
            per = defaultdict(lambda: [0, 0])
            for r in e.rows:
                per[r.channel][0] += r.orders
                per[r.channel][1] += r.sessions
            for ch, (o, s) in per.items():
                if s:
                    conv[ch].append(o / s)
    for ch, rate in cfg.marketing.conversion_rates.items():
        if ch == "deal_site":
            continue  # the affiliate channel: coupon clicks all year, deal sites only in promo_margin_trap
        vals = conv[ch]
        mean = sum(vals) / len(vals)
        assert abs(mean / rate - 1) < 0.30, (ch, mean, rate)
        sd = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
        assert sd > 0.03 * mean, (ch, sd)  # conversion varies with demand noise and spend


def test_spend_boost_and_its_truth_row_once(monkeypatch):
    world, out = make_world(make_cfg([CHURN]))
    mk.bootstrap_campaigns(world)
    run(world, local(2025, 4, 10), step_hours=13)
    eff = [e for e in out if isinstance(e, PlantedEffect) and e.effect == "spend_boost"]
    always = next(c for c in mk.regular_campaigns(world, 2025)
                  if c.influencer_code == "GLOWWITHMIA" and c.start == date(2025, 1, 1))
    # one row per boosted campaign of the code (the always-on campaign and the collaboration flights
    # inside the window), each on the first export in which it is boosted
    boosted = sorted(c.campaign_id for c in mk.regular_campaigns(world, 2025)
                     if c.channel == "influencer" and c.influencer_code == "GLOWWITHMIA"
                     and c.start <= date(2025, 3, 30) and c.end >= date(2025, 2, 3))
    assert sorted(e.entity_id for e in eff) == boosted and len(boosted) > 1
    e = next(x for x in eff if x.entity_id == always.campaign_id)
    assert (e.scenario_id, e.window_id, e.entity_type, e.entity_id, e.magnitude) == (
        "influencer_cohort_churn", "influencer_cohort_churn:config:0", "campaign", always.campaign_id, 2.5)
    assert e.at == mk.export_due(world, date(2025, 2, 3))
    # mkt.campaigns shows the boosted budget while the window runs, and the plain one afterwards
    budget = [ev for ev in out if isinstance(ev, CampaignStatusChanged) and ev.campaign_id == always.campaign_id
              and ev.daily_budget_eur is not None]
    assert [(b.at, b.daily_budget_eur, b.status) for b in budget] == [
        (e.at, round(always.daily_budget_eur * 2.5, 2), "active"),
        (mk.export_due(world, date(2025, 3, 31)), always.daily_budget_eur, "active")]  # end date inclusive
    spend = defaultdict(float)
    for ev in out:
        if isinstance(ev, CampaignSpendReported):
            for r in ev.rows:
                if r.campaign_id == always.campaign_id:
                    spend[r.spend_date] = r.spend_eur
    pre = [v for d, v in spend.items() if d < date(2025, 2, 3)]
    inside = [v for d, v in spend.items() if date(2025, 2, 3) <= d <= date(2025, 3, 30)]
    assert sum(inside) / len(inside) >= 2 * sum(pre) / len(pre)
    # the boosted plan raises acquisition through the code (against the same day without the window)
    t_in = local(2025, 3, 5, 12)
    plain, _ = make_world()
    codes = sorted(world.cfg.customers.influencer_codes)
    i = codes.index("GLOWWITHMIA")

    def code_weight(w):
        cum = mk._day(w, w.local_date(t_in)).code_cum
        return cum[i] - (cum[i - 1] if i else 0.0)

    assert code_weight(world) > 1.5 * code_weight(plain)
    assert mk.new_customer_multiplier(world, t_in) > mk.new_customer_multiplier(plain, t_in)


def test_window_open_at_history_start_records_on_the_first_export():
    world, out = make_world(make_cfg([{**CHURN, "start": "2024-12-01"}]))
    run(world, local(2025, 1, 9))
    eff = [e for e in out if isinstance(e, PlantedEffect) and e.effect == "spend_boost"]
    assert len(eff) == 1 and eff[0].at == mk.export_due(world, date(2025, 1, 6))


def test_partition_invariance(monkeypatch):
    results = []
    for step in (1.0, 17.0):
        world, out = make_world(make_cfg([TRAP, CHURN]), hs=local(2025, 2, 24))
        mk.bootstrap_campaigns(world)
        rng = Rng(9, "orders")
        orders = []
        for i in range(10):
            orders += _orders_for(world, date(2025, 2, 24) + timedelta(days=i), rng, scale=0.3)
        schedule_orders(world, monkeypatch, orders)
        run(world, local(2025, 3, 8), step_hours=step)
        results.append([repr(e) for e in out])
    assert results[0] == results[1]
    assert any("OrdersAttributed" in r for r in results[0])


def test_note_order_ignores_missing_order_and_mkt_day_kind_is_registered():
    world, out = make_world()
    assert mk.note_order(world, None, None, "social", True) is None
    assert world.store.count("mkt_day") == 0 and out == []


def test_late_config_window_defines_its_campaigns_when_known():
    world, out = make_world(make_cfg([TRAP]))
    before = {c.campaign_id: c.defined_at for c in mk.scenario_campaigns(world)}
    assert before["EM-X000000"] == world.at_local(date(2025, 2, 17), 9)
    known = local(2025, 2, 25, 10)
    world.scenarios.set_known_at({"promo_margin_trap:config:0": known})  # start unchanged: same version
    after = {c.campaign_id: c.defined_at for c in mk.scenario_campaigns(world)}
    assert after == {"AF-X000000": known + timedelta(microseconds=1), "EM-X000000": known + timedelta(microseconds=1)}
    world.now = known
    run(world, local(2025, 3, 4))
    defined = [e for e in out if isinstance(e, CampaignDefined) and e.campaign.scenario_id]
    assert sorted((e.campaign.campaign_id, e.at, e.status) for e in defined) == [
        ("AF-X000000", known + timedelta(microseconds=1), "planned"),
        ("EM-X000000", known + timedelta(microseconds=1), "planned")]
