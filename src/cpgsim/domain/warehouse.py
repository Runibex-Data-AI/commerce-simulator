"""Central warehouse: stock positions, production replenishment, daily snapshots.

One warehouse (``cfg.company.warehouse``) holds the stock for every channel.
Each SKU has a :class:`~cpgsim.domain.model.StockPosition` (entity kind
``stock``) with ``on_hand`` units, ``reserved`` units (allocated to open
deliveries, not yet issued) and ``in_production`` units (open production
orders). ``available = on_hand - reserved``.

Public API (see the wave-2 contract): :func:`available`, :func:`reserve`,
:func:`unreserve`, :func:`issue`, :func:`receive_return`. Helpers for the
warehouse calendar (:func:`is_working_day`, :func:`next_working_day`,
:func:`add_working_days`, :func:`calendar`) are used by fulfilment.

Lifecycle:

* bootstrap (order 20): a position per SKU on sale at history start with
  ``expected_daily_units * initial_cover_days`` on hand (jittered per SKU
  between 75 % and 125 % so replenishment does not start for every SKU on the
  same day), recorded as ``StockMoved(adjustment)``;
* ``wh.plan`` (07:00 local on warehouse working days): creates positions for
  SKUs launching within the next four weeks, then per SKU compares the
  inventory position (on hand - reserved + in production) with the forecast
  outflow. Below ``reorder_cover_days`` of cover a production order tops the
  position up to ``target_cover_days`` (rounded up to ten cases). The goods
  receipt ``wh.receipt:{sku_id}:{yyyymmdd}`` follows after
  ``production_lead_days`` working days with log-normal noise, 08:00-14:00.
  Delisted SKUs are not replenished; their leftover stock is written off
  (scrap) 60 days after the delisting and the position is removed;
* ``wh.snapshot`` (23:55 local, every day): ``StockSnapshotTaken`` for all
  positions and the EWMA of daily outflow;
* ``wh.block`` / ``wh.unblock`` (``hero_oos_substitution``): at the window
  start all available stock of the hero (``on_hand - reserved``) goes under a
  quality block (``blocked``, movement ``adjustment`` with reference
  ``QI-BLOCK <window>``); while the block is active, hero stock that becomes
  available (unreserved, returned) is blocked too and production receipts are
  held on the position (``held_receipts``). Stock already reserved for open
  deliveries still ships. At the window end the blocked lot is scrapped
  (``QI-RECALL <window>``) and the held receipts are booked in order. A window
  already open at history start is blocked by a bootstrap.

The planner forecast blends the demand model
(``demandplan.expected_daily_units``) with the observed outflow and carries a
weekly planner error, so stock-outs are rare but happen, mostly when demand
spikes (promotions, retailer forward buying, Black Friday) meet a long
production lead time.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Iterator

from ..engine.queue import Event
from ..engine.registry import arrival_source, bootstrap, handler
from .events import StockMoved, StockSnapshotRow, StockSnapshotTaken
from .model import StockPosition

QI_BLOCK_PRIORITY = 10

PLAN_HOUR = 7
SNAPSHOT_HOUR, SNAPSHOT_MINUTE = 23, 55
RECEIPT_FIRST_HOUR, RECEIPT_WINDOW_HOURS = 8, 6.0  # receipts 08:00-14:00
PRELAUNCH_DAYS = 28  # positions (and planning) start this many days before launch
LOT_CASES = 10  # production lot size: multiples of ten cases
EWMA_ALPHA = 0.1  # smoothing of avg_daily_out per daily snapshot
PRODUCTION_LEAD_SIGMA = 0.18  # log-sd of the production lead time
FORECAST_SIGMA = 0.15  # weekly planner forecast error (log-sd)
FORECAST_OFFSETS = (7, 14)  # days ahead at which the demand model is sampled
OBSERVED_WEIGHT = 0.75  # weight of the observed outflow when it runs above the model
INITIAL_COVER_JITTER = (0.75, 1.25)
DELIST_WRITE_OFF_DAYS = 60

PLAN_PRIORITY = 40
RECEIPT_PRIORITY = 30
SNAPSHOT_PRIORITY = 90


# -- warehouse calendar --------------------------------------------------------


class WarehouseCalendar:
    """Calendar adapter for the warehouse location and its configured working days.

    Satisfies ``models.delays.WorkCalendar``: working days are the configured
    weekdays without public holidays at the asked location (normally the
    warehouse); delivery days pass through to the business calendar.
    """

    def __init__(self, cal, working_days) -> None:
        self._cal = cal
        self._wd = frozenset(int(x) for x in working_days)

    def is_working_day(self, d: date, country: str = "DE", region: str | None = None) -> bool:
        return d.weekday() in self._wd and not self._cal.is_holiday(d, country, region)

    def is_delivery_day(self, d: date, country: str = "DE", region: str | None = None) -> bool:
        return self._cal.is_delivery_day(d, country, region)


def location(world) -> tuple[str, str]:
    wh = world.cfg.company.warehouse
    return wh.country, wh.region


def calendar(world) -> WarehouseCalendar:
    cal = getattr(world, "_wh_calendar", None)
    if cal is None or cal._cal is not world.ref.calendar:
        cal = WarehouseCalendar(world.ref.calendar, world.cfg.company.warehouse.working_days)
        try:
            world._wh_calendar = cal
        except AttributeError:  # pragma: no cover - slotted world
            pass
    return cal


def is_working_day(world, d: date) -> bool:
    country, region = location(world)
    return calendar(world).is_working_day(d, country, region)


def next_working_day(world, d: date, include_today: bool = False) -> date:
    x = d if include_today else d + timedelta(days=1)
    for _ in range(60):
        if is_working_day(world, x):
            return x
        x += timedelta(days=1)
    raise RuntimeError(f"no warehouse working day within 60 days of {d}")


def add_working_days(world, d: date, n: int) -> date:
    x = d
    for _ in range(max(0, n)):
        x = next_working_day(world, x)
    return x


# -- positions -----------------------------------------------------------------


def _round_up(x: float, step: int) -> int:
    step = max(int(step), 1)
    if x <= 0:
        return 0
    return int(math.ceil(x / step - 1e-9)) * step


def _position(world, sku_id: int) -> StockPosition:
    """The SKU's position for mutation; creates an empty one if missing."""
    store = world.store
    if store.has("stock", sku_id):
        return store.mut("stock", sku_id)
    return store.put("stock", sku_id, StockPosition(sku_id=int(sku_id), on_hand=0))


def _move(world, sku_id: int, movement_type: str, qty: int, delivery_id: int | None = None,
          return_id: int | None = None, reference: str | None = None) -> None:
    world.emit(
        StockMoved(
            at=world.now,
            movement_id=world.next_id("stock_movement"),
            sku_id=int(sku_id),
            movement_type=movement_type,
            qty=int(qty),
            delivery_id=delivery_id,
            return_id=return_id,
            reference=reference,
        )
    )


def available(world, sku_id: int) -> int:
    pos = world.store.get("stock", sku_id)
    if pos is None:
        return 0
    return max(0, pos.on_hand - pos.reserved)


def reserve(world, sku_id: int, qty: int) -> int:
    """Reserve up to ``qty`` units; returns the granted quantity (<= available).

    The shortfall is remembered in ``backorder_qty`` until the next planning
    run, which adds it to the production need.
    """
    qty = int(qty)
    pos = _position(world, sku_id)
    if qty <= 0:
        return 0
    granted = min(qty, max(0, pos.on_hand - pos.reserved))
    pos.reserved += granted
    if granted < qty:
        pos.backorder_qty += qty - granted
    return granted


def unreserve(world, sku_id: int, qty: int) -> None:
    qty = int(qty)
    if qty <= 0 or not world.store.has("stock", sku_id):
        return
    pos = world.store.mut("stock", sku_id)
    pos.reserved = max(0, pos.reserved - qty)
    b = _active_block(world, sku_id)
    if b is not None:  # released hero units go straight to the quality block
        _block_available(world, pos, b.window_id)


def issue(world, sku_id: int, qty: int, delivery_id: int) -> None:
    """Goods issue of reserved stock for a shipped delivery."""
    qty = int(qty)
    if qty <= 0:
        return
    pos = _position(world, sku_id)
    pos.on_hand -= qty
    pos.reserved = max(0, pos.reserved - qty)
    pos.issued_today += qty
    _move(world, sku_id, "goods_issue", -qty, delivery_id=delivery_id)


def receive_return(world, sku_id: int, qty: int, return_id: int | None, resaleable: bool,
                   delivery_id: int | None = None) -> None:
    """Goods coming back (customer return or returned-to-sender parcel).

    Always records ``customer_return +qty``; damaged goods are scrapped right
    away with ``scrap -qty`` so on hand is unchanged.
    """
    qty = int(qty)
    if qty <= 0:
        return
    pos = _position(world, sku_id)
    pos.on_hand += qty
    _move(world, sku_id, "customer_return", qty, delivery_id=delivery_id, return_id=return_id)
    if not resaleable:
        pos.on_hand -= qty
        _move(world, sku_id, "scrap", -qty, delivery_id=delivery_id, return_id=return_id,
              reference="return not resaleable")
        return
    b = _active_block(world, sku_id)
    if b is not None:  # returned hero units are booked into the quality block
        _block_available(world, pos, b.window_id)


# -- forecasting ---------------------------------------------------------------


def _expected(world, sku_id: int, d: date) -> float:
    from . import demandplan

    return max(0.0, float(demandplan.expected_daily_units(world, sku_id, d)))


def _model_rate(world, sku, d: date) -> float:
    """Mean modelled daily units over the replenishment horizon."""
    pts = []
    first_full = sku.launch_date + timedelta(days=14)
    for k in FORECAST_OFFSETS:
        t = d + timedelta(days=k)
        if t < first_full:
            t = first_full
        if sku.delist_date is not None and t >= sku.delist_date:
            pts.append(0.0)
            continue
        pts.append(_expected(world, sku.sku_id, t))
    return sum(pts) / len(pts)


def _forecast(world, sku, pos: StockPosition, d: date) -> float:
    r_model = _model_rate(world, sku, d)
    r = max(r_model, OBSERVED_WEIGHT * pos.avg_daily_out + (1 - OBSERVED_WEIGHT) * r_model)
    iso = d.isocalendar()
    err = world.rng("wh.forecast", sku.sku_id, iso[0], iso[1]).lognormal_median(1.0, FORECAST_SIGMA)
    return r * err


# -- bootstrap -----------------------------------------------------------------


@bootstrap(order=20)
def init_stock(world) -> None:
    """Initial inventory for every SKU on sale at history start."""
    d0 = world.local_date()
    cover_days = world.cfg.company.warehouse.initial_cover_days
    lo, hi = INITIAL_COVER_JITTER
    for sku in world.ref.catalogue.active(d0):
        if world.store.has("stock", sku.sku_id):
            continue
        pts = [_expected(world, sku.sku_id, d0 + timedelta(days=k)) for k in (0, 14, 28)]
        rate = sum(pts) / len(pts)
        u = world.rng("wh.init", sku.sku_id).random()
        qty = _round_up(rate * cover_days * (lo + (hi - lo) * u), sku.units_per_case)
        world.store.put(
            "stock", sku.sku_id,
            StockPosition(sku_id=sku.sku_id, on_hand=qty, avg_daily_out=round(rate, 4)),
        )
        if qty > 0:
            _move(world, sku.sku_id, "adjustment", qty, reference="initial inventory")


# -- arrival sources -----------------------------------------------------------


def _local_dates(world, start: datetime, end: datetime) -> Iterator[date]:
    d = world.local_date(start) - timedelta(days=1)
    last = world.local_date(end) + timedelta(days=1)
    while d <= last:
        yield d
        d += timedelta(days=1)


@arrival_source("wh.plan")
def plan_arrivals(world, start: datetime, end: datetime):
    for d in _local_dates(world, start, end):
        if not is_working_day(world, d):
            continue
        due = world.at_local(d, PLAN_HOUR)
        if start < due <= end:
            yield Event(due=due, kind="wh.plan", key=f"wh.plan:{d:%Y%m%d}",
                        data={"date": d.isoformat()}, priority=PLAN_PRIORITY)


@arrival_source("wh.snapshot")
def snapshot_arrivals(world, start: datetime, end: datetime):
    for d in _local_dates(world, start, end):
        due = world.at_local(d, SNAPSHOT_HOUR, SNAPSHOT_MINUTE)
        if start < due <= end:
            yield Event(due=due, kind="wh.snapshot", key=f"wh.snapshot:{d:%Y%m%d}",
                        data={"date": d.isoformat()}, priority=SNAPSHOT_PRIORITY)


# -- planning ------------------------------------------------------------------


def production_order_no(sku_id: int, d: date) -> str:
    return f"PRD{d:%Y%m%d}{int(sku_id):05d}"


@handler("wh.plan")
def on_plan(world, ev) -> None:
    d = date.fromisoformat(ev.data["date"])
    cat = world.ref.catalogue
    store = world.store
    horizon = d + timedelta(days=PRELAUNCH_DAYS)
    for sku in cat.skus:  # sorted by sku_id
        if sku.launch_date <= horizon and (sku.delist_date is None or d < sku.delist_date):
            if not store.has("stock", sku.sku_id):
                store.put("stock", sku.sku_id, StockPosition(sku_id=sku.sku_id, on_hand=0))
    for sid, _ in list(store.items("stock")):
        _plan_sku(world, cat.get(int(sid)), d)


def _plan_sku(world, sku, d: date) -> None:
    store = world.store
    wh = world.cfg.company.warehouse
    pos = store.get("stock", sku.sku_id)
    if sku.delist_date is not None and d >= sku.delist_date:
        if d >= sku.delist_date + timedelta(days=DELIST_WRITE_OFF_DAYS) and pos.reserved == 0 \
                and pos.in_production == 0:
            if pos.on_hand > 0:
                _move(world, sku.sku_id, "scrap", -pos.on_hand, reference="delisted write-off")
            store.remove("stock", sku.sku_id)
        elif pos.backorder_qty:
            store.mut("stock", sku.sku_id).backorder_qty = 0
        return

    rate = _forecast(world, sku, pos, d)
    reorder_days, target_days = float(wh.reorder_cover_days), float(wh.target_cover_days)
    if sku.delist_date is not None:
        days_left = max(0, (sku.delist_date - d).days)
        reorder_days = min(reorder_days, days_left)
        target_days = min(target_days, days_left)
    position = pos.on_hand - pos.reserved + pos.in_production
    qty = 0
    if rate > 0 and position < rate * reorder_days:
        need = rate * target_days - position + pos.backorder_qty
        qty = _round_up(need, sku.units_per_case * LOT_CASES)
    if qty <= 0 and not pos.backorder_qty:
        return
    pos = store.mut("stock", sku.sku_id)
    pos.backorder_qty = 0
    if qty <= 0:
        return
    pos.in_production += qty
    r = world.rng("wh.production", sku.sku_id, d.isoformat())
    lead = max(3, round(wh.production_lead_days * r.lognormal_median(1.0, PRODUCTION_LEAD_SIGMA)))
    receipt_day = add_working_days(world, d, lead)
    due = world.at_local(receipt_day, RECEIPT_FIRST_HOUR) + timedelta(
        minutes=r.random() * RECEIPT_WINDOW_HOURS * 60.0
    )
    world.schedule(
        "wh.receipt", due, f"wh.receipt:{sku.sku_id}:{d:%Y%m%d}",
        {"sku_id": sku.sku_id, "qty": qty, "order": production_order_no(sku.sku_id, d)},
        priority=RECEIPT_PRIORITY,
    )


@handler("wh.receipt")
def on_receipt(world, ev) -> None:
    sku_id = int(ev.data["sku_id"])
    if _active_block(world, sku_id) is not None:  # held until the block ends (booked by wh.unblock)
        pos = _position(world, sku_id)
        pos.held_receipts.append(dict(ev.data))
        return
    _book_receipt(world, ev.data)


def _book_receipt(world, data: dict) -> None:
    sku_id, qty = int(data["sku_id"]), int(data["qty"])
    pos = _position(world, sku_id)
    pos.on_hand += qty
    pos.in_production = max(0, pos.in_production - qty)
    _move(world, sku_id, "goods_receipt", qty, reference=data.get("order"))


# -- hero quality block (hero_oos_substitution) ----------------------------------------


def _hero_api():
    import importlib

    return importlib.import_module("cpgsim.scenarios.hero_oos_substitution")


def _blocks(world) -> list:
    """Quality blocks of all hero_oos_substitution windows (cached per scenario version)."""
    sc = world.scenarios
    if sc is None or not hasattr(sc, "windows"):
        return []
    from . import promotions

    key = promotions._version(world)
    c = world.__dict__.get("_wh_blocks")
    if c is None or c[0] != key:
        c = (key, _hero_api().blocks(world))
        world.__dict__["_wh_blocks"] = c
    return c[1]


def _active_block(world, sku_id: int):
    """The block of hero ``sku_id`` active now (latest start first), or None."""
    hit = [b for b in _blocks(world) if b.sku_id == sku_id and b.start <= world.now < b.end]
    return max(hit, key=lambda b: b.start) if hit else None


def _block_available(world, pos: StockPosition, window_id: str) -> int:
    """Move the position's available units (``on_hand - reserved``) into the quality block."""
    q = pos.on_hand - pos.reserved
    if q <= 0:
        return 0
    pos.on_hand -= q
    pos.blocked += q
    _move(world, pos.sku_id, "adjustment", -q, reference=f"QI-BLOCK {window_id}")
    return q


@arrival_source("wh.quality_block")
def quality_block_arrivals(world, start: datetime, end: datetime):
    """Block and unblock events of the hero windows (pure in the current scenario windows)."""
    out = []
    for b in _blocks(world):
        data = {"window_id": b.window_id, "sku_id": b.sku_id}
        if start < b.start <= end:
            out.append(Event(due=b.start, kind="wh.block", key=f"wh.block:{b.window_id}", data=data,
                             priority=QI_BLOCK_PRIORITY))
        if start < b.end <= end:
            out.append(Event(due=b.end, kind="wh.unblock", key=f"wh.unblock:{b.window_id}", data=dict(data),
                             priority=QI_BLOCK_PRIORITY))
    return out


@bootstrap(order=21)
def bootstrap_quality_blocks(world) -> None:
    """A hero window already open at history start is blocked right away (after the stock bootstrap)."""
    hs = world.now
    for b in _blocks(world):
        if b.start <= hs < b.end:
            _apply_block(world, b)


def _apply_block(world, b) -> None:
    hero_mod = _hero_api()
    pos = _position(world, b.sku_id)
    q = _block_available(world, pos, b.window_id)
    w = world.scenarios.window(b.window_id)
    resolved_on = hero_mod.resolution_date(world, w).isoformat() if w is not None else None
    detail = {"window_id": b.window_id, "hero": b.sku_id, "sibling": b.sibling_id, "resolved_on": resolved_on}
    world.scenarios.record_effect(world, hero_mod.SCENARIO_ID, b.window_id, "sku", b.sku_id, "stock_block",
                                  q, detail)
    world.scenarios.record_effect(world, hero_mod.SCENARIO_ID, b.window_id, "sku", b.sibling_id,
                                  "substitution_target", b.share, detail)


@handler("wh.block")
def on_block(world, ev) -> None:
    wid, sku_id = ev.data["window_id"], int(ev.data["sku_id"])
    b = _active_block(world, sku_id)
    if b is None or b.window_id != wid:
        return  # window withdrawn or changed since the event was queued
    _apply_block(world, b)


@handler("wh.unblock")
def on_unblock(world, ev) -> None:
    wid, sku_id = ev.data["window_id"], int(ev.data["sku_id"])
    if not world.store.has("stock", sku_id):
        return
    if _active_block(world, sku_id) is not None:
        return  # another window of the same hero still blocks it; its unblock releases everything
    pos = world.store.mut("stock", sku_id)
    if pos.blocked > 0:
        _move(world, sku_id, "scrap", -pos.blocked, reference=f"QI-RECALL {wid}")
        pos.blocked = 0
    held, pos.held_receipts = list(pos.held_receipts), []
    for data in held:
        _book_receipt(world, data)


# -- snapshots -----------------------------------------------------------------


@handler("wh.snapshot")
def on_snapshot(world, ev) -> None:
    d = date.fromisoformat(ev.data["date"])
    store = world.store
    rows = []
    for sid, pos in list(store.items("stock")):
        rows.append(StockSnapshotRow(sku_id=pos.sku_id, on_hand=pos.on_hand, reserved=pos.reserved,
                                     in_production=pos.in_production))
        p = store.mut("stock", sid)
        p.avg_daily_out = round((1 - EWMA_ALPHA) * p.avg_daily_out + EWMA_ALPHA * p.issued_today, 4)
        p.issued_today = 0
    world.emit(StockSnapshotTaken(at=world.now, snapshot_date=d, rows=rows))
