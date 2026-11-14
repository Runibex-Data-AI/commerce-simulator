"""The tick loop: one engine for backfill and live.

Each slice ``(clock, end]``:

1. every arrival source contributes the arrivals due in the slice to the queue;
2. every queued event with ``due <= end`` is processed in ``(due, priority,
   key)`` order, with ``world.now`` set to the event's due time (events
   scheduled by a handler that fall inside the slice are processed too);
3. the clock moves to ``end``.

Arrivals and queued events share one ordering, so the business that results
does not depend on the slice length. A commit writes all buffered business
rows, the entity-state delta, the queue delta, the clock and a run-log row in
**one** transaction, so a crash at any point resumes from a consistent state.

Backfill uses long slices and commits every ``backfill.commit_every_hours``
of simulated time. Live mode uses ``tick_seconds`` slices against wall time
and commits every tick; it never runs ahead of wall time.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .. import domain, writers
from ..config import Config
from ..writers.base import Dispatcher, WriterContext
from .entities import EntityStore
from .queue import EventQueue
from .registry import ARRIVAL_SOURCES, BOOTSTRAPS, HANDLERS, ON_LOAD, ordered
from .world import UTC, World

log = logging.getLogger("cpgsim.engine")


class StateMismatch(RuntimeError):
    pass


class HistoryStartInFuture(ValueError):
    pass


def months_before(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def utcnow() -> datetime:
    return datetime.now(UTC)


class Simulation:
    def __init__(self, cfg: Config, conn) -> None:
        self.cfg = cfg
        self.conn = conn
        self.store = EntityStore()
        self.queue = EventQueue()
        self.world = World(cfg, self.store, self.queue, emit=lambda e: None)
        domain.build(self.world)
        ctx = WriterContext(seed=self.world.seed, cfg=cfg, ref=self.world.ref, tz=self.world.tz)
        self.dispatcher = Dispatcher(writers.build_writers(ctx))
        self.world._emit = self.dispatcher.dispatch
        self.batch = self.dispatcher.batch
        self.clock: datetime | None = None
        self.events_since_commit = 0
        self._stop = threading.Event()

    # -- state -------------------------------------------------------------
    def read_clock(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT sim_time, history_start, seed, config_fingerprint FROM sim.clock WHERE id = 1"
            )
            return cur.fetchone()

    def open(
        self,
        history_start: date | None = None,
        months: int | None = None,
        now: datetime | None = None,
        allow_config_change: bool = False,
    ) -> bool:
        """Load existing state, or start a fresh simulation. Returns True if fresh."""
        row = self.read_clock()
        if row is None:
            now = now or utcnow()
            if history_start is None:
                m = self.cfg.history_months if months is None else months
                history_start = months_before(now.astimezone(self.world.tz).date(), m)
            start = self.world.at_local(history_start, 0)
            if start > now:
                raise HistoryStartInFuture("history start lies in the future")
            self._fresh_start(start)
            return True
        sim_time, hist, seed, fp = row
        if int(seed) != self.world.seed:
            raise StateMismatch(
                f"database holds a simulation with seed {seed}, config has {self.world.seed};"
                " run `sim reset` to start over"
            )
        if fp != self.cfg.fingerprint():
            msg = "config changed since the simulation started; determinism is no longer guaranteed"
            if not allow_config_change:
                log.warning(msg)
        self._load(sim_time, hist)
        return False

    def _fresh_start(self, start: datetime) -> None:
        w = self.world
        w.now = start
        w.history_start = start
        self.clock = start
        from ..domain.model import Counters

        self.store.put("counters", "all", Counters())
        for fn in ordered(BOOTSTRAPS):
            fn(w)
        for fn in ordered(ON_LOAD):
            fn(w)
        self.commit(start, start, mode="init", events=0, fresh=True)
        log.info("fresh simulation at %s (seed %s)", start.isoformat(), w.seed)

    def _load(self, sim_time: datetime, hist: datetime) -> None:
        w = self.world
        with self.conn.cursor() as cur:
            cur.execute("SELECT kind, id, data FROM sim.entity_state")
            self.store.load(cur.fetchall())
            cur.execute("SELECT key, due, priority, kind, data FROM sim.event_queue")
            self.queue.load(cur.fetchall())
        self.conn.commit()
        self.clock = sim_time.astimezone(UTC)
        w.now = self.clock
        w.history_start = hist.astimezone(UTC)
        self.refresh_scenarios()
        for fn in ordered(ON_LOAD):
            fn(w)
        log.info("resumed at %s with %d queued events", self.clock.isoformat(), len(self.queue))

    def refresh_scenarios(self) -> None:
        sm = self.world.scenarios
        if sm is not None and hasattr(sm, "load_overrides"):
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id, scenario_id, action, at, params FROM sim.scenario_overrides ORDER BY id"
                )
                sm.load_overrides(cur.fetchall())
            self.conn.commit()

    # -- commit ------------------------------------------------------------
    def commit(self, sim_from: datetime, sim_to: datetime, mode: str, events: int, fresh=False) -> dict:
        t0 = time.perf_counter()
        try:
            with self.conn.cursor() as cur:
                rows = self.batch.flush(cur)
                ent = self.store.flush(cur)
                q = self.queue.flush(cur)
                if fresh:
                    cur.execute(
                        "INSERT INTO sim.clock (id, sim_time, history_start, seed, config_fingerprint,"
                        " started_wall, updated_wall) VALUES (1, %s, %s, %s, %s, now(), now())",
                        (sim_to, self.world.history_start, self.world.seed, self.cfg.fingerprint()),
                    )
                else:
                    cur.execute(
                        "UPDATE sim.clock SET sim_time = %s, updated_wall = now(),"
                        " config_fingerprint = %s WHERE id = 1",
                        (sim_to, self.cfg.fingerprint()),
                    )
                cur.execute(
                    "INSERT INTO sim.run_log (wall_time, mode, sim_from, sim_to, events, rows,"
                    " queue_depth, duration_ms) VALUES (now(), %s, %s, %s, %s, %s, %s, %s)",
                    (
                        mode, sim_from, sim_to, events, json.dumps(rows), len(self.queue),
                        int((time.perf_counter() - t0) * 1000),
                    ),
                )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        self.batch.clear()
        self.store.mark_flushed()
        self.queue.mark_flushed()
        return {"rows": rows, "entities": ent, "queue": q}

    # -- advancing -----------------------------------------------------------
    def _process_slice(self, start: datetime, end: datetime) -> int:
        w = self.world
        for name in sorted(ARRIVAL_SOURCES):
            for ev in ARRIVAL_SOURCES[name](w, start, end):
                if not (start < ev.due <= end):
                    raise RuntimeError(f"arrival source {name} produced {ev.key} outside slice")
                self.queue.push(ev)
        n = 0
        q = self.queue
        while True:
            due = q.peek_due()
            if due is None or due > end:
                break
            ev = q.pop()
            w.now = ev.due
            fn = HANDLERS.get(ev.kind)
            if fn is None:
                raise RuntimeError(f"no handler registered for event kind {ev.kind!r}")
            fn(w, ev)
            n += 1
        w.now = end
        return n

    def advance(
        self,
        target: datetime,
        slice_len: timedelta,
        commit_every: timedelta,
        mode: str,
        progress: Callable[[datetime, int], None] | None = None,
    ) -> int:
        """Advance the clock to ``target`` (never beyond wall time)."""
        assert self.clock is not None
        wall = utcnow()
        if target > wall:
            target = wall
        total = 0
        commit_from = self.clock
        pending_events = 0
        max_rows = self.cfg.backfill.max_batch_rows
        self.world.mode = "live" if mode == "live" else "backfill"
        while self.clock < target and not self._stop.is_set():
            end = min(self.clock + slice_len, target)
            n = self._process_slice(self.clock, end)
            self.clock = end
            total += n
            pending_events += n
            if end >= target or end - commit_from >= commit_every or len(self.batch) >= max_rows:
                self.commit(commit_from, end, mode, pending_events)
                commit_from = end
                pending_events = 0
                if progress:
                    progress(end, total)
        if commit_from < self.clock:
            self.commit(commit_from, self.clock, mode, pending_events)
        return total

    def backfill(self, until: datetime | None = None, progress=None) -> int:
        bf = self.cfg.backfill
        return self.advance(
            until or utcnow(),
            timedelta(minutes=bf.tick_minutes),
            timedelta(hours=bf.commit_every_hours),
            "backfill",
            progress,
        )

    def stop(self, *_args) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def run_live(self, max_ticks: int | None = None, progress=None) -> None:
        """Run until stopped. Catches up at backfill speed first if behind."""
        tick = timedelta(seconds=self.cfg.tick_seconds)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except ValueError:  # not in main thread (tests)
                pass
        ticks = 0
        while not self._stop.is_set():
            self.refresh_scenarios()
            now = utcnow()
            assert self.clock is not None
            if now - self.clock > 4 * tick:
                log.info("catching up from %s to %s", self.clock.isoformat(), now.isoformat())
                self.backfill(now, progress)
                continue
            if now > self.clock:
                self.advance(now, tick, tick, "live")
                ticks += 1
                if progress:
                    progress(self.clock, ticks)
            if max_ticks is not None and ticks >= max_ticks:
                break
            sleep_for = (self.clock + tick - utcnow()).total_seconds()
            if sleep_for > 0:
                self._stop.wait(sleep_for)
