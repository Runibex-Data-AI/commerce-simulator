"""Retailer and marketplace data feeds (phase 2).

Retail hands each DC's period sell-out to this module, which accumulates it per
feed period (``feed_acc`` entities) and delivers it as files at the sender's
schedule (``FeedDelivered``; restatements as new loads). See the phase-2
design, section 3.

Public API (called by ``domain/retail.py``)::

    record_sellout_week(world, channel, dc, week_start, sold, lost, demand, on_hand)
    record_marketplace_day(world, channel, dc, day, sold, demand, on_hand, on_order)

Flow
====

=========================  ====================================================
retail.on_sellout          ``record_sellout_week``: one DC's week goes into the
(Mon 00:30, per DC)        accumulator ``"<ch>:W:<week start>"`` (weekly) or
                           ``"<ch>:M:<retail period start>"`` (monthly, 4-4-5
                           retail months). The store/online split happens here
                           (rng ``("feeds.split", dc, week_start)``); values are
                           consumer gross (RRP less the retailer's promotion
                           discount that week) and net (gross / 1.19). The DC's
                           period-end stock is overwritten by every week, so
                           the last week of a retail month wins.
retail.on_sellout          ``record_marketplace_day``: both fulfilment centres
(daily 00:30, per FC)      go into ``"<ch>:D:<day>"`` (sold, demand incl. lost
                           demand, on hand, on order per SKU).
``feeds.deliver``          Arrival source ``feeds.delivery`` (pure in time and
(sender's schedule)        config): weekly files Monday, monthly files on the
                           configured weekday after the period's last Sunday,
                           marketplace reports the next morning. 3 % of retailer
                           files arrive 1-2 days late (rng ``feeds.delivery``).
                           The handler first re-sends a pending restatement of
                           the previous period (run 2, found by its derived
                           accumulator id), then delivers the current period
                           (run 1) and removes its accumulator.
=========================  ====================================================

Restatement (chaos defect ``sellout_restatement``, retailer periods only):
``chaos.hit(world, "sellout_restatement", channel, period_start)``. Run 1
under-reports one region's store rows by ``m ~ U(restatement_missing_share)``
(region and ``m`` from rng ``("feeds.restate", channel, period_start)``,
``units = floor(units * (1 - m))``, value pro rata, stock correct). The
accumulator stays (``meta["restate"]``) and is re-sent in full as run 2 with
the channel's next regular delivery. Truth is recorded when run 1 is emitted:
``("feed_load", <run-1 load id>, "restated", m, {...})``, where
``units_original`` is what run 1 reported for the region's store rows and
``units_restated`` what run 2 reports.

A period whose first day precedes the local history start date (a partial
first week, the perfumery's partial first retail month) is neither accumulated
nor delivered.
"""

from __future__ import annotations

import importlib
import math
from datetime import date, datetime, timedelta

from ..engine.queue import Event
from ..engine.registry import arrival_source, handler
from ..models.retail_calendar import retail_period
from ..models.seasonality import marketplace_sale_week
from ..writers import keys  # load ids (truth names the run-1 and run-2 loads)
from .events import FeedDelivered, MarketplaceInventoryRow, MarketplaceSalesRow, SelloutRow
from .model import FeedAccumulator

RETAIL_VAT = 0.19
SPLIT_SIGMA = 0.10  # log-normal spread of the online share per DC and week
VIEWS_SIGMA = 0.15  # log-normal noise of marketplace product page views
DELIVERY_PRIORITY = 60
DELIVERY_KIND = "feeds.deliver"

SELLOUT_WEEKLY = "sellout_weekly"
SELLOUT_MONTHLY = "sellout_monthly"
MP_SALES = "mp_sales_daily"
MP_INVENTORY = "mp_inventory_daily"
MP = "mp"  # feed type of the marketplace delivery event (sales and inventory together)

STORE, ONLINE = "store", "online"

#: Accumulator row layouts (``FeedAccumulator.rows``).
R_STORE, R_ONLINE, R_NET, R_GROSS, R_ON_HAND = range(5)  # "dc|sku" (W / M)
D_SOLD, D_DEMAND, D_ON_HAND, D_ON_ORDER = range(4)  # "sku" (D)


def _api(name: str):
    """Another domain module's public API (looked up at call time, so tests can swap it)."""
    return importlib.import_module(f"cpgsim.domain.{name}")


def _chaos():
    return importlib.import_module("cpgsim.chaos")


# ---------------------------------------------------------------------------
# Periods and ids
# ---------------------------------------------------------------------------


def acc_id(channel: str, period_type: str, period_start: date) -> str:
    """Id of the ``feed_acc`` entity of one feed period."""
    return f"{channel}:{period_type}:{period_start.isoformat()}"


def feed_type_of(frequency: str) -> str:
    """Feed type of a retailer's ``sellout_frequency`` (``mp`` for daily marketplace reports)."""
    if frequency == "weekly":
        return SELLOUT_WEEKLY
    if frequency == "monthly":
        return SELLOUT_MONTHLY
    if frequency == "daily":
        return MP
    raise ValueError(f"unknown sellout_frequency {frequency!r}")


def sellout_period(feed_type: str, week_start: date) -> tuple[str, date, date]:
    """``(period_type, first Monday, last Sunday)`` of the feed period containing ``week_start``."""
    if feed_type == SELLOUT_MONTHLY:
        _, _, first, last = retail_period(week_start)
        return "M", first, last
    return "W", week_start, week_start + timedelta(days=6)


def previous_period_start(feed_type: str, period_start: date) -> date:
    """First day of the period before the one starting on ``period_start``."""
    if feed_type == SELLOUT_MONTHLY:
        return retail_period(period_start - timedelta(days=1))[2]
    return period_start - timedelta(days=7)


def _hist_date(world) -> date:
    return world.local_date(world.history_start)


def _retailer_cfg(world, channel: str):
    rc = getattr(world.cfg.channels, channel, None)
    if rc is None:
        raise ValueError(f"unknown retailer channel {channel!r}")
    return rc


def _online_share(world, rc) -> float:
    s = rc.online_share
    return float(world.cfg.retail.online_share if s is None else s)


def _promo_discounts(world, channel: str, d: date) -> dict[int, float]:
    """Consumer discount per SKU of ``channel``'s promotions running on local day ``d``."""
    out: dict[int, float] = {}
    for p in _api("promotions").retail_promos(world, channel, d):
        for sku_id in p.sku_ids:
            if p.discount_pct > out.get(sku_id, 0.0):
                out[sku_id] = float(p.discount_pct)
    return out


def _get_or_create(world, aid: str, channel: str, ptype: str, start: date, end: date) -> FeedAccumulator:
    if world.store.has("feed_acc", aid):
        return world.store.mut("feed_acc", aid)
    acc = FeedAccumulator(channel=channel, period_type=ptype, period_start=start, period_end=end)
    world.store.put("feed_acc", aid, acc)
    return acc


def _mark_seen(acc: FeedAccumulator, token: str, dc: str) -> bool:
    """Register one (DC, week/day) contribution; False if it was already added."""
    seen = acc.meta.setdefault("seen", [])
    if token in seen:
        return False
    seen.append(token)
    if dc not in acc.dcs_reported:
        acc.dcs_reported.append(dc)
        acc.dcs_reported.sort()
    return True


# ---------------------------------------------------------------------------
# Public API (called by retail)
# ---------------------------------------------------------------------------


def record_sellout_week(
    world,
    channel: str,
    dc: str,
    week_start: date,
    sold: dict[int, int],
    lost: dict[int, int],
    demand: dict[int, int],
    on_hand: dict[int, int],
) -> None:
    """Add one DC's weekly sell-out (sold, lost, demand, period-end on hand per SKU) to its feed period.

    ``on_hand`` covers the DC's listed SKUs after the week's sales; every one of
    them gets a store row in the delivered file (stock 0 = out of stock).
    """
    rc = _retailer_cfg(world, channel)
    ftype = feed_type_of(rc.sellout_frequency)
    if ftype == MP:
        raise ValueError(f"{channel} reports daily marketplace data; use record_marketplace_day")
    ptype, start, end = sellout_period(ftype, week_start)
    if start < _hist_date(world):
        return
    acc = _get_or_create(world, acc_id(channel, ptype, start), channel, ptype, start, end)
    if not _mark_seen(acc, f"{dc}|{week_start.isoformat()}", dc):
        return

    cat = world.ref.catalogue
    rng = world.rng("feeds.split", dc, week_start.isoformat())
    share = min(max(_online_share(world, rc) * rng.lognormal_median(1.0, SPLIT_SIGMA), 0.0), 1.0)
    discounts = _promo_discounts(world, channel, week_start + timedelta(days=3))
    rows = acc.rows
    for sku_id in sorted(sold):
        units = int(sold[sku_id])
        if units <= 0:
            continue
        x = units * share
        base = int(x)
        online = base + (1 if rng.random() < x - base else 0)
        gross = units * cat.get(sku_id).rrp_eur * (1.0 - discounts.get(sku_id, 0.0))
        row = rows.setdefault(f"{dc}|{sku_id}", [0, 0, 0.0, 0.0, 0])
        row[R_STORE] += units - online
        row[R_ONLINE] += online
        row[R_NET] = round(row[R_NET] + gross / (1.0 + RETAIL_VAT), 6)
        row[R_GROSS] = round(row[R_GROSS] + gross, 6)
    for sku_id in sorted(on_hand):
        rows.setdefault(f"{dc}|{sku_id}", [0, 0, 0.0, 0.0, 0])
    prefix = f"{dc}|"
    for key, row in rows.items():
        if key.startswith(prefix):
            row[R_ON_HAND] = max(int(on_hand.get(int(key[len(prefix):]), 0)), 0)


def record_marketplace_day(
    world,
    channel: str,
    dc: str,
    day: date,
    sold: dict[int, int],
    demand: dict[int, int],
    on_hand: dict[int, int],
    on_order: dict[int, int],
) -> None:
    """Add one fulfilment centre's daily marketplace sales and inventory to the day's reports."""
    if day < _hist_date(world):
        return
    acc = _get_or_create(world, acc_id(channel, "D", day), channel, "D", day, day)
    if not _mark_seen(acc, dc, dc):
        return
    rows = acc.rows
    for sku_id in sorted(set(sold) | set(demand) | set(on_hand) | set(on_order)):
        row = rows.setdefault(str(sku_id), [0, 0, 0, 0])
        row[D_SOLD] += int(sold.get(sku_id, 0))
        row[D_DEMAND] += int(demand.get(sku_id, 0))
        row[D_ON_HAND] += max(int(on_hand.get(sku_id, 0)), 0)
        row[D_ON_ORDER] += max(int(on_order.get(sku_id, 0)), 0)


# ---------------------------------------------------------------------------
# Delivery schedule (pure: seed, config, time)
# ---------------------------------------------------------------------------


def _at_local_hours(world, d: date, hours: float) -> datetime:
    secs = min(max(int(round(hours * 3600.0)), 0), 24 * 3600 - 1)
    return world.at_local(d, secs // 3600, (secs // 60) % 60, secs % 60)


def _nominal(channel_freq: str, d: date, hist: date, monthly_weekday: int):
    """``(feed_type, period_start, period_end)`` nominally delivered on local date ``d`` (or None)."""
    if channel_freq == "weekly":
        if d.weekday() != 0:
            return None
        start = d - timedelta(days=7)
        out = (SELLOUT_WEEKLY, start, d - timedelta(days=1))
    elif channel_freq == "monthly":
        last = d - timedelta(days=1 + monthly_weekday)
        if last.weekday() != 6:
            return None
        _, _, first, period_last = retail_period(last)
        if period_last != last:
            return None
        out = (SELLOUT_MONTHLY, first, last)
    elif channel_freq == "daily":
        day = d - timedelta(days=1)
        out = (MP, day, day)
    else:
        raise ValueError(f"unknown sellout_frequency {channel_freq!r}")
    return out if out[1] >= hist else None


def delivery_time(world, channel: str, feed_type: str, period_start: date, period_end: date) -> tuple[datetime, int]:
    """When a feed file arrives (aware UTC) and how many days late it is (pure)."""
    fc = world.cfg.feeds
    if feed_type == MP:
        rng = world.rng("feeds.delivery", channel, MP, period_start.isoformat())
        lo, hi = fc.marketplace_report_hours
        return _at_local_hours(world, period_end + timedelta(days=1), lo + (hi - lo) * rng.random()), 0
    rng = world.rng("feeds.delivery", channel, feed_type, period_start.isoformat())
    late = rng.random() < fc.late_delivery_rate
    k = rng.randint(min(fc.late_delivery_days), max(fc.late_delivery_days))
    u = rng.random()
    if feed_type == SELLOUT_MONTHLY:
        nominal = period_end + timedelta(days=1 + fc.monthly_delivery_weekday)
        lo, hi = fc.monthly_delivery_hours
    else:
        nominal = period_end + timedelta(days=1)
        lo, hi = fc.weekly_delivery_hours.get(channel, fc.default_weekly_delivery_hours)
    days_late = k if late else 0
    return _at_local_hours(world, nominal + timedelta(days=days_late), lo + (hi - lo) * u), days_late


def delivery_key(channel: str, feed_type: str, period_start: date) -> str:
    return f"feeds.deliver:{channel}:{feed_type}:{period_start:%Y%m%d}"


@arrival_source("feeds.delivery")
def delivery_arrivals(world, start: datetime, end: datetime):
    retailers = world.cfg.channels.retailers()
    if not retailers:
        return []
    fc = world.cfg.feeds
    hist = _hist_date(world)
    max_late = max(max(fc.late_delivery_days), 0)
    # A file due in (start, end] was nominally due up to max_late days earlier;
    # a due time never lies before its nominal date (lateness only adds days).
    d = world.local(start).date() - timedelta(days=max_late + 1)
    d1 = world.local(end).date()
    out = []
    while d <= d1:
        for channel, rc in retailers.items():
            nom = _nominal(rc.sellout_frequency, d, hist, fc.monthly_delivery_weekday)
            if nom is None:
                continue
            ftype, ps, pe = nom
            due, days_late = delivery_time(world, channel, ftype, ps, pe)
            if start < due <= end:
                out.append(Event(
                    due=due, kind=DELIVERY_KIND, key=delivery_key(channel, ftype, ps),
                    data={"channel": channel, "feed_type": ftype, "period_start": ps.isoformat(),
                          "period_end": pe.isoformat(), "days_late": days_late},
                    priority=DELIVERY_PRIORITY,
                ))
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Delivery handler
# ---------------------------------------------------------------------------


@handler(DELIVERY_KIND)
def on_deliver(world, ev) -> None:
    channel = ev.data["channel"]
    ftype = ev.data["feed_type"]
    ps = date.fromisoformat(ev.data["period_start"])
    if ftype == MP:
        _deliver_marketplace(world, channel, ps)
    else:
        _deliver_sellout(world, channel, ftype, ps)


def sellout_rows(acc: FeedAccumulator, region_dc: str | None = None, missing: float = 0.0) -> list[SelloutRow]:
    """Neutral rows of a sell-out period, sorted by DC, sales channel and SKU.

    Every listed article gets a store row (with the DC's period-end stock);
    online rows only where units > 0. With ``region_dc``, that DC's store rows
    are under-reported by the share ``missing`` (floor of the units, value pro
    rata; stock unchanged).
    """
    keyed = []
    for key, row in acc.rows.items():
        dc, sku = key.split("|")
        keyed.append((dc, int(sku), row))
    keyed.sort(key=lambda x: (x[0], x[1]))
    out: list[SelloutRow] = []
    online_rows: list[SelloutRow] = []
    current_dc = None

    def flush_online():
        out.extend(online_rows)
        online_rows.clear()

    for dc, sku_id, row in keyed:
        if dc != current_dc:
            flush_online()
            current_dc = dc
        su, ou = int(row[R_STORE]), int(row[R_ONLINE])
        net, gross = float(row[R_NET]), float(row[R_GROSS])
        total = su + ou
        s_net = net * su / total if total else 0.0
        s_gross = gross * su / total if total else 0.0
        units = su
        if region_dc is not None and dc == region_dc and su > 0:
            units = int(math.floor(su * (1.0 - missing)))
            s_net, s_gross = s_net * units / su, s_gross * units / su
        out.append(SelloutRow(dc_account=dc, sales_channel=STORE, sku_id=sku_id, units=units,
                              value_net_eur=round(s_net, 2), value_gross_eur=round(s_gross, 2),
                              stock_units=int(row[R_ON_HAND])))
        if ou > 0:
            online_rows.append(SelloutRow(dc_account=dc, sales_channel=ONLINE, sku_id=sku_id, units=ou,
                                          value_net_eur=round(net * ou / total, 2),
                                          value_gross_eur=round(gross * ou / total, 2), stock_units=None))
    flush_online()
    return out


def _emit_sellout(world, acc: FeedAccumulator, ftype: str, run: int, rows: list[SelloutRow]) -> None:
    world.emit(FeedDelivered(
        at=world.now, channel=acc.channel, feed_type=ftype, period_start=acc.period_start,
        period_end=acc.period_end, run=run, restates_run=(run - 1) if run > 1 else None, sellout=rows,
    ))


def _deliver_sellout(world, channel: str, ftype: str, ps: date) -> None:
    ptype = "M" if ftype == SELLOUT_MONTHLY else "W"
    # 1. A restatement of the previous period goes out with this regular delivery.
    prev_id = acc_id(channel, ptype, previous_period_start(ftype, ps))
    prev = world.store.get("feed_acc", prev_id)
    if prev is not None and prev.meta.get("restate"):
        _emit_sellout(world, prev, ftype, 2, sellout_rows(prev))
        world.store.remove("feed_acc", prev_id)

    # 2. The period itself.
    aid = acc_id(channel, ptype, ps)
    acc = world.store.get("feed_acc", aid)
    if acc is None:
        return
    chaos = _chaos()
    dcs = sorted(acc.dcs_reported)
    if not dcs or not chaos.hit(world, "sellout_restatement", channel, ps):
        _emit_sellout(world, acc, ftype, 1, sellout_rows(acc))
        world.store.remove("feed_acc", aid)
        return

    rng = world.rng("feeds.restate", channel, ps.isoformat())
    region = dcs[int(rng.random() * len(dcs)) % len(dcs)]
    lo, hi = world.cfg.feeds.restatement_missing_share
    m = round(lo + (hi - lo) * rng.random(), 4)
    true_rows = sellout_rows(acc)
    reported = sellout_rows(acc, region, m)
    _emit_sellout(world, acc, ftype, 1, reported)
    acc = world.store.mut("feed_acc", aid)
    run1 = keys.feed_load_id(channel, ftype, ps, 1)
    acc.meta.update({"restate": True, "run1_load": run1, "region_dc": region, "missing_share": m})

    def region_units(rows):
        return sum(r.units for r in rows if r.dc_account == region and r.sales_channel == STORE)

    chaos.record(world, "sellout_restatement", "feed_load", run1, "restated", m, {
        "restatement_load_id": keys.feed_load_id(channel, ftype, ps, 2),
        "channel": channel,
        "period_start": ps.isoformat(),
        "region_dc": region,
        "units_original": region_units(reported),
        "units_restated": region_units(true_rows),
    })


def _conversion(world, sku_id: int) -> float:
    """Units per product page view of one SKU on the marketplace (pure in the SKU)."""
    lo, hi = world.cfg.feeds.marketplace_conversion
    cache = world.__dict__.setdefault("_feeds_conv", {})
    key = (world.seed, lo, hi, sku_id)
    c = cache.get(key)
    if c is None:
        c = lo + (hi - lo) * world.rng("feeds.mp.conv", sku_id).random()
        cache[key] = c
    return c


def marketplace_price(world, channel: str, sku_id: int, day: date, discounts: dict[int, float] | None = None) -> float:
    """Gross consumer price on the marketplace on ``day`` (unrounded)."""
    fc = world.cfg.feeds
    price = world.ref.catalogue.get(sku_id).rrp_eur * fc.marketplace_price_index
    lo, hi = marketplace_sale_week(day.year)
    if lo <= day <= hi:
        price *= 1.0 - fc.marketplace_sale_discount
    if discounts is None:
        discounts = _promo_discounts(world, channel, day)
    return price * (1.0 - discounts.get(sku_id, 0.0))


def _deliver_marketplace(world, channel: str, day: date) -> None:
    aid = acc_id(channel, "D", day)
    acc = world.store.get("feed_acc", aid)
    if acc is None:
        return
    discounts = _promo_discounts(world, channel, day)
    rng = world.rng("feeds.mp.views", day.isoformat())
    sales: list[MarketplaceSalesRow] = []
    inventory: list[MarketplaceInventoryRow] = []
    for sku_id in sorted(int(k) for k in acc.rows):
        sold, demand, on_hand, on_order = (int(x) for x in acc.rows[str(sku_id)])
        noise = rng.lognormal_median(1.0, VIEWS_SIGMA)
        views = int(round(demand / _conversion(world, sku_id) * noise)) if demand > 0 else 0
        if sold > 0 or views > 0:
            price = marketplace_price(world, channel, sku_id, day, discounts)
            sales.append(MarketplaceSalesRow(sku_id=sku_id, units=sold, revenue_gross_eur=round(sold * price, 2),
                                             glance_views=views))
        if on_hand > 0 or on_order > 0:
            inventory.append(MarketplaceInventoryRow(sku_id=sku_id, sellable_units=on_hand, inbound_units=on_order))
    world.emit(FeedDelivered(at=world.now, channel=channel, feed_type=MP_SALES, period_start=day,
                             period_end=day, run=1, mp_sales=sales))
    world.emit(FeedDelivered(at=world.now, channel=channel, feed_type=MP_INVENTORY, period_start=day,
                             period_end=day, run=1, mp_inventory=inventory))
    world.store.remove("feed_acc", aid)
