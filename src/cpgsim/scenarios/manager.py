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
factor, detail = the call context). Factor names in phase 1:

=====================  ======================================  ==================
Factor                 Asked by                                Provided by
=====================  ======================================  ==================
``carrier_delay``      fulfilment, per web delivery            ``carrier_decay``
``carrier_exception``  fulfilment, per web delivery            ``carrier_decay``
``sku_demand``         demand plan, per SKU/channel/day        (phase 2)
``web_orders``         demand plan, total web demand           (phase 2)
``web_new_customers``  web arrivals                            (phase 2)
``repurchase``         customers, per repurchase intent        (phase 2)
``retail_order``       retail, per purchase order              (phase 2)
=====================  ======================================  ==================

``scenario_promotions()`` lists promotion specs (``promo_margin_trap``), which
the promotions calendar turns into ordinary promotions.

Determinism: windows are a pure function of config and the override rows, so
caches keyed by ``version`` (which changes only when the windows change) stay
valid, and the ``scen.window`` arrival source reads nothing but the windows.

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
from . import promo_margin_trap as _promo_margin_trap  # noqa: F401  (registers the implementation)
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
        self._warned: set[str] = set()
        self._effects_seen: OrderedDict[str, None] = OrderedDict()
        self._promos: list[dict[str, Any]] | None = None
        self._config_windows = self._build_config_windows()
        self._windows: list[ScenarioWindow] = []
        self._closes: list[tuple[datetime, str, int]] = []
        self._signature: tuple = ()
        self._install([replace(w) for w in self._config_windows], [])

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
        """Apply ``sim.scenario_overrides`` rows ``(id, scenario_id, action, at, params)``.

        Called every live tick with the full table; returns immediately when
        the rows are unchanged. ``version`` increases only when the resulting
        windows differ. Rows naming unknown scenarios or carrying invalid
        parameters are skipped with a warning (the live loop keeps running).
        """
        new_rows = [tuple(r) for r in rows] if rows else []
        if new_rows == self._rows:
            return
        self._rows = new_rows
        windows = [replace(w) for w in self._config_windows]
        closes: list[tuple[datetime, str, int]] = []
        for row in sorted(new_rows, key=lambda r: int(r[0])):
            oid, sid, action, at, params = row[:5]
            oid = int(oid)
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
                        window_id=f"{sid}:cli:{oid}", scenario_id=sid, params=p, start=at, end=None,
                        source="cli", index=CLI_INDEX_OFFSET + oid, override_id=oid,
                        position=len(windows),
                    )
                )
            elif action == "disable":
                open_ = [w for w in windows if w.scenario_id == sid and w.contains(at)]
                if not open_:
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
            if not w.contains(t):
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
    """A CLI disable closes a window: update its recorded end."""
    world.emit(
        ScenarioWindowClosed(at=world.now, window_id=ev.data["window_id"], end=_aware_utc(ev.data["end"]))
    )


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
