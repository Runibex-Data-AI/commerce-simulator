"""The World: everything a handler needs, passed explicitly.

A handler receives ``(world, event)``. ``world.now`` is the event's due time.
Handlers read and mutate entities via ``world.store``, schedule follow-ups via
``world.schedule``, draw randomness via ``world.rng`` and report business facts
via ``world.emit``. They never touch the database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from .entities import EntityStore
from .queue import Event, EventQueue
from .rng import Rng

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

UTC = timezone.utc


class SchedulingError(RuntimeError):
    pass


class World:
    def __init__(
        self,
        cfg: "Config",
        store: EntityStore,
        queue: EventQueue,
        emit: Callable[[Any], None],
    ) -> None:
        self.cfg = cfg
        self.seed: int = int(cfg.seed)
        self.tz = ZoneInfo(cfg.timezone)
        self.store = store
        self.queue = queue
        self._emit = emit
        self.now: datetime = datetime(1970, 1, 1, tzinfo=UTC)
        self.history_start: datetime = self.now
        # Reference data and services, attached by cpgsim.domain.build(world):
        self.ref: Any = None  # domain.reference.ReferenceData
        self.scenarios: Any = None  # scenarios.manager.ScenarioManager
        self.mode: str = "backfill"  # backfill | live (informational only; never branch on it)

    # -- randomness ------------------------------------------------------
    def rng(self, *parts: object) -> Rng:
        return Rng(self.seed, *parts)

    # -- scheduling ------------------------------------------------------
    def schedule(
        self,
        kind: str,
        due: datetime,
        key: str,
        data: dict[str, Any] | None = None,
        priority: int = 50,
    ) -> Event:
        if due.tzinfo is None:
            raise SchedulingError(f"naive datetime for {key}")
        if due < self.now:
            raise SchedulingError(f"{key} scheduled in the past: {due} < {self.now}")
        due = due.astimezone(UTC)
        ev = Event(due=due, kind=kind, key=key, data=data or {}, priority=priority)
        self.queue.push(ev)
        return ev

    def is_scheduled(self, key: str) -> bool:
        return self.queue.contains(key)

    # -- ids -------------------------------------------------------------
    def next_id(self, name: str) -> int:
        c = self.store.mut("counters", "all")
        v = c.values.get(name, 0) + 1
        c.values[name] = v
        return v

    # -- output ----------------------------------------------------------
    def emit(self, event: Any) -> None:
        self._emit(event)

    # -- time helpers ----------------------------------------------------
    def local(self, dt: datetime | None = None) -> datetime:
        return (dt or self.now).astimezone(self.tz)

    def local_date(self, dt: datetime | None = None) -> date:
        return self.local(dt).date()

    def at_local(self, d: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
        """Aware UTC datetime for a local wall-clock time on date ``d``.

        Non-existent local times (spring-forward gap) resolve forward via fold=0
        semantics of zoneinfo; ambiguous times take the first occurrence.
        """
        local = datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=self.tz)
        return local.astimezone(UTC)

    def in_days(self, days: float) -> datetime:
        return self.now + timedelta(days=days)
