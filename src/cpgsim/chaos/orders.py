"""Order defects: statuses set in error (shop, wms) and missing ERP order references.

``status_error`` (CQ-3)
    0.3 % of web orders (``hit(defect, order_id)``). With rng
    ``("chaos.status_error", order_id)`` the order gets a **shop** error (60 %)
    or a **wms** error on its first shipment (40 %):

    * shop: ``chaos.status_err:{order_id}`` 1-6 h after placement sets the
      order ``cancelled`` with ``cancelled_at`` = the error time;
    * wms: once the first shipment has shipped, 0.5-3 h after the goods issue
      the shipment is set ``cancelled`` (``CX``) with ``cancelled_local``. The
      first check runs a day after placement and re-checks every 12 h, at
      most ``WMS_MAX_RETRIES`` times; a shipment that never ships (cancelled,
      long backorder) gets no error.

    ``chaos.status_fix:{order_id}`` 2-10 h after the error writes the then
    current status back (``StatusErrorCorrected``). The error timestamp is
    kept, as when a status is reverted by hand: the residue is
    ``status <> 'cancelled' AND cancelled_at IS NOT NULL`` (shop) and
    ``status_code <> 'CX' AND cancelled_local IS NOT NULL`` (wms). The truth
    row is written at the correction.

    No phase-1 lifecycle leaves a ``cancelled`` order or shipment: both are
    terminal (``web._DEAD_STATUSES`` guards every later transition of an
    order; fulfilment only cancels open deliveries and never moves a
    cancelled one on), so the residue query counts only planted errors.

``missing_order_ref``
    1 % of web sales documents carry no shop order reference (``bstnk`` NULL)
    or a truncated one (``doc.meta["order_ref"]``, rendered by the ERP writer).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..domain.events import StatusErrorCorrected, StatusErrorSet
from ..engine.registry import handler
from .core import hit, record

log = logging.getLogger(__name__)

STATUS = "status_error"
ORDER_REF = "missing_order_ref"

SET_KIND = "chaos.status_err"
FIX_KIND = "chaos.status_fix"

SHOP_SHARE = 0.6
WRONG_STATUS = "cancelled"  # neutral; 'cancelled' in WEB_ORDER_STATUSES and DELIVERY_STATUSES
SHOP_SET_HOURS = (1.0, 6.0)  # after placement
WMS_SET_HOURS = (0.5, 3.0)  # after the first shipment's goods issue
FIX_HOURS = (2.0, 10.0)  # after the error
WMS_FIRST_CHECK_HOURS = 24.0  # first look for the shipment, after placement
WMS_RETRY_HOURS = 12.0
WMS_MAX_RETRIES = 5
#: Longest time from placement to the correction (hours). Orders and deliveries
#: stay in the store at least ``web.CLEANUP_AFTER_DAYS`` after their final state.
MAX_SPAN_HOURS = WMS_FIRST_CHECK_HOURS + WMS_MAX_RETRIES * WMS_RETRY_HOURS + WMS_SET_HOURS[1] + FIX_HOURS[1]

RESIDUE_COLUMN = {"shop": "shop.orders.cancelled_at", "wms": "wms.shipments.cancelled_local"}


def _between(u: float, bounds: tuple[float, float]) -> float:
    return bounds[0] + u * (bounds[1] - bounds[0])


def plan(world, order_id: int) -> dict:
    """System and timings of an order's status error (rng ``("chaos.status_error", order_id)``)."""
    rng = world.rng("chaos.status_error", int(order_id))
    system = "shop" if rng.random() < SHOP_SHARE else "wms"
    set_h = _between(rng.random(), SHOP_SET_HOURS if system == "shop" else WMS_SET_HOURS)
    fix_h = _between(rng.random(), FIX_HOURS)
    return {"system": system, "set_h": set_h, "fix_h": fix_h}


def on_order_placed(world, order) -> None:
    """A web order was placed: 0.3 % get a status set in error (shop or first shipment)."""
    if order is None:
        return
    oid = int(order.order_id)
    if not hit(world, STATUS, oid):
        return
    p = plan(world, oid)
    data = {"order_id": oid, "system": p["system"], "set_h": p["set_h"], "fix_h": p["fix_h"], "attempt": 0}
    hours = p["set_h"] if p["system"] == "shop" else WMS_FIRST_CHECK_HOURS
    world.schedule(SET_KIND, world.now + timedelta(hours=hours), f"{SET_KIND}:{oid}", data)


def _retry(world, data: dict, due: datetime) -> None:
    n = int(data.get("attempt", 0)) + 1
    world.schedule(SET_KIND, due, f"{SET_KIND}:{data['order_id']}:{n}", {**data, "attempt": n})


@handler(SET_KIND)
def on_status_error_set(world, ev) -> None:
    d = ev.data
    oid = int(d["order_id"])
    system = d["system"]
    o = world.store.get("web_order", oid)
    if o is None:
        log.info("status error of order %s not set: order no longer in the store", oid)
        return
    if system == "shop":
        if o.status == WRONG_STATUS:
            return  # really cancelled already: an error would overwrite the true cancellation time
        entity_id, status_now = oid, o.status
    else:
        dlv = world.store.get("delivery", o.delivery_ids[0]) if o.delivery_ids else None
        if dlv is not None and dlv.status == WRONG_STATUS:
            return  # cancelled before shipping: nothing to set in error
        if dlv is None or dlv.shipped_at is None:
            if int(d.get("attempt", 0)) < WMS_MAX_RETRIES:
                _retry(world, d, world.now + timedelta(hours=WMS_RETRY_HOURS))
            return  # never shipped in time: no error
        target = dlv.shipped_at + timedelta(hours=float(d["set_h"]))
        if target > world.now:
            _retry(world, d, target)
            return
        entity_id, status_now = dlv.delivery_id, dlv.status
    world.emit(StatusErrorSet(at=world.now, system=system, entity_id=entity_id, wrong_status=WRONG_STATUS,
                              error_at=world.now))
    world.schedule(
        FIX_KIND,
        world.now + timedelta(hours=float(d["fix_h"])),
        f"{FIX_KIND}:{oid}",
        {"order_id": oid, "system": system, "entity_id": entity_id, "set_at": world.now.isoformat(),
         "status_at_set": status_now},
    )


@handler(FIX_KIND)
def on_status_error_fix(world, ev) -> None:
    d = ev.data
    oid = int(d["order_id"])
    system = d["system"]
    entity_id = int(d["entity_id"])
    kind = "web_order" if system == "shop" else "delivery"
    ent = world.store.get(kind, entity_id)
    if ent is not None:
        status = ent.status
    else:
        status = d["status_at_set"]
        log.warning("status error of %s %s corrected to the status at set time: entity no longer in the store",
                    kind, entity_id)
    world.emit(StatusErrorCorrected(at=world.now, system=system, entity_id=entity_id, status=status))
    set_at = datetime.fromisoformat(d["set_at"])
    hours = round((world.now - set_at).total_seconds() / 3600.0, 4)
    record(
        world, STATUS, "web_order", oid, "status_error", hours,
        {
            "system": system,
            "wrong_status": WRONG_STATUS,
            "corrected_status": status,
            "set_at": set_at.isoformat(),
            "corrected_at": world.now.isoformat(),
            "residue_column": RESIDUE_COLUMN[system],
            **({"delivery_id": entity_id} if system == "wms" else {}),
        },
    )


def order_ref(world, doc) -> None:
    """A web sales document is about to be created: 1 % lose or truncate the shop order reference."""
    if doc is None or doc.doc_type != "web":
        return
    sid = int(doc.sales_doc_id)
    if not hit(world, ORDER_REF, sid):
        return
    variant = "missing" if world.rng("chaos.missing_order_ref", sid).random() < 0.5 else "truncated"
    doc.meta["order_ref"] = variant
    record(world, ORDER_REF, "sales_doc", sid, "missing_order_ref", None,
           {"web_order_id": doc.order_id, "variant": variant})
