"""Feeds domain (phase-2 design, section 3): accumulation, split, delivery, restatements.

Runs on a lightweight World with every retailer channel enabled. Promotions are
replaced by a small fake (``sys.modules`` swap), scenarios by a stub that
captures truth records, so the feeds logic is tested on its own.
"""

from __future__ import annotations

import math
import sys
import types
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from cpgsim.config import load_config
from cpgsim.domain import feeds
from cpgsim.domain.events import FeedDelivered
from cpgsim.domain.model import ENTITY_KINDS, Counters, FeedAccumulator
from cpgsim.domain.reference import build_reference
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import Event, EventQueue
from cpgsim.engine.registry import ARRIVAL_SOURCES, HANDLERS
from cpgsim.engine.world import World
from cpgsim.models.retail_calendar import retail_period
from cpgsim.models.seasonality import marketplace_sale_week
from cpgsim.writers import keys

HIST = date(2025, 3, 3)  # a Monday, first day of ISO week 10 (retail period P03 starts on week 9)
ALL_ON = {"shopnova": {"enabled": True}, "belle": {"enabled": True}}
_REF: dict = {}


class StubScenarios:
    version = 0
    history_start = None

    def __init__(self) -> None:
        self.effects: list[tuple] = []

    def factor(self, name, t, world=None, record=None, **ctx):
        return 1.0

    def active(self, *a, **k):
        return []

    def scenario_promotions(self):
        return []

    def record_effect(self, world, scenario_id, window_id, entity_type, entity_id, effect, magnitude, detail=None):
        self.effects.append((world.now, scenario_id, window_id, entity_type, entity_id, effect, magnitude, detail))


def cfg_with(**over):
    """All channels on; restatements off unless a test sets the rate."""
    channels = dict(ALL_ON)
    channels.update(over.pop("channels", {}))
    chaos = {"sellout_restatement_rate": 0.0}
    chaos.update(over.pop("chaos", {}))
    return load_config(channels=channels, chaos=chaos, **over)


def make_world(cfg=None, start: date = HIST):
    cfg = cfg or cfg_with()
    out: list = []
    store = EntityStore()
    for kind, cls in ENTITY_KINDS.items():
        store.register(kind, cls)
    store.put("counters", "all", Counters())
    world = World(cfg, store, EventQueue(), emit=out.append)
    key = cfg.fingerprint()
    if key not in _REF:
        _REF[key] = build_reference(cfg)
    world.ref = _REF[key]
    world.scenarios = StubScenarios()
    world.now = world.history_start = world.at_local(start, 0)
    return world, out


def fake_promotions(promos=()):
    """``retail_promos(world, channel, d)``: promotions (channel, sku_ids, discount, first day, last day)."""
    mod = types.ModuleType("cpgsim.domain.promotions")

    def retail_promos(world, channel, d):
        return [SimpleNamespace(sku_ids=list(p[1]), discount_pct=p[2])
                for p in promos if p[0] == channel and p[3] <= d <= p[4]]

    mod.retail_promos = retail_promos
    return mod


@pytest.fixture(autouse=True)
def no_promos(monkeypatch):
    monkeypatch.setitem(sys.modules, "cpgsim.domain.promotions", fake_promotions())


def use_promos(monkeypatch, promos):
    monkeypatch.setitem(sys.modules, "cpgsim.domain.promotions", fake_promotions(promos))


def listed(world, channel, d, n=None):
    ids = world.ref.catalogue.retailer_assortment(channel, d)
    return ids[:n] if n else ids


def deliver(world, channel, feed_type, ps, pe=None):
    """Run the delivery handler for one period at its scheduled time."""
    pe = pe or ps
    due, late = feeds.delivery_time(world, channel, feed_type, ps, pe)
    world.now = max(world.now, due)
    ev = Event(due=due, kind=feeds.DELIVERY_KIND, key=feeds.delivery_key(channel, feed_type, ps),
               data={"channel": channel, "feed_type": feed_type, "period_start": ps.isoformat(),
                     "period_end": pe.isoformat(), "days_late": late}, priority=60)
    HANDLERS[feeds.DELIVERY_KIND](world, ev)


def delivered(out):
    return [e for e in out if isinstance(e, FeedDelivered)]


def week(world, channel, dc, ws, sold, on_hand=None):
    feeds.record_sellout_week(world, channel, dc, ws, sold, {}, dict(sold), on_hand if on_hand is not None else
                              {k: 5 for k in sold})


# ---------------------------------------------------------------------------
# Accumulation and the store/online split
# ---------------------------------------------------------------------------


def test_weekly_accumulation_values_and_split(monkeypatch):
    world, out = make_world()
    skus = listed(world, "drogerie_kranz", HIST, 4)
    use_promos(monkeypatch, [("drogerie_kranz", [skus[0]], 0.25, HIST, HIST + timedelta(days=6))])
    sold = {skus[0]: 40, skus[1]: 13, skus[2]: 0}
    feeds.record_sellout_week(world, "drogerie_kranz", "KRANZ-DC1", HIST, sold, {skus[1]: 2}, sold,
                              {skus[0]: 7, skus[1]: 0, skus[2]: 3, skus[3]: 11})
    feeds.record_sellout_week(world, "drogerie_kranz", "KRANZ-DC2", HIST, {skus[1]: 5}, {}, {skus[1]: 5},
                              {skus[1]: 9})
    acc: FeedAccumulator = world.store.get("feed_acc", f"drogerie_kranz:W:{HIST}")
    assert (acc.period_type, acc.period_start, acc.period_end) == ("W", HIST, HIST + timedelta(days=6))
    assert acc.dcs_reported == ["KRANZ-DC1", "KRANZ-DC2"]
    cat = world.ref.catalogue
    r0 = acc.rows[f"KRANZ-DC1|{skus[0]}"]
    assert r0[0] + r0[1] == 40 and r0[4] == 7
    assert r0[3] == pytest.approx(40 * cat.get(skus[0]).rrp_eur * 0.75)
    assert r0[2] == pytest.approx(r0[3] / 1.19)
    r1 = acc.rows[f"KRANZ-DC1|{skus[1]}"]
    assert r1[0] + r1[1] == 13 and r1[4] == 0
    assert r1[3] == pytest.approx(13 * cat.get(skus[1]).rrp_eur)
    # listed articles without sales still get a row (0 units, the DC's stock)
    assert acc.rows[f"KRANZ-DC1|{skus[2]}"] == [0, 0, 0.0, 0.0, 3]
    assert acc.rows[f"KRANZ-DC1|{skus[3]}"] == [0, 0, 0.0, 0.0, 11]
    assert acc.rows[f"KRANZ-DC2|{skus[1]}"][4] == 9
    assert not out  # nothing is emitted before the delivery


def test_same_week_twice_is_ignored():
    world, _ = make_world()
    sku = listed(world, "mylo", HIST, 1)[0]
    week(world, "mylo", "MYLO-DC1", HIST, {sku: 10})
    week(world, "mylo", "MYLO-DC1", HIST, {sku: 10})
    row = world.store.get("feed_acc", f"mylo:W:{HIST}").rows[f"MYLO-DC1|{sku}"]
    assert row[0] + row[1] == 10


def test_online_split_share_and_determinism():
    """Online units are about online_share of sold units (lognormal per DC and week); same inputs, same split."""
    totals = {}
    for channel, dc, share in (("drogerie_kranz", "KRANZ-DC1", 0.14), ("mylo", "MYLO-DC2", 0.11)):
        world, _ = make_world()
        skus = listed(world, channel, HIST)
        on, tot = 0, 0
        for k in range(30):
            ws = HIST + timedelta(weeks=k)
            sold = {s: 3 + (s * 7 + k) % 40 for s in skus}
            week(world, channel, dc, ws, sold)
            acc = world.store.get("feed_acc", f"{channel}:W:{ws}")
            on += sum(r[1] for r in acc.rows.values())
            tot += sum(r[0] + r[1] for r in acc.rows.values())
        totals[channel] = on / tot
        assert totals[channel] == pytest.approx(share, rel=0.08)
    # determinism: a second world gives the same rows
    w1, _ = make_world()
    w2, _ = make_world()
    skus = listed(w1, "mylo", HIST, 20)
    for w in (w1, w2):
        week(w, "mylo", "MYLO-DC1", HIST, {s: 17 for s in skus})
    assert w1.store.get("feed_acc", f"mylo:W:{HIST}").rows == w2.store.get("feed_acc", f"mylo:W:{HIST}").rows


def test_belle_retail_month_accumulates_and_last_week_stock_wins():
    world, _ = make_world()
    # history starts on the first Monday of P04 (ISO week 14 of 2025)
    _, p, first, last = retail_period(date(2025, 3, 31))
    assert (p, first, last) == (4, date(2025, 3, 31), date(2025, 4, 27))
    world, out = make_world(start=first)
    sku = listed(world, "belle", first, 1)[0]
    for k in range(4):
        week(world, "belle", "BELLE-DC1", first + timedelta(weeks=k), {sku: 10 + k}, {sku: 100 - k})
    acc = world.store.get("feed_acc", f"belle:M:{first}")
    assert (acc.period_type, acc.period_end) == ("M", last)
    row = acc.rows[f"BELLE-DC1|{sku}"]
    assert row[0] + row[1] == 10 + 11 + 12 + 13
    assert row[4] == 97  # the last week of the period wins
    deliver(world, "belle", "sellout_monthly", first, last)
    (e,) = delivered(out)
    assert (e.feed_type, e.period_start, e.period_end, e.run) == ("sellout_monthly", first, last, 1)
    store = [r for r in e.sellout if r.sales_channel == "store"]
    assert store[0].stock_units == 97
    assert not world.store.has("feed_acc", f"belle:M:{first}")


def test_history_start_skips_partial_periods():
    world, out = make_world(start=date(2025, 3, 5))  # a Wednesday inside P03
    sku = listed(world, "mylo", HIST, 1)[0]
    week(world, "mylo", "MYLO-DC1", HIST, {sku: 4})  # week starts before the history start
    week(world, "belle", "BELLE-DC1", HIST, {sku: 4})  # P03 started on 2025-02-24
    feeds.record_marketplace_day(world, "shopnova", "SHOPNOVA-DC1", date(2025, 3, 4), {sku: 1}, {sku: 1},
                                 {sku: 1}, {})
    assert world.store.count("feed_acc") == 0
    # ... and the arrival source schedules nothing for them
    evs = ARRIVAL_SOURCES["feeds.delivery"](world, world.now, world.at_local(date(2025, 3, 12), 0))
    starts = {e.data["period_start"] for e in evs}
    assert "2025-03-03" not in starts and "2025-03-04" not in starts
    assert {e.data["period_start"] for e in evs if e.data["feed_type"] == "mp"} >= {"2025-03-05", "2025-03-06"}


# ---------------------------------------------------------------------------
# Delivered rows
# ---------------------------------------------------------------------------


def test_delivered_rows_store_and_online(monkeypatch):
    world, out = make_world()
    skus = listed(world, "drogerie_kranz", HIST, 3)
    feeds.record_sellout_week(world, "drogerie_kranz", "KRANZ-DC1", HIST, {skus[0]: 50, skus[1]: 0}, {},
                              {skus[0]: 50, skus[1]: 6}, {skus[0]: 12, skus[1]: 0, skus[2]: 4})
    deliver(world, "drogerie_kranz", "sellout_weekly", HIST, HIST + timedelta(days=6))
    (e,) = delivered(out)
    assert (e.channel, e.feed_type, e.run, e.restates_run) == ("drogerie_kranz", "sellout_weekly", 1, None)
    by = {(r.sku_id, r.sales_channel): r for r in e.sellout}
    # every listed article has a store row; an out-of-stock article without sales says so explicitly
    assert by[(skus[1], "store")].units == 0 and by[(skus[1], "store")].stock_units == 0
    assert by[(skus[2], "store")].units == 0 and by[(skus[2], "store")].stock_units == 4
    assert (skus[1], "online") not in by and (skus[2], "online") not in by
    st, on = by[(skus[0], "store")], by.get((skus[0], "online"))
    assert st.stock_units == 12
    units_online = on.units if on else 0
    assert st.units + units_online == 50
    if on:
        assert on.stock_units is None
    gross = 50 * world.ref.catalogue.get(skus[0]).rrp_eur
    assert st.value_gross_eur + (on.value_gross_eur if on else 0) == pytest.approx(gross, abs=0.011)
    assert st.value_net_eur == pytest.approx(st.value_gross_eur / 1.19, abs=0.006)
    assert world.store.count("feed_acc") == 0


def test_delivery_without_accumulator_emits_nothing():
    world, out = make_world()
    deliver(world, "mylo", "sellout_weekly", HIST, HIST + timedelta(days=6))
    assert not delivered(out)


# ---------------------------------------------------------------------------
# Delivery timing (pure arrival source)
# ---------------------------------------------------------------------------


def _local(world, t: datetime) -> datetime:
    return world.local(t)


def test_delivery_schedule_per_sender():
    world, _ = make_world()
    end = world.at_local(HIST + timedelta(weeks=10), 0)
    evs = ARRIVAL_SOURCES["feeds.delivery"](world, world.now, end)
    assert len({e.key for e in evs}) == len(evs)
    fc = world.cfg.feeds
    for e in evs:
        assert e.kind == "feeds.deliver" and e.priority == 60
        ch, ft = e.data["channel"], e.data["feed_type"]
        ps, pe = date.fromisoformat(e.data["period_start"]), date.fromisoformat(e.data["period_end"])
        loc = _local(world, e.due)
        hour = loc.hour + loc.minute / 60 + loc.second / 3600
        late = e.data["days_late"]
        if ft == "mp":
            assert ch == "shopnova" and e.key == f"feeds.deliver:shopnova:mp:{ps:%Y%m%d}"
            assert ps == pe and loc.date() == ps + timedelta(days=1) and late == 0
            assert 7.0 <= hour <= 9.0
        elif ft == "sellout_weekly":
            assert ch in ("drogerie_kranz", "mylo") and ps.weekday() == 0 and pe == ps + timedelta(days=6)
            assert e.key == f"feeds.deliver:{ch}:sellout_weekly:{ps:%Y%m%d}"
            assert loc.date() == pe + timedelta(days=1 + late)
            lo, hi = fc.weekly_delivery_hours[ch]
            assert lo <= hour <= hi
        else:
            assert ch == "belle" and ft == "sellout_monthly"
            assert retail_period(ps)[2:] == (ps, pe)
            assert loc.date() == pe + timedelta(days=3 + late)  # Wednesday after the last Sunday
            assert loc.date().weekday() == (2 + late) % 7
            assert 8.0 <= hour <= 12.0
    kinds = {(e.data["channel"], e.data["feed_type"]) for e in evs}
    assert kinds == {("drogerie_kranz", "sellout_weekly"), ("mylo", "sellout_weekly"),
                     ("belle", "sellout_monthly"), ("shopnova", "mp")}


def test_disabled_channels_get_no_deliveries():
    off = {"shopnova": {"enabled": False}, "belle": {"enabled": False}}
    world, _ = make_world(load_config(channels=off))
    evs = ARRIVAL_SOURCES["feeds.delivery"](world, world.now, world.at_local(HIST + timedelta(weeks=8), 0))
    assert {e.data["channel"] for e in evs} == {"drogerie_kranz", "mylo"}


def test_late_delivery_rate_and_days():
    world, _ = make_world()
    n, late, days = 0, 0, set()
    for ch in ("drogerie_kranz", "mylo"):
        for k in range(1500):
            ps = HIST + timedelta(weeks=k)
            due, d = feeds.delivery_time(world, ch, "sellout_weekly", ps, ps + timedelta(days=6))
            n += 1
            if d:
                late += 1
                days.add(d)
                assert world.local(due).date() == ps + timedelta(days=7 + d)
    p = 0.03
    assert abs(late / n - p) < 3 * math.sqrt(p * (1 - p) / n)
    assert days == {1, 2}
    for k in range(200):  # marketplace reports are never late
        day = HIST + timedelta(days=k)
        assert feeds.delivery_time(world, "shopnova", "mp", day, day)[1] == 0


def test_arrivals_are_partition_invariant():
    world, _ = make_world()
    end = world.at_local(HIST + timedelta(weeks=9), 0)
    one = ARRIVAL_SOURCES["feeds.delivery"](world, world.now, end)
    many, t = [], world.now
    while t < end:
        t1 = min(t + timedelta(hours=5), end)
        many.extend(ARRIVAL_SOURCES["feeds.delivery"](world, t, t1))
        t = t1
    key = lambda e: (e.due, e.key)  # noqa: E731
    assert sorted((e.due, e.key, e.data) for e in one) == sorted((e.due, e.key, e.data) for e in many)
    assert len(one) == len({e.key for e in one}) > 50
    assert sorted(one, key=key) == sorted(many, key=key)


# ---------------------------------------------------------------------------
# Restatements
# ---------------------------------------------------------------------------


def _restating_cfg(rate=1.0):
    return cfg_with(chaos={"sellout_restatement_rate": rate})


def test_restatement_run1_underreports_one_region_and_records_truth():
    world, out = make_world(_restating_cfg())
    skus = listed(world, "drogerie_kranz", HIST, 30)
    ws = HIST
    for dc in ("KRANZ-DC1", "KRANZ-DC2", "KRANZ-DC3"):
        week(world, "drogerie_kranz", dc, ws, {s: 20 + s % 9 for s in skus}, {s: 8 for s in skus})
    true_rows = feeds.sellout_rows(world.store.get("feed_acc", f"drogerie_kranz:W:{ws}"))
    deliver(world, "drogerie_kranz", "sellout_weekly", ws, ws + timedelta(days=6))
    (run1,) = delivered(out)
    assert run1.run == 1 and run1.restates_run is None
    acc = world.store.get("feed_acc", f"drogerie_kranz:W:{ws}")
    assert acc is not None and acc.meta["restate"] is True
    region, m = acc.meta["region_dc"], acc.meta["missing_share"]
    lo, hi = world.cfg.feeds.restatement_missing_share
    assert lo <= m <= hi and region in ("KRANZ-DC1", "KRANZ-DC2", "KRANZ-DC3")
    truth = {(r.dc_account, r.sales_channel, r.sku_id): r for r in true_rows}
    for r in run1.sellout:
        t = truth[(r.dc_account, r.sales_channel, r.sku_id)]
        assert r.stock_units == t.stock_units  # stock is reported correctly
        if r.dc_account == region and r.sales_channel == "store":
            assert r.units == math.floor(t.units * (1 - m))
            if t.units:
                assert r.value_gross_eur == pytest.approx(t.value_gross_eur * r.units / t.units, abs=0.011)
        else:
            assert (r.units, r.value_gross_eur, r.value_net_eur) == (t.units, t.value_gross_eur, t.value_net_eur)
    # truth at run 1, once
    run1_id = keys.feed_load_id("drogerie_kranz", "sellout_weekly", ws, 1)
    (eff,) = world.scenarios.effects
    _, sid, wid, etype, eid, effect, mag, detail = eff
    assert (sid, wid, etype, eid, effect, mag) == (
        "sellout_restatement", "sellout_restatement:chaos", "feed_load", run1_id, "restated", m)
    assert detail["restatement_load_id"] == "KRZ_SO_20250303_02"
    assert detail["region_dc"] == region
    assert detail["units_original"] == sum(r.units for r in run1.sellout
                                           if r.dc_account == region and r.sales_channel == "store")
    assert detail["units_restated"] == sum(t.units for t in true_rows
                                           if t.dc_account == region and t.sales_channel == "store")
    assert detail["units_original"] < detail["units_restated"]


def test_restatement_run2_goes_out_with_the_next_delivery():
    world, out = make_world(_restating_cfg())
    skus = listed(world, "mylo", HIST, 10)
    ws1, ws2 = HIST, HIST + timedelta(weeks=1)
    week(world, "mylo", "MYLO-DC1", ws1, {s: 9 for s in skus})
    week(world, "mylo", "MYLO-DC2", ws1, {s: 11 for s in skus})
    true1 = feeds.sellout_rows(world.store.get("feed_acc", f"mylo:W:{ws1}"))
    deliver(world, "mylo", "sellout_weekly", ws1, ws1 + timedelta(days=6))
    week(world, "mylo", "MYLO-DC1", ws2, {s: 3 for s in skus})
    deliver(world, "mylo", "sellout_weekly", ws2, ws2 + timedelta(days=6))
    evs = delivered(out)
    assert [(e.period_start, e.run, e.restates_run) for e in evs] == [
        (ws1, 1, None), (ws1, 2, 1), (ws2, 1, None)]
    assert evs[1].sellout == true1  # run 2 restates in full, with the true values
    assert evs[1].at == evs[2].at
    assert not world.store.has("feed_acc", f"mylo:W:{ws1}")
    assert world.store.has("feed_acc", f"mylo:W:{ws2}")  # rate 1: the new week is pending too
    assert len(world.scenarios.effects) == 2


def test_pending_restatement_lookup_by_derived_id_for_retail_months():
    world, out = make_world(_restating_cfg(), start=date(2025, 3, 31))
    p4 = retail_period(date(2025, 3, 31))
    p5 = retail_period(p4[3] + timedelta(days=1))
    assert feeds.previous_period_start("sellout_monthly", p5[2]) == p4[2]
    assert feeds.previous_period_start("sellout_weekly", HIST) == HIST - timedelta(weeks=1)
    sku = listed(world, "belle", p4[2], 1)[0]
    for k in range(4):
        week(world, "belle", "BELLE-DC1", p4[2] + timedelta(weeks=k), {sku: 30})
    deliver(world, "belle", "sellout_monthly", p4[2], p4[3])
    # an unrelated pending accumulator of another channel is not touched
    assert world.store.get("feed_acc", f"belle:M:{p4[2]}").meta["restate"]
    deliver(world, "belle", "sellout_monthly", p5[2], p5[3])  # P05 itself was never accumulated
    evs = delivered(out)
    assert [(e.period_start, e.run) for e in evs] == [(p4[2], 1), (p4[2], 2)]
    assert evs[1].feed_type == "sellout_monthly" and evs[1].restates_run == 1
    assert world.store.count("feed_acc") == 0


def test_restatement_rate_is_about_ten_percent_of_periods():
    world, _ = make_world(load_config(channels=ALL_ON))  # the spec default rate
    from cpgsim import chaos

    n, hits = 0, 0
    for ch in ("drogerie_kranz", "mylo", "belle"):
        for k in range(1200):
            n += 1
            hits += chaos.hit(world, "sellout_restatement", ch, HIST + timedelta(weeks=k))
    p = 0.10
    assert abs(hits / n - p) < 3 * math.sqrt(p * (1 - p) / n)


def test_restatement_off_when_rate_zero():
    world, out = make_world(_restating_cfg(0.0))
    sku = listed(world, "mylo", HIST, 1)[0]
    week(world, "mylo", "MYLO-DC1", HIST, {sku: 9})
    deliver(world, "mylo", "sellout_weekly", HIST, HIST + timedelta(days=6))
    assert [e.run for e in delivered(out)] == [1]
    assert not world.scenarios.effects and world.store.count("feed_acc") == 0


# ---------------------------------------------------------------------------
# Marketplace reports
# ---------------------------------------------------------------------------


def test_marketplace_day_accumulates_both_fcs_and_reports(monkeypatch):
    world, out = make_world()
    a, b, c, d = listed(world, "shopnova", HIST, 4)
    day = HIST + timedelta(days=2)
    use_promos(monkeypatch, [("shopnova", [b], 0.20, day, day)])
    feeds.record_marketplace_day(world, "shopnova", "SHOPNOVA-DC1", day, {a: 3, b: 0}, {a: 3, b: 4},
                                 {a: 10, b: 0, c: 0, d: 0}, {b: 24})
    feeds.record_marketplace_day(world, "shopnova", "SHOPNOVA-DC2", day, {a: 2}, {a: 2}, {a: 5}, {})
    acc = world.store.get("feed_acc", f"shopnova:D:{day}")
    assert acc.rows[str(a)] == [5, 5, 15, 0] and acc.rows[str(b)] == [0, 4, 0, 24]
    deliver(world, "shopnova", "mp", day)
    sales_ev, inv_ev = delivered(out)
    assert (sales_ev.feed_type, inv_ev.feed_type) == ("mp_sales_daily", "mp_inventory_daily")
    assert sales_ev.period_start == sales_ev.period_end == day and sales_ev.run == inv_ev.run == 1
    sales = {r.sku_id: r for r in sales_ev.mp_sales}
    assert set(sales) == {a, b}  # c and d: no units, no views
    rrp = world.ref.catalogue.get(a).rrp_eur
    assert sales[a].units == 5 and sales[a].revenue_gross_eur == round(5 * rrp * 0.93, 2)
    lo, hi = world.cfg.feeds.marketplace_conversion
    assert 5 / hi * 0.5 < sales[a].glance_views < 5 / lo * 2.0
    # out of stock: views without units
    assert sales[b].units == 0 and sales[b].glance_views > 0 and sales[b].revenue_gross_eur == 0
    inv = {r.sku_id: (r.sellable_units, r.inbound_units) for r in inv_ev.mp_inventory}
    assert inv == {a: (15, 0), b: (0, 24)}
    assert world.store.count("feed_acc") == 0


def test_marketplace_price_sale_week_and_promotion(monkeypatch):
    world, _ = make_world()
    sku = listed(world, "shopnova", HIST, 1)[0]
    rrp = world.ref.catalogue.get(sku).rrp_eur
    lo, _ = marketplace_sale_week(2025)
    assert feeds.marketplace_price(world, "shopnova", sku, lo - timedelta(days=1)) == pytest.approx(rrp * 0.93)
    assert feeds.marketplace_price(world, "shopnova", sku, lo) == pytest.approx(rrp * 0.93 * 0.85)
    use_promos(monkeypatch, [("shopnova", [sku], 0.30, lo, lo)])
    assert feeds.marketplace_price(world, "shopnova", sku, lo) == pytest.approx(rrp * 0.93 * 0.85 * 0.70)


def test_marketplace_conversion_is_pure_per_sku():
    w1, _ = make_world()
    w2, _ = make_world()
    lo, hi = w1.cfg.feeds.marketplace_conversion
    convs = [feeds._conversion(w1, s) for s in range(1, 60)]
    assert convs == [feeds._conversion(w2, s) for s in range(1, 60)]
    assert all(lo <= c <= hi for c in convs) and len(set(convs)) == len(convs)


# ---------------------------------------------------------------------------
# Partition invariance with a sell-out feeder (in the role of retail.on_sellout)
# ---------------------------------------------------------------------------


def _feeder_sources(world):
    """Weekly and daily sell-out calls at 00:30 local, as retail makes them (pure arrivals)."""

    def arrivals(w, start, end):
        out = []
        d = w.local(start).date()
        while d <= w.local(end).date():
            t = w.at_local(d, 0, 30)
            if start < t <= end:
                out.append(Event(due=t, kind="test.feeds_sellout", key=f"test.feeds_sellout:{d:%Y%m%d}",
                                 data={"day": d.isoformat()}, priority=20))
            d += timedelta(days=1)
        return out

    def on_sellout(w, ev):
        d = date.fromisoformat(ev.data["day"])
        cat = w.ref.catalogue
        if d.weekday() == 0:
            ws = d - timedelta(days=7)
            for ch, dcs in (("drogerie_kranz", 3), ("mylo", 2), ("belle", 2)):
                prefix = {"drogerie_kranz": "KRANZ", "mylo": "MYLO", "belle": "BELLE"}[ch]
                for i in range(1, dcs + 1):
                    rng = w.rng("test.sold", ch, i, ws.isoformat())
                    ids = cat.retailer_assortment(ch, ws)
                    sold = {s: rng.randint(0, 30) for s in ids}
                    feeds.record_sellout_week(w, ch, f"{prefix}-DC{i}", ws, sold, {}, sold,
                                              {s: rng.randint(0, 20) for s in ids})
        day = d - timedelta(days=1)
        ids = cat.retailer_assortment("shopnova", day)
        for i in (1, 2):
            rng = w.rng("test.mp", i, day.isoformat())
            sold = {s: rng.randint(0, 4) for s in ids}
            feeds.record_marketplace_day(w, "shopnova", f"SHOPNOVA-DC{i}", day, sold,
                                         {s: v + rng.randint(0, 2) for s, v in sold.items()},
                                         {s: rng.randint(0, 9) for s in ids}, {s: rng.randint(0, 1) * 12 for s in ids})

    return arrivals, on_sellout


def _run(world, until, step_hours):
    t = world.now
    step = timedelta(hours=step_hours)
    while t < until:
        end = min(t + step, until)
        for name in ("feeds.delivery", "test.feeds_sellout"):
            for ev in ARRIVAL_SOURCES[name](world, t, end):
                assert t < ev.due <= end
                world.queue.push(ev)
        while (due := world.queue.peek_due()) is not None and due <= end:
            ev = world.queue.pop()
            world.now = ev.due
            HANDLERS[ev.kind](world, ev)
        world.now = end
        t = end


def test_feeds_partition_invariance(monkeypatch):
    """Two tick lengths give identical delivered feeds (restatements and truth included)."""
    results = []
    for step in (1.0, 13.0):
        world, out = make_world(_restating_cfg(0.3), start=date(2025, 3, 31))
        arrivals, on_sellout = _feeder_sources(world)
        monkeypatch.setitem(ARRIVAL_SOURCES, "test.feeds_sellout", arrivals)
        monkeypatch.setitem(HANDLERS, "test.feeds_sellout", on_sellout)
        _run(world, world.at_local(date(2025, 3, 31) + timedelta(weeks=10), 0), step)
        evs = delivered(out)
        results.append((
            [(e.at, e.channel, e.feed_type, e.period_start, e.run, e.sellout, e.mp_sales, e.mp_inventory)
             for e in evs],
            list(world.scenarios.effects),
        ))
        kinds = {(e.channel, e.feed_type) for e in evs}
        assert kinds == {("drogerie_kranz", "sellout_weekly"), ("mylo", "sellout_weekly"),
                         ("belle", "sellout_monthly"), ("shopnova", "mp_sales_daily"),
                         ("shopnova", "mp_inventory_daily")}
        assert any(e.run == 2 for e in evs) and world.scenarios.effects
        # bounded state: at most the current and one pending period per channel
        assert world.store.count("feed_acc") <= 2 * 4
    assert results[0] == results[1]


def test_accumulator_survives_a_restart():
    """A pending accumulator saved and loaded (JSON) delivers the same rows and restatement."""
    import json

    from cpgsim.engine.codec import from_jsonable, to_jsonable

    outs = []
    for restart in (False, True):
        world, out = make_world(_restating_cfg())
        skus = listed(world, "drogerie_kranz", HIST, 12)
        for dc in ("KRANZ-DC1", "KRANZ-DC2"):
            week(world, "drogerie_kranz", dc, HIST, {s: 7 + s % 5 for s in skus})
        deliver(world, "drogerie_kranz", "sellout_weekly", HIST, HIST + timedelta(days=6))
        if restart:
            for aid, acc in list(world.store.items("feed_acc")):
                blob = json.loads(json.dumps(to_jsonable(acc)))
                world.store.put("feed_acc", aid, from_jsonable(FeedAccumulator, blob))
        ws2 = HIST + timedelta(weeks=1)
        week(world, "drogerie_kranz", "KRANZ-DC1", ws2, {s: 2 for s in skus})
        deliver(world, "drogerie_kranz", "sellout_weekly", ws2, ws2 + timedelta(days=6))
        outs.append([(e.period_start, e.run, e.sellout) for e in delivered(out)])
    assert outs[0] == outs[1]
    assert [x[:2] for x in outs[0]] == [(HIST, 1), (HIST, 2), (HIST + timedelta(weeks=1), 1)]
