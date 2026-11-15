"""Carrier, return, master-data, DST chaos and the truth windows of the defects."""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from cpgsim import chaos
from cpgsim.chaos import dst as cdst
from cpgsim.chaos import logistics as clog
from cpgsim.chaos import windows as cwin
from cpgsim.domain import fulfilment, masterdata
from cpgsim.domain.events import CarrierScan, CustomerMaterialListed, ReturnCreated, ScenarioWindowRecorded
from cpgsim.domain.model import Address, Delivery, DeliveryLine, ReturnLine, WebReturn
from cpgsim.writers import wms as wms_writer

from .conftest import START, UTC, chaos_cfg

ADDR = Address("DE", "DE-BY", "80331", "München", "Lindenweg", "4")
CHAOS_OFF = dict(
    duplicate_customer_rate=0.0, malformed_postcode_rate=0.0, status_error_rate=0.0,
    duplicate_carrier_event_rate=0.0, missing_order_ref_rate=0.0, unmapped_article_rate=0.0,
    sellout_restatement_rate=0.0, free_text_return_reason_rate=0.0, gdpr_erasure_rate_monthly=0.0,
    dst_truth=False,
)


def _binomial_ok(k: int, n: int, p: float) -> bool:
    return abs(k - n * p) <= 3 * math.sqrt(n * p * (1 - p)) + 1


def _delivery(did: int) -> Delivery:
    return Delivery(delivery_id=did, kind="web", sales_doc_id=1, ship_to_account="WEB-DE", ship_to=ADDR,
                    carrier="Swiftbox", lines=[DeliveryLine(1, 1, 1, 10)], created_at=START,
                    planned_ship_date=START.date(), tracking_no=f"SWB{did:010d}")


# -- duplicate carrier events -----------------------------------------------------------------------


def test_duplicate_carrier_event_rate(harness):
    h = harness()
    n_dlv, scans = 4000, 0
    for did in range(1, n_dlv + 1):
        d = _delivery(did)
        for code in ("label_created", "picked_up", "hub_scan", "out_for_delivery", "delivered"):
            fulfilment._scan(h.world, d, code, "Kassel")
            scans += 1
    dups = h.effects("duplicate_carrier_event")
    assert _binomial_ok(len(dups), scans, 0.01)
    events = h.of(CarrierScan)
    assert len(events) == scans + len(dups)
    ids = [wms_writer.carrier_event_id(e.delivery_id, e.scan_seq) for e in events]
    assert len(ids) == len(set(ids))  # referential integrity: every event has its own id
    assert len(h.rows("wms.carrier_events")) == len(events)


def test_duplicate_carrier_event_content_and_truth(harness):
    h = harness(chaos_cfg(duplicate_carrier_event_rate=1.0))
    d = _delivery(5)
    h.world.now = START + timedelta(hours=3)
    fulfilment._scan(h.world, d, "hub_scan", "Hub Kassel", "sorted")
    a, b = h.of(CarrierScan)
    assert (a.scan_seq, b.scan_seq, d.scan_seq) == (1, 2, 2)  # one duplicate, no chain
    for f in ("at", "code", "location", "detail", "tracking_no", "carrier", "delivery_id"):
        assert getattr(a, f) == getattr(b, f), f
    (eff,) = h.effects("duplicate_carrier_event")
    assert (eff.entity_type, eff.entity_id, eff.detail) == (
        "carrier_scan", "5:2", {"original_scan_seq": 1, "code": "hub_scan"})
    # same (shipment, code, local time, location) twice in wms
    rows = list(h.rows("wms.carrier_events").values())
    assert len({(r["shipment_id"], r["event_code"], r["event_local"], r["location"]) for r in rows}) == 1
    assert len({r["event_id"] for r in rows}) == 2
    assert clog.SCAN_SEQ_LIMIT == wms_writer.CARRIER_EVENT_SEQ_RANGE


def test_duplicate_carrier_event_respects_event_id_range(harness):
    h = harness(chaos_cfg(duplicate_carrier_event_rate=1.0))
    d = _delivery(6)
    d.scan_seq = clog.SCAN_SEQ_LIMIT - 1
    chaos.after_scan(h.world, d, "hub_scan", "Kassel", None)
    assert d.scan_seq == clog.SCAN_SEQ_LIMIT - 1 and h.events == []


# -- free-text return reasons ---------------------------------------------------------------------


def _ret(rid: int, reason: str = "did_not_like") -> WebReturn:
    return WebReturn(return_id=rid, order_id=1, delivery_id=1, customer_id=1, reason=reason,
                     lines=[ReturnLine(1, 1, 1, 1)], requested_at=START)


def test_free_text_return_reason_rate_and_contradictions(harness):
    h = harness()
    n, texts = 20_000, 0
    for rid in range(1, n + 1):
        if chaos.return_free_text(h.world, _ret(rid), "DE") is not None:
            texts += 1
    assert _binomial_ok(texts, n, 0.15)
    hc = harness(chaos_cfg(free_text_return_reason_rate=1.0))
    reasons = sorted(clog.RETURN_TEXTS)
    contra = 0
    for rid in range(1, 5001):
        reason = reasons[rid % len(reasons)]
        t = chaos.return_free_text(hc.world, _ret(rid, reason), "CH")
        assert "ß" not in t
        (eff,) = [e for e in hc.effects("free_text_return_reason") if e.entity_id == str(rid)]
        assert eff.detail["coded_reason"] == reason and eff.detail["text"] == t
        base = {p.lower().replace("ß", "ss") for p in clog.RETURN_TEXTS[eff.detail["text_reason"]]}
        assert t.lower().rstrip("!.") in base
        contra += eff.detail["contradicts"]
        assert eff.detail["contradicts"] == (eff.detail["text_reason"] != reason)
    assert _binomial_ok(contra, 5000, 0.10)


def test_free_text_return_reason_reaches_wms(harness):
    h = harness(chaos_cfg(free_text_return_reason_rate=1.0))
    sku = h.world.ref.catalogue.skus[0].sku_id
    r = WebReturn(return_id=3, order_id=1, delivery_id=1, customer_id=1, reason="wrong_item",
                  lines=[ReturnLine(1, 1, sku, 1)], requested_at=START)
    r.free_text_reason = chaos.return_free_text(h.world, r, "DE")
    h.world.emit(ReturnCreated(at=START, ret=r))
    (row,) = h.rows("wms.returns").values()
    assert row["reason_text"] == r.free_text_reason and row["reason_code"] == "WI"


# -- unmapped articles ----------------------------------------------------------------------------


def test_unmapped_article_is_pure_and_records_once_per_article(harness):
    h = harness()
    n = 40_000
    unmapped = sum(chaos.is_unmapped(h.world, "mylo", s) for s in range(1, n + 1))
    assert _binomial_ok(unmapped, n, 0.05) and h.events == []  # is_unmapped records nothing
    sku = next(s for s in range(1, 1000) if chaos.is_unmapped(h.world, "drogerie_kranz", s)
               and h.world.ref.catalogue.get(s) is not None)
    assert chaos.knmt_suppressed(h.world, "drogerie_kranz", sku)
    (eff,) = h.effects("unmapped_article")
    acct = h.world.ref.accounts.retailer("drogerie_kranz").account_id
    assert (eff.entity_type, eff.entity_id) == ("customer_material", f"{acct}:{sku}")
    assert eff.detail == {"channel": "drogerie_kranz",
                          "article_no": h.world.ref.catalogue.customer_article("drogerie_kranz", sku)}
    assert chaos.is_unmapped(h.world, "drogerie_kranz", str(sku))  # ids hash by value
    assert not chaos.knmt_suppressed(h.world, "drogerie_kranz", next(
        s for s in range(1, 1000) if not chaos.is_unmapped(h.world, "drogerie_kranz", s)))


def test_knmt_coverage_per_retailer(harness):
    h = harness()
    cat = h.world.ref.catalogue
    listed: Counter = Counter()
    for sku in cat.skus:
        for ch in h.world.cfg.channels.retailers():
            if masterdata.retailer_lists(h.world, ch, sku):
                listed[ch] += 1
        masterdata._emit_customer_materials(h.world, sku)
    mapped = Counter()
    accounts = {h.world.ref.accounts.retailer(ch).account_id: ch for ch in listed}
    for e in h.of(CustomerMaterialListed):
        mapped[accounts[e.account_id]] += 1
    effs = h.effects("unmapped_article")
    assert len(effs) == sum(listed.values()) - sum(mapped.values())
    assert len({e.effect_id for e in effs}) == len(effs)
    total = sum(listed.values())
    assert _binomial_ok(total - sum(mapped.values()), total, 0.05)


# -- DST ----------------------------------------------------------------------------------------


def test_dst_transitions_and_truth(harness):
    h = harness()
    end = datetime(2026, 1, 1, tzinfo=UTC)
    evs = cdst.dst_arrivals(h.world, START, end)
    assert [(e.key, e.due, e.data["effect"]) for e in evs] == [
        ("chaos.dst:2025-03-30", datetime(2025, 3, 30, 1, tzinfo=UTC), "skipped_local_hour"),
        ("chaos.dst:2025-10-26", datetime(2025, 10, 26, 1, tzinfo=UTC), "ambiguous_local_hour"),
    ]
    assert (evs[0].data["local_from"], evs[0].data["local_to"]) == ("2025-03-30T02:00:00", "2025-03-30T03:00:00")
    assert (evs[1].data["local_from"], evs[1].data["local_to"]) == ("2025-10-26T03:00:00", "2025-10-26T02:00:00")
    assert all(e.priority == 5 for e in evs)
    for e in evs:
        h.queue.push(e)
    h.run_until(end)
    effs = h.effects("dst_local_time")
    assert [(e.entity_type, e.entity_id, e.effect, e.at) for e in effs] == [
        ("dst_transition", "2025-03-30", "skipped_local_hour", evs[0].due),
        ("dst_transition", "2025-10-26", "ambiguous_local_hour", evs[1].due),
    ]
    assert effs[0].detail["tz"] == "Europe/Berlin" and "wms.carrier_events.event_local" in effs[0].detail["columns"]
    off = harness(chaos_cfg(dst_truth=False))
    assert cdst.dst_arrivals(off.world, START, end) == []


@pytest.mark.parametrize("step", [timedelta(minutes=17), timedelta(hours=1), timedelta(hours=5, minutes=3),
                                  timedelta(days=2)])
def test_dst_arrivals_partition_invariant(harness, step):
    h = harness()
    start, end = datetime(2025, 3, 29, tzinfo=UTC), datetime(2025, 10, 28, tzinfo=UTC)
    whole = [(e.key, e.due, e.data) for e in cdst.dst_arrivals(h.world, start, end)]
    parts, t = [], start
    while t < end:
        nxt = min(end, t + step)
        parts += [(e.key, e.due, e.data) for e in cdst.dst_arrivals(h.world, t, nxt)]
        t = nxt
    assert parts == whole and len(whole) == 2


def test_dst_transition_at_slice_boundary():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    t = datetime(2025, 3, 30, 1, tzinfo=UTC)
    assert [x[0] for x in cdst.transitions(tz, t - timedelta(seconds=1), t)] == [t]
    assert cdst.transitions(tz, t, t + timedelta(hours=3)) == []
    other = ZoneInfo("Australia/Lord_Howe")  # half-hour shifts at a half hour: bisection finds them
    got = cdst.transitions(other, datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    assert len(got) == 2 and all(abs(a - b) == timedelta(minutes=30) for _, a, b in got)


# -- truth windows -------------------------------------------------------------------------------


def test_record_windows_one_per_enabled_defect(harness):
    h = harness()
    cwin.record_windows(h.world)
    recs = h.of(ScenarioWindowRecorded)
    assert [r.scenario_id for r in recs] == list(chaos.DEFECTS)
    for r in recs:
        assert r.window_id == f"{r.scenario_id}:chaos" and r.source == "chaos"
        assert r.start == START and r.end is None and r.at == START
        assert r.params == {"rate": chaos.rate(h.world, r.scenario_id)}
    rows = h.rows("truth.scenario_windows")
    assert rows[("status_error:chaos",)]["params"] == '{"rate": 0.003}'
    assert rows[("dst_local_time:chaos",)]["source"] == "chaos"
    off = harness(chaos_cfg(**CHAOS_OFF))
    cwin.record_windows(off.world)
    assert off.events == []
    some = harness(chaos_cfg(**{**CHAOS_OFF, "status_error_rate": 0.01}))
    cwin.record_windows(some.world)
    assert [r.window_id for r in some.of(ScenarioWindowRecorded)] == ["status_error:chaos"]


def test_chaos_registers_its_sources_handlers_and_bootstrap():
    from cpgsim.domain import HANDLER_MODULES
    from cpgsim.engine.registry import ARRIVAL_SOURCES, BOOTSTRAPS, HANDLERS

    assert HANDLER_MODULES[-1] == "cpgsim.chaos"
    assert {"chaos.dst", "chaos.gdpr"} <= set(ARRIVAL_SOURCES)
    assert {"chaos.dup_customer", "chaos.status_err", "chaos.status_fix", "chaos.gdpr", "chaos.gdpr_erase",
            "chaos.dst"} <= set(HANDLERS)
    assert any(name == "cpgsim.chaos.windows.record_windows" and order == 1 for order, name, _ in BOOTSTRAPS)


def test_truth_effect_ids_unique_across_defects(harness):
    """Each (defect, entity, effect) is recorded exactly once: the truth buffer rejects duplicates."""
    h = harness(chaos_cfg(duplicate_carrier_event_rate=0.5, free_text_return_reason_rate=0.5,
                          missing_order_ref_rate=0.5, malformed_postcode_rate=0.5))
    for i in range(1, 400):
        d = _delivery(i)
        fulfilment._scan(h.world, d, "hub_scan", "Kassel")
        fulfilment._scan(h.world, d, "delivered", "München")
        chaos.return_free_text(h.world, _ret(i), "DE")
        chaos.order_ref(h.world, SimpleNamespace(sales_doc_id=i, doc_type="web", order_id=i, meta={}))
        chaos.shop_address(h.world, i, Address("DE", "DE-BY", "80331", "München"))
    effs = h.effects()
    assert len(effs) == len({e.effect_id for e in effs}) == len(h.rows("truth.planted_effects"))
