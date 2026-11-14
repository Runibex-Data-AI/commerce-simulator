"""Order chaos: statuses set in error (shop, wms) and missing ERP order references."""

from __future__ import annotations

import math
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

import pytest

from cpgsim import chaos
from cpgsim.chaos import orders as corders
from cpgsim.domain import web
from cpgsim.domain.events import (
    DeliveryChanged,
    DeliveryCreated,
    SalesDocCreated,
    StatusErrorCorrected,
    StatusErrorSet,
    WebOrderPlaced,
)
from cpgsim.domain.model import Address, Customer, Delivery, DeliveryLine, OrderLine, SalesDoc, WebOrder
from cpgsim.writers import keys

from .conftest import START, chaos_cfg

ONLY_STATUS = dict(status_error_rate=1.0, duplicate_customer_rate=0.0, malformed_postcode_rate=0.0)
ADDR = Address("DE", "DE-BY", "80331", "München", "Lindenweg", "4")


def _binomial_ok(k: int, n: int, p: float) -> bool:
    return abs(k - n * p) <= 3 * math.sqrt(n * p * (1 - p)) + 1


def _order_with_system(h, system: str, start: int = 1) -> int:
    oid = start
    while corders.plan(h.world, oid)["system"] != system:
        oid += 1
    return oid


def _place(h, oid: int, status: str = "authorised") -> WebOrder:
    sku = h.world.ref.catalogue.skus[0]
    o = WebOrder(order_id=oid, customer_id=1, country="DE", currency="EUR", placed_at=h.world.now, ship_to=ADDR,
                 lines=[OrderLine(1, sku.sku_id, 1, 9.99, 0.0, 0.19)], shipping_fee=0.0, payment_method="card",
                 status=status)
    h.store.put("web_order", oid, o)
    c = Customer(1, "DE", ADDR, START, "direct")
    h.world.emit(WebOrderPlaced(at=h.world.now, order=o, customer=c, source="direct"))
    return o


def _delivery(h, did: int, oid: int) -> Delivery:
    sku = h.world.ref.catalogue.skus[0]
    d = Delivery(delivery_id=did, kind="web", sales_doc_id=900 + oid, ship_to_account="WEB-DE", ship_to=ADDR,
                 carrier="Swiftbox", lines=[DeliveryLine(1, sku.sku_id, 1, 10)], created_at=h.world.now,
                 planned_ship_date=h.world.local_date(), order_id=oid)
    h.store.put("delivery", did, d)
    doc = SalesDoc(sales_doc_id=900 + oid, doc_type="web", sold_to="WEB-DE", ship_to="WEB-DE", currency="EUR",
                   fx_rate_eur=1.0, created_at=h.world.now, requested_date=h.world.local_date(),
                   external_ref=str(oid), items=[], channel="web", order_id=oid)
    h.world.emit(DeliveryCreated(at=h.world.now, delivery=d, sales_doc=doc))
    h.store.mut("web_order", oid).delivery_ids.append(did)
    return d


# -- rates -------------------------------------------------------------------------------------------


def test_status_error_rate_and_system_split(harness):
    h = harness()
    n = 200_000
    hits = 0
    for oid in range(1, n + 1):
        chaos.on_order_placed(h.world, SimpleNamespace(order_id=oid))
    hits = len(h.queue)
    assert _binomial_ok(hits, n, 0.003)
    systems = Counter(corders.plan(h.world, oid)["system"] for oid in range(1, 20_001))
    assert _binomial_ok(systems["shop"], 20_000, 0.6)
    for oid in range(1, 2000):
        p = corders.plan(h.world, oid)
        lo, hi = corders.SHOP_SET_HOURS if p["system"] == "shop" else corders.WMS_SET_HOURS
        assert lo <= p["set_h"] <= hi and 2.0 <= p["fix_h"] <= 10.0
    assert h.events == []  # nothing is written before the error is set


def test_missing_order_ref_rate_variants_and_truth(harness):
    h = harness()
    n = 50_000
    flagged = Counter()
    for sid in range(1, n + 1):
        doc = SimpleNamespace(sales_doc_id=sid, doc_type="web", order_id=sid + 7, meta={})
        chaos.order_ref(h.world, doc)
        if doc.meta:
            flagged[doc.meta["order_ref"]] += 1
    total = sum(flagged.values())
    assert _binomial_ok(total, n, 0.01)
    assert _binomial_ok(flagged["missing"], total, 0.5)
    effs = h.effects("missing_order_ref")
    assert len(effs) == total == len({e.effect_id for e in effs})
    e = effs[0]
    assert e.entity_type == "sales_doc" and e.detail["web_order_id"] == int(e.entity_id) + 7
    retail = SimpleNamespace(sales_doc_id=effs[0].entity_id, doc_type="retail", order_id=1, meta={})
    chaos.order_ref(h.world, retail)
    assert retail.meta == {}


def test_missing_order_ref_renders_in_vbak(harness):
    h = harness(chaos_cfg(missing_order_ref_rate=1.0))
    for sid in range(1, 40):
        doc = SalesDoc(sales_doc_id=sid, doc_type="web", sold_to="WEB-DE", ship_to="WEB-DE", currency="EUR",
                       fx_rate_eur=1.0, created_at=START, requested_date=START.date(), external_ref=str(sid + 100),
                       items=[], channel="web", order_id=sid + 100)
        chaos.order_ref(h.world, doc)
        h.world.emit(SalesDocCreated(at=START, doc=doc))
    rows = list(h.rows("erp.vbak").values())
    assert len(rows) == 39
    full = {sid: keys.shop_order_id(h.world.seed, sid + 100) for sid in range(1, 40)}
    variants = Counter()
    for e in h.effects("missing_order_ref"):
        variants[e.detail["variant"]] += 1
    nulls = [r for r in rows if r["bstnk"] is None]
    short = [r for r in rows if r["bstnk"] is not None]
    assert len(nulls) == variants["missing"] and len(short) == variants["truncated"]
    assert all(len(r["bstnk"]) == keys.TRUNCATED_REF_LEN for r in short)
    assert all(any(f.startswith(r["bstnk"]) for f in full.values()) for r in short)


# -- status error: shop ------------------------------------------------------------------------------


def test_shop_status_error_residue_and_correction(harness):
    h = harness(chaos_cfg(**ONLY_STATUS))
    oid = _order_with_system(h, "shop")
    p = corders.plan(h.world, oid)
    o = _place(h, oid)
    chaos.on_order_placed(h.world, o)
    set_at = START + timedelta(hours=p["set_h"])
    assert h.queue.peek_due() == set_at
    h.run_until(set_at)
    (s,) = h.of(StatusErrorSet)
    assert (s.system, s.entity_id, s.wrong_status, s.error_at, s.at) == ("shop", oid, "cancelled", set_at, set_at)
    sid = (keys.shop_order_id(h.world.seed, oid),)
    row = h.rows("shop.orders")[sid]
    # between set and fix: the row reads cancelled
    assert row["status"] == "cancelled" and row["cancelled_at"] == set_at
    # the real lifecycle moves on meanwhile
    h.world.now = set_at + timedelta(minutes=30)
    o = h.store.mut("web_order", oid)
    web.set_status(h.world, o, "released")
    fix_at = set_at + timedelta(hours=p["fix_h"])
    h.run_until(fix_at)
    (c,) = h.of(StatusErrorCorrected)
    assert (c.system, c.entity_id, c.status, c.at) == ("shop", oid, "released", fix_at)
    row = h.rows("shop.orders")[sid]
    assert row["status"] == "released" and row["cancelled_at"] == set_at  # residue kept
    assert row["updated_at"] == fix_at
    (eff,) = h.effects("status_error")
    assert (eff.entity_type, eff.entity_id, eff.at) == ("web_order", str(oid), fix_at)
    assert eff.magnitude == pytest.approx(p["fix_h"], abs=1e-3)
    assert eff.detail["system"] == "shop" and eff.detail["corrected_status"] == "released"
    assert eff.detail["residue_column"] == "shop.orders.cancelled_at"


def test_shop_status_error_updates_committed_row(harness):
    """Set and correction on a row flushed in an earlier batch become UPDATEs."""
    h = harness(chaos_cfg(**ONLY_STATUS))
    oid = _order_with_system(h, "shop")
    p = corders.plan(h.world, oid)
    o = _place(h, oid)
    chaos.on_order_placed(h.world, o)
    h.dispatcher.batch.clear()
    h.run_until(START + timedelta(hours=p["set_h"]))
    sid = (keys.shop_order_id(h.world.seed, oid),)
    upd = h.updates("shop.orders")[sid]
    assert upd["status"] == "cancelled" and "cancelled_at" in upd
    h.dispatcher.batch.clear()
    h.run_until(START + timedelta(hours=p["set_h"] + p["fix_h"]))
    upd = h.updates("shop.orders")[sid]
    assert upd["status"] == "authorised" and "cancelled_at" not in upd


def test_shop_status_error_skips_a_really_cancelled_order(harness):
    h = harness(chaos_cfg(**ONLY_STATUS))
    oid = _order_with_system(h, "shop")
    o = _place(h, oid, status="cancelled")
    chaos.on_order_placed(h.world, o)
    h.run_until(START + timedelta(days=2))
    assert h.of(StatusErrorSet) == [] and h.effects("status_error") == []


# -- status error: wms -------------------------------------------------------------------------------


def test_wms_status_error_waits_for_the_shipment(harness):
    h = harness(chaos_cfg(**ONLY_STATUS))
    oid = _order_with_system(h, "wms")
    p = corders.plan(h.world, oid)
    o = _place(h, oid)
    chaos.on_order_placed(h.world, o)
    d = _delivery(h, 77, oid)
    h.run_until(START + timedelta(hours=24))
    assert h.of(StatusErrorSet) == []  # not shipped yet: re-check in 12 h
    assert h.queue.contains(f"chaos.status_err:{oid}:1")
    shipped = START + timedelta(hours=30)
    h.world.now = shipped
    d = h.store.mut("delivery", 77)
    d.status, d.shipped_at = "shipped", shipped
    h.world.emit(DeliveryChanged(at=shipped, delivery_id=77, status="shipped", fields={"kind": "web"}))
    h.run_until(START + timedelta(hours=36))
    # found shipped at the 36 h check; the error falls set_h after the goods issue
    target = shipped + timedelta(hours=p["set_h"])
    if target > START + timedelta(hours=36):
        assert h.of(StatusErrorSet) == []
        h.run_until(target)
    (s,) = h.of(StatusErrorSet)
    assert (s.system, s.entity_id) == ("wms", 77) and s.at == max(target, START + timedelta(hours=36))
    row = h.rows("wms.shipments")[(keys.wms_shipment_id(77),)]
    assert row["status_code"] == "CX" and row["cancelled_local"] == s.error_at.astimezone(h.world.tz).replace(
        tzinfo=None)
    d = h.store.mut("delivery", 77)
    d.status = "in_transit"
    h.run_until(s.at + timedelta(hours=11))
    (c,) = h.of(StatusErrorCorrected)
    assert (c.system, c.entity_id, c.status) == ("wms", 77, "in_transit")
    row = h.rows("wms.shipments")[(keys.wms_shipment_id(77),)]
    assert row["status_code"] == "IT" and row["cancelled_local"] is not None  # residue kept
    (eff,) = h.effects("status_error")
    assert eff.detail["residue_column"] == "wms.shipments.cancelled_local" and eff.detail["delivery_id"] == 77
    assert h.rows("shop.orders")[(keys.shop_order_id(h.world.seed, oid),)]["cancelled_at"] is None


def test_wms_status_error_gives_up_on_a_shipment_that_never_ships(harness):
    h = harness(chaos_cfg(**ONLY_STATUS))
    oid = _order_with_system(h, "wms")
    o = _place(h, oid)
    chaos.on_order_placed(h.world, o)
    _delivery(h, 78, oid)
    h.run_until(START + timedelta(days=10))
    assert h.of(StatusErrorSet) == [] and len(h.queue) == 0 and h.effects("status_error") == []


def test_wms_status_error_skips_cancelled_delivery_and_missing_order(harness):
    h = harness(chaos_cfg(**ONLY_STATUS))
    oid = _order_with_system(h, "wms")
    o = _place(h, oid)
    chaos.on_order_placed(h.world, o)
    _delivery(h, 79, oid)
    h.store.mut("delivery", 79).status = "cancelled"
    oid2 = _order_with_system(h, "wms", oid + 1)
    chaos.on_order_placed(h.world, _place(h, oid2))
    h.store.remove("web_order", oid2)
    h.run_until(START + timedelta(days=10))
    assert h.of(StatusErrorSet) == [] and len(h.queue) == 0


def test_fix_uses_status_at_set_time_when_the_entity_is_gone(harness, caplog):
    h = harness(chaos_cfg(**ONLY_STATUS))
    oid = _order_with_system(h, "shop")
    p = corders.plan(h.world, oid)
    chaos.on_order_placed(h.world, _place(h, oid))
    h.run_until(START + timedelta(hours=p["set_h"]))
    h.store.remove("web_order", oid)
    h.run_until(START + timedelta(days=1))
    (c,) = h.of(StatusErrorCorrected)
    assert c.status == "authorised"
    assert "no longer in the store" in caplog.text


def test_entities_outlive_the_status_fix():
    """Orders and deliveries stay in the store at least 30 days after their final state."""
    assert corders.MAX_SPAN_HOURS < 5 * 24 < web.CLEANUP_AFTER_DAYS * 24


def test_cancelled_is_terminal_in_phase1_lifecycles():
    """The residue query needs: nothing leaves 'cancelled' (shop order or wms shipment)."""
    import inspect

    from cpgsim.domain import fulfilment

    assert "cancelled" in web._DEAD_STATUSES
    # every handler that moves an order on after shipping guards dead statuses or needs a shipped delivery
    src = inspect.getsource(web.on_shipped)
    assert "_DEAD_STATUSES" in src
    # fulfilment cancels open deliveries only and never picks or ships a cancelled one
    assert 'd.status != "open"' in inspect.getsource(fulfilment.cancel_delivery)
    assert 'd.status != "open"' in inspect.getsource(fulfilment.on_pick)
    assert 'd.status != "picked"' in inspect.getsource(fulfilment.on_ship)


def test_status_error_uses_neutral_vocabulary():
    from cpgsim.domain.model import DELIVERY_STATUSES, WEB_ORDER_STATUSES

    assert corders.WRONG_STATUS in WEB_ORDER_STATUSES and corders.WRONG_STATUS in DELIVERY_STATUSES
