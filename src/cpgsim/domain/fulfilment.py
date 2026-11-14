"""Outbound deliveries for web and retail: pick, ship, carrier scans, outcomes.

Public API (wave-2 contract): :func:`create_delivery`, :func:`cancel_delivery`.

Web parcels
    Picked on the next warehouse working day after release (same day when
    released before 08:00 on a working day), between ``pick_start_hour`` and
    15:00, skewed towards the morning; the carrier collects 1-4 h later, by
    18:00. At pick: tracking number and ``label_created`` scan. At shipment:
    goods issue, freight cost, ``picked_up`` scan, the web invoice
    (``billing.create_invoice``) and ``web.shipped:{delivery_id}`` for the
    order owner. The whole carrier journey is drawn at shipment:

    * transit = log-normal(median * carrier_delay factor, sigma) delivery days,
      median by destination region, then country, then ``default``;
      delivery days skip Sundays and public holidays at the destination
      (``models.delays.parcel_delivery_moment``);
    * outcome: lost (``lost_rate``), exception (``exception_rate`` x carrier
      multiplier x carrier_exception factor), else delivered first time;
    * scans: ``hub_scan`` the same night at the origin hub (status
      ``in_transit``), a second ``hub_scan`` at the destination hub early on
      the delivery day when transit is longer than one night,
      ``out_for_delivery`` that morning, then ``delivered`` or
      ``delivery_failed``. A failed attempt (status ``exception``, notifies
      ``web.delivery_exception``) is redelivered the next delivery day
      (``exception_redelivered_share``) or returned to the warehouse after
      5-7 days (``returned_to_sender``; stock back via
      ``warehouse.receive_return``). A lost parcel has no scans after the hub
      and is declared lost ``lost_declared_after_days`` after shipment.
    * every final outcome schedules ``web.delivery_outcome:{delivery_id}``.

Retail pallets
    Picked on the planned ship date in the morning, shipped around midday by
    the pallet forwarder (freight per case), delivered 1-2 working days later
    in the DC's receiving window (``models.delays.pallet_delivery_date``) with
    ``delivered`` and ``pod_signed`` scans; ``retail.shipped`` and
    ``retail.pod`` notify the retail module.

The carrier journey is stored as a list of steps in ``Delivery.meta['plan']``
and worked through by ``ful.scan:{delivery_id}:{step}`` events, one at a time.
Deliveries are never removed here: their owners (web, retail) remove them.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta

from ..engine.registry import handler
from ..engine.rng import derive_seed
from ..models import delays
from . import billing, warehouse
from .events import CarrierScan, DeliveryChanged, DeliveryCreated, SalesDocChanged
from .model import Address, Delivery, DeliveryLine, SalesDoc

log = logging.getLogger("cpgsim.domain.fulfilment")

WEB_PICK_END_HOUR = 15
WEB_SAME_DAY_BEFORE_HOUR = 8
CARRIER_LAST_PICKUP_HOUR = 18
SHIP_AFTER_PICK_HOURS = (1.0, 4.0)
RETAIL_PICK_HOURS = (6.0, 10.0)
RETAIL_SHIP_HOURS = (11.0, 14.0)
PALLET_RECEIVING_HOURS = (6.0, 12.0)
PALLET_LEAD_TWO_DAYS_PROB = 0.35
HUB_SCAN_HOURS = (20.0, 23.5)
DEST_HUB_SCAN_HOURS = (2.5, 5.0)
OFD_HOURS = (6.5, 8.5)
RTS_DAYS = (5, 7)
WAREHOUSE_EVENT_HOURS = (9.0, 15.0)

STEP_PRIORITY = 50

DELIVERED_DETAILS = (("recipient", 0.62), ("neighbour", 0.2), ("parcel_locker", 0.1), ("safe_place", 0.08))
FAILED_DETAILS = (("recipient_absent", 0.72), ("address_issue", 0.14), ("refused", 0.08), ("damaged", 0.06))

# Parcel hub city per destination region (fictional carriers, real cities).
HUB_CITY = {
    "DE-BW": "Stuttgart", "DE-BY": "Nürnberg", "DE-BE": "Berlin", "DE-BB": "Berlin",
    "DE-HB": "Bremen", "DE-HH": "Hamburg", "DE-HE": "Frankfurt am Main", "DE-MV": "Rostock",
    "DE-NI": "Hannover", "DE-NW": "Dortmund", "DE-RP": "Koblenz", "DE-SL": "Saarbrücken",
    "DE-SN": "Leipzig", "DE-ST": "Magdeburg", "DE-SH": "Neumünster", "DE-TH": "Erfurt",
    "AT-1": "Wien", "AT-2": "Klagenfurt", "AT-3": "Wien", "AT-4": "Linz", "AT-5": "Salzburg",
    "AT-6": "Graz", "AT-7": "Innsbruck", "AT-8": "Wolfurt", "AT-9": "Wien",
    "CH-ZH": "Zürich", "CH-BE": "Bern", "CH-LU": "Luzern", "CH-BS": "Basel", "CH-BL": "Basel",
    "CH-GE": "Genève", "CH-VD": "Lausanne", "CH-TI": "Lugano", "CH-SG": "St. Gallen",
    "CH-AG": "Aarau", "CH-SO": "Härkingen", "CH-FR": "Bern", "CH-VS": "Sion", "CH-GR": "Chur",
}
COUNTRY_HUB = {"DE": "Kassel", "AT": "Wien", "CH": "Zürich"}

_TRACKING_PREFIX = {"Paketblitz": "PB", "Nordpost": "NP", "Swiftbox": "SX"}
_TRACKING_MULT_10 = 7_919_377_123  # coprime to 10: an affine bijection on 10-digit numbers
_TRACKING_MULT_9 = 387_420_489  # 3**18, coprime to 10


# -- small helpers ---------------------------------------------------------------


def tracking_number(world, carrier: str, delivery_id: int, kind: str) -> str:
    """Deterministic, unique per delivery: parcel prefix + 10 digits, pallet "FW" + 9 digits."""
    if kind == "retail":
        off = derive_seed(world.seed, "ful.tracking.fw") % 10**9
        return f"FW{(_TRACKING_MULT_9 * delivery_id + off) % 10**9:09d}"
    prefix = _TRACKING_PREFIX.get(carrier) or ("".join(c for c in carrier if c.isalpha()).upper()[:2] or "XX")
    off = derive_seed(world.seed, "ful.tracking", prefix) % 10**10
    return f"{prefix}{(_TRACKING_MULT_10 * delivery_id + off) % 10**10:010d}"


def _at(d: date, hour: float, tz) -> datetime:
    secs = min(max(int(round(hour * 3600.0)), 0), 86_399)
    return datetime.combine(d, time(secs // 3600, (secs // 60) % 60, secs % 60), tzinfo=tz)


def _uniform(r, lo: float, hi: float) -> float:
    return lo + (hi - lo) * r.random()


def _pick_weighted(r, choices) -> str:
    x = r.random() * sum(w for _, w in choices)
    for name, w in choices:
        x -= w
        if x < 0:
            return name
    return choices[-1][0]


def hub_city(addr: Address) -> str:
    return HUB_CITY.get(addr.region) or COUNTRY_HUB.get(addr.country) or addr.city


def _wh_city(world) -> str:
    return world.cfg.company.warehouse.city


def _parcel_median(ccfg, addr: Address) -> float:
    md = ccfg.median_days
    for k in (addr.region, addr.country, "default"):
        if k in md:
            return float(md[k])
    return 1.5


def _choose_parcel_carrier(world, delivery_id: int, country: str) -> str:
    parcel = world.cfg.carriers.parcel
    names = sorted(parcel)
    weights = [float(parcel[n].share.get(country, 0.0)) for n in names]
    if sum(weights) <= 0:
        weights = [1.0] * len(names)
    r = world.rng("ful.carrier", delivery_id)
    x = r.random() * sum(weights)
    for n, w in zip(names, weights):
        x -= w
        if x < 0:
            return n
    return names[-1]


def _delivery_for_update(world, delivery_id: int) -> Delivery | None:
    if not world.store.has("delivery", delivery_id):
        return None
    return world.store.mut("delivery", delivery_id)


def _sales_doc(world, sales_doc_id: int) -> SalesDoc | None:
    return world.store.get("sales_doc", sales_doc_id)


def _scan(world, d: Delivery, code: str, location: str, detail: str | None = None) -> None:
    d.scan_seq += 1
    world.emit(
        CarrierScan(
            at=world.now,
            delivery_id=d.delivery_id,
            scan_seq=d.scan_seq,
            code=code,
            carrier=d.carrier,
            location=location,
            detail=detail,
            tracking_no=d.tracking_no,
        )
    )


def _status(world, d: Delivery, status: str, **fields) -> None:
    d.status = status
    f = {"kind": d.kind}
    f.update(fields)
    world.emit(DeliveryChanged(at=world.now, delivery_id=d.delivery_id, status=status, fields=f))


def _update_delivered_qty(world, sales_doc: SalesDoc, lines, sign: int) -> None:
    doc = billing.sales_doc_for_update(world, sales_doc)
    by_no = {it.item_no: it for it in doc.items}
    items: dict[int, dict] = {}
    for sales_item_no, qty in lines:
        it = by_no.get(sales_item_no)
        if it is None:
            continue
        it.delivered_qty = max(0, it.delivered_qty + sign * qty)
        items[sales_item_no] = {"delivered_qty": it.delivered_qty, "confirmed_qty": it.confirmed_qty}
    header = billing.refresh_header(doc)
    if header or items:
        world.emit(SalesDocChanged(at=world.now, sales_doc_id=doc.sales_doc_id, header=header, items=items))


# -- public API ------------------------------------------------------------------


def create_delivery(
    world,
    kind: str,
    sales_doc: SalesDoc,
    lines: list[tuple[int, int, int]],
    ship_to_account: str,
    ship_to: Address,
    carrier: str | None,
    planned_ship_date: date,
    order_id: int,
    seq_in_order: int = 1,
) -> Delivery:
    """Create an outbound delivery for already reserved stock and schedule its pick.

    ``lines`` are ``(sku_id, qty, sales_item_no)``. For web deliveries the
    planned ship date is replaced by the warehouse's own plan (the pick day).
    """
    if kind not in ("web", "retail"):
        raise ValueError(f"unknown delivery kind {kind!r}")
    dlines = [
        DeliveryLine(line_no=i + 1, sku_id=int(sku), qty=int(qty), sales_item_no=int(item_no))
        for i, (sku, qty, item_no) in enumerate([l for l in lines if int(l[1]) > 0])
    ]
    if not dlines:
        raise ValueError("create_delivery: no lines with a positive quantity")
    delivery_id = world.next_id("delivery")
    if carrier is None:
        carrier = (
            _choose_parcel_carrier(world, delivery_id, ship_to.country)
            if kind == "web"
            else world.cfg.carriers.pallet_forwarder
        )
    r = world.rng("ful.pick_time", delivery_id)
    pick_at = _web_pick_moment(world, r) if kind == "web" else _retail_pick_moment(world, r, planned_ship_date)
    if kind == "web":
        planned_ship_date = pick_at.date()
    d = Delivery(
        delivery_id=delivery_id,
        kind=kind,
        sales_doc_id=sales_doc.sales_doc_id,
        ship_to_account=ship_to_account,
        ship_to=ship_to,
        carrier=carrier,
        lines=dlines,
        created_at=world.now,
        planned_ship_date=planned_ship_date,
        order_id=order_id,
        seq_in_order=seq_in_order,
    )
    d.meta["pick_at"] = pick_at.astimezone(world.now.tzinfo).isoformat()
    world.store.put("delivery", delivery_id, d)
    world.emit(DeliveryCreated(at=world.now, delivery=d, sales_doc=sales_doc))
    _update_delivered_qty(world, sales_doc, [(l.sales_item_no, l.qty) for l in dlines], +1)
    world.schedule("ful.pick", pick_at, f"ful.pick:{delivery_id}", {"delivery_id": delivery_id},
                   priority=STEP_PRIORITY)
    return d


def cancel_delivery(world, delivery_id: int, reason: str) -> bool:
    """Cancel a delivery that has not been picked yet; returns False if too late."""
    d = world.store.get("delivery", delivery_id)
    if d is None or d.status != "open":
        return False
    d = world.store.mut("delivery", delivery_id)
    _status(world, d, "cancelled", line_nos=[l.line_no for l in d.lines], reason=reason)
    for l in d.lines:
        warehouse.unreserve(world, l.sku_id, l.qty)
    sd = _sales_doc(world, d.sales_doc_id)
    if sd is not None:
        _update_delivered_qty(world, sd, [(l.sales_item_no, l.qty) for l in d.lines], -1)
    return True


# -- pick and ship ---------------------------------------------------------------


def _web_pick_moment(world, r) -> datetime:
    wh = world.cfg.company.warehouse
    country, region = warehouse.location(world)
    return delays.pick_moment(
        r, warehouse.calendar(world), world.local(), wh.pick_start_hour, WEB_PICK_END_HOUR,
        country, region, same_day_before_hour=WEB_SAME_DAY_BEFORE_HOUR,
    )


def _retail_pick_moment(world, r, planned: date) -> datetime:
    tz = world.tz
    now_local = world.local()
    d = warehouse.next_working_day(world, planned, include_today=True)
    pick = _at(d, _uniform(r, *RETAIL_PICK_HOURS), tz)
    if pick < now_local:
        late = now_local + timedelta(minutes=_uniform(r, 15, 60))
        if warehouse.is_working_day(world, late.date()) and late.hour < WEB_PICK_END_HOUR \
                and late.date() == now_local.date():
            pick = late
        else:
            nd = warehouse.next_working_day(world, now_local.date())
            pick = _at(nd, _uniform(r, *RETAIL_PICK_HOURS), tz)
    return pick


@handler("ful.pick")
def on_pick(world, ev) -> None:
    d = world.store.get("delivery", ev.data["delivery_id"])
    if d is None or d.status != "open":
        return  # cancelled before the pick
    d = world.store.mut("delivery", d.delivery_id)
    for l in d.lines:
        l.picked_qty = l.qty
    d.tracking_no = tracking_number(world, d.carrier, d.delivery_id, d.kind)
    d.meta.pop("pick_at", None)
    _status(world, d, "picked", picked={l.line_no: l.picked_qty for l in d.lines}, tracking_no=d.tracking_no)
    r = world.rng("ful.ship_time", d.delivery_id)
    now_local = world.local()
    if d.kind == "web":
        _scan(world, d, "label_created", _wh_city(world))
        ship = now_local + timedelta(hours=_uniform(r, *SHIP_AFTER_PICK_HOURS))
        latest = _at(now_local.date(), CARRIER_LAST_PICKUP_HOUR - 0.25 + 0.25 * r.random(), world.tz)
        ship = min(ship, max(latest, now_local + timedelta(minutes=30)))
    else:
        ship = _at(now_local.date(), _uniform(r, *RETAIL_SHIP_HOURS), world.tz)
        ship = max(ship, now_local + timedelta(minutes=45 + 60 * r.random()))
    world.schedule("ful.ship", ship, f"ful.ship:{d.delivery_id}", {"delivery_id": d.delivery_id},
                   priority=STEP_PRIORITY)


def _freight(world, d: Delivery) -> float:
    cfg = world.cfg.carriers
    if d.kind == "web":
        ccfg = cfg.parcel.get(d.carrier)
        if ccfg is None:
            return 0.0
        return round(float(ccfg.cost_eur.get(d.ship_to.country, max(ccfg.cost_eur.values()))), 2)
    cat = world.ref.catalogue
    cases = sum(math.ceil(l.qty / max(cat.get(l.sku_id).units_per_case, 1)) for l in d.lines)
    return round(cfg.pallet_cost_eur_per_case * cases, 2)


@handler("ful.ship")
def on_ship(world, ev) -> None:
    d = world.store.get("delivery", ev.data["delivery_id"])
    if d is None or d.status != "picked":
        return
    d = world.store.mut("delivery", d.delivery_id)
    for l in d.lines:
        warehouse.issue(world, l.sku_id, l.picked_qty or l.qty, d.delivery_id)
    d.freight_cost_eur = _freight(world, d)
    d.shipped_at = world.now
    _status(world, d, "shipped", tracking_no=d.tracking_no, freight_cost_eur=d.freight_cost_eur,
            shipped_at=d.shipped_at)
    _scan(world, d, "picked_up", _wh_city(world))
    if d.kind == "web":
        billing_id = None
        sd = _sales_doc(world, d.sales_doc_id)
        if sd is None:
            log.warning("delivery %s: sales doc %s missing, no invoice", d.delivery_id, d.sales_doc_id)
        else:
            day = world.local_date()
            inv = billing.create_invoice(world, sd, d, None, billing_date=day, due_date=day,
                                         include_shipping=True)
            billing_id = inv.billing_id
        world.schedule("web.shipped", world.now, f"web.shipped:{d.delivery_id}",
                       {"delivery_id": d.delivery_id, "order_id": d.order_id, "billing_id": billing_id})
        plan = _plan_parcel(world, d)
    else:
        world.schedule("retail.shipped", world.now, f"retail.shipped:{d.delivery_id}",
                       {"delivery_id": d.delivery_id, "order_id": d.order_id})
        plan = _plan_pallet(world, d)
    d.meta["plan"] = [[t.astimezone(world.now.tzinfo).isoformat(), step] for t, step in plan]
    _schedule_step(world, d, 0)


# -- carrier journey plans ---------------------------------------------------------


def _carrier_factors(world, d: Delivery) -> tuple[float, float]:
    sc = world.scenarios
    ctx = dict(carrier=d.carrier, region=d.ship_to.region, country=d.ship_to.country)
    rec = ("delivery", d.delivery_id)
    f_delay = float(sc.factor("carrier_delay", world.now, world=world, record=rec, **ctx))
    f_exc = float(sc.factor("carrier_exception", world.now, world=world, record=rec, **ctx))
    return f_delay, f_exc


def _plan_parcel(world, d: Delivery) -> list[tuple[datetime, str]]:
    """Draw the whole carrier journey of a web parcel at shipment."""
    fcfg = world.cfg.fulfilment
    ccfg = world.cfg.carriers.parcel.get(d.carrier)
    cal = world.ref.calendar
    tz = world.tz
    country, region = d.ship_to.country, d.ship_to.region
    r = world.rng("ful.transit", d.delivery_id)
    f_delay, f_exc = _carrier_factors(world, d)
    median = _parcel_median(ccfg, d.ship_to) if ccfg is not None else 1.5
    sigma = ccfg.sigma if ccfg is not None else 0.45
    exc_mult = ccfg.exception_multiplier if ccfg is not None else 1.0
    transit = delays.lognormal_days(r, median * f_delay, sigma)
    shipped = world.local()
    attempt = delays.parcel_delivery_moment(r, cal, shipped, transit, country, region)

    u = world.rng("ful.outcome", d.delivery_id).random()
    p_lost = fcfg.lost_rate
    p_exc = min(0.95, fcfg.exception_rate * exc_mult * f_exc)
    outcome = "lost" if u < p_lost else ("exception" if u < p_lost + p_exc else "delivered")
    d.meta["transit_days"] = round(transit, 3)

    plan: list[tuple[datetime, str]] = []
    hub = _at(shipped.date(), _uniform(r, *HUB_SCAN_HOURS), tz)
    plan.append((max(hub, shipped + timedelta(minutes=30)), "hub"))
    if outcome == "lost":
        decl = shipped.date() + timedelta(days=fcfg.lost_declared_after_days)
        decl = warehouse.next_working_day(world, decl, include_today=True)
        plan.append((_at(decl, _uniform(r, *WAREHOUSE_EVENT_HOURS), tz), "lost"))
        return plan

    def visit(day_moment: datetime, final_step: str) -> None:
        if (day_moment.date() - shipped.date()).days > 1:
            plan.append((_at(day_moment.date(), _uniform(r, *DEST_HUB_SCAN_HOURS), tz), "hub2"))
        ofd = _at(day_moment.date(), _uniform(r, *OFD_HOURS), tz)
        plan.append((min(ofd, day_moment - timedelta(minutes=20)), "ofd"))
        plan.append((day_moment, final_step))

    if outcome == "delivered":
        visit(attempt, "delivered")
        return plan

    visit(attempt, "failed")
    resched = attempt + timedelta(hours=_uniform(r, 0.5, 2.5))
    if r.random() < fcfg.exception_redelivered_share:
        plan.append((resched, "resched"))
        second = delays.parcel_delivery_moment(r, cal, attempt, 1.0 + r.random(), country, region)
        ofd = _at(second.date(), _uniform(r, *OFD_HOURS), tz)
        plan.append((max(min(ofd, second - timedelta(minutes=20)), resched + timedelta(minutes=5)), "ofd"))
        plan.append((second, "delivered"))
    else:
        back = attempt.date() + timedelta(days=r.randint(*RTS_DAYS))
        back = warehouse.next_working_day(world, back, include_today=True)
        plan.append((_at(back, _uniform(r, *WAREHOUSE_EVENT_HOURS), tz), "rts"))
    return plan


def _plan_pallet(world, d: Delivery) -> list[tuple[datetime, str]]:
    r = world.rng("ful.pallet", d.delivery_id)
    tz = world.tz
    shipped = world.local()
    lead = 2 if r.random() < PALLET_LEAD_TWO_DAYS_PROB else 1
    day = delays.pallet_delivery_date(world.ref.calendar, shipped.date(), lead,
                                      d.ship_to.country, d.ship_to.region)
    arrive = _at(day, _uniform(r, *PALLET_RECEIVING_HOURS), tz)
    arrive = max(arrive, shipped + timedelta(hours=3))
    pod = arrive + timedelta(minutes=_uniform(r, 10, 40))
    return [(arrive, "pallet_delivered"), (pod, "pod")]


def _schedule_step(world, d: Delivery, i: int) -> None:
    plan = d.meta.get("plan") or []
    if i >= len(plan):
        return
    due = datetime.fromisoformat(plan[i][0])
    if due < world.now:
        due = world.now
    world.schedule("ful.scan", due, f"ful.scan:{d.delivery_id}:{i}",
                   {"delivery_id": d.delivery_id, "step": i}, priority=STEP_PRIORITY)


def _finish(world, d: Delivery, outcome: str) -> None:
    d.outcome = outcome
    d.meta.pop("plan", None)
    if d.kind == "web":
        world.schedule("web.delivery_outcome", world.now, f"web.delivery_outcome:{d.delivery_id}",
                       {"delivery_id": d.delivery_id, "order_id": d.order_id, "outcome": outcome})


@handler("ful.scan")
def on_scan(world, ev) -> None:
    d = _delivery_for_update(world, ev.data["delivery_id"])
    if d is None:
        return
    i = int(ev.data["step"])
    plan = d.meta.get("plan") or []
    if i >= len(plan):
        return
    step = plan[i][1]
    r = world.rng("ful.scan", d.delivery_id, i)
    to = d.ship_to
    if step == "hub":
        _scan(world, d, "hub_scan", f"Hub {_wh_city(world)}")
        _status(world, d, "in_transit")
    elif step == "hub2":
        _scan(world, d, "hub_scan", f"Hub {hub_city(to)}")
    elif step == "ofd":
        d.attempts += 1
        _scan(world, d, "out_for_delivery", to.city)
        _status(world, d, "out_for_delivery")
    elif step == "delivered":
        d.delivered_at = world.now
        _scan(world, d, "delivered", to.city, _pick_weighted(r, DELIVERED_DETAILS))
        _status(world, d, "delivered", delivered_at=d.delivered_at)
        _finish(world, d, "delivered")
    elif step == "failed":
        _scan(world, d, "delivery_failed", to.city, _pick_weighted(r, FAILED_DETAILS))
        first = not d.meta.get("exception_notified")
        _status(world, d, "exception")
        if first and d.kind == "web":
            d.meta["exception_notified"] = True
            world.schedule("web.delivery_exception", world.now, f"web.delivery_exception:{d.delivery_id}",
                           {"delivery_id": d.delivery_id, "order_id": d.order_id})
    elif step == "resched":
        _scan(world, d, "redelivery_scheduled", f"Depot {hub_city(to)}")
    elif step == "rts":
        _scan(world, d, "returned_to_sender", _wh_city(world), "returned to sender")
        for l in d.lines:
            warehouse.receive_return(world, l.sku_id, l.picked_qty or l.qty, None, True,
                                     delivery_id=d.delivery_id)
        _status(world, d, "returned_to_sender")
        _finish(world, d, "returned_to_sender")
    elif step == "lost":
        _status(world, d, "lost")
        _finish(world, d, "lost")
    elif step == "pallet_delivered":
        d.delivered_at = world.now
        _scan(world, d, "delivered", to.city)
        _status(world, d, "delivered", delivered_at=d.delivered_at,
                pod_signed_by=_pod_signer(world, d))
    elif step == "pod":
        _scan(world, d, "pod_signed", to.city, d.meta.get("pod_signed_by"))
        d.outcome = "delivered"
        d.meta.pop("plan", None)
        world.schedule("retail.pod", world.now, f"retail.pod:{d.delivery_id}",
                       {"delivery_id": d.delivery_id, "order_id": d.order_id})
        return
    else:  # pragma: no cover - plans only contain the steps above
        raise ValueError(f"unknown carrier step {step!r}")
    if d.meta.get("plan"):
        _schedule_step(world, d, i + 1)


def _pod_signer(world, d: Delivery) -> str:
    from . import people

    seed_int = derive_seed(world.seed, "ful.pod_signer", d.delivery_id) % (2**31)
    name = people.person(seed_int, d.ship_to.country if d.ship_to.country in ("DE", "AT", "CH") else "DE")[
        "last_name"
    ]
    d.meta["pod_signed_by"] = name
    return name
