"""Fixtures for the chaos tests: a small in-memory world running the real chaos module.

``Harness`` holds a world with the real config, reference data and scenario
manager, an entity store, an event queue and the real writers (rows are read
from the write buffers, no database). ``run_until`` works through the queue
like the engine (``(due, priority, key)`` order); ``arrivals`` feeds a slice
of the chaos arrival sources.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import cpgsim.chaos  # noqa: F401  (registers the chaos handlers)
from cpgsim.config import Config, load_config
from cpgsim.domain.model import ENTITY_KINDS, Counters
from cpgsim.domain.reference import build_reference
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import EventQueue
from cpgsim.engine.registry import ARRIVAL_SOURCES, HANDLERS
from cpgsim.engine.world import World
from cpgsim.scenarios import ScenarioManager
from cpgsim.writers import build_writers
from cpgsim.writers.base import Dispatcher, WriterContext

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2025, 3, 2, 23, 0, tzinfo=UTC)  # 2025-03-03 00:00 Berlin (Monday)

_REF: dict = {}


def ref_for(cfg: Config):
    key = tuple(sorted(cfg.channels.retailers()))
    if key not in _REF:
        _REF[key] = build_reference(cfg)
    return _REF[key]


def chaos_cfg(**rates) -> Config:
    """Default config with chaos rates overridden (e.g. ``status_error_rate=1.0``)."""
    return load_config(chaos=rates) if rates else load_config()


class Harness:
    def __init__(self, cfg: Config | None = None, start: datetime = START) -> None:
        self.cfg = cfg or load_config()
        self.store = EntityStore()
        for kind, cls in ENTITY_KINDS.items():
            self.store.register(kind, cls)
        self.store.put("counters", "all", Counters())
        self.events: list = []
        self.queue = EventQueue()
        self.world = World(self.cfg, self.store, self.queue, emit=self._emit)
        self.world.ref = ref_for(self.cfg)
        self.world.scenarios = ScenarioManager(self.cfg)
        self.world.now = start
        self.world.history_start = start
        self.world.scenarios.attach(self.world)
        ctx = WriterContext(seed=self.world.seed, cfg=self.cfg, ref=self.world.ref, tz=self.world.tz)
        self.dispatcher = Dispatcher(build_writers(ctx))

    def _emit(self, e) -> None:
        self.events.append(e)
        self.dispatcher.dispatch(e)

    # -- reading ----------------------------------------------------------
    def of(self, etype) -> list:
        return [e for e in self.events if isinstance(e, etype)]

    def effects(self, scenario_id: str | None = None) -> list:
        from cpgsim.domain.events import PlantedEffect

        return [e for e in self.of(PlantedEffect) if scenario_id is None or e.scenario_id == scenario_id]

    def rows(self, table: str) -> dict:
        """Pending rows of ``table`` by primary key (inserts, with updates applied)."""
        return dict(self.dispatcher.batch.table(table).inserts)

    def updates(self, table: str) -> dict:
        return dict(self.dispatcher.batch.table(table).updates)

    # -- driving ----------------------------------------------------------
    def run_until(self, t: datetime) -> int:
        n = 0
        while True:
            due = self.queue.peek_due()
            if due is None or due > t:
                break
            ev = self.queue.pop()
            self.world.now = ev.due
            HANDLERS[ev.kind](self.world, ev)
            n += 1
        self.world.now = max(self.world.now, t)
        return n

    def arrivals(self, name: str, start: datetime, end: datetime) -> list:
        evs = list(ARRIVAL_SOURCES[name](self.world, start, end))
        for ev in evs:
            assert start < ev.due <= end
            self.queue.push(ev)
        return evs


@pytest.fixture
def harness():
    """Factory: ``harness(cfg=None, start=START)``."""
    return Harness
