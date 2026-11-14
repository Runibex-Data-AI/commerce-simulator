"""``hero_oos_substitution``: a hero SKU runs out and a sibling absorbs part of its demand.

The story: the central warehouse puts all available stock of one hero SKU
under a quality block for ``duration_weeks`` (default three weeks) and scraps
it at the end (a recall). Every channel runs dry: web orders backorder, retail
purchase orders are cut, retailer distribution centres sell out one after the
other (the lowest cover first). ``substitution_share`` (default 40 %) of the
hero's demand moves to its nearest sibling; the rest is lost. Finding it
means joining stock levels and movements (``wms``) with sell-out (``feeds``)
and cut purchase orders and service penalties (``erp``).

Parameters (defaults in ``config/scenarios/hero_oos_substitution.yaml``):

``sku``
    ``auto`` (default), a SKU id or a web shop SKU code. ``auto`` picks the
    most popular hero active at the window start whose first active sibling
    is listed by the reference retailer (Drogerie Kranz if enabled, else the
    first enabled retailer).
``duration_weeks``
    Length of the stock-out; the window closes by itself after it.
``substitution_share``
    Share of the hero's demand moved to the sibling (web: expected units;
    retail: lost units).
``recovery_weeks``
    Weeks after the window during which retailer stock recovers (retail
    substitution and the AC1 exclusion span).

The pair (hero, sibling) is resolved **once per window** at the local date of
``max(window start, history start)`` and used unchanged for the whole window
and recovery, even if a sibling launches or is delisted meanwhile. The
business modules ask the helpers below (warehouse: ``blocks`` and
``active_block``; retail: ``retail_blocks``; demand plan:
``world.scenarios.transfers("sku_demand", ...)``).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from .base import Scenario, ScenarioParamError, as_int, as_share, register

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from .manager import ScenarioWindow

log = logging.getLogger("cpgsim.scenarios")

SCENARIO_ID = "hero_oos_substitution"
#: Reference retailer for ``sku: auto`` when enabled; else the first enabled retailer.
REFERENCE_RETAILER = "drogerie_kranz"


class Block(NamedTuple):
    """One window's stock-out of a hero (aware UTC ``start`` inclusive, ``end`` exclusive)."""

    window_id: str
    sku_id: int  # the hero
    sibling_id: int
    share: float  # substitution share
    start: datetime
    end: datetime


@register
class HeroOosSubstitution(Scenario):
    id = SCENARIO_ID
    description = "A hero SKU runs out for weeks; a sibling absorbs part of its demand, the rest is lost."
    defaults = {"sku": "auto", "duration_weeks": 3, "substitution_share": 0.40, "recovery_weeks": 6}
    factors = ()

    def normalize(self, params: dict[str, Any] | None, cfg: "Config | None" = None) -> dict[str, Any]:
        p = super().normalize(params, cfg)
        sku = p.get("sku")
        if sku is None or (isinstance(sku, str) and not sku.strip()):
            raise ScenarioParamError(f"scenario {self.id}: sku is required ('auto', a SKU id or a SKU code)")
        if isinstance(sku, bool):
            raise ScenarioParamError(f"scenario {self.id}: sku must be 'auto', a SKU id or a SKU code, got {sku!r}")
        if isinstance(sku, (int, float)):
            p["sku"] = as_int(self.id, "sku", sku)
        else:
            s = str(sku).strip()
            p["sku"] = "auto" if s.casefold() == "auto" else (int(s) if s.isdigit() else s)
        p["duration_weeks"] = as_int(self.id, "duration_weeks", p.get("duration_weeks"))
        p["substitution_share"] = as_share(self.id, "substitution_share", p.get("substitution_share"))
        p["recovery_weeks"] = as_int(self.id, "recovery_weeks", p.get("recovery_weeks"), minimum=0)
        return p

    def duration(self, params: dict[str, Any]) -> timedelta | None:
        return timedelta(weeks=int(params["duration_weeks"]))

    def transfers(
        self, name: str, window: "ScenarioWindow", t: datetime, ctx: dict[str, Any]
    ) -> list[tuple[int, int, float]]:
        if name != "sku_demand" or ctx.get("channel") != "web" or not window.contains(t):
            return []
        pair = resolve(ctx["world"], window)
        if pair is None:
            return []
        hero, sibling = pair
        return [(hero, sibling, float(window.params["substitution_share"]))]


# -- resolution ------------------------------------------------------------------


def _cache(world) -> dict:
    d = world.__dict__.setdefault("_scenario_cache", {})
    return d.setdefault(SCENARIO_ID, {})


def _reference_retailer(world) -> str | None:
    from ..domain.accounts import RETAILER_CHANNELS

    enabled = world.cfg.channels.retailers()
    if REFERENCE_RETAILER in enabled:
        return REFERENCE_RETAILER
    for ch in RETAILER_CHANNELS:
        if ch in enabled:
            return ch
    return None


def resolution_date(world, window: "ScenarioWindow") -> date:
    """Local date ``d0`` of ``max(window start, history start)``."""
    t = window.start
    hs = getattr(world, "history_start", None)
    if hs is not None and hs > t:
        t = hs
    return world.local_date(t)


def _resolve_uncached(world, window: "ScenarioWindow", d0: date) -> tuple[int, int] | None:
    cat = world.ref.catalogue
    want = window.params.get("sku", "auto")
    if want == "auto":
        ref = _reference_retailer(world)
        listed = set(cat.retailer_assortment(ref, d0)) if ref is not None else None
        heroes = sorted((s for s in cat.active(d0) if s.hero), key=lambda s: (-s.popularity, s.sku_id))
        for hero in heroes:
            sibs = cat.siblings(hero.sku_id, d0)
            if not sibs:
                continue
            if listed is not None and sibs[0].sku_id not in listed:
                continue
            return hero.sku_id, sibs[0].sku_id
        return None
    hero = None
    if isinstance(want, int):
        try:
            hero = cat.get(want)
        except KeyError:
            hero = None
    else:
        hero = next((s for s in cat.skus if s.code == want), None)
    if hero is None:
        return None
    sibs = cat.siblings(hero.sku_id, d0)
    return (hero.sku_id, sibs[0].sku_id) if sibs else None


def resolve(world, window: "ScenarioWindow") -> tuple[int, int] | None:
    """``(hero_id, sibling_id)`` of the window, resolved once at :func:`resolution_date`; pure.

    ``None`` (with one warning) when the configured SKU is unknown or has no
    active sibling.
    """
    from ..domain import promotions  # lazy: domain modules import the scenarios

    key = (window.window_id, promotions._version(world))
    c = _cache(world)
    if key in c:
        return c[key]
    if len(c) > 256:
        c.clear()
    pair = _resolve_uncached(world, window, resolution_date(world, window))
    if pair is None:
        warned = world.__dict__.setdefault("_scenario_warned", set())
        if window.window_id not in warned:
            warned.add(window.window_id)
            log.warning(
                "scenario %s (%s): no hero with an active sibling for sku=%r on %s; no effect",
                SCENARIO_ID, window.window_id, window.params.get("sku"), resolution_date(world, window),
            )
    c[key] = pair
    return pair


# -- helpers for the business modules -----------------------------------------------


def blocks(world) -> list[Block]:
    """Stock-out blocks of all windows (past, current and future); pure in the windows."""
    sm = world.scenarios
    if sm is None:
        return []
    out: list[Block] = []
    for w in sm.windows(SCENARIO_ID):
        if w.end is None:
            continue  # self-limited windows always have an end once the history start is known
        start = w.start
        if w.from_history_start and getattr(world, "history_start", None) is not None:
            start = world.history_start
        if w.end <= start:
            continue
        pair = resolve(world, w)
        if pair is None:
            continue
        out.append(Block(w.window_id, pair[0], pair[1], float(w.params["substitution_share"]), start, w.end))
    return out


def active_block(world, sku_id: int, t: datetime) -> Block | None:
    """The block of hero ``sku_id`` active at ``t`` (latest start first), or None."""
    hit = [b for b in blocks(world) if b.sku_id == sku_id and b.start <= t < b.end]
    return max(hit, key=lambda b: b.start) if hit else None


def recovery_end(world, block: Block) -> datetime:
    """End of the block's retail recovery span (``end + recovery_weeks``, local wall clock)."""
    w = world.scenarios.window(block.window_id)
    weeks = int(w.params.get("recovery_weeks", 0)) if w is not None else 0
    return (block.end.astimezone(world.tz) + timedelta(weeks=weeks)).astimezone(block.end.tzinfo)


def retail_blocks(world, channel: str, d: date | datetime) -> list[Block]:
    """Blocks whose span ``[start, end + recovery_weeks)`` contains ``d`` (all retail channels).

    A ``date`` is compared with the local dates of the span.
    """
    out = []
    for b in blocks(world):
        rec = recovery_end(world, b)
        if isinstance(d, datetime):
            inside = b.start <= d < rec
        else:
            inside = world.local_date(b.start) <= d < world.local_date(rec)
        if inside:
            out.append(b)
    return out
