"""Master data: materials, B2B accounts, customer materials, FX rates, standard costs.

Public API (pure functions of seed, config and history start):

* ``fx_rate(world, currency, d)``: EUR per 1 unit of ``currency`` for the month
  of ``d`` (``EUR`` -> 1.0). CHF follows a deterministic, mean-reverting monthly
  random walk around 1.05.
* ``std_cost(world, sku_id, d)``: standard cost valid on ``d``. The catalogue's
  ``Sku.std_cost_eur`` is the cost at history start; it is revised every
  1 January after history start by +1..4 % (deterministic per SKU and year).

Emission:

* bootstrap (order 10): ``B2BAccountCreated`` for every account (with a
  deterministic account-opening date ``opened_on`` 5-13 years before history
  start, earlier than any back-dated web customer), ``ServiceMaterialListed``
  for the shipping service material (the web freight item), ``SkuListed`` for
  SKUs listed by history start (listing date = launch date - 60 days, carried
  as ``listed_on``; SKUs delisted more than a year before history start are
  skipped), ``CustomerMaterialListed`` for each retailer's eligible
  assortment among them, ``FxRatePublished`` for the start month.
* arrival ``master.fx``: 1st of every month 00:05 local -> ``FxRatePublished``.
* arrival ``master.cost``: 1 January 00:10 local -> ``StandardCostRevised`` per
  listed SKU.
* arrival ``master.listing``: listing date 06:00 local -> ``SkuListed`` (with
  the standard cost valid that day) and ``CustomerMaterialListed`` for every
  retailer that lists the SKU at some point.

Each event moment is either covered by the bootstrap (moment <= history start)
or by the arrival source (moment > history start), never both.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime, timedelta

from ..engine.queue import Event
from ..engine.registry import arrival_source, bootstrap, handler
from ..engine.rng import Rng
from .events import (
    B2BAccountCreated,
    CustomerMaterialListed,
    FxRatePublished,
    ServiceMaterialListed,
    SkuListed,
    StandardCostRevised,
)
from .customers import _Lazy
from .model import Sku

chaos = _Lazy("cpgsim.chaos")

LISTING_LEAD_DAYS = 60
LISTING_HOUR = 6
FX_HOUR, FX_MINUTE = 0, 5
COST_HOUR, COST_MINUTE = 0, 10
DELISTED_SKIP_DAYS = 365

# FX: x_k = PHI * x_{k-1} + N(0, SIGMA); rate = BASE * exp(x_k), monthly from FX_EPOCH.
FX_BASE: dict[str, float] = {"CHF": 1.05}
FX_PHI = 0.88
FX_SIGMA = 0.013
FX_EPOCH = (2015, 1)

COST_REVISION_RANGE = (0.01, 0.04)

# Account opening dates of the seeded B2B accounts, in days before history
# start. The minimum lies before every back-dated web customer (warm-start
# customers reach back at most ~1,760 days: subscription age <= 1,400 days plus
# up to 360 days before the subscription) so the collective web accounts exist
# before the first customer who bought through them.
ACCOUNT_OPENED_DAYS = (1830, 4750)
SERVICE_MATERIALS = ("shipping",)

_fx_cache: dict[tuple[int, str], list[float]] = {}
_rev_cache: dict[tuple[int, int, int], float] = {}
_cost_cache: dict[tuple, float] = {}
_listing_index: dict[int, tuple[object, dict[date, list[int]]]] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _month_index(d: date) -> int:
    return (d.year - FX_EPOCH[0]) * 12 + (d.month - FX_EPOCH[1])


def _fx_walk(seed: int, currency: str, k: int) -> float:
    walk = _fx_cache.setdefault((seed, currency), [0.0])
    while len(walk) <= k:
        i = len(walk)
        eps = Rng(seed, "master.fx", currency, i).gauss(0.0, FX_SIGMA)
        walk.append(FX_PHI * walk[-1] + eps)
    return walk[k]


def fx_rate(world, currency: str, d: date) -> float:
    """EUR per 1 unit of ``currency`` for the month of ``d`` (EUR -> 1.0)."""
    if currency == "EUR":
        return 1.0
    try:
        base = FX_BASE[currency]
    except KeyError:
        raise ValueError(f"no exchange rate model for currency {currency!r}") from None
    k = max(_month_index(d), 0)
    return round(base * math.exp(_fx_walk(world.seed, currency, k)), 5)


def _revision(seed: int, sku_id: int, year: int) -> float:
    key = (seed, sku_id, year)
    r = _rev_cache.get(key)
    if r is None:
        lo, hi = COST_REVISION_RANGE
        r = lo + (hi - lo) * Rng(seed, "master.cost", sku_id, year).random()
        _rev_cache[key] = r
    return r


def _first_revision_year(world) -> int:
    """First calendar year whose 1 January revision happens after history start."""
    hs = world.history_start
    y = world.local(hs).year
    if world.at_local(date(y, 1, 1), COST_HOUR, COST_MINUTE) <= hs:
        y += 1
    return y


def std_cost(world, sku_id: int, d: date) -> float:
    """Standard cost (EUR per unit) valid on local date ``d``."""
    key = (world.seed, world.history_start, sku_id, d.year)
    hit = _cost_cache.get(key)
    if hit is None:
        sku = world.ref.catalogue.get(sku_id)
        f = 1.0
        for y in range(_first_revision_year(world), d.year + 1):
            f *= 1.0 + _revision(world.seed, sku_id, y)
        hit = round(sku.std_cost_eur * f, 2)
        if len(_cost_cache) > 100_000:
            _cost_cache.clear()
        _cost_cache[key] = hit
    return hit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def listing_date(sku: Sku) -> date:
    return sku.launch_date - timedelta(days=LISTING_LEAD_DAYS)


def _listings_by_date(world) -> dict[date, list[int]]:
    cat = world.ref.catalogue
    hit = _listing_index.get(id(cat))
    if hit is None or hit[0] is not cat:
        idx: dict[date, list[int]] = {}
        for sku in cat.skus:
            idx.setdefault(listing_date(sku), []).append(sku.sku_id)
        hit = (cat, idx)
        _listing_index[id(cat)] = hit
    return hit[1]


def account_opened_on(world, account_id: str) -> date:
    """Deterministic account-opening date of a seeded account (before history start)."""
    lo, hi = ACCOUNT_OPENED_DAYS
    rng = Rng(world.seed, "master.account_opened", account_id)
    days = lo + int(rng.random() * (hi - lo))
    return world.local(world.history_start).date() - timedelta(days=days)


def _listing_moment(world, sku: Sku) -> datetime:
    return world.at_local(listing_date(sku), LISTING_HOUR)


def retailer_lists(world, channel: str, sku: Sku) -> bool:
    """Whether a retailer channel lists ``sku`` at some point of its life.

    Mirrors ``Catalogue.retailer_assortment``: eligible brand and either a hero
    or a listing key above the channel's fixed threshold (gift sets count: they
    are listed every November and December).
    """
    cat = world.ref.catalogue
    rc = cat._retailer_cfg(channel)
    if not cat._eligible(sku, rc):
        return False
    return sku.hero or cat._keys(channel)[sku.sku_id] >= cat._threshold(channel)


def _retailers(world) -> list[str]:
    return list(world.cfg.channels.retailers())


def _emit_customer_materials(world, sku: Sku) -> None:
    cat = world.ref.catalogue
    for channel in _retailers(world):
        if retailer_lists(world, channel, sku):
            if chaos.knmt_suppressed(world, channel, sku.sku_id):
                continue  # retailer article without a customer-material mapping (chaos; truth recorded)
            world.emit(
                CustomerMaterialListed(
                    at=world.now,
                    account_id=world.ref.accounts.retailer(channel).account_id,
                    sku_id=sku.sku_id,
                    customer_article=cat.customer_article(channel, sku.sku_id),
                )
            )


def _listed_sku(world, sku: Sku, d: date) -> Sku:
    """Copy of the shared catalogue SKU carrying the standard cost valid on ``d``."""
    cost = std_cost(world, sku.sku_id, d)
    if cost == sku.std_cost_eur:
        return sku
    return dataclasses.replace(sku, std_cost_eur=cost)


def _emit_fx(world, month_start: date) -> None:
    for cur in sorted(FX_BASE):
        world.emit(
            FxRatePublished(
                at=world.now,
                from_currency=cur,
                to_currency="EUR",
                valid_from=month_start,
                rate=fx_rate(world, cur, month_start),
            )
        )


def _local_dates(world, start: datetime, end: datetime) -> list[date]:
    """Local dates touched by the slice ``(start, end]``."""
    d0 = world.local(start).date()
    d1 = world.local(end).date()
    out = []
    d = d0
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@bootstrap(order=10)
def bootstrap_master_data(world) -> None:
    hs = world.history_start
    hs_date = world.local(hs).date()
    opened: list[date] = []
    for acc in world.ref.accounts.all:
        d = account_opened_on(world, acc.account_id)
        opened.append(d)
        world.emit(B2BAccountCreated(at=world.now, account=acc, opened_on=d))
    # Service materials were created together with the first sales accounts.
    since = min(opened) if opened else hs_date
    for service in SERVICE_MATERIALS:
        world.emit(ServiceMaterialListed(at=world.now, service=service, listed_on=since))
    skip_before = hs_date - timedelta(days=DELISTED_SKIP_DAYS)
    for sku in world.ref.catalogue.skus:
        if _listing_moment(world, sku) > hs:
            continue  # listed later by master.listing
        if sku.delist_date is not None and sku.delist_date < skip_before:
            continue
        world.emit(SkuListed(at=world.now, sku=_listed_sku(world, sku, hs_date), listed_on=listing_date(sku)))
        _emit_customer_materials(world, sku)
    month_start = hs_date.replace(day=1)
    if world.at_local(month_start, FX_HOUR, FX_MINUTE) <= hs:
        _emit_fx(world, month_start)


# ---------------------------------------------------------------------------
# Arrival sources (pure: seed, config, time)
# ---------------------------------------------------------------------------


@arrival_source("master.fx")
def fx_arrivals(world, start: datetime, end: datetime):
    out = []
    for d in _local_dates(world, start, end):
        if d.day != 1:
            continue
        t = world.at_local(d, FX_HOUR, FX_MINUTE)
        if start < t <= end:
            out.append(Event(due=t, kind="master.fx", key=f"master.fx:{d:%Y%m}",
                             data={"month": d.isoformat()}, priority=10))
    return out


@arrival_source("master.cost")
def cost_arrivals(world, start: datetime, end: datetime):
    out = []
    for d in _local_dates(world, start, end):
        if d.month != 1 or d.day != 1:
            continue
        t = world.at_local(d, COST_HOUR, COST_MINUTE)
        if start < t <= end:
            out.append(Event(due=t, kind="master.cost", key=f"master.cost:{d.year}",
                             data={"year": d.year}, priority=10))
    return out


@arrival_source("master.listing")
def listing_arrivals(world, start: datetime, end: datetime):
    idx = _listings_by_date(world)
    out = []
    for d in _local_dates(world, start, end):
        ids = idx.get(d)
        if not ids:
            continue
        t = world.at_local(d, LISTING_HOUR)
        if start < t <= end:
            for sku_id in ids:
                out.append(Event(due=t, kind="master.listing", key=f"master.listing:{sku_id}",
                                 data={"sku_id": sku_id}, priority=10))
    return out


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@handler("master.fx")
def on_fx(world, ev) -> None:
    _emit_fx(world, date.fromisoformat(ev.data["month"]))


@handler("master.cost")
def on_cost(world, ev) -> None:
    year = int(ev.data["year"])
    valid_from = date(year, 1, 1)
    rev_moment = world.at_local(valid_from, COST_HOUR, COST_MINUTE)
    skip_before = world.local(world.history_start).date() - timedelta(days=DELISTED_SKIP_DAYS)
    for sku in world.ref.catalogue.skus:
        if _listing_moment(world, sku) > rev_moment:
            continue  # not listed yet; its SkuListed will carry the revised cost
        if sku.delist_date is not None and (
            sku.delist_date <= valid_from or sku.delist_date < skip_before
        ):
            continue
        world.emit(
            StandardCostRevised(
                at=world.now,
                sku_id=sku.sku_id,
                std_cost_eur=std_cost(world, sku.sku_id, valid_from),
                valid_from=valid_from,
            )
        )


@handler("master.listing")
def on_listing(world, ev) -> None:
    sku = world.ref.catalogue.get(int(ev.data["sku_id"]))
    world.emit(SkuListed(at=world.now, sku=_listed_sku(world, sku, world.local_date()), listed_on=listing_date(sku)))
    _emit_customer_materials(world, sku)
