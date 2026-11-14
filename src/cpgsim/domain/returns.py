"""Web returns, refunds and credit memos.

Public API (wave 2 contract)::

    maybe_schedule_return(world, order, delivery) -> None
    refund(world, order, amount, reason, return_id=None, credit_items=None,
           include_shipping=False, delivery_id=None) -> None

Event kinds owned here and their timing:

* ``ret.request:{delivery_id}``: with probability ``return_rate`` per delivered
  parcel, 0.5-``return_window_days`` days after delivery, 08:00-22:00 local.
  Creates the WebReturn (``ReturnCreated``), order ``return_requested``.
* ``ret.label:{return_id}``: 10 min - 4 h later, ``label_issued``.
* ``ret.received:{return_id}``: ``return_receive_days`` (3-10) days after the
  request, on a warehouse working day 08:00-16:00; order lines
  ``returned_qty``, order ``returned``.
* ``ret.inspected:{return_id}``: 1-2 working days later, 09:00-16:00. Rejected
  with ``return_reject_rate`` (``inspected`` then ``rejected``); otherwise line
  conditions (resaleable share by reason), stock back via
  ``warehouse.receive_return`` and ``ret.refund:{return_id}`` 2-30 h later.
* ``ret.refund:{return_id}``: ``refund(...)`` with a credit memo, return
  ``refunded``.

Reasons follow ``return_reason_mix``; ``allergic_reaction`` only when the
parcel holds a line of the dermocosmetic brand (otherwise redistributed).
"""

from __future__ import annotations

from datetime import timedelta

from ..engine.registry import handler
from ..engine.rng import Rng
from .customers import lazy, pick
from .events import PaymentStatusChanged, RefundIssued, ReturnChanged, ReturnCreated, WebOrderLinesChanged
from .model import Delivery, Refund, ReturnLine, WebOrder, WebReturn

web = lazy("web")
warehouse = lazy("warehouse")
billing = lazy("billing")

ALLERGIC = "allergic_reaction"
RESALEABLE_BY_REASON = {"damaged_in_transit": 0.12, ALLERGIC: 0.35, "wrong_item": 0.92}
FULL_RETURN_SHARE = {"did_not_like": 0.25, "damaged_in_transit": 0.35, "wrong_item": 0.15, ALLERGIC: 0.0}


def _derma_brand(world) -> str | None:
    return world.ref.catalogue.brand_of_role("derma")


def _working_moment(world, d, rng: Rng, start_hour: float, span_hours: float):
    """A moment on the first warehouse working day on or after ``d``."""
    wh = world.cfg.company.warehouse
    cal = world.ref.calendar
    if not cal.is_working_day(d, wh.country, wh.region):
        d = cal.next_working_day(d, wh.country, wh.region)
    t = world.at_local(d, int(start_hour)) + timedelta(hours=(start_hour % 1) + rng.random() * span_hours)
    if t <= world.now:
        t = world.now + timedelta(minutes=30 + rng.random() * 90)
    return t


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def maybe_schedule_return(world, order: WebOrder, delivery: Delivery) -> None:
    """Decide whether a delivered parcel will be returned and schedule the request."""
    fcfg = world.cfg.fulfilment
    rng = world.rng("ret.decide", delivery.delivery_id)
    if rng.random() >= fcfg.return_rate:
        return
    derma = _derma_brand(world)
    cat = world.ref.catalogue
    has_derma = derma is not None and any(cat.get(dl.sku_id).brand == derma for dl in delivery.lines)
    mix = dict(fcfg.return_reason_mix)
    if not has_derma:
        mix.pop(ALLERGIC, None)
    if not mix:
        mix = {"did_not_like": 1.0}
    reason = pick(rng, mix)
    window = max(1.0, float(fcfg.return_window_days))
    days = 0.5 + rng.random() * (window - 0.5)
    base = delivery.delivered_at or world.now
    d = world.local_date(base + timedelta(days=days))
    t = world.at_local(d, 8) + timedelta(hours=rng.random() * 14)
    if t <= world.now:
        t = world.now + timedelta(hours=2 + rng.random() * 10)
    o = world.store.mut("web_order", order.order_id)
    web.open_work(o, +1)
    world.schedule(
        "ret.request",
        t,
        f"ret.request:{delivery.delivery_id}",
        {"order_id": o.order_id, "delivery_id": delivery.delivery_id, "reason": reason},
    )


def refund(
    world,
    order: WebOrder,
    amount: float,
    reason: str,
    return_id: int | None = None,
    credit_items: list[tuple[int, int]] | None = None,
    include_shipping: bool = False,
    delivery_id: int | None = None,
) -> int | None:
    """Refund ``amount`` (gross, order currency) and raise the credit memo.

    ``reason``: return | lost | returned_to_sender | cancelled_after_capture.
    ``credit_items``: ``(sales_item_no, qty)`` credited on the invoice of
    ``delivery_id`` (or of the return's delivery). Returns the refund id
    (None if nothing was left to refund).
    """
    o = world.store.mut("web_order", order.order_id)
    amount = round(amount, 2)
    if o.captured_total > 0:
        amount = min(amount, round(o.captured_total - o.refunded_total, 2))
    if amount <= 0:
        return None
    pid = o.meta.get("auth_payment")
    rid = world.next_id("refund")
    world.emit(
        RefundIssued(
            at=world.now,
            refund=Refund(
                refund_id=rid,
                order_id=o.order_id,
                payment_id=int(pid) if pid is not None else None,
                amount=amount,
                currency=o.currency,
                reason=reason,
                created_at=world.now,
                return_id=return_id,
            ),
        )
    )
    o.refunded_total = round(o.refunded_total + amount, 2)
    full = o.captured_total > 0 and o.refunded_total >= o.captured_total - 0.01
    if pid is not None:
        world.emit(
            PaymentStatusChanged(
                at=world.now,
                payment_id=int(pid),
                status="refunded" if full else "partially_refunded",
                amount=o.refunded_total,
            )
        )
    if delivery_id is None and return_id is not None:
        r = world.store.get("web_return", return_id)
        delivery_id = r.delivery_id if r is not None else None
    inv = web.invoice_of_delivery(world, o, delivery_id) if delivery_id is not None else None
    if inv is not None and credit_items and world.store.get("billing", inv) is not None:
        cm = billing.create_credit_memo(world, inv, list(credit_items), reason, include_shipping=include_shipping)
        if cm is not None:
            o.billing_ids.append(cm.billing_id)
            web.schedule_settlement(
                world, cm.billing_id, round(cm.net_value + cm.tax, 2), world.rng("ret.cm_settle", rid)
            )
    web.set_status(world, o, "refunded" if full else "partially_refunded")
    return rid


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _return_lines(world, rng: Rng, o: WebOrder, dlv: Delivery, reason: str) -> list[ReturnLine]:
    cat = world.ref.catalogue
    cands = [(dl.sales_item_no // 10, dl.sku_id, dl.qty) for dl in dlv.lines if dl.qty > 0]
    if reason == ALLERGIC:
        derma = _derma_brand(world)
        cands = [c for c in cands if cat.get(c[1]).brand == derma] or cands
        line_no, sku, qty = cands[int(rng.random() * len(cands)) % len(cands)]
        chosen = [(line_no, sku, 1)]
    elif rng.random() < FULL_RETURN_SHARE.get(reason, 0.2):
        chosen = cands
    else:
        line_no, sku, qty = cands[int(rng.random() * len(cands)) % len(cands)]
        if reason == "did_not_like" and qty > 1:
            qty = 1 + int(rng.random() * qty) % qty
        chosen = [(line_no, sku, qty)]
    return [ReturnLine(line_no=i, order_line_no=ln, sku_id=s, qty=q) for i, (ln, s, q) in enumerate(chosen, 1)]


@handler("ret.request")
def on_request(world, ev) -> None:
    oid = int(ev.data["order_id"])
    did = int(ev.data["delivery_id"])
    reason = ev.data["reason"]
    o = world.store.get("web_order", oid)
    if o is None:
        return
    o = world.store.mut("web_order", oid)
    dlv = world.store.get("delivery", did)
    if dlv is None or not dlv.lines or o.status in ("refunded", "cancelled"):
        web.open_work(o, -1, world.now)
        web.maybe_final(world, o)
        return
    rid = world.next_id("web_return")
    rng = world.rng("ret.lines", rid)
    r = WebReturn(
        return_id=rid,
        order_id=oid,
        delivery_id=did,
        customer_id=o.customer_id,
        reason=reason,
        lines=_return_lines(world, rng, o, dlv, reason),
        requested_at=world.now,
    )
    world.store.put("web_return", rid, r)
    world.emit(ReturnCreated(at=world.now, ret=r))
    o.return_ids.append(rid)
    web.set_status(world, o, "return_requested")
    world.schedule(
        "ret.label", world.now + timedelta(minutes=10 + rng.random() * 230), f"ret.label:{rid}", {"return_id": rid}
    )


@handler("ret.label")
def on_label(world, ev) -> None:
    rid = int(ev.data["return_id"])
    r = world.store.get("web_return", rid)
    if r is None or r.status != "requested":
        return
    r = world.store.mut("web_return", rid)
    r.status = "label_issued"
    world.emit(ReturnChanged(at=world.now, return_id=rid, status="label_issued"))
    rng = world.rng("ret.transit", rid)
    lo, hi = world.cfg.fulfilment.return_receive_days
    days = lo + rng.random() * max(0, hi - lo)
    d = world.local_date(r.requested_at + timedelta(days=days))
    t = _working_moment(world, d, rng, 8.0, 8.0)
    world.schedule("ret.received", t, f"ret.received:{rid}", {"return_id": rid})


@handler("ret.received")
def on_received(world, ev) -> None:
    rid = int(ev.data["return_id"])
    r = world.store.get("web_return", rid)
    if r is None or r.status != "label_issued":
        return
    r = world.store.mut("web_return", rid)
    r.status = "received"
    world.emit(ReturnChanged(at=world.now, return_id=rid, status="received"))
    o = world.store.get("web_order", r.order_id)
    if o is not None:
        o = world.store.mut("web_order", r.order_id)
        changes = {}
        for rl in r.lines:
            ln = o.lines[rl.order_line_no - 1]
            ln.returned_qty += rl.qty
            changes[ln.line_no] = {"returned_qty": ln.returned_qty}
        world.emit(WebOrderLinesChanged(at=world.now, order_id=o.order_id, lines=changes))
        if o.status not in ("partially_refunded", "refunded"):
            web.set_status(world, o, "returned")
    rng = world.rng("ret.inspect_time", rid)
    wh = world.cfg.company.warehouse
    d = world.ref.calendar.add_working_days(world.local_date(), 1 + int(rng.random() * 2), wh.country, wh.region)
    t = _working_moment(world, d, rng, 9.0, 7.0)
    world.schedule("ret.inspected", t, f"ret.inspected:{rid}", {"return_id": rid})


@handler("ret.inspected")
def on_inspected(world, ev) -> None:
    rid = int(ev.data["return_id"])
    r = world.store.get("web_return", rid)
    if r is None or r.status != "received":
        return
    r = world.store.mut("web_return", rid)
    fcfg = world.cfg.fulfilment
    rng = world.rng("ret.inspect", rid)
    rejected = rng.random() < fcfg.return_reject_rate
    p_ok = RESALEABLE_BY_REASON.get(r.reason, fcfg.return_resaleable_rate)
    conds: dict[int, str] = {}
    for rl in r.lines:
        rl.condition = "resaleable" if rng.random() < p_ok else "damaged"
        conds[rl.line_no] = rl.condition
    r.status = "inspected"
    world.emit(ReturnChanged(at=world.now, return_id=rid, status="inspected", line_conditions=conds))
    if rejected:
        r.status = "rejected"
        world.emit(ReturnChanged(at=world.now, return_id=rid, status="rejected"))
        o = world.store.get("web_order", r.order_id)
        if o is not None:
            o = world.store.mut("web_order", r.order_id)
            web.open_work(o, -1, world.now)
            web.maybe_final(world, o)
        return
    for rl in r.lines:
        warehouse.receive_return(world, rl.sku_id, rl.qty, rid, rl.condition == "resaleable", delivery_id=r.delivery_id)
    world.schedule(
        "ret.refund", world.now + timedelta(hours=2 + rng.random() * 28), f"ret.refund:{rid}", {"return_id": rid}
    )


@handler("ret.refund")
def on_refund(world, ev) -> None:
    rid = int(ev.data["return_id"])
    r = world.store.get("web_return", rid)
    if r is None or r.status != "inspected":
        return
    r = world.store.mut("web_return", rid)
    o = world.store.get("web_order", r.order_id)
    if o is None:
        return
    o = world.store.mut("web_order", r.order_id)
    amount = sum(web.line_gross(o, rl.order_line_no, rl.qty) for rl in r.lines)
    returned = {rl.order_line_no: rl.qty for rl in r.lines}
    dlv = world.store.get("delivery", r.delivery_id)
    full_order = all(ln.returned_qty >= ln.shipped_qty for ln in o.lines)
    include_shipping = bool(full_order and o.shipping_fee > 0 and dlv is not None and dlv.seq_in_order == 1)
    if include_shipping:
        amount += o.shipping_fee
    items = [(ln_no * 10, q) for ln_no, q in sorted(returned.items())]
    r.refund_id = refund(
        world, o, round(amount, 2), "return", return_id=rid, credit_items=items,
        include_shipping=include_shipping, delivery_id=r.delivery_id,
    )
    r.status = "refunded"
    world.emit(ReturnChanged(at=world.now, return_id=rid, status="refunded"))
    web.open_work(o, -1, world.now)
    web.maybe_final(world, o)
