"""ERP billing: invoices, credit memos and payment clearing.

Invoices bill delivery lines (or explicit sales items) at the sales document's
net prices, pro rata per unit. The last invoice of an item bills the
remainder, so the billed totals of an item add up to its net value exactly.
The web shipping charge is the sales document's freight item (item 900,
``sku_id`` 0, see ``SalesItem.item_type``); it goes on the first invoice of the
order as item 900 with ``sales_item_no`` 900. The freight item is never
delivered: it counts for the billing status only. Goods items are numbered
10, 20, ... skipping 900 (``model.goods_item_no``), so long retailer invoices
never collide with it, and the shipping charge is recognised by ``sku_id`` 0.
Credit memos reference an earlier invoice and are priced like it. ``clear``
records the incoming payment.

Billing documents are entities of kind ``billing``. They are removed
``RETENTION_DAYS`` after their billing date (``ful.billing_purge``), long after
anything can still refer to them (web returns close within ~6 weeks of
shipment, retailer payments arrive within ~70 days of the invoice).

The handler is registered here but imported through ``domain.fulfilment``.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..engine.registry import handler
from .events import BillingDocChanged, BillingDocCreated, SalesDocChanged
from .model import FREIGHT_ITEM_NO, BillingDoc, BillingItem, Delivery, SalesDoc, SalesItem, goods_item_no

SHIPPING_ITEM_NO = FREIGHT_ITEM_NO
RETENTION_DAYS = 100
PURGE_HOUR = 3


def is_shipping_item(it: BillingItem) -> bool:
    """The billed shipping charge (``sku_id`` 0), whatever its item number."""
    return it.sku_id == 0


# -- sales document flow status (shared with fulfilment) ------------------------


def _target(item, kind: str) -> int:
    """Quantity an item is expected to reach for delivery / billing."""
    if kind == "delivery" and item.item_type == "freight":
        return 0  # non-stock service item: never delivered
    if item.rejection_reason:
        return item.delivered_qty if kind == "billing" else 0
    return item.confirmed_qty


def flow_status(doc: SalesDoc, kind: str) -> str:
    """``none`` | ``partial`` | ``full`` for ``kind`` ``delivery`` or ``billing``."""
    attr = "delivered_qty" if kind == "delivery" else "billed_qty"
    done_any = False
    full = True
    relevant = False
    for it in doc.items:
        done = getattr(it, attr)
        if done > 0:
            done_any = True
        target = _target(it, kind)
        if target > 0:
            relevant = True
            if done < target:
                full = False
    if not done_any:
        return "none"
    return "full" if (full and relevant) else "partial"


def refresh_header(doc: SalesDoc) -> dict:
    """Recompute delivery/billing/overall status; returns the header fields that changed."""
    changes: dict = {}
    ds, bs = flow_status(doc, "delivery"), flow_status(doc, "billing")
    if ds != doc.delivery_status:
        doc.delivery_status = ds
        changes["delivery_status"] = ds
    if bs != doc.billing_status:
        doc.billing_status = bs
        changes["billing_status"] = bs
    if doc.status not in ("rejected",):
        if ds == "full" and bs == "full":
            status = "completed"
        elif ds != "none" or bs != "none":
            status = "in_process"
        else:
            status = doc.status
        if status != doc.status:
            doc.status = status
            changes["status"] = status
    return changes


def freight_item(doc: SalesDoc) -> SalesItem | None:
    """The document's freight (shipping charge) item, if it has one."""
    for it in doc.items:
        if it.item_type == "freight":
            return it
    return None


def sales_doc_for_update(world, doc: SalesDoc) -> SalesDoc:
    """The stored sales document (marked dirty), or ``doc`` itself if it is not stored."""
    if world.store.has("sales_doc", doc.sales_doc_id):
        return world.store.mut("sales_doc", doc.sales_doc_id)
    return doc


# -- helpers ---------------------------------------------------------------------


def _std_cost(world, sku_id: int, d: date) -> float:
    from . import masterdata

    return float(masterdata.std_cost(world, sku_id, d))


def _fx(world, currency: str, d: date) -> float:
    if currency == "EUR":
        return 1.0
    from . import masterdata

    return float(masterdata.fx_rate(world, currency, d))


def _schedule_purge(world, doc: BillingDoc) -> None:
    due = world.at_local(doc.billing_date + timedelta(days=RETENTION_DAYS), PURGE_HOUR)
    if due < world.now:
        due = world.now
    world.schedule("ful.billing_purge", due, f"ful.billing_purge:{doc.billing_id}",
                   {"billing_id": doc.billing_id}, priority=95)


# -- invoices --------------------------------------------------------------------


def create_invoice(
    world,
    sales_doc: SalesDoc,
    delivery: Delivery | None,
    items: list[tuple[int, int]] | None,
    billing_date: date,
    due_date: date,
    include_shipping: bool,
) -> BillingDoc:
    """Invoice delivery lines (or explicit ``(sales_item_no, qty)`` items)."""
    doc = sales_doc_for_update(world, sales_doc)
    by_no = {it.item_no: it for it in doc.items}
    # (sales_item_no, qty, delivery_line_no)
    to_bill: list[tuple[int, int, int | None]] = []
    if items is None:
        if delivery is None:
            raise ValueError("create_invoice needs a delivery or explicit items")
        for ln in delivery.lines:
            to_bill.append((ln.sales_item_no, ln.qty, ln.line_no))
    else:
        line_of = {}
        if delivery is not None:
            for ln in delivery.lines:
                line_of.setdefault(ln.sales_item_no, ln.line_no)
        for item_no, qty in items:
            to_bill.append((int(item_no), int(qty), line_of.get(int(item_no))))

    billing_id = world.next_id("billing")
    delivery_id = delivery.delivery_id if delivery is not None else None
    billed_meta: dict = doc.meta.setdefault("billed", {})
    out: list[BillingItem] = []
    item_changes: dict[int, dict] = {}
    for sales_item_no, qty, dl_no in to_bill:
        if qty <= 0:
            continue
        si = by_no[sales_item_no]
        prev_net, prev_tax = billed_meta.get(str(sales_item_no), [0.0, 0.0])
        new_billed = si.billed_qty + qty
        if new_billed >= si.qty:
            net = round(si.net_value - prev_net, 2)
            tax = round(si.tax - prev_tax, 2)
        else:
            net = round(si.net_value / si.qty * qty, 2) if si.qty else 0.0
            tax = round(si.tax / si.qty * qty, 2) if si.qty else 0.0
        billed_meta[str(sales_item_no)] = [round(prev_net + net, 2), round(prev_tax + tax, 2)]
        si.billed_qty = new_billed
        item_changes[sales_item_no] = {"billed_qty": si.billed_qty, "confirmed_qty": si.confirmed_qty}
        out.append(
            BillingItem(
                item_no=goods_item_no(len(out) + 1),
                sku_id=si.sku_id,
                qty=qty,
                net_value=net,
                tax=tax,
                cost=round(_std_cost(world, si.sku_id, billing_date) * qty, 2),
                sales_item_no=sales_item_no,
                delivery_id=delivery_id,
                delivery_line_no=dl_no,
            )
        )
    fi = freight_item(doc)
    if fi is not None:
        # The freight item is billed once, in full, on the first invoice.
        if include_shipping and fi.billed_qty < fi.confirmed_qty and not fi.rejection_reason:
            out.append(
                BillingItem(
                    item_no=SHIPPING_ITEM_NO,
                    sku_id=0,
                    qty=fi.confirmed_qty,
                    net_value=round(fi.net_value, 2),
                    tax=round(fi.tax, 2),
                    cost=round(fi.cost, 2),
                    sales_item_no=fi.item_no,
                    delivery_id=delivery_id,
                    delivery_line_no=None,
                )
            )
            fi.billed_qty = fi.confirmed_qty
            billed_meta[str(fi.item_no)] = [round(fi.net_value, 2), round(fi.tax, 2)]
            item_changes[fi.item_no] = {"billed_qty": fi.billed_qty, "confirmed_qty": fi.confirmed_qty}
            doc.meta["shipping_billed"] = True
    elif include_shipping and not doc.meta.get("shipping_billed") and (doc.shipping_net or doc.shipping_tax):
        # Documents without a freight item (state from before the freight item existed).
        out.append(
            BillingItem(
                item_no=SHIPPING_ITEM_NO,
                sku_id=0,
                qty=1,
                net_value=round(doc.shipping_net, 2),
                tax=round(doc.shipping_tax, 2),
                cost=0.0,
                sales_item_no=doc.items[0].item_no if doc.items else 10,
                delivery_id=delivery_id,
                delivery_line_no=None,
            )
        )
        doc.meta["shipping_billed"] = True

    inv = BillingDoc(
        billing_id=billing_id,
        doc_kind="invoice",
        sales_doc_id=doc.sales_doc_id,
        payer=doc.sold_to,
        currency=doc.currency,
        fx_rate_eur=round(_fx(world, doc.currency, billing_date), 6),
        billing_date=billing_date,
        created_at=world.now,
        due_date=due_date,
        items=out,
    )
    world.store.put("billing", billing_id, inv)
    if delivery is not None:
        if world.store.has("delivery", delivery.delivery_id):
            world.store.mut("delivery", delivery.delivery_id).billing_id = billing_id
        else:
            delivery.billing_id = billing_id
    header = refresh_header(doc)
    world.emit(BillingDocCreated(at=world.now, doc=inv))
    if header or item_changes:
        world.emit(SalesDocChanged(at=world.now, sales_doc_id=doc.sales_doc_id, header=header,
                                   items=item_changes))
    _schedule_purge(world, inv)
    return inv


# -- credit memos ----------------------------------------------------------------


def create_credit_memo(
    world,
    invoice_id: int,
    items: list[tuple[int, int]],
    reason: str,
    include_shipping: bool = False,
) -> BillingDoc | None:
    """Credit memo for ``(sales_item_no, qty)`` of an earlier invoice.

    Quantities are capped at the invoiced quantity. Returns None (and emits
    nothing) if nothing is left to credit, e.g. only shipping was asked for
    on an invoice without a shipping charge.
    """
    inv = world.store.get("billing", invoice_id)
    if inv is None:
        raise KeyError(f"invoice {invoice_id} not found (purged or never created)")
    by_sales_item: dict[int, BillingItem] = {}
    for it in inv.items:
        if not is_shipping_item(it):
            by_sales_item.setdefault(it.sales_item_no, it)
    out: list[BillingItem] = []
    for sales_item_no, qty in items or []:
        ref = by_sales_item.get(int(sales_item_no))
        if ref is None or qty <= 0:
            continue
        qty = min(int(qty), ref.qty)
        if qty == ref.qty:
            net, tax, cost = ref.net_value, ref.tax, ref.cost
        else:
            net = round(ref.net_value / ref.qty * qty, 2)
            tax = round(ref.tax / ref.qty * qty, 2)
            cost = round(ref.cost / ref.qty * qty, 2)
        out.append(
            BillingItem(
                item_no=goods_item_no(len(out) + 1),
                sku_id=ref.sku_id,
                qty=qty,
                net_value=net,
                tax=tax,
                cost=cost,
                sales_item_no=ref.sales_item_no,
                delivery_id=ref.delivery_id,
                delivery_line_no=ref.delivery_line_no,
                reference_item_no=ref.item_no,
            )
        )
    if include_shipping:
        for it in inv.items:
            if is_shipping_item(it):
                out.append(
                    BillingItem(
                        item_no=SHIPPING_ITEM_NO,
                        sku_id=0,
                        qty=1,
                        net_value=it.net_value,
                        tax=it.tax,
                        cost=0.0,
                        sales_item_no=it.sales_item_no,
                        delivery_id=it.delivery_id,
                        reference_item_no=it.item_no,
                    )
                )
                break
    if not out:
        return None
    today = world.local_date()
    cm = BillingDoc(
        billing_id=world.next_id("billing"),
        doc_kind="credit_memo",
        sales_doc_id=inv.sales_doc_id,
        payer=inv.payer,
        currency=inv.currency,
        fx_rate_eur=inv.fx_rate_eur,
        billing_date=today,
        created_at=world.now,
        due_date=today,
        items=out,
        reference_billing_id=inv.billing_id,
        reason=reason,
    )
    world.store.put("billing", cm.billing_id, cm)
    world.emit(BillingDocCreated(at=world.now, doc=cm))
    _schedule_purge(world, cm)
    return cm


# -- clearing --------------------------------------------------------------------


def clear(world, billing_id: int, amount: float | None = None, status: str | None = None) -> None:
    """Payment received for a billing document (invoice, or credit memo paid out).

    The status is derived (``partially_cleared`` when less than the total was
    paid) unless ``status`` is given: retail payments that short-pay trade
    deductions clear the invoice in full (``status="cleared"``); the residual is
    recorded on the deduction cases (``erp.zdeduct_clr``).
    """
    doc = world.store.get("billing", billing_id)
    total = None
    if doc is not None:
        total = round(doc.net_value + doc.tax, 2)
    paid = round(float(amount), 2) if amount is not None else total
    derived = "cleared"
    if total is not None and paid is not None and paid < total - 0.005:
        derived = "partially_cleared"
    status = status if status is not None else derived
    if doc is not None:
        doc = world.store.mut("billing", billing_id)
        doc.paid_at = world.now
        doc.paid_amount = paid
    fields = {"paid_at": world.now, "status": status}
    if paid is not None:
        fields["paid_amount"] = paid
    world.emit(BillingDocChanged(at=world.now, billing_id=billing_id, fields=fields))


@handler("ful.billing_purge")
def on_billing_purge(world, ev) -> None:
    world.store.remove("billing", ev.data["billing_id"])
