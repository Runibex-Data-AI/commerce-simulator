"""The event queue: future actions, persisted in ``sim.event_queue``.

Events are processed strictly in ``(due, priority, key)`` order. ``key`` is a
unique, content-derived string (e.g. ``"web.ship:184467:1"``), never a
counter assigned at scheduling time, so the processing order does not depend on
when an event happened to be pushed (which differs with tick length).

The in-memory heap is the working copy. At each commit the queue writes only
its delta: events scheduled since the last commit that are still pending are
inserted; events that were persisted and have since been processed are
deleted. ``sim`` is internal, so deletes there do not touch the loading
contract.
"""

from __future__ import annotations

import heapq
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable


@dataclass(slots=True)
class Event:
    due: datetime  # aware, UTC
    kind: str  # handler name, e.g. "web.payment_result"
    key: str  # globally unique, deterministic
    data: dict[str, Any] = field(default_factory=dict)  # JSON-serialisable only
    priority: int = 50  # lower runs first among events with identical due

    def sort_key(self) -> tuple:
        return (self.due, self.priority, self.key)


class DuplicateEventKey(RuntimeError):
    pass


class EventQueue:
    def __init__(self) -> None:
        self._heap: list[tuple[datetime, int, str, Event]] = []
        self._pending: set[str] = set()  # keys currently in heap
        self._persisted: set[str] = set()  # keys present in sim.event_queue
        self._new: dict[str, Event] = {}  # scheduled since last flush, not yet persisted
        self._done_persisted: list[str] = []  # persisted keys processed since last flush

    def __len__(self) -> int:
        return len(self._heap)

    def push(self, ev: Event) -> None:
        if ev.key in self._pending:
            raise DuplicateEventKey(ev.key)
        self._pending.add(ev.key)
        self._new[ev.key] = ev
        heapq.heappush(self._heap, (ev.due, ev.priority, ev.key, ev))

    def contains(self, key: str) -> bool:
        return key in self._pending

    def peek_due(self) -> datetime | None:
        return self._heap[0][0] if self._heap else None

    def pop(self) -> Event:
        _, _, key, ev = heapq.heappop(self._heap)
        self._pending.discard(key)
        if key in self._new:
            del self._new[key]
        elif key in self._persisted:
            self._persisted.discard(key)
            self._done_persisted.append(key)
        return ev

    # -- persistence -----------------------------------------------------
    def load(self, rows: Iterable[tuple[str, datetime, int, str, Any]]) -> None:
        """Rebuild from ``(key, due, priority, kind, data)`` rows."""
        self._heap.clear()
        self._pending.clear()
        self._persisted.clear()
        self._new.clear()
        self._done_persisted.clear()
        for key, due, priority, kind, data in rows:
            if isinstance(data, str):
                data = json.loads(data)
            ev = Event(due=due, kind=kind, key=key, data=data or {}, priority=priority)
            self._heap.append((ev.due, ev.priority, ev.key, ev))
            self._pending.add(key)
            self._persisted.add(key)
        heapq.heapify(self._heap)

    def flush(self, cur) -> tuple[int, int]:
        """Write the delta in the caller's transaction. Returns (inserted, deleted)."""
        deleted = len(self._done_persisted)
        if self._done_persisted:
            cur.execute(
                "DELETE FROM sim.event_queue WHERE key = ANY(%s)", (self._done_persisted,)
            )
        inserted = len(self._new)
        if self._new:
            with cur.copy(
                "COPY sim.event_queue (key, due, priority, kind, data) FROM STDIN"
            ) as cp:
                for ev in self._new.values():
                    cp.write_row(
                        (ev.key, ev.due, ev.priority, ev.kind, json.dumps(ev.data, default=str))
                    )
        return inserted, deleted

    def mark_flushed(self) -> None:
        """Call after the transaction that ran :meth:`flush` committed."""
        self._persisted.update(self._new.keys())
        self._new.clear()
        self._done_persisted.clear()
