"""Scenario windows, factors and ground-truth recording.

Scenarios are the intended way to create demo stories. A scenario is a
time-bounded modifier on the business models (see ``scenarios/base.py``); this
module decides *when* each one is active and writes *what* it did to the
``truth`` schema.

Where windows come from
-----------------------

* **Config** (``scenarios:`` in the YAML, or ``scenarios_append`` in a private
  overlay). ``start`` and ``end`` are local calendar dates in the business
  time zone: the window opens at 00:00 local on ``start`` and stays open
  through the whole ``end`` day (it closes at 00:00 local the day after).
  Omit ``end`` for an open-ended window, omit ``start`` for "from the start of
  history". ``enabled: false`` keeps an entry without using it. Window id:
  ``"<scenario_id>:config:<position in the scenarios list>"``.
* **CLI overrides** (``sim scenario enable|disable <id>``), stored in
  ``sim.scenario_overrides`` and re-read by the runner every live tick via
  :meth:`ScenarioManager.load_overrides`. ``enable`` opens a window at the
  switch time with the parameters the CLI wrote (generic definition from
  ``config/scenarios/<id>.yaml`` plus ``--param``; anything missing is filled
  from the implementation defaults). ``disable`` closes the most recently
  started window of that scenario that is open at the switch time (a config
  window too). Window id: ``"<scenario_id>:cli:<override id>"``.

How business modules use it
---------------------------

``world.scenarios.factor(name, t, **ctx)`` is the product of the factors of all
windows active at ``t`` whose implementation provides ``name`` and applies to
``ctx``; it is exactly ``1.0`` when nothing applies. Pass
``world=world, record=(entity_type, entity_id)`` at decision points that affect
one entity and every contributing window writes a row to
``truth.planted_effects`` (effect = the factor name, magnitude = that window's
factor, detail = the call context). Factor names:

=====================  ==========================================  ===========================
Factor                 Asked by                                    Provided by
=====================  ==========================================  ===========================
``carrier_delay``      fulfilment, per web delivery                ``carrier_decay``
``carrier_exception``  fulfilment, per web delivery                ``carrier_decay``
``repurchase``         customers, per new customer (record) and    ``influencer_cohort_churn``
                       per repurchase intent (lifetime factor)
``mkt_spend``          marketing, campaign plan per channel/code   ``influencer_cohort_churn``
``retail_push``        retail, per purchase order; demand plan     ``retailer_overstock``
``sku_demand``         demand plan, per SKU/channel/day            (unused)
``web_orders``         demand plan, total web demand               (unused)
``web_new_customers``  web arrivals                                (unused)
``retail_order``       retail, per purchase order                  (unused)
=====================  ==========================================  ===========================

A factor in an implementation's ``lifetime_factors`` is asked for every ``t``
from the window's start on, also after its end (``repurchase``: customers
acquired inside the window keep the multiplier for life).

``transfers(name, t, world=world, **ctx)`` lists demand transfers
``(window, from_sku, to_sku, share)`` of the active windows
(``hero_oos_substitution`` moves a share of the hero's web demand to a sibling).

``scenario_promotions()`` lists promotion specs (``promo_margin_trap``), which
the promotions calendar turns into ordinary promotions.

An implementation with a ``duration(params)`` bounds its windows: the end is
``start + duration`` (or the configured end, if earlier), so a live window
closes by itself and a later ``disable`` is a no-op.

Determinism: windows are a pure function of config, the override rows and
the simulation time, so caches keyed by ``version`` (which changes only when
the windows change) stay valid, and the ``scen.window`` arrival source reads
nothing but the windows.

Causality of live switches
--------------------------

An override row takes effect at its ``at`` and is invisible before: the
engine calls :meth:`ScenarioManager.advance_to` with the start ``s`` of every
slice ``(s, e]`` and splits slices at :meth:`ScenarioManager.next_switch`, so
a row is applied exactly to the times ``t >= at`` (``factor()``,
``active()``, ``scenario_promotions()``, look-ahead consumers and ``version``
alike). An engine that saw a row long before its ``at`` (it was down and
caught up) therefore produces the same business as one that saw it a moment
before. Without ``advance_to`` (unit tests, tools) every row is visible.

The CLI picks ``at`` strictly after the committed clock under
``db.SCENARIO_LOCK``. A row or config window that nevertheless becomes known
with its start at or behind the clock (clock skew, manual SQL, an overlay
changed between restarts) would never be recorded in truth nor announced,
because window starts and announcements are emitted for ``(s, e]`` only. The
runner reconciles that: a late override's ``at`` is moved to just after the
clock in ``sim.scenario_overrides``; a late config window gets
``known_at = clock`` (:meth:`ScenarioManager.set_known_at`, persisted in the
``scenario_ledger`` entity), so it starts at ``max(start, known_at + 1 us)``
and its promotion is announced no earlier than that.

Ground truth
------------

* ``ScenarioWindowRecorded`` (``truth.scenario_windows``) at each window's
  start, via the ``scen.window`` arrival source and handler. A config window
  is recorded with its end; a live window with ``end`` unknown. Windows
  already open at history start are recorded by a bootstrap hook with
  ``at`` = history start (``start`` keeps the configured start; a window
  without a start is recorded as starting at history start).
* ``ScenarioWindowClosed`` (update of ``end_at``) at each CLI disable.
* ``PlantedEffect`` (``truth.planted_effects``) from ``factor(..., record=...)``
  and from :meth:`ScenarioManager.record_effect`.
"""

from __future__ import annotations

import bisect
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from ..domain.events import PlantedEffect, ScenarioWindowClosed, ScenarioWindowRecorded
from ..engine.queue import Event
from ..engine.registry import arrival_source, bootstrap, handler, on_load
from . import base
from . import carrier_decay as _carrier_decay  # noqa: F401  (registers the implementation)
from . import hero_oos_substitution as _hero_oos_substitution  # noqa: F401  (registers the implementation)
from . import influencer_cohort_churn as _influencer_cohort_churn  # noqa: F401  (registers the implementation)
from . import promo_margin_trap as _promo_margin_trap  # noqa: F401  (registers the implementation)
from . import retailer_overstock as _retailer_overstock  # noqa: F401  (registers the implementation)
from .base import ScenarioParamError

log = logging.getLogger("cpgsim.scenarios")

UTC = timezone.utc
# Start of a config window without ``start``: "from the start of history".
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Index offset for live windows (promotion ids are 900000 + index).
CLI_INDEX_OFFSET = 1000
# Recent effect ids remembered to drop accidental duplicate records.
_EFFECT_MEMORY = 200_000

RECORD_KIND = "scen.window"
CLOSE_KIND = "scen.close"
#: Smallest time step (Postgres timestamptz resolution).
EPS = timedelta(microseconds=1)
LEDGER_KIND = "scenario_ledger"


class UnknownScenarioError(ValueError):
    """A config entry names a scenario id with no implementation."""


@dataclass
class ScenarioWindow:
    """One period during which a scenario is active, with its full parameters.

    ``start`` is inclusive and ``end`` exclusive (aware UTC); ``end`` is
    ``None`` while the window is open-ended. ``source`` is ``config`` or
    ``cli``. ``index`` is stable for the window's lifetime (config position, or
    ``1000 + override id`` for live windows) and numbers scenario promotions.
    """

    window_id: str
    scenario_id: str
    params: dict
    start: datetime
    end: datetime | None
    source: str
    index: int = 0
    override_id: int | None = None
    position: int = field(default=0, repr=False)  # definition order (config, then overrides)

    def contains(self, t: datetime) -> bool:
        return self.start <= t and (self.end is None or t < self.end)

    @property
    def from_history_start(self) -> bool:
        """True for a config window without ``start``."""
        return self.start == EPOCH


@dataclass
class ScenarioLedger:
    """What the engine has seen of the scenario sources (``sim.entity_state``).

    ``overrides``: ids of ``sim.scenario_overrides`` rows already read (a row
    not in it whose ``at`` is at or behind the clock is late and gets clamped).
    ``config``: config window id -> ``[configured start ("" = from history
    start), known_at]``; ``known_at`` is ``None`` for windows known since the
    fresh start, else the clock at which a later config change introduced it.
    """

    overrides: list[int] = field(default_factory=list)
    config: dict[str, list] = field(default_factory=dict)


class ScenarioManager:
    """All scenario windows of a simulation; attached as ``world.scenarios``.

    Constructed as ``ScenarioManager(cfg)``: config windows are built and
    validated immediately (unknown scenario ids and bad parameters raise).
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.version = 0
        self.history_start: datetime | None = None
        self._rows: list[tuple] = []
        self._through: datetime | None = None  # None: every override row is visible
        self._visible: tuple[int, ...] = ()
        self._switches: list[datetime] = []  # sorted (at - EPS) of all rows
        self._known_at: dict[str, datetime] = {}
        self._warned: set[str] = set()
        self._effects_seen: OrderedDict[str, None] = OrderedDict()
        self._promos: list[dict[str, Any]] | None = None
        self._config_windows = self._build_config_windows()
        self._windows: list[ScenarioWindow] = []
        self._closes: list[tuple[datetime, str, int]] = []
        self._signature: tuple = ()
        self._install(self._effective_config_windows(), [])

    # -- construction --------------------------------------------------------
    def _local_midnight(self, d: date) -> datetime:
        return datetime.combine(d, time(0), tzinfo=self.tz).astimezone(UTC)

    def _build_config_windows(self) -> list[ScenarioWindow]:
        out: list[ScenarioWindow] = []
        for i, sc in enumerate(self.cfg.scenarios):
            impl = base.get(sc.id)
            if impl is None:
                if sc.id in base.PLANNED:
                    log.warning(
                        "scenario %r (config scenarios[%d]) is not implemented yet (phase 2); ignored",
                        sc.id, i,
                    )
                    continue
                known = sorted(set(base.REGISTRY) | base.PLANNED)
                raise UnknownScenarioError(
                    f"config scenarios[{i}]: unknown scenario id {sc.id!r}; known ids: {', '.join(known)}"
                )
            if not sc.enabled:
                continue
            try:
                params = impl.normalize(sc.params(), self.cfg)
            except ScenarioParamError as e:
                raise ScenarioParamError(f"config scenarios[{i}]: {e}") from None
            start = self._local_midnight(sc.start) if sc.start else EPOCH
            end = self._local_midnight(sc.end + timedelta(days=1)) if sc.end else None
            if end is not None and end <= start:
                raise ScenarioParamError(
                    f"config scenarios[{i}] ({sc.id}): end {sc.end} lies before start {sc.start}"
                )
            out.append(
                ScenarioWindow(
                    window_id=f"{sc.id}:config:{i}", scenario_id=sc.id, params=params,
                    start=start, end=end, source="config", index=i, position=len(out),
                )
            )
        return out

    def _install(self, windows: list[ScenarioWindow], closes: list[tuple[datetime, str, int]]) -> None:
        self._windows = windows
        self._by_id = {w.window_id: w for w in windows}
        self._by_scenario: dict[str, list[ScenarioWindow]] = {}
        self._by_factor: dict[str, list[tuple[ScenarioWindow, base.Scenario]]] = {}
        for w in windows:
            self._by_scenario.setdefault(w.scenario_id, []).append(w)
            impl = base.REGISTRY[w.scenario_id]
            for name in impl.factors:
                self._by_factor.setdefault(name, []).append((w, impl))
        starts = sorted((w.start, w.window_id) for w in windows if not w.from_history_start)
        self._start_keys = [s for s, _ in starts]
        self._start_ids = [wid for _, wid in starts]
        self._closes = sorted(closes)
        self._close_keys = [c[0] for c in self._closes]
        self._signature = self._make_signature(windows, self._closes)
        self._promos = None

    @staticmethod
    def _make_signature(windows: list[ScenarioWindow], closes) -> tuple:
        return (
            tuple(
                (w.window_id, w.start, w.end, json.dumps(w.params, sort_keys=True, default=str))
                for w in windows
            ),
            tuple(closes),
        )

    def _warn_once(self, key: str, msg: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    # -- CLI overrides ---------------------------------------------------------
    def load_overrides(self, rows: Iterable[tuple] | None) -> None:
        """Know ``sim.scenario_overrides`` rows ``(id, scenario_id, action, at, params)``.

        Called every live tick with the full table; returns immediately when
        the rows are unchanged. Only rows whose ``at`` has been reached (see
        :meth:`advance_to`) shape the windows. ``version`` increases only when
        the resulting windows differ. Rows naming unknown scenarios or
        carrying invalid parameters are skipped with a warning (the live loop
        keeps running).
        """
        new_rows = [tuple(r) for r in rows] if rows else []
        if new_rows == self._rows:
            return
        self._rows = new_rows
        self._switches = sorted({_aware_utc(r[3]) - EPS for r in new_rows})
        self._rebuild(force=True)

    def advance_to(self, t: datetime) -> None:
        """Make visible the override rows that apply to every time after ``t``.

        The engine calls this with the start ``s`` of each slice ``(s, e]``; a
        row is visible iff ``at <= s + 1 us``, i.e. it applies to all of the
        slice, and :meth:`next_switch` keeps slices from straddling a row's
        ``at``.
        """
        self._through = t
        self._rebuild()

    def next_switch(self, after: datetime) -> datetime | None:
        """Earliest slice boundary ``> after`` at which an override row becomes visible."""
        i = bisect.bisect_right(self._switches, after)
        return self._switches[i] if i < len(self._switches) else None

    def set_known_at(self, known_at: dict[str, datetime]) -> None:
        """Config windows introduced after the fresh start: id -> clock when first seen.

        Such a window starts no earlier than ``known_at + 1 us`` and its scenario
        promotion is announced no earlier than that (nothing before the clock
        can be written any more). The runner persists the map in the ledger.
        """
        self._known_at = {k: _aware_utc(v) for k, v in known_at.items()}
        self._rebuild(force=True)

    def config_window_starts(self) -> list[tuple[str, str]]:
        """``(window_id, configured start iso or "")`` of the config windows (ledger signature)."""
        return [(w.window_id, "" if w.from_history_start else w.start.isoformat()) for w in self._config_windows]

    def _effective_config_windows(self) -> list[ScenarioWindow]:
        out = []
        for w in self._config_windows:
            w = replace(w)
            k = self._known_at.get(w.window_id)
            if k is not None and w.start <= k:
                w.start = k + EPS
                if w.end is not None and w.end <= w.start:
                    self._warn_once(
                        f"late:{w.window_id}",
                        "config scenario window %s ended before it became known at %s; ignored",
                        w.window_id, k.isoformat(),
                    )
                    continue
            # A window without a start begins at history start (once known).
            base_start = self.history_start if w.from_history_start else w.start
            if base_start is not None:
                w.end = _bounded_end(w.scenario_id, w.params, base_start, w.end, self.tz)
            out.append(w)
        return out

    def _rebuild(self, force: bool = False) -> None:
        if self._through is None:
            visible = tuple(int(r[0]) for r in self._rows)
        else:
            lim = self._through + EPS
            visible = tuple(int(r[0]) for r in self._rows if _aware_utc(r[3]) <= lim)
        if not force and visible == self._visible:
            return
        self._visible = visible
        vis = set(visible)
        windows = self._effective_config_windows()
        closes: list[tuple[datetime, str, int]] = []
        for row in sorted(self._rows, key=lambda r: int(r[0])):
            oid, sid, action, at, params = row[:5]
            oid = int(oid)
            if oid not in vis:
                continue
            at = _aware_utc(at)
            if action == "enable":
                impl = base.get(sid)
                if impl is None:
                    why = "not implemented yet (phase 2)" if sid in base.PLANNED else "unknown"
                    self._warn_once(f"{oid}", "scenario override #%d: scenario %r is %s; ignored", oid, sid, why)
                    continue
                try:
                    p = impl.normalize(_params(params), self.cfg)
                except (ScenarioParamError, ValueError) as e:
                    self._warn_once(f"{oid}", "scenario override #%d ignored: %s", oid, e)
                    continue
                windows.append(
                    ScenarioWindow(
                        window_id=f"{sid}:cli:{oid}", scenario_id=sid, params=p, start=at,
                        end=_bounded_end(sid, p, at, None, self.tz),
                        source="cli", index=CLI_INDEX_OFFSET + oid, override_id=oid,
                        position=len(windows),
                    )
                )
            elif action == "disable":
                open_ = [w for w in windows if w.scenario_id == sid and w.contains(at)]
                if not open_:
                    ended = [w for w in windows if w.scenario_id == sid and w.end is not None
                             and w.start <= at and w.end <= at]
                    if ended:
                        self._warn_once(
                            f"{oid}", "scenario override #%d: the %r window %s already ended at %s"
                            " (self-limited); nothing to disable",
                            oid, sid, ended[-1].window_id, ended[-1].end.isoformat(),
                        )
                    else:
                        self._warn_once(
                            f"{oid}", "scenario override #%d: no open %r window at %s; nothing to disable",
                            oid, sid, at.isoformat(),
                        )
                    continue
                w = max(open_, key=lambda x: (x.start, x.position))
                w.end = at
                closes.append((at, w.window_id, oid))
            else:
                self._warn_once(f"{oid}", "scenario override #%d: unknown action %r; ignored", oid, action)
        if self._make_signature(windows, sorted(closes)) != self._signature:
            self._install(windows, closes)
            self.version += 1

    # -- queries --------------------------------------------------------------
    def windows(self, scenario_id: str | None = None) -> list[ScenarioWindow]:
        """Windows in definition order (config, then CLI overrides by id)."""
        if scenario_id is None:
            return list(self._windows)
        return list(self._by_scenario.get(scenario_id, ()))

    def window(self, window_id: str) -> ScenarioWindow | None:
        return self._by_id.get(window_id)

    def active(self, scenario_id: str, t: datetime) -> list[ScenarioWindow]:
        """Windows of ``scenario_id`` that contain ``t``."""
        return [w for w in self._by_scenario.get(scenario_id, ()) if w.contains(t)]

    def factor(
        self,
        name: str,
        t: datetime,
        world=None,
        record: tuple[str, object] | None = None,
        **ctx: Any,
    ) -> float:
        """Product of the active windows' factors for ``name`` given ``ctx``.

        ``1.0`` when no window applies. With ``world`` and
        ``record=(entity_type, entity_id)``, every contributing window whose
        factor is not 1 writes a ``PlantedEffect`` (effect id
        ``"<window_id>:<entity_type>:<entity_id>:<name>"``).
        """
        cands = self._by_factor.get(name)
        if not cands:
            return 1.0
        f = 1.0
        for w, impl in cands:
            if not w.contains(t) and not (name in impl.lifetime_factors and w.start <= t):
                continue
            v = impl.factor(name, w, t, ctx)
            if v is None:
                continue
            v = float(v)
            f *= v
            if record is not None and world is not None and v != 1.0:
                etype, eid = record
                self._emit_effect(world, w.scenario_id, w.window_id, str(etype), eid, name, v, ctx)
        return f

    def transfers(self, name: str, t: datetime, **ctx: Any) -> list[tuple[ScenarioWindow, int, int, float]]:
        """Demand transfers ``(window, from_sku, to_sku, share)`` of the windows active at ``t``.

        ``ctx`` must contain ``world`` (callers pass ``world=world``); it is passed
        to the implementations unchanged. Empty when nothing applies.
        """
        out: list[tuple[ScenarioWindow, int, int, float]] = []
        for w in self._windows:
            if not w.contains(t):
                continue
            impl = base.REGISTRY[w.scenario_id]
            if type(impl).transfers is base.Scenario.transfers:
                continue
            for src, dst, share in impl.transfers(name, w, t, ctx):
                out.append((w, int(src), int(dst), float(share)))
        return out

    def visible_from(self, window: ScenarioWindow) -> datetime | None:
        """Earliest time at which rows about ``window`` may be written, or ``None`` (no bound).

        A live (CLI) window becomes known at its start; a config window that was
        added after the fresh start at ``known_at + 1 us``; any other config
        window was known from the start of the simulation.
        """
        if window.source == "cli":
            return window.start
        k = self._known_at.get(window.window_id)
        return k + EPS if k is not None else None

    def record_effect(
        self,
        world,
        scenario_id: str,
        window_id: str | None,
        entity_type: str,
        entity_id,
        effect: str,
        magnitude: float | None,
        detail: dict | None = None,
    ) -> None:
        """Record one planted effect on one entity (``truth.planted_effects``).

        ``window_id=None`` resolves to the most recently started window of
        ``scenario_id`` active at ``world.now`` (if any). The effect id is
        ``"<window_id>:<entity_type>:<entity_id>:<effect>"``; recording the same
        id twice is a no-op.
        """
        if window_id is None:
            act = self.active(scenario_id, world.now)
            if act:
                window_id = max(act, key=lambda x: (x.start, x.position)).window_id
        self._emit_effect(world, scenario_id, window_id, str(entity_type), entity_id, effect, magnitude, detail)

    def _emit_effect(self, world, scenario_id, window_id, entity_type, entity_id, effect, magnitude, detail):
        eid = str(entity_id)
        effect_id = f"{window_id or scenario_id}:{entity_type}:{eid}:{effect}"
        seen = self._effects_seen
        if effect_id in seen:
            return
        seen[effect_id] = None
        if len(seen) > _EFFECT_MEMORY:
            seen.popitem(last=False)
        world.emit(
            PlantedEffect(
                at=world.now,
                effect_id=effect_id,
                scenario_id=scenario_id,
                window_id=window_id,
                entity_type=entity_type,
                entity_id=eid,
                effect=effect,
                magnitude=None if magnitude is None else float(magnitude),
                detail=_jsonable(dict(detail or {})),
            )
        )

    def scenario_promotions(self) -> list[dict[str, Any]]:
        """Promotion specs of all promo scenario windows, sorted by start.

        Each spec: ``window_id, scenario_id, brand, discount_pct, free_shipping,
        start, end, index, live`` (``live``: switched on from the CLI). Specs
        are returned for past, active and future windows alike; the promotions
        calendar picks those starting in the month it builds. An open-ended
        window yields a promotion of at most 28 days; a window closed early
        ends its promotion at the close.
        """
        if self._promos is None:
            out: list[dict[str, Any]] = []
            for w in self._windows:
                impl = base.REGISTRY[w.scenario_id]
                if type(impl).promotions is base.Scenario.promotions:
                    continue
                ww = w
                if w.from_history_start and self.history_start is not None:
                    ww = replace(w, start=self.history_start)
                for spec in impl.promotions(ww)[:1]:
                    spec = dict(spec)
                    if spec["end"] <= spec["start"]:
                        continue  # disabled at the moment it started
                    spec.update(
                        window_id=w.window_id, scenario_id=w.scenario_id, index=w.index,
                        live=w.source == "cli",
                    )
                    k = self._known_at.get(w.window_id)
                    if k is not None:
                        spec["announce_not_before"] = k + EPS
                    out.append(spec)
            out.sort(key=lambda s: (s["start"], s["index"]))
            self._promos = out
        return [dict(s) for s in self._promos]

    # -- truth recording --------------------------------------------------------
    def attach(self, world) -> None:
        """Remember the history start (needed for windows without a start)."""
        if world.history_start != self.history_start:
            self.history_start = world.history_start
            self._promos = None
            if any(w.from_history_start and _has_duration(w.scenario_id) for w in self._config_windows):
                self._rebuild(force=True)  # their self-limited end counts from history start

    def truth_arrivals(self, start: datetime, end: datetime) -> list[Event]:
        """Record/close events due in ``(start, end]``; pure in the windows."""
        out: list[Event] = []
        lo = bisect.bisect_right(self._start_keys, start)
        hi = bisect.bisect_right(self._start_keys, end)
        for i in range(lo, hi):
            w = self._by_id[self._start_ids[i]]
            out.append(
                Event(
                    due=w.start, kind=RECORD_KIND, key=f"{RECORD_KIND}:{w.window_id}",
                    data=_window_data(w), priority=0,
                )
            )
        lo = bisect.bisect_right(self._close_keys, start)
        hi = bisect.bisect_right(self._close_keys, end)
        for at, wid, oid in self._closes[lo:hi]:
            out.append(
                Event(
                    due=at, kind=CLOSE_KIND, key=f"{CLOSE_KIND}:{wid}",
                    data={"window_id": wid, "end": at.isoformat(), "override_id": oid}, priority=1,
                )
            )
        return out

    def initial_records(self, world) -> list[ScenarioWindowRecorded]:
        """Windows already open at history start (recorded at history start)."""
        hs = world.history_start
        out = []
        for w in sorted(self._windows, key=lambda x: (x.start, x.position)):
            if w.start <= hs and (w.end is None or w.end > hs):
                out.append(
                    ScenarioWindowRecorded(
                        at=hs, scenario_id=w.scenario_id, window_id=w.window_id,
                        params=_jsonable(w.params), start=hs if w.from_history_start else w.start,
                        end=w.end, source=w.source,
                    )
                )
        return out


# -- helpers -------------------------------------------------------------------


def _has_duration(scenario_id: str) -> bool:
    impl = base.get(scenario_id)
    return impl is not None and type(impl).duration is not base.Scenario.duration


def _bounded_end(
    scenario_id: str, params: dict, start: datetime, end: datetime | None, tz: ZoneInfo
) -> datetime | None:
    """``end`` limited to ``start + duration(params)`` for self-limiting scenarios.

    The duration is added in local wall-clock time, so a window opening at
    00:00 local closes at 00:00 local, also across a DST change.
    """
    impl = base.get(scenario_id)
    if impl is None:
        return end
    dur = impl.duration(params)
    if dur is None:
        return end
    limit = (start.astimezone(tz) + dur).astimezone(UTC)
    return limit if end is None else min(end, limit)


def _aware_utc(t: datetime) -> datetime:
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


def _params(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, (str, bytes)):
        params = json.loads(params) if params else {}
    if not isinstance(params, dict):
        raise ScenarioParamError(f"params must be a mapping, got {type(params).__name__}")
    return dict(params)


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (set, frozenset)):
        return [_jsonable(x) for x in sorted(v, key=str)]
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)


def _window_data(w: ScenarioWindow) -> dict[str, Any]:
    return {
        "window_id": w.window_id,
        "scenario_id": w.scenario_id,
        "params": _jsonable(w.params),
        "start": w.start.isoformat(),
        "end": w.end.isoformat() if w.end else None,
        "source": w.source,
    }


# -- engine hooks --------------------------------------------------------------


@arrival_source(RECORD_KIND)
def window_arrivals(world, start: datetime, end: datetime) -> list[Event]:
    """Truth events for window starts and CLI disables in ``(start, end]``."""
    sm = world.scenarios
    if sm is None:
        return []
    return sm.truth_arrivals(start, end)


@handler(RECORD_KIND)
def on_window_start(world, ev) -> None:
    """A window starts: record it (with its end when already known)."""
    d = ev.data
    w = world.scenarios.window(d["window_id"]) if world.scenarios is not None else None
    if w is not None:
        params, start, end, source, sid = _jsonable(w.params), w.start, w.end, w.source, w.scenario_id
    else:  # window no longer defined (overrides changed): record what was scheduled
        params, source, sid = d["params"], d["source"], d["scenario_id"]
        start = _aware_utc(d["start"])
        end = _aware_utc(d["end"]) if d.get("end") else None
    world.emit(
        ScenarioWindowRecorded(
            at=world.now, scenario_id=sid, window_id=d["window_id"], params=params,
            start=start, end=end, source=source,
        )
    )


@handler(CLOSE_KIND)
def on_window_close(world, ev) -> None:
    """A CLI disable closes a window: update its recorded end and end its promotion."""
    from ..domain import promotions  # lazy: promotions reads the manager

    world.emit(
        ScenarioWindowClosed(at=world.now, window_id=ev.data["window_id"], end=_aware_utc(ev.data["end"]))
    )
    promotions.scenario_window_closed(world, ev.data["window_id"])


@bootstrap(order=0)
def record_initial_windows(world) -> None:
    """At history start: remember it and record windows that are already open."""
    sm = world.scenarios
    if sm is None:
        return
    sm.attach(world)
    for rec in sm.initial_records(world):
        world.emit(rec)


@on_load(order=0)
def attach_history_start(world) -> None:
    if world.scenarios is not None:
        world.scenarios.attach(world)
