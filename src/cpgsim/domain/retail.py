"""Retail channel: hidden consumer demand and DC inventory, purchase orders by EDI.

Every enabled retailer channel (phase 1: the two drugstore chains) has a few
distribution centres (``accounts.dcs(channel)``). Per DC the module keeps a
:class:`~cpgsim.domain.model.RetailerInventory` (store id = DC account id):
units on hand, units on order and a smoothed weekly forecast *level* per SKU
(``forecast``: units per week without promotion and seasonal effects; the
planning system projects it with ``seasonality.category_factor``, as a buyer
with last year's curves would).
None of this is visible in phase 1 data; what shows is the sell-in (POs, sales
documents, deliveries, invoices, payments).

Weekly rhythm per DC (local time)

=====================  ======================================================
Monday 00:30           ``retail.sellout``: consumers buy last week's hidden
                       demand (``demandplan.retail_weekly_demand`` times
                       log-normal noise with ``demand_noise_cv``), capped by on
                       hand; the rest is lost. The forecast level EWMA
                       (``forecast_alpha``) learns from sell-out divided by the
                       promotion and season factors; a sold-out week counts as
                       at least the forecast (censored demand).
order day 05:00        ``retail.po_cycle``: DC number ``i`` (1-based over all
                       enabled retailers' DCs, in account order) orders every
                       ``order_cycle_days`` on weekday ``i % 5`` for weekly
                       cycles. Order-up-to: ``level * season(mid-coverage) *
                       (cycle + lead + safety weeks) + forward buy - on hand -
                       on order``, times
                       ``lognormal(0, forecast_error_sd)`` and the
                       ``retail_order`` scenario factor, rounded up to cases.
                       Forward buy: expected promo uplift of the retailer's
                       promotions starting within ``retail_forward_buy_weeks``;
                       those lines get an off-invoice ``promo_discount`` of
                       ``retail_funding_share * discount``. Creates a RetailPO
                       and its SalesDoc (``SalesDocCreated`` = received by EDI).
+2..8 h (working)      ``retail.confirm``: ``warehouse.reserve`` per line, cut
                       to whole cases (cuts -> ``SalesDocChanged``), delivery
                       planned with ``fulfilment.create_delivery(kind="retail")``
                       for the next warehouse working day.
ship (fulfilment)      ``retail.shipped``: delivered_qty on the sales items,
                       sales doc in process, PO shipped.
POD (fulfilment)       ``retail.pod``: on hand += qty, on order -= qty, PO
                       delivered; invoice 0-1 days later in the evening batch.
invoice                ``retail.invoice``: ``billing.create_invoice`` with due
                       date = billing date + payment terms; PO invoiced.
due +/- jitter         ``retail.payment``: ``billing.clear`` on a working day;
                       PO paid; PO, sales doc, delivery and invoice entities
                       are removed.
=====================  ======================================================

Because orders replenish towards a target around true demand, sell-in and
sell-out differ from week to week (order noise, case rounding, cycles, forward
buys, lost sales) but their cumulative totals stay within the inventory swing,
so they converge over a quarter.

Last week's sell-out, lost sales and consumer demand per SKU are kept for
phase 2 feeds in a second ``retailer_inventory`` entity per DC, id
``"<dc>:sellout"`` (``on_hand`` = sold units, ``on_order`` = lost units,
``forecast`` = consumer demand), because ``RetailerInventory`` has no ``meta``.
"""

from __future__ import annotations

import dataclasses
import importlib
import math
from datetime import date, datetime, timedelta

from ..engine.queue import Event
from ..engine.registry import arrival_source, bootstrap, handler
from ..models.delays import next_working_moment
from ..models.pricing import elasticity_lift
from ..models.seasonality import category_factor
from .events import SalesDocChanged, SalesDocCreated
from .model import Condition, RetailerInventory, RetailPO, RetailPOLine, SalesDoc, SalesItem, goods_item_no

RETAIL_VAT = 0.19
SELLOUT_HOUR, SELLOUT_MINUTE = 0, 30
ORDER_EPOCH = date(2024, 1, 1)  # a Monday: anchors order-day arithmetic
CONFIRM_WINDOW = (6, 18)  # local hours in which order desk confirmations happen
INVOICE_BATCH_HOUR = 19.0  # evening billing run, 19:00-21:00
PAYMENT_HOURS = (9.0, 15.0)
SELLOUT_SUFFIX = ":sellout"
MIN_PROMO_FACTOR = 0.2
MIN_SEASON_FACTOR = 0.05


def _api(name: str):
    """Another domain module's public API (looked up at call time, so tests can swap it)."""
    return importlib.import_module(f"cpgsim.domain.{name}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def dc_plan(world) -> list[tuple[str, str, int]]:
    """``(channel, dc_account_id, index)`` for every DC of an enabled retailer (index 1-based)."""
    retailers = world.cfg.channels.retailers()
    out: list[tuple[str, str, int]] = []
    i = 0
    for acc in world.ref.accounts.all:
        if acc.kind == "retailer_dc" and acc.retailer in retailers:
            i += 1
            out.append((acc.retailer, acc.account_id, i))
    return out


def is_order_day(cycle_days: int, index: int, d: date) -> bool:
    """DC ``index`` orders on ``d``? Weekly cycles: weekday ``index % 5``."""
    cycle = max(int(cycle_days), 1)
    return (d.toordinal() - ORDER_EPOCH.toordinal() - index % 5) % cycle == 0


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _local_dates(world, start: datetime, end: datetime) -> list[date]:
    d = world.local(start).date()
    d1 = world.local(end).date()
    out = []
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


def _wh_region(world) -> str:
    return world.cfg.company.warehouse.region


def _cycle_weeks(rc) -> float:
    return rc.order_cycle_days / 7.0


def _lead_weeks(world) -> float:
    lo, hi = world.cfg.retail.lead_days
    return ((lo + hi) / 2.0 + 1.0) / 7.0  # confirmation + ship next working day + pallet lead


def _promo_factor(world, sku_id: int, channel: str, d: date) -> float:
    f = _api("promotions").demand_factor(world, sku_id, channel, d)
    return max(float(f), MIN_PROMO_FACTOR)


def _season(world, sku_id: int, d: date) -> float:
    profile = world.ref.catalogue.get(sku_id).season_profile
    return max(category_factor(profile, d), MIN_SEASON_FACTOR)


def _baseline_level(world, channel: str, dc: str, sku_id: int, week_start: date) -> float:
    """Planned weekly demand without promotion and season effects (the buyer's starting level)."""
    mid = week_start + timedelta(days=3)
    m = _api("demandplan").retail_weekly_demand(world, channel, dc, sku_id, week_start)
    return m / (_promo_factor(world, sku_id, channel, mid) * _season(world, sku_id, mid))


def po_number(channel: str, po_id: int, d: date) -> str:
    """The retailer's own PO number format (stable, unique per PO)."""
    if channel == "drogerie_kranz":
        return f"45{po_id:08d}"
    if channel == "mylo":
        return f"MY{d:%y%m}-{po_id:06d}"
    if channel == "shopnova":
        return f"SNV-PO-{po_id:07d}"
    if channel == "belle":
        return f"BEL/{d:%Y}/{po_id:05d}"
    return f"PO{po_id:08d}"


def price_line(list_price: float, qty: int, trade_discount: float,
               promo_rate: float = 0.0) -> tuple[list[Condition], float, float]:
    """Pricing conditions, net value and tax (EUR) for one retail PO line.

    list price -> customer discount (trade discount) -> off-invoice promo
    discount on the discounted value -> 19 % VAT on net.
    """
    gross = round(list_price * qty, 2)
    cd = round(-gross * trade_discount, 2)
    conds = [
        Condition("list_price", list_price, gross),
        Condition("customer_discount", trade_discount, cd),
    ]
    net = gross + cd
    if promo_rate > 0:
        pd = round(-net * promo_rate, 2)
        conds.append(Condition("promo_discount", round(promo_rate, 4), pd))
        net += pd
    net = round(net, 2)
    tax = round(net * RETAIL_VAT, 2)
    conds.append(Condition("tax", RETAIL_VAT, tax))
    return conds, net, tax


def _lognormal_mean1(rng, cv: float) -> float:
    if cv <= 0:
        rng.gauss(0.0, 1.0)
        return 1.0
    s2 = math.log(1.0 + cv * cv)
    return math.exp(math.sqrt(s2) * rng.gauss(0.0, 1.0) - s2 / 2.0)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@bootstrap(order=30)
def bootstrap_retail(world) -> None:
    retailers = world.cfg.channels.retailers()
    cat = world.ref.catalogue
    d = world.local_date()
    ws = monday(d)
    safety = world.cfg.retail.safety_weeks
    for channel, dc, _ in dc_plan(world):
        # Average steady state of the order-up-to policy just before the Monday
        # sell-out: about cycle + lead + safety weeks at DCs whose weekly delivery
        # lands before Monday, one cycle less where it is still in transit.
        cover = _cycle_weeks(retailers[channel]) + safety
        inv = RetailerInventory(dc_account=dc, retailer=channel)
        for sku_id in cat.retailer_assortment(channel, d):
            level = round(_baseline_level(world, channel, dc, sku_id, ws), 3)
            inv.forecast[sku_id] = level
            inv.on_hand[sku_id] = int(round(level * _season(world, sku_id, ws + timedelta(days=3)) * cover))
        world.store.put("retailer_inventory", dc, inv)


# ---------------------------------------------------------------------------
# Arrival sources (pure: seed, config, time)
# ---------------------------------------------------------------------------


@arrival_source("retail.sellout")
def sellout_arrivals(world, start: datetime, end: datetime):
    out = []
    plan = None
    for d in _local_dates(world, start, end):
        if d.weekday() != 0:
            continue
        t = world.at_local(d, SELLOUT_HOUR, SELLOUT_MINUTE)
        if not start < t <= end:
            continue
        plan = plan or dc_plan(world)
        ws = (d - timedelta(days=7)).isoformat()
        for channel, dc, _ in plan:
            out.append(Event(due=t, kind="retail.sellout", key=f"retail.sellout:{dc}:{d:%Y%m%d}",
                             data={"channel": channel, "dc": dc, "week_start": ws}, priority=20))
    return out


@arrival_source("retail.po_cycle")
def po_cycle_arrivals(world, start: datetime, end: datetime):
    retailers = world.cfg.channels.retailers()
    hour = world.cfg.retail.order_hour_local
    out = []
    plan = None
    for d in _local_dates(world, start, end):
        t = world.at_local(d, hour)
        if not start < t <= end:
            continue
        plan = plan or dc_plan(world)
        for channel, dc, idx in plan:
            if is_order_day(retailers[channel].order_cycle_days, idx, d):
                out.append(Event(due=t, kind="retail.po_cycle", key=f"retail.po_cycle:{dc}:{d:%Y%m%d}",
                                 data={"channel": channel, "dc": dc}, priority=30))
    return out


# ---------------------------------------------------------------------------
# Sell-out (hidden consumer demand)
# ---------------------------------------------------------------------------


@handler("retail.sellout")
def on_sellout(world, ev) -> None:
    channel, dc = ev.data["channel"], ev.data["dc"]
    if not world.store.has("retailer_inventory", dc):
        return
    inv: RetailerInventory = world.store.mut("retailer_inventory", dc)
    rcfg = world.cfg.retail
    cat = world.ref.catalogue
    dp = _api("demandplan")
    ws = date.fromisoformat(ev.data["week_start"])
    mid = ws + timedelta(days=3)
    assortment = set(cat.retailer_assortment(channel, mid))
    alpha = rcfg.forecast_alpha
    rng = world.rng("retail.sellout", dc, ws.isoformat())
    sold_m: dict[int, float] = {}
    lost_m: dict[int, float] = {}
    want_m: dict[int, float] = {}
    for sku_id in sorted(inv.on_hand):
        noise = _lognormal_mean1(rng, rcfg.demand_noise_cv)
        u = rng.random()
        fc = inv.forecast.get(sku_id)
        if sku_id in assortment:
            mean = dp.retail_weekly_demand(world, channel, dc, sku_id, ws)
            pf = _promo_factor(world, sku_id, channel, mid) * _season(world, sku_id, mid)
        else:  # delisted or out of season: remaining stock sells off at the last forecast
            pf = _season(world, sku_id, mid)
            mean = (fc or 0.0) * pf
        x = max(mean, 0.0) * noise
        want = int(x) + (1 if u < x - int(x) else 0)
        oh = int(inv.on_hand.get(sku_id, 0))
        sold = min(oh, want)
        inv.on_hand[sku_id] = oh - sold
        if fc is not None and sku_id in assortment:
            obs = float(sold) if sold < oh or want == 0 else max(float(sold), fc * pf)
            inv.forecast[sku_id] = round(alpha * obs / pf + (1.0 - alpha) * fc, 3)
        if want:
            want_m[sku_id] = want
            sold_m[sku_id] = sold
            if want > sold:
                lost_m[sku_id] = want - sold

    # Forget SKUs the DC no longer lists once nothing is left or on its way.
    coming = set(cat.retailer_assortment(channel, world.local_date() + timedelta(days=3)))
    for sku_id in sorted(set(inv.on_hand) | set(inv.forecast)):
        if sku_id in coming:
            continue
        if inv.on_hand.get(sku_id, 0) <= 0 and inv.on_order.get(sku_id, 0) <= 0:
            inv.on_hand.pop(sku_id, None)
            inv.on_order.pop(sku_id, None)
            inv.forecast.pop(sku_id, None)

    world.store.put(
        "retailer_inventory",
        dc + SELLOUT_SUFFIX,
        RetailerInventory(dc_account=dc, retailer=channel, on_hand=sold_m, on_order=lost_m,
                          forecast=want_m),
    )


# ---------------------------------------------------------------------------
# Purchase order cycle
# ---------------------------------------------------------------------------


@handler("retail.po_cycle")
def on_po_cycle(world, ev) -> None:
    channel, dc = ev.data["channel"], ev.data["dc"]
    if not world.store.has("retailer_inventory", dc):
        return
    rc = world.cfg.channels.retailers()[channel]
    rcfg = world.cfg.retail
    pcfg = world.cfg.promotions
    cat = world.ref.catalogue
    promotions = _api("promotions")
    today = world.local_date()
    ws = monday(today)
    inv: RetailerInventory = world.store.mut("retailer_inventory", dc)

    fwd_end = today + timedelta(weeks=pcfg.retail_forward_buy_weeks)
    promos = sorted(promotions.retail_promos_starting(world, channel, today, fwd_end),
                    key=lambda p: p.promo_id)
    cover = _cycle_weeks(rc) + _lead_weeks(world) + rcfg.safety_weeks
    horizon_mid = today + timedelta(days=round(cover * 3.5))  # season factor at mid-coverage
    f_sc = float(world.scenarios.factor("retail_order", world.now, channel=channel, dc=dc))
    rng = world.rng("retail.po", dc, today.isoformat())

    lines: list[tuple[int, int, object]] = []  # (sku_id, qty, promo or None)
    for sku_id in cat.retailer_assortment(channel, today):
        z = rng.gauss(0.0, rcfg.forecast_error_sd)
        level = inv.forecast.get(sku_id)
        if level is None:  # newly listed: start from the plan
            level = round(_baseline_level(world, channel, dc, sku_id, ws), 3)
            inv.forecast[sku_id] = level
        sku = cat.get(sku_id)
        uplift = 0.0
        promo = None
        for p in promos:
            if not promotions.is_eligible(p, sku):
                continue
            days = max((p.end - p.start).total_seconds() / 86400.0, 1.0)
            p_mid = world.local_date(p.start) + timedelta(days=days / 2.0)
            uplift += (level * _season(world, sku_id, p_mid)
                       * (elasticity_lift(p.discount_pct, pcfg.elasticity) - 1.0) * days / 7.0)
            if promo is None or p.discount_pct > promo.discount_pct:
                promo = p
        fc = level * _season(world, sku_id, horizon_mid)
        need = fc * cover + uplift - inv.on_hand.get(sku_id, 0) - inv.on_order.get(sku_id, 0)
        if need <= 0:
            continue
        q = need * math.exp(z) * f_sc
        upc = max(int(sku.units_per_case), 1)
        qty = int(math.ceil(q / upc - 1e-9)) * upc
        if qty > 0:
            lines.append((sku_id, qty, promo))
    if not lines:
        return

    accounts = world.ref.accounts
    dc_acc = accounts.get(dc)
    po_id = world.next_id("retail_po")
    sales_doc_id = world.next_id("sales_doc")
    r2 = world.rng("retail.po.terms", po_id)
    lead = r2.randint(*rcfg.lead_days)
    requested = world.ref.calendar.add_working_days(today, lead, "DE", dc_acc.address.region)
    number = po_number(channel, po_id, today)
    masterdata = _api("masterdata")

    po_lines: list[RetailPOLine] = []
    items: list[SalesItem] = []
    promo_ids: set[int] = set()
    for n, (sku_id, qty, promo) in enumerate(lines, start=1):
        sku = cat.get(sku_id)
        article = cat.customer_article(channel, sku_id)
        rate = 0.0
        if promo is not None:
            rate = pcfg.retail_funding_share * promo.discount_pct
            promo_ids.add(promo.promo_id)
        conds, net, tax = price_line(sku.list_price_eur, qty, rc.trade_discount, rate)
        po_lines.append(RetailPOLine(line_no=n, sku_id=sku_id, customer_article=article, qty=qty))
        items.append(
            SalesItem(
                item_no=goods_item_no(n),  # 900 is the freight item number
                sku_id=sku_id,
                qty=qty,
                confirmed_qty=qty,  # requested; cuts follow at confirmation
                net_value=net,
                tax=tax,
                cost=round(masterdata.std_cost(world, sku_id, today) * qty, 2),
                conditions=conds,
                customer_article=article,
            )
        )
        inv.on_order[sku_id] = inv.on_order.get(sku_id, 0) + qty

    po = RetailPO(
        po_id=po_id,
        retailer=channel,
        dc_account=dc,
        po_number=number,
        created_at=world.now,
        requested_delivery=requested,
        lines=po_lines,
        status="received",
        sales_doc_id=sales_doc_id,
        promo_ids=sorted(promo_ids),
    )
    doc = SalesDoc(
        sales_doc_id=sales_doc_id,
        doc_type="retail",
        sold_to=accounts.retailer(channel).account_id,
        ship_to=dc,
        currency="EUR",
        fx_rate_eur=masterdata.fx_rate(world, "EUR", today),
        created_at=world.now,
        requested_date=requested,
        external_ref=number,
        items=items,
        channel=channel,
        order_id=po_id,
    )
    world.store.put("retail_po", po_id, po)
    world.store.put("sales_doc", sales_doc_id, doc)
    world.emit(SalesDocCreated(at=world.now, doc=doc))
    if f_sc != 1.0:  # record the planted effect against the sales document
        world.scenarios.factor("retail_order", world.now, world=world,
                               record=("sales_doc", sales_doc_id), channel=channel, dc=dc)

    t = world.now + timedelta(hours=r2.uniform(*rcfg.confirm_hours))
    t = next_working_moment(world.ref.calendar, world.local(t), "DE", _wh_region(world),
                            CONFIRM_WINDOW[0], CONFIRM_WINDOW[1])
    world.schedule("retail.confirm", t, f"retail.confirm:{po_id}", {"po_id": po_id})


# ---------------------------------------------------------------------------
# Confirmation and delivery planning
# ---------------------------------------------------------------------------


@handler("retail.confirm")
def on_confirm(world, ev) -> None:
    po_id = int(ev.data["po_id"])
    if not world.store.has("retail_po", po_id):
        return
    po: RetailPO = world.store.mut("retail_po", po_id)
    doc: SalesDoc = world.store.mut("sales_doc", po.sales_doc_id)
    inv: RetailerInventory | None = (
        world.store.mut("retailer_inventory", po.dc_account)
        if world.store.has("retailer_inventory", po.dc_account) else None
    )
    warehouse = _api("warehouse")
    cat = world.ref.catalogue
    changes: dict[int, dict] = {}
    dlines: list[tuple[int, int, int]] = []
    for line, item in zip(po.lines, doc.items):
        granted = int(warehouse.reserve(world, line.sku_id, line.qty))
        upc = max(int(cat.get(line.sku_id).units_per_case), 1)
        keep = (granted // upc) * upc
        if keep < granted:
            warehouse.unreserve(world, line.sku_id, granted - keep)
        line.confirmed_qty = keep
        item.confirmed_qty = keep
        if keep != line.qty:
            ch: dict = {"confirmed_qty": keep}
            if keep == 0:
                item.rejection_reason = "out_of_stock"
                ch["rejection_reason"] = "out_of_stock"
            changes[item.item_no] = ch
            if inv is not None:
                left = inv.on_order.get(line.sku_id, 0) - (line.qty - keep)
                inv.on_order[line.sku_id] = max(left, 0)
        if keep > 0:
            dlines.append((line.sku_id, keep, item.item_no))

    if not dlines:
        doc.status = "rejected"
        po.status = "cancelled"
        world.emit(SalesDocChanged(at=world.now, sales_doc_id=doc.sales_doc_id,
                                   header={"status": "rejected"}, items=changes))
        world.store.remove("retail_po", po_id)
        world.store.remove("sales_doc", doc.sales_doc_id)
        return

    po.status = "confirmed"
    if changes:
        world.emit(SalesDocChanged(at=world.now, sales_doc_id=doc.sales_doc_id, items=changes))

    region = _wh_region(world)
    planned = world.ref.calendar.next_working_day(world.local_date(), "DE", region)
    dc_acc = world.ref.accounts.get(po.dc_account)
    delivery = _api("fulfilment").create_delivery(
        world, "retail", doc, dlines, po.dc_account, dataclasses.replace(dc_acc.address),
        None, planned, po_id, 1,
    )
    po.delivery_id = delivery.delivery_id
    po.status = "delivery_planned"


# ---------------------------------------------------------------------------
# Shipment, proof of delivery, invoice, payment
# ---------------------------------------------------------------------------


def _po_for_delivery(world, delivery_id: int):
    d = world.store.get("delivery", delivery_id)
    if d is None or d.order_id is None or not world.store.has("retail_po", d.order_id):
        return None, None
    return d, world.store.mut("retail_po", d.order_id)


@handler("retail.shipped")
def on_shipped(world, ev) -> None:
    d, po = _po_for_delivery(world, int(ev.data["delivery_id"]))
    if po is None:
        return
    doc: SalesDoc = world.store.mut("sales_doc", po.sales_doc_id)
    by_item = {i.item_no: i for i in doc.items}
    shipped: dict[int, int] = {}
    for line in d.lines:
        shipped[line.sales_item_no] = shipped.get(line.sales_item_no, 0) + int(line.qty)
    items: dict[int, dict] = {}
    for item_no in sorted(shipped):
        item = by_item[item_no]
        item.delivered_qty = shipped[item_no]
        items[item_no] = {"delivered_qty": item.delivered_qty, "confirmed_qty": item.confirmed_qty}
    doc.status = "in_process"
    world.emit(SalesDocChanged(at=world.now, sales_doc_id=doc.sales_doc_id,
                               header={"status": "in_process"}, items=items))
    po.status = "shipped"


@handler("retail.pod")
def on_pod(world, ev) -> None:
    d, po = _po_for_delivery(world, int(ev.data["delivery_id"]))
    if po is None:
        return
    if world.store.has("retailer_inventory", po.dc_account):
        inv: RetailerInventory = world.store.mut("retailer_inventory", po.dc_account)
        for line in d.lines:
            q = int(line.qty)
            inv.on_hand[line.sku_id] = int(inv.on_hand.get(line.sku_id, 0)) + q
            inv.on_order[line.sku_id] = max(inv.on_order.get(line.sku_id, 0) - q, 0)
    po.status = "delivered"

    rng = world.rng("retail.invoice", po.po_id)
    lo, hi = world.cfg.retail.invoice_after_pod_days
    cal = world.ref.calendar
    bd = world.local_date() + timedelta(days=rng.randint(lo, hi))
    bd = cal.next_working_day(bd, "DE", _wh_region(world), include_today=True)
    t = world.at_local(bd, 0) + timedelta(hours=INVOICE_BATCH_HOUR + 2.0 * rng.random())
    t = max(t, world.now + timedelta(minutes=30))
    world.schedule("retail.invoice", t, f"retail.invoice:{po.po_id}",
                   {"po_id": po.po_id, "delivery_id": d.delivery_id})


@handler("retail.invoice")
def on_invoice(world, ev) -> None:
    po_id = int(ev.data["po_id"])
    if not world.store.has("retail_po", po_id):
        return
    po: RetailPO = world.store.mut("retail_po", po_id)
    doc = world.store.get("sales_doc", po.sales_doc_id)
    delivery = world.store.get("delivery", po.delivery_id)
    rc = world.cfg.channels.retailers()[po.retailer]
    bd = world.local_date()
    due = bd + timedelta(days=rc.payment_terms_days)
    inv_doc = _api("billing").create_invoice(world, doc, delivery, None, bd, due, False)
    po.billing_id = inv_doc.billing_id
    po.status = "invoiced"
    if world.store.has("sales_doc", po.sales_doc_id):
        world.store.mut("sales_doc", po.sales_doc_id).status = "completed"
    world.emit(SalesDocChanged(at=world.now, sales_doc_id=po.sales_doc_id,
                               header={"status": "completed"}))
    if delivery is not None:
        world.store.remove("delivery", delivery.delivery_id)

    rng = world.rng("retail.payment", po_id)
    j = int(world.cfg.retail.payment_days_jitter)
    pay = due + timedelta(days=rng.randint(-j, j))
    pay = world.ref.calendar.next_working_day(pay, "DE", _wh_region(world), include_today=True)
    lo, hi = PAYMENT_HOURS
    t = world.at_local(pay, 0) + timedelta(hours=lo + (hi - lo) * rng.random())
    t = max(t, world.now + timedelta(days=1))
    world.schedule("retail.payment", t, f"retail.payment:{po_id}",
                   {"po_id": po_id, "billing_id": inv_doc.billing_id})


@handler("retail.payment")
def on_payment(world, ev) -> None:
    po_id = int(ev.data["po_id"])
    if not world.store.has("retail_po", po_id):
        return
    po: RetailPO = world.store.mut("retail_po", po_id)
    billing_id = int(ev.data.get("billing_id") or po.billing_id)
    _api("billing").clear(world, billing_id)
    po.status = "paid"
    world.store.remove("retail_po", po_id)
    world.store.remove("sales_doc", po.sales_doc_id)
    world.store.remove("billing", billing_id)
