"""Trade deductions and promotion agreements (phase-2 design, section 5.4).

Runs the deductions module on a lightweight World with the real reference
data, the real (pure) promotion calendar and the real scenario manager. Retail
is replaced by a small driver that calls the public API the way
``domain/retail.py`` does (``record_sellout`` after each sell-out week,
``take_on_payment`` at each invoice payment, ``record_pod``, ``record_cut``).
The erp rows are checked through the real ``ErpWriter``.
"""

from __future__ import annotations

import math
import sys
import types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cpgsim.config import Config
from cpgsim.domain import deductions as ded
from cpgsim.domain import promotions
from cpgsim.domain.events import (
    DeductionChanged,
    DeductionClaimed,
    DeductionCleared,
    PlantedEffect,
    TradeAgreementDefined,
)
from cpgsim.domain.model import ENTITY_KINDS, Counters, Deduction, RetailPO
from cpgsim.domain.reference import build_reference
from cpgsim.engine.codec import from_jsonable, to_jsonable
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import EventQueue
from cpgsim.engine.registry import ARRIVAL_SOURCES, HANDLERS
from cpgsim.engine.world import World
from cpgsim.scenarios.manager import ScenarioManager
from cpgsim.writers import keys
from cpgsim.writers.base import Dispatcher, WriterContext
from cpgsim.writers.erp import ErpWriter

BERLIN = ZoneInfo("Europe/Berlin")
SOURCES = ("ded.agreements", "ded.promo_claims", "ded.overstock")
OVERSTOCK = {"id": "retailer_overstock", "retailer": "mylo", "over_order": 1.30, "duration_weeks": 13,
             "cut_weeks": 6, "start": "2026-01-05"}

_REF: dict = {}


def local(y, m, d, h=0, mi=0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=BERLIN).astimezone(ZoneInfo("UTC"))


def make_cfg(enable=("shopnova", "belle"), deductions: dict | None = None, scenarios: list | None = None) -> Config:
    data = Config().model_dump()
    for ch in ("shopnova", "belle"):  # explicit, whatever the shipped default
        data["channels"][ch]["enabled"] = ch in enable
    for k, v in (deductions or {}).items():
        data["deductions"][k] = v
    data["scenarios"] = scenarios or []
    return Config.model_validate(data)


def make_world(cfg: Config, start: datetime):
    collected: list = []
    store = EntityStore()
    for kind, cls in ENTITY_KINDS.items():
        store.register(kind, cls)
    store.put("counters", "all", Counters())
    world = World(cfg, store, EventQueue(), emit=collected.append)
    key = tuple(sorted(cfg.channels.retailers()))
    if key not in _REF:
        _REF[key] = build_reference(cfg)
    world.ref = _REF[key]
    world.scenarios = ScenarioManager(cfg)
    world.now = world.history_start = start
    world.scenarios.attach(world)
    return world, collected


def run(world, until: datetime, sources=SOURCES, step_hours: float = 6.0, actions=()):
    """Mini runner: arrival sources per slice, queue events and driver actions in time order."""
    acts = sorted(actions, key=lambda a: a[0])
    i = 0
    t = world.now
    step = timedelta(hours=step_hours)
    while t < until:
        end = min(t + step, until)
        for name in sorted(sources):
            for ev in ARRIVAL_SOURCES[name](world, t, end):
                assert t < ev.due <= end, ev.key
                world.queue.push(ev)
        while True:
            due = world.queue.peek_due()
            a_due = acts[i][0] if i < len(acts) else None
            if (due is None or due > end) and (a_due is None or a_due > end):
                break
            if a_due is not None and a_due <= end and (due is None or a_due < due):
                world.now = a_due
                acts[i][1](world)
                i += 1
                continue
            ev = world.queue.pop()
            world.now = ev.due
            HANDLERS[ev.kind](world, ev)
        world.now = end
        t = end


def bootstrap(world) -> None:
    ded.bootstrap_agreements(world)


def of(events, cls):
    return [e for e in events if isinstance(e, cls)]


def erp_rows(world, events) -> dict[str, dict]:
    """Final erp rows (one batch: every update is coalesced into its insert)."""
    w = ErpWriter(WriterContext(seed=world.seed, cfg=world.cfg, ref=world.ref, tz=world.tz))
    d = Dispatcher([w])
    for ev in events:
        d.dispatch(ev)
    out = {}
    for name, buf in d.batch.buffers.items():
        assert not buf.updates, f"{name}: update of a row not inserted in the batch"
        out[name] = dict(buf.inserts)
    return out


def kranz_promos(world, d0: date, d1: date):
    return [p for p in promotions.promotions_between(world, d0, d1, "drogerie_kranz") if p.channel == "drogerie_kranz"]


# ---------------------------------------------------------------------------
# Agreements
# ---------------------------------------------------------------------------


def test_agreements_at_announcement_and_bootstrap():
    cfg = make_cfg()
    hs = local(2025, 3, 3)
    world, out = make_world(cfg, hs)
    bootstrap(world)
    scheduled_at_start = {k for k in (f"ded.agreement:{p.promo_id}" for ch in cfg.channels.retailers()
                                      for p in promotions.promotions_between(world, date(2025, 1, 1),
                                                                             date(2025, 5, 1), ch))
                          if world.is_scheduled(k)}
    run(world, local(2025, 7, 1))
    agreements = of(out, TradeAgreementDefined)
    assert agreements
    by_id = {a.promo_id: a for a in agreements}
    assert len(by_id) == len(agreements)  # once per promotion
    hd = date(2025, 3, 3)
    expected = set()
    for ch in ("drogerie_kranz", "mylo", "shopnova", "belle"):
        for p in promotions.promotions_between(world, hd, date(2025, 8, 12), ch):
            if p.channel == ch and p.announced_at + timedelta(minutes=1) <= local(2025, 7, 1) and p.end >= hs:
                expected.add(p.promo_id)
    assert set(by_id) == expected
    # promotions announced before the history start and still running or upcoming: bootstrapped
    early = [p for p in expected if promotions.find(world, p).announced_at < hs]
    assert early, "fixture should include a promotion announced before the history start"
    assert {f"ded.agreement:{p}" for p in early} == scheduled_at_start

    p = promotions.find(world, next(iter(sorted(pid for pid in expected if pid not in early))))
    a = by_id[p.promo_id]
    assert a.at == p.announced_at + timedelta(minutes=1)
    assert a.start == p.start.astimezone(BERLIN).date() and a.end == p.end.astimezone(BERLIN).date()
    assert a.off_invoice_share == cfg.promotions.retail_funding_share
    assert a.scanback_share == 0.25 and a.discount_pct == p.discount_pct
    assert [i.sku_id for i in a.items] == sorted(p.sku_ids)
    for it in a.items:
        sku = world.ref.catalogue.get(it.sku_id)
        assert it.scan_rate_net == round(0.25 * p.discount_pct * sku.rrp_eur / 1.19, 4)
        assert it.customer_article == world.ref.catalogue.customer_article(p.channel, it.sku_id)
    fees = {a.channel: a.advertising_fee_eur for a in agreements}
    assert fees["drogerie_kranz"] == 2500.0 and fees["mylo"] == 1800.0
    assert fees.get("shopnova") is None and fees.get("belle") is None
    kr = next(a for a in agreements if a.channel == "drogerie_kranz")
    y, w, _ = kr.start.isocalendar()
    assert kr.text == f"AKTION KW{w:02d}/{y}"


def test_channel_without_promotion_type_has_no_agreements_or_claims():
    types_ = Config().deductions.model_dump()["types"]
    types_["mylo"]["promotion"] = False
    cfg = make_cfg(enable=(), deductions={"types": types_})
    world, out = make_world(cfg, local(2025, 3, 3))
    bootstrap(world)
    run(world, local(2025, 9, 1), step_hours=24)
    assert {a.channel for a in of(out, TradeAgreementDefined)} == {"drogerie_kranz"}
    # mylo still raises its WKZ fee, without an agreement reference
    wkz = [e.deduction for e in of(out, DeductionClaimed) if e.deduction.channel == "mylo"]
    assert wkz and all(d.kind == "advertising" and d.promo_id is None for d in wkz)
    world.now = local(2025, 9, 1)
    ded.record_sellout(world, "mylo", "MYLO-DC1", date(2025, 5, 5), 7, {1: 5})
    assert not list(world.store.items("promo_claim"))


# ---------------------------------------------------------------------------
# Promotion claims
# ---------------------------------------------------------------------------


def test_record_sellout_accumulates_promo_units_per_week_and_brand():
    cfg = make_cfg(enable=())
    world, _ = make_world(cfg, local(2025, 1, 6))
    p = kranz_promos(world, date(2025, 3, 1), date(2025, 6, 30))[0]
    ws = p.start.astimezone(BERLIN).date()
    sku_in = sorted(p.sku_ids)[0]
    sku_out = next(s for s in world.ref.catalogue.retailer_assortment("drogerie_kranz", ws) if s not in p.sku_ids)
    brand = world.ref.catalogue.get(sku_in).brand
    world.now = local(2025, 7, 1)
    for dc in ("KRANZ-DC1", "KRANZ-DC2"):
        ded.record_sellout(world, "drogerie_kranz", dc, ws, 7, {sku_in: 30, sku_out: 99})
    ded.record_sellout(world, "drogerie_kranz", "KRANZ-DC1", ws - timedelta(days=7), 7, {sku_in: 40})
    basis = world.store.get("promo_claim", f"{p.promo_id}:{ws:%Y%m%d}:{brand}")
    assert basis is not None and basis.units == {sku_in: 60} and basis.week_start == ws
    assert len(list(world.store.items("promo_claim"))) == 1
    # daily periods: each day goes into the day's promotion week
    ded.record_sellout(world, "drogerie_kranz", "KRANZ-DC1", ws + timedelta(days=2), 1, {sku_in: 4})
    assert world.store.get("promo_claim", f"{p.promo_id}:{ws:%Y%m%d}:{brand}").units == {sku_in: 64}
    # a period covered partly by the promotion counts pro rata (3 of 7 days)
    ded.record_sellout(world, "drogerie_kranz", "KRANZ-DC3", ws - timedelta(days=4), 7, {sku_in: 70})
    assert world.store.get("promo_claim", f"{p.promo_id}:{ws:%Y%m%d}:{brand}").units == {sku_in: 94}
    # survives a save/load round trip
    b = world.store.get("promo_claim", f"{p.promo_id}:{ws:%Y%m%d}:{brand}")
    assert from_jsonable(type(b), to_jsonable(b)) == b


def _promo_week_basis(world, units_per_sku: int = 20):
    """A Kranz promotion week with sold units for every promoted SKU of its first brand."""
    p = kranz_promos(world, date(2025, 3, 1), date(2025, 6, 30))[0]
    ws = p.start.astimezone(BERLIN).date()
    cat = world.ref.catalogue
    brand = sorted({cat.get(s).brand for s in p.sku_ids})[0]
    sold = {s: units_per_sku + s % 7 for s in p.sku_ids if cat.get(s).brand == brand}
    ded.record_sellout(world, "drogerie_kranz", "KRANZ-DC1", ws, 7, sold)
    return p, ws, brand, sold


def _claim_event(p, ws, brand):
    return types.SimpleNamespace(data={"promo_id": p.promo_id, "channel": "drogerie_kranz",
                                       "week_start": ws.isoformat(), "brand": brand})


def test_promotion_claim_items_amount_and_matching():
    cfg = make_cfg(enable=(), deductions={"overclaim_rate": 0.0, "invalid_claim_rate": 0.0})
    world, out = make_world(cfg, local(2025, 1, 6))
    p, ws, brand, sold = _promo_week_basis(world)
    world.now = local(2025, 7, 1, 11)
    ded.on_claim(world, _claim_event(p, ws, brand))
    (c,) = of(out, DeductionClaimed)
    d = c.deduction
    assert d.kind == "promotion" and d.status == "claimed" and d.payer == "KRANZ"
    assert d.period_from == ws and d.period_to == ws + timedelta(days=6) and d.promo_id == p.promo_id
    assert d.promo_text == ded.promo_text("drogerie_kranz", ws)
    assert [i.sku_id for i in d.items] == sorted(sold)
    for it in d.items:
        rate = ded.scan_rate(world, p, it.sku_id)
        assert it.units == sold[it.sku_id] and it.rate_net == rate
        assert it.amount == round(it.units * rate * 1.19, 2)
        assert it.customer_article == world.ref.catalogue.customer_article("drogerie_kranz", it.sku_id)
    assert d.amount == round(sum(i.amount for i in d.items), 2) == d.validated_amount
    assert not world.store.has("promo_claim", f"{p.promo_id}:{ws:%Y%m%d}:{brand}")  # removed at claim
    assert world.store.get("deduction_book", "KRANZ").open_ids == [d.deduction_id]
    assert world.is_scheduled(f"ded.match:{d.deduction_id}")
    # matching -> accepted at the claimed amount; nothing taken yet, so the case stays open
    run(world, local(2025, 8, 15), sources=())
    statuses = [(e.status, e.fields) for e in of(out, DeductionChanged)]
    assert [s for s, _ in statuses] == ["matched", "accepted"]
    assert statuses[1][1]["accepted_amount"] == d.amount
    assert world.store.get("deduction", d.deduction_id).status == "accepted"
    # the next payment takes it and closes it
    taken = ded.take_on_payment(world, "drogerie_kranz", 4711, 10_000.0)
    assert taken == d.amount
    (clr,) = of(out, DeductionCleared)
    assert (clr.kind, clr.billing_id, clr.amount, clr.seq, clr.taken_total) == ("taken", 4711, d.amount, 1, d.amount)
    assert of(out, DeductionChanged)[-1].fields == {"closed_on": world.local_date()}
    assert not world.store.has("deduction", d.deduction_id)
    assert world.store.get("deduction_book", "KRANZ").open_ids == []


def test_claim_without_basis_does_nothing():
    world, out = make_world(make_cfg(enable=()), local(2025, 1, 6))
    p = kranz_promos(world, date(2025, 3, 1), date(2025, 6, 30))[0]
    world.now = local(2025, 7, 1, 11)
    ded.on_claim(world, _claim_event(p, p.start.astimezone(BERLIN).date(), "Velmora"))
    assert out == [] and not list(world.store.items("deduction"))


def test_overclaim_disputed_then_resolved_and_repaid():
    cfg = make_cfg(enable=(), deductions={"overclaim_rate": 1.0, "invalid_claim_rate": 0.0})
    world, out = make_world(cfg, local(2025, 1, 6))
    p, ws, brand, sold = _promo_week_basis(world)
    world.now = local(2025, 7, 1, 11)
    ded.on_claim(world, _claim_event(p, ws, brand))
    d = of(out, DeductionClaimed)[0].deduction
    assert d.meta == {"overclaim": True}
    for it in d.items:
        u = sold[it.sku_id]
        assert math.ceil(u * 1.05) <= it.units <= math.ceil(u * 1.30)
    assert d.amount > d.validated_amount * 1.02
    # short-paid in full before the brand looked at it
    world.now = local(2025, 7, 2, 12)
    assert ded.take_on_payment(world, "drogerie_kranz", 1, 50_000.0) == d.amount
    run(world, local(2025, 12, 1), sources=())
    ch = of(out, DeductionChanged)
    assert ch[0].status == "disputed" and ch[0].fields["dispute_reason"] == "D01"
    assert ch[1].status == "accepted" and ch[1].fields["accepted_amount"] == d.validated_amount
    repaid = [e for e in of(out, DeductionCleared) if e.kind == "repaid"]
    assert len(repaid) == 1
    r = repaid[0]
    assert r.billing_id is None and r.amount == round(d.amount - d.validated_amount, 2) and r.seq == 2
    assert r.taken_total == d.amount and r.repaid_total == r.amount
    assert ch[-1].fields == {"closed_on": r.on}
    assert not list(world.store.items("deduction"))


def test_overclaim_always_settles_at_the_validated_amount():
    """An over-agreement dispute (D01) is never fully reversed: the agreed scan-back on the feed units is owed."""
    cfg = make_cfg(enable=(), deductions={"overclaim_rate": 1.0, "invalid_claim_rate": 0.0})
    world, out = make_world(cfg, local(2025, 1, 6))
    p, ws, brand, _ = _promo_week_basis(world)
    world.now = local(2025, 7, 1, 11)
    ded.on_claim(world, _claim_event(p, ws, brand))
    d = of(out, DeductionClaimed)[0].deduction
    run(world, local(2025, 12, 1), sources=())
    ch = of(out, DeductionChanged)
    assert [c.status for c in ch[:2]] == ["disputed", "accepted"]
    assert ch[1].fields["accepted_amount"] == d.validated_amount > 0
    assert "reversed" not in {c.status for c in ch}


def test_unexplained_duplicate_claim():
    cfg = make_cfg(enable=(), deductions={"overclaim_rate": 0.0, "invalid_claim_rate": 1.0})
    world, out = make_world(cfg, local(2025, 1, 6))
    p, ws, brand, sold = _promo_week_basis(world)
    world.now = local(2025, 7, 1, 11)
    ded.on_claim(world, _claim_event(p, ws, brand))
    key = f"ded.invalid:{p.promo_id}:{ws:%Y%m%d}:{brand}"
    assert world.is_scheduled(key)
    run(world, local(2025, 8, 15), sources=())
    claims = [e.deduction for e in of(out, DeductionClaimed)]
    zx = [d for d in claims if d.kind == "unexplained"]
    assert len(zx) == 1
    x = zx[0]
    assert x.validated_amount == 0.0 and x.promo_id == p.promo_id and x.period_from == ws
    assert 7 <= (x.claimed_on - claims[0].claimed_on).days <= 32
    for it in x.items:
        assert math.ceil(sold[it.sku_id] * 0.3) <= it.units <= sold[it.sku_id]
    assert x.amount == round(sum(i.amount for i in x.items), 2)
    disputes = [e for e in of(out, DeductionChanged) if e.deduction_id == x.deduction_id and e.status == "disputed"]
    assert disputes and disputes[0].fields["dispute_reason"] == "D04"


def test_claim_and_wkz_schedule_is_pure_and_bounded():
    world, _ = make_world(make_cfg(enable=()), local(2025, 1, 6))
    events = []
    t0 = local(2025, 1, 6)
    for name in ("ded.promo_claims",):
        events += list(ARRIVAL_SOURCES[name](world, t0, local(2025, 12, 31)))
    claims = [e for e in events if e.kind == "ded.claim"]
    wkz = [e for e in events if e.kind == "ded.wkz"]
    assert claims and wkz
    cal = world.ref.calendar
    for e in claims:
        ws = date.fromisoformat(e.data["week_start"])
        lt = e.due.astimezone(BERLIN)
        assert e.key == f"ded.claim:{e.data['promo_id']}:{ws:%Y%m%d}:{e.data['brand']}"
        assert 14 <= (lt.date() - (ws + timedelta(days=6))).days <= 56 + 6
        assert 10 <= lt.hour < 16
        assert cal.is_working_day(lt.date(), "DE", world.ref.accounts.retailer(e.data["channel"]).address.region)
    for e in wkz:
        p = promotions.find(world, e.data["promo_id"])
        assert e.key == f"ded.wkz:{p.promo_id}" and p.channel in ("drogerie_kranz", "mylo")
        assert 14 <= (e.due.astimezone(BERLIN).date() - p.end.astimezone(BERLIN).date()).days <= 62
    # one claim per promotion week and brand
    assert len({e.key for e in claims}) == len(claims)
    # a fresh world computes the same schedule
    world2, _ = make_world(make_cfg(enable=()), local(2025, 1, 6))
    again = list(ARRIVAL_SOURCES["ded.promo_claims"](world2, t0, local(2025, 12, 31)))
    assert [(e.due, e.key, e.data) for e in again] == [(e.due, e.key, e.data) for e in events]


def test_no_claims_for_promotions_ended_before_history_start():
    world, _ = make_world(make_cfg(enable=()), local(2025, 6, 2))
    events = list(ARRIVAL_SOURCES["ded.promo_claims"](world, world.now, local(2025, 9, 30)))
    for e in events:
        assert promotions.find(world, e.data["promo_id"]).end >= world.history_start


def test_partition_invariance():
    def collect(step_hours):
        world, out = make_world(make_cfg(scenarios=[OVERSTOCK]), local(2025, 12, 1))
        seen = []
        t, until = world.now, local(2026, 7, 1)
        step = timedelta(hours=step_hours)
        while t < until:
            end = min(t + step, until)
            for name in SOURCES:
                for ev in ARRIVAL_SOURCES[name](world, t, end):
                    assert t < ev.due <= end
                    seen.append((ev.due, ev.kind, ev.key, ev.data, ev.priority))
            t = end
        return sorted(seen, key=lambda x: (x[0], x[2]))

    a, b, c = collect(1), collect(24), collect(7.5)
    assert a == b == c
    kinds = {x[1] for x in a}
    assert kinds == {"ded.agreement", "ded.claim", "ded.wkz", "ded.overstock"}


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


def _open(world, amount, status="accepted", accepted=None, channel="drogerie_kranz", kind="shortage"):
    d = ded._new(world, kind, channel, amount, amount)
    d.status = status
    if status == "accepted":
        d.accepted_amount = accepted if accepted is not None else amount
    world.store.put("deduction", d.deduction_id, d)
    ded._book_add(world, d.payer, d.deduction_id)
    return d.deduction_id


def test_partial_taking_splits_large_deduction_and_keeps_taking_behind_it():
    world, out = make_world(make_cfg(enable=()), local(2025, 3, 3, 12))
    big = _open(world, 1000.0)
    small = _open(world, 50.0)
    pending = _open(world, 80.0, status="claimed")  # claimed claims are taken at their amount too
    assert ded.take_on_payment(world, "drogerie_kranz", 1, 400.0) == 400.0
    assert ded.take_on_payment(world, "drogerie_kranz", 2, 400.0) == 400.0
    assert world.store.get("deduction", big).taken == 800.0
    assert ded.take_on_payment(world, "drogerie_kranz", 3, 400.0) == 330.0  # 200 + 50 + 80
    clr = of(out, DeductionCleared)
    assert [(c.deduction_id, c.billing_id, c.amount, c.seq) for c in clr] == [
        (big, 1, 400.0, 1), (big, 2, 400.0, 2), (big, 3, 200.0, 3), (small, 3, 50.0, 1), (pending, 3, 80.0, 1)]
    closed = [e.deduction_id for e in of(out, DeductionChanged) if "closed_on" in e.fields]
    assert closed == [big, small]  # the claimed one is fully taken but not resolved yet
    assert world.store.get("deduction_book", "KRANZ").open_ids == [pending]
    # nothing open: an invoice is paid in full
    assert ded.take_on_payment(world, "drogerie_kranz", 4, 99.0) == 0.0
    # other payers are not touched
    assert ded.take_on_payment(world, "mylo", 5, 500.0) == 0.0


def test_take_never_exceeds_invoice_and_skips_future_claims():
    world, out = make_world(make_cfg(enable=()), local(2025, 3, 3, 12))
    a = _open(world, 0.004 + 10.0)
    future = _open(world, 5.0)
    world.store.mut("deduction", future).claimed_at = world.now + timedelta(hours=1)
    assert ded.take_on_payment(world, "drogerie_kranz", 1, 6.0) == 6.0
    assert ded.take_on_payment(world, "drogerie_kranz", 2, 100.0) == 4.0
    assert world.store.get("deduction", future).taken == 0.0
    assert not world.store.has("deduction", a)
    assert ded.take_on_payment(world, "drogerie_kranz", 3, 0.0) == 0.0


def test_contractual_coop_and_invoice_advertising():
    world, out = make_world(make_cfg(), local(2025, 3, 3, 12))
    taken = ded.take_on_payment(world, "shopnova", 77, 12_345.67)
    assert taken == round(0.04 * 12_345.67, 2)
    (c,) = of(out, DeductionClaimed)
    d = c.deduction
    assert (d.kind, d.status, d.billing_id, d.accepted_amount, d.amount) == ("coop", "accepted", 77, taken, taken)
    (clr,) = of(out, DeductionCleared)
    assert (clr.kind, clr.billing_id, clr.amount) == ("taken", 77, taken)
    assert of(out, DeductionChanged)[0].fields == {"closed_on": date(2025, 3, 3)}
    assert not list(world.store.items("deduction")) and not list(world.store.items("deduction_book"))
    out.clear()
    assert ded.take_on_payment(world, "belle", 78, 1000.0) == 20.0
    (c,) = of(out, DeductionClaimed)
    assert c.deduction.kind == "advertising" and c.deduction.payer == "BELLE"
    # Kranz has neither
    assert ded.take_on_payment(world, "drogerie_kranz", 79, 1000.0) == 0.0


def test_contractual_first_then_open_deductions():
    world, out = make_world(make_cfg(), local(2025, 3, 3, 12))
    _open(world, 500.0, channel="shopnova")
    assert ded.take_on_payment(world, "shopnova", 1, 300.0) == 300.0  # 12 co-op + 288
    clr = of(out, DeductionCleared)
    assert [c.amount for c in clr] == [12.0, 288.0]


def test_resolution_below_taken_repays_and_reversed_untaken_closes():
    world, out = make_world(make_cfg(enable=()), local(2025, 3, 3, 12))
    d = ded._new(world, "shortage", "drogerie_kranz", 300.0, 0.0, meta={"dispute_reason": "D03"})
    ded._claim(world, d)
    ded.take_on_payment(world, "drogerie_kranz", 9, 1000.0)
    world.now = local(2025, 3, 10, 12)
    ded.on_match(world, types.SimpleNamespace(data={"deduction_id": d.deduction_id}))
    assert world.store.get("deduction", d.deduction_id).dispute_reason == "D03"
    ded.on_resolve(world, types.SimpleNamespace(data={"deduction_id": d.deduction_id, "u": 0.9}))  # reversed
    assert world.is_scheduled(f"ded.repay:{d.deduction_id}")
    run(world, local(2025, 5, 1), sources=())
    rep = [c for c in of(out, DeductionCleared) if c.kind == "repaid"]
    assert len(rep) == 1 and rep[0].amount == 300.0 and rep[0].repaid_total == 300.0
    assert not list(world.store.items("deduction"))

    # goodwill: an unverifiable claim accepted in full
    d2 = ded._new(world, "shortage", "drogerie_kranz", 40.0, 0.0)
    ded._claim(world, d2)
    world.store.mut("deduction", d2.deduction_id).status = "disputed"
    ded.on_resolve(world, types.SimpleNamespace(data={"deduction_id": d2.deduction_id, "u": 0.01}))
    x = world.store.get("deduction", d2.deduction_id)
    assert x.status == "accepted" and x.accepted_amount == 40.0  # open until taken
    assert ded.take_on_payment(world, "drogerie_kranz", 10, 100.0) == 40.0
    assert not world.store.has("deduction", d2.deduction_id)


def test_entity_survives_round_trip():
    world, _ = make_world(make_cfg(enable=()), local(2025, 1, 6))
    p, ws, brand, _ = _promo_week_basis(world)
    world.now = local(2025, 7, 1, 11)
    ded.on_claim(world, _claim_event(p, ws, brand))
    (did, d), = list(world.store.items("deduction"))
    assert from_jsonable(Deduction, to_jsonable(d)) == d
    b = world.store.get("deduction_book", "KRANZ")
    assert from_jsonable(type(b), to_jsonable(b)) == b


# ---------------------------------------------------------------------------
# Shortage and service penalties
# ---------------------------------------------------------------------------


def test_shortage_claim_after_pod():
    cfg = make_cfg(enable=(), deductions={"shortage_claim_rate": 1.0, "shortage_valid_share": 1.0})
    world, out = make_world(cfg, local(2025, 3, 3, 12))
    cat = world.ref.catalogue
    skus = cat.retailer_assortment("mylo", date(2025, 3, 3))[:3]
    ded.record_pod(world, 11, "mylo", "MYLO-DC1", 501, [(s, 48) for s in skus])
    assert world.is_scheduled("ded.shortage:501")
    run(world, local(2025, 5, 1), sources=())
    d = of(out, DeductionClaimed)[0].deduction
    assert d.kind == "shortage" and d.delivery_id == 501 and d.payer == "MYLO"
    assert 3 <= (d.claimed_on - date(2025, 3, 3)).days <= 18 and 1 <= len(d.items) <= 2
    td = cfg.channels.mylo.trade_discount
    for it in d.items:
        sku = cat.get(it.sku_id)
        assert it.units == min(sku.units_per_case, 48)
        assert it.rate_net == round(sku.list_price_eur * (1 - td), 4)
        assert it.amount == round(it.units * it.rate_net * 1.19, 2)
    assert d.validated_amount == d.amount
    assert [e.status for e in of(out, DeductionChanged)][:2] == ["matched", "accepted"]


def test_invalid_shortage_is_disputed_pod_complete():
    cfg = make_cfg(enable=(), deductions={"shortage_claim_rate": 1.0, "shortage_valid_share": 0.0})
    world, out = make_world(cfg, local(2025, 3, 3, 12))
    ded.record_pod(world, 11, "mylo", "MYLO-DC1", 502, [(1, 24)])
    run(world, local(2025, 6, 1), sources=())
    d = of(out, DeductionClaimed)[0].deduction
    assert d.validated_amount == 0.0
    disp = of(out, DeductionChanged)[0]
    assert disp.status == "disputed" and disp.fields["dispute_reason"] == "D03"


def test_shortage_rate_is_statistically_plausible():
    world, _ = make_world(make_cfg(enable=()), local(2025, 3, 3, 12))
    n = 6000
    for i in range(n):
        ded.record_pod(world, i, "drogerie_kranz", "KRANZ-DC1", 10_000 + i, [(1, 24), (2, 24)])
    hits = sum(1 for i in range(n) if world.is_scheduled(f"ded.shortage:{10_000 + i}"))
    p = 0.03
    assert abs(hits - n * p) < 3 * math.sqrt(n * p * (1 - p))


def test_service_penalty_after_large_cut():
    world, out = make_world(make_cfg(), local(2025, 3, 3, 12))
    world.store.put("retail_po", 21, RetailPO(po_id=21, retailer="drogerie_kranz", dc_account="KRANZ-DC1",
                                                po_number="4500000021", created_at=world.now,
                                                requested_delivery=date(2025, 3, 6), lines=[], sales_doc_id=321))
    ded.record_cut(world, 21, "drogerie_kranz", "KRANZ-DC1", 1000, 50, 400.0)  # 5 %: not above threshold
    assert not world.is_scheduled("ded.penalty:21")
    ded.record_cut(world, 21, "drogerie_kranz", "KRANZ-DC1", 1000, 51, 400.0)
    assert world.is_scheduled("ded.penalty:21")
    ded.record_cut(world, 22, "belle", "BELLE-DC1", 100, 90, 900.0)  # Belle raises no penalties
    assert not world.is_scheduled("ded.penalty:22")
    run(world, local(2025, 5, 1), sources=())
    d = of(out, DeductionClaimed)[0].deduction
    assert (d.kind, d.amount, d.validated_amount, d.sales_doc_id) == ("service_penalty", 20.0, 20.0, 321)
    assert 14 <= (d.claimed_on - date(2025, 3, 3)).days <= 38
    rows = erp_rows(world, out)
    z = rows["erp.zdeduct"][(keys.belnr_deduction(d.deduction_id),)]
    assert z["zztype"] == "ZL01" and z["vbeln_va"] == keys.vbeln_sales(321) and z["zzstatus"] == "A"


# ---------------------------------------------------------------------------
# Clearance claims (retailer_overstock)
# ---------------------------------------------------------------------------


@pytest.fixture
def flat_demand(monkeypatch):
    mod = types.ModuleType("cpgsim.domain.demandplan")
    mod.retail_weekly_demand = lambda world, ch, dc, sku, ws: 10.0
    monkeypatch.setitem(sys.modules, "cpgsim.domain.demandplan", mod)
    return mod


def test_clearance_claims_after_overstock_window(flat_demand):
    cfg = make_cfg(enable=(), scenarios=[OVERSTOCK], deductions={"overclaim_rate": 0.0})
    world, out = make_world(cfg, local(2025, 12, 1))
    (w,) = world.scenarios.windows("retailer_overstock")
    assert w.end == local(2026, 4, 6)
    evs = list(ARRIVAL_SOURCES["ded.overstock"](world, world.now, local(2026, 12, 31)))
    assert [(e.key, e.due) for e in evs] == [
        (f"ded.overstock:{w.window_id}:1", local(2026, 4, 20, 10)),
        (f"ded.overstock:{w.window_id}:2", local(2026, 5, 11, 10)),
    ]
    cat = world.ref.catalogue
    total = 0.0
    ws = date(2026, 1, 5)
    while ws < date(2026, 4, 6):
        total += sum(10.0 * cat.get(s).list_price_eur for s in cat.retailer_assortment("mylo", ws + timedelta(days=3)))
        ws += timedelta(days=7)
    total *= 2  # two Mylo DCs
    expected = round(0.20 * 0.30 * total * (1 - cfg.channels.mylo.trade_discount) * 1.19 / 2, 2)
    assert ded.clearance_amount(world, w) == expected

    world.now = local(2026, 4, 1)
    run(world, local(2026, 9, 1), sources=("ded.overstock",))
    zp = [e.deduction for e in of(out, DeductionClaimed) if e.deduction.kind == "clearance"]
    assert len(zp) == 2 and all(d.amount == expected and d.window_id == w.window_id for d in zp)
    assert all(d.validated_amount == round(expected * 0.8, 2) and d.payer == "MYLO" for d in zp)
    eff = of(out, PlantedEffect)
    assert len(eff) == 2 and {e.entity_id for e in eff} == {str(d.deduction_id) for d in zp}
    assert all(e.effect == "extra_deduction" and e.magnitude == expected and e.entity_type == "deduction"
               for e in eff)
    # disputed (over agreement), then accepted at 80 %: open until taken
    acc = [e for e in of(out, DeductionChanged) if e.status == "accepted"]
    assert len(acc) == 2 and all(e.fields["accepted_amount"] == round(expected * 0.8, 2) for e in acc)
    assert ded.take_on_payment(world, "mylo", 1, 1e7) == round(2 * round(expected * 0.8, 2), 2)
    assert not list(world.store.items("deduction"))


def test_no_clearance_for_open_or_disabled_windows(flat_demand):
    cfg = make_cfg(enable=(), scenarios=[{**OVERSTOCK, "retailer": "belle"}])  # belle disabled
    world, _ = make_world(cfg, local(2025, 12, 1))
    assert list(ARRIVAL_SOURCES["ded.overstock"](world, world.now, local(2026, 12, 31))) == []


# ---------------------------------------------------------------------------
# Lifecycle end to end, reconciliation R1-R3, entity removal
# ---------------------------------------------------------------------------


def _driver(world, start: date, end: date, invoice_gross: float, pay_until: date | None = None):
    """Retail stand-in: weekly sell-out per DC (Monday 00:30) and two payments a week per retailer."""
    acts = []
    invoices: dict[int, tuple[str, float, float]] = {}
    seq = [0]
    cat = world.ref.catalogue
    retailers = world.cfg.channels.retailers()
    d = start
    while d < end:
        if d.weekday() == 0:
            ws = d - timedelta(days=7)

            def sell(w, ws=ws):
                for ch in sorted(retailers):
                    for acc in w.ref.accounts.dcs(ch):
                        sold = {s: 3 + s % 11 for s in cat.retailer_assortment(ch, ws + timedelta(days=3))}
                        ded.record_sellout(w, ch, acc.account_id, ws, 7, sold)

            acts.append((world.at_local(d, 0, 30), sell))
        if d.weekday() in (1, 3) and (pay_until is None or d < pay_until):
            def pay(w, d=d):
                for ch in sorted(retailers):
                    seq[0] += 1
                    gross = round(invoice_gross * (0.4 + (seq[0] * 37 % 100) / 100.0), 2)
                    taken = ded.take_on_payment(w, ch, seq[0], gross)
                    assert 0.0 <= taken <= gross
                    invoices[seq[0]] = (ch, gross, taken)

            acts.append((world.at_local(d, 12), pay))
        d += timedelta(days=1)
    return acts, invoices


def _check_reconciliation(rows, invoices):
    zd = rows.get("erp.zdeduct", {})
    items = rows.get("erp.zdeduct_item", {})
    clr = rows.get("erp.zdeduct_clr", {})
    kona = rows.get("erp.kona", {})
    taken_by_inv: dict[str, float] = {}
    t_by, p_by = {}, {}
    for (belnr, _), r in clr.items():
        assert (belnr,) in zd  # every clearing item belongs to a claim
        if r["zzkind"] == "T":
            taken_by_inv[r["vbeln_vf"]] = taken_by_inv.get(r["vbeln_vf"], 0.0) + r["dmbtr"]
            t_by[belnr] = t_by.get(belnr, 0.0) + r["dmbtr"]
        else:
            assert r["vbeln_vf"] is None
            p_by[belnr] = p_by.get(belnr, 0.0) + r["dmbtr"]
    # R1: paid + taken = gross per invoice
    for bid, (_, gross, taken) in invoices.items():
        paid = round(gross - taken, 2)
        assert round(paid + taken_by_inv.get(keys.vbeln_billing(bid), 0.0), 2) == gross
        assert round(taken_by_inv.get(keys.vbeln_billing(bid), 0.0), 2) == taken
    closed = 0
    for (belnr,), r in zd.items():
        t, p = round(t_by.get(belnr, 0.0), 2), round(p_by.get(belnr, 0.0), 2)
        assert r["zztaken"] == t and r["zzrepaid"] == p
        if r["zzclose_date"] is not None:  # R2
            closed += 1
            assert r["zzstatus"] in ("A", "R")
            assert round(t - p, 2) == (r["zzaccepted"] or 0.0)
            if r["zzstatus"] == "R":
                assert r["zzaccepted"] == 0.0
        if r["zztype"] == "ZP01":  # R3
            s = round(sum(i["wrbtr"] for (b, _), i in items.items() if b == belnr), 2)
            assert r["wrbtr"] == s and (r["knuma"],) in kona
        if r["knuma"] is not None:
            assert (r["knuma"],) in kona
    return closed


def test_lifecycle_reconciles_and_entities_close():
    cfg = make_cfg(deductions={"overclaim_rate": 0.3, "invalid_claim_rate": 0.2})
    hs = local(2025, 3, 3)
    world, out = make_world(cfg, hs)
    bootstrap(world)
    actions, invoices = _driver(world, date(2025, 3, 3), date(2026, 3, 2), 9000.0)
    run(world, local(2026, 3, 2), actions=actions)
    claims = [e.deduction for e in of(out, DeductionClaimed)]
    kinds = {d.kind for d in claims}
    assert {"promotion", "advertising", "coop", "unexplained"} <= kinds
    assert all(d.payer in ("KRANZ", "MYLO", "SHOPNOVA", "BELLE") for d in claims)
    # deduction ids increase with time and are unique
    ids = [d.deduction_id for d in claims]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    rows = erp_rows(world, out)
    closed = _check_reconciliation(rows, invoices)
    assert closed > 0.8 * len(claims) - 30
    # open entities are recent: claim + match + dispute + repay + payment slack
    bound = timedelta(days=14 + 7 + 56 + 20 + 30)
    for _, d in list(world.store.items("deduction")):
        assert d.claimed_at > world.now - bound, d
    for _, b in list(world.store.items("promo_claim")):
        assert b.week_start > world.local_date() - timedelta(days=8 * 7 + 21)
    # statistics: overclaim share of promotion claims near the configured 30 %
    promo = [d for d in claims if d.kind == "promotion"]
    share = sum(1 for d in promo if d.meta.get("overclaim")) / len(promo)
    assert abs(share - 0.3) < 3 * math.sqrt(0.3 * 0.7 / len(promo))


def test_books_empty_after_claims_stop():
    """With payments continuing after the last claim, every deduction closes and the books empty."""
    cfg = make_cfg(enable=(), deductions={"overclaim_rate": 0.5, "invalid_claim_rate": 0.3})
    hs = local(2025, 3, 3)
    world, out = make_world(cfg, hs)
    bootstrap(world)
    stop = local(2025, 9, 1)
    actions, invoices = _driver(world, date(2025, 3, 3), date(2026, 3, 2), 9000.0)
    run(world, stop, actions=[a for a in actions if a[0] <= stop])
    # no new claim sources after stop, but matching, resolution, repayment and payments go on
    run(world, local(2026, 3, 2), sources=(), actions=[a for a in actions if a[0] > stop])
    assert not list(world.store.items("deduction"))
    books = list(world.store.items("deduction_book"))
    assert books and all(b.open_ids == [] for _, b in books)
    rows = erp_rows(world, out)
    zd = rows["erp.zdeduct"]
    assert all(r["zzclose_date"] is not None for r in zd.values())
    _check_reconciliation(rows, invoices)


def test_deductions_are_deterministic():
    def once():
        cfg = make_cfg(deductions={"overclaim_rate": 0.3, "invalid_claim_rate": 0.2,
                                   "shortage_claim_rate": 0.2})
        world, out = make_world(cfg, local(2025, 3, 3))
        bootstrap(world)
        actions, _ = _driver(world, date(2025, 3, 3), date(2025, 9, 1), 5000.0)
        pods = []
        for i in range(40):
            t = local(2025, 3, 10) + timedelta(days=3 * i, hours=11)
            pods.append((t, lambda w, i=i: ded.record_pod(w, i, "mylo", "MYLO-DC1", 900 + i, [(1, 24), (5, 12)])))
        run(world, local(2025, 9, 1), actions=actions + pods)
        return [(type(e).__name__, repr(e)) for e in out]

    assert once() == once()


def test_billing_clear_status_override():
    from cpgsim.domain import billing
    from cpgsim.domain.events import BillingDocChanged
    from cpgsim.domain.model import BillingDoc, BillingItem

    world, out = make_world(make_cfg(enable=()), local(2025, 3, 3, 12))
    doc = BillingDoc(billing_id=5, doc_kind="invoice", sales_doc_id=1, payer="KRANZ", currency="EUR",
                     fx_rate_eur=1.0, billing_date=date(2025, 3, 1), created_at=world.now,
                     due_date=date(2025, 4, 15),
                     items=[BillingItem(10, 1, 10, net_value=100.0, tax=19.0, cost=40.0, sales_item_no=10)])
    world.store.put("billing", 5, doc)
    billing.clear(world, 5, 100.0)  # derived: short payment without an override
    billing.clear(world, 5, 100.0, status="cleared")  # retail: residual moved to deduction cases
    a, b = of(out, BillingDocChanged)
    assert a.fields["status"] == "partially_cleared" and b.fields["status"] == "cleared"
    assert b.fields["paid_amount"] == 100.0
    billing.clear(world, 6, 50.0, status="cleared")  # purged billing entity is tolerated
    assert of(out, BillingDocChanged)[-1].fields["status"] == "cleared"
