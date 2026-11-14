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

Housekeeping: every ``VACUUM_EVERY_STATE_ROWS`` state rows written, the engine
vacuums ``sim.entity_state`` and ``sim.event_queue`` between commits (a
backfill churns them faster than autovacuum keeps up); :meth:`Simulation.after_backfill`
vacuums and analyzes them and analyzes the business schemas.

Scenario overrides apply exactly from their ``at`` on: slices are split so that
no slice straddles a switch (``ScenarioManager.advance_to / next_switch``).

Single writer: :meth:`Simulation.open` takes the session-level advisory lock
``db.WRITER_LOCK`` and holds it until the connection closes (or
:meth:`Simulation.close`); a second engine, and ``sim reset``, refuse while it
is held. Every commit is also fenced on ``sim.clock`` (``seed`` and the
previous ``sim_time``), so a writer whose state was reset underneath it fails
instead of writing into another timeline.

Contract timestamps: in backfill ``created_at``/``updated_at`` are the event
time (deterministic). In :meth:`Simulation.run_live` (including the catch-up
after downtime) they are the commit time, taken just before the rows are
written and never earlier than the slice end, so ``updated_at`` never lies
behind rows a BI tool has already loaded; business timestamp columns keep the
event time.
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

import psycopg

from .. import db, domain, writers
from ..config import Config
from ..writers.base import Dispatcher, WriterContext
from .entities import EntityStore
from .queue import EventQueue
from .registry import ARRIVAL_SOURCES, BOOTSTRAPS, HANDLERS, ON_LOAD, ordered
from .world import UTC, World

log = logging.getLogger("cpgsim.engine")

#: Smallest time step (Postgres ``timestamptz`` resolution).
EPS = timedelta(microseconds=1)
#: Rows per round trip when streaming state on resume.
LOAD_ITERSIZE = 5000


class StateMismatch(RuntimeError):
    pass


class EngineLocked(StateMismatch):
    """Another engine holds the writer lock on this database."""


class HistoryStartInFuture(ValueError):
    pass


def months_before(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


#: Shortest wait of a live loop iteration that had nothing to advance.
MIN_IDLE_SECONDS = 0.25
#: State rows written (entity upserts/deletes plus queue inserts/deletes) after
#: which ``sim.entity_state`` and ``sim.event_queue`` are vacuumed. A backfill
#: commits about 7,000 such rows per simulated day, faster than autovacuum
#: reclaims them: without this, 24 months left 480-890 MB of heap for 62 MB of
#: live state (about 220 MB with it). The dead rows between two runs stay under ~70 MB.
VACUUM_EVERY_STATE_ROWS = 50_000


def utcnow() -> datetime:
    return datetime.now(UTC)


class Simulation:
    def __init__(self, cfg: Config, conn) -> None:
        from ..scenarios.manager import LEDGER_KIND, ScenarioLedger

        self.cfg = cfg
        self.conn = conn
        self.store = EntityStore()
        self.queue = EventQueue()
        self.world = World(cfg, self.store, self.queue, emit=lambda e: None)
        domain.build(self.world)
        self.store.register(LEDGER_KIND, ScenarioLedger)
        ctx = WriterContext(seed=self.world.seed, cfg=cfg, ref=self.world.ref, tz=self.world.tz)
        self.dispatcher = Dispatcher(writers.build_writers(ctx))
        self.world._emit = self.dispatcher.dispatch
        self.batch = self.dispatcher.batch
        self.clock: datetime | None = None
        self.events_since_commit = 0
        #: Stamp contract columns with the commit time (set by run_live).
        self.commit_time_stamps = False
        self._committed: datetime | None = None  # sim_time of the last commit (fence)
        self._locked = False
        self._stop = threading.Event()
        self._state_rows = 0  # state rows written since the last vacuum

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
        lock_wait: float = 0.0,
    ) -> bool:
        """Load existing state, or start a fresh simulation. Returns True if fresh.

        Takes the writer lock first (waiting up to ``lock_wait`` seconds) and
        raises :class:`EngineLocked` if another engine holds it.
        ``allow_config_change`` only turns the config-change warning into an
        info line; the change is recorded in ``sim.config_history`` either way.
        """
        self._lock(lock_wait)
        try:
            return self._open(history_start, months, now, allow_config_change)
        except BaseException:
            self.close()
            raise

    def _open(self, history_start, months, now, allow_config_change) -> bool:
        row = self.read_clock()
        if row is None:
            self._assert_no_leftover_state()
            now = now or utcnow()
            if history_start is None:
                m = self.cfg.history_months if months is None else months
                history_start = months_before(now.astimezone(self.world.tz).date(), m)
            start = self.world.at_local(history_start, 0)
            if start > now:
                self.conn.rollback()
                raise HistoryStartInFuture("history start lies in the future")
            self._fresh_start(start)
            return True
        sim_time, hist, seed, fp = row
        if int(seed) != self.world.seed:
            self.conn.rollback()
            raise StateMismatch(
                f"database holds a simulation with seed {seed}, config has {self.world.seed};"
                " run `sim reset` to start over"
            )
        self._check_config(fp, sim_time.astimezone(UTC), allow_config_change)
        self._load(sim_time, hist)
        return False

    def close(self) -> None:
        """Release the writer lock (the connection stays open)."""
        if self._locked:
            try:
                db.release_writer_lock(self.conn)
            except psycopg.Error:  # connection already gone: the lock went with it
                pass
            self._locked = False

    def _lock(self, wait: float) -> None:
        if self._locked:
            return
        try:
            db.acquire_writer_lock(self.conn, wait=wait)
        except db.WriterLockHeld as e:
            raise EngineLocked(str(e)) from None
        self._locked = True

    def _assert_no_leftover_state(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM sim.entity_state) OR EXISTS (SELECT 1 FROM sim.event_queue)"
            )
            leftover = cur.fetchone()[0]
        if leftover:
            self.conn.rollback()
            raise StateMismatch(
                "sim.clock is empty but sim.entity_state / sim.event_queue are not (left over from"
                " a writer that kept running after a reset?); run `sim reset` to start over"
            )

    def _check_config(self, stored: str, sim_time: datetime, allow: bool) -> None:
        """Warn about, and durably record, a config that differs from the starting one.

        ``sim.clock.config_fingerprint`` keeps the fingerprint the simulation
        started with; each switch to a new fingerprint adds one row to
        ``sim.config_history`` (old, new, sim time), so ``sim status`` keeps
        showing that the history was produced by more than one config.
        """
        cur_fp = self.cfg.fingerprint()
        if stored == cur_fp:
            return
        with self.conn.cursor() as cur:
            if stored == self.cfg.legacy_fingerprint():
                # Same config, fingerprint written by an older version (it
                # also hashed tick and backfill settings): store the new form.
                cur.execute("UPDATE sim.clock SET config_fingerprint = %s WHERE id = 1", (cur_fp,))
                self.conn.commit()
                return
            last = None
            try:
                cur.execute("SAVEPOINT cpgsim_cfg")
                cur.execute(
                    "SELECT new_fingerprint FROM sim.config_history ORDER BY id DESC LIMIT 1"
                )
                r = cur.fetchone()
                last = r[0] if r else None
                if last != cur_fp:
                    cur.execute(
                        "INSERT INTO sim.config_history (sim_time, old_fingerprint, new_fingerprint)"
                        " VALUES (%s, %s, %s)",
                        (sim_time, last or stored, cur_fp),
                    )
                cur.execute("RELEASE SAVEPOINT cpgsim_cfg")
            except psycopg.errors.UndefinedTable:
                cur.execute("ROLLBACK TO SAVEPOINT cpgsim_cfg")
                log.warning("sim.config_history missing (run `sim init`); config change not recorded")
        self.conn.commit()
        msg = (
            "config differs from the one the simulation started with (fingerprint %s, now %s);"
            " determinism against a replay with either config is no longer guaranteed"
            " (recorded in sim.config_history; `sim reset` starts over)"
        )
        (log.info if allow else log.warning)(msg, stored, cur_fp)

    def _fresh_start(self, start: datetime) -> None:
        from ..scenarios.manager import LEDGER_KIND, ScenarioLedger

        w = self.world
        w.now = start
        w.history_start = start
        self.clock = start
        from ..domain.model import Counters

        self.store.put("counters", "all", Counters())
        sm = w.scenarios
        if sm is not None and hasattr(sm, "config_window_starts"):
            self.store.put(
                LEDGER_KIND, "all",
                ScenarioLedger(config={wid: [sig, None] for wid, sig in sm.config_window_starts()}),
            )
            sm.advance_to(start)
        # Overrides switched before the first run (e.g. `sim scenario enable` right
        # after `sim init`) must be known from the start, exactly as after a restart.
        self.refresh_scenarios()
        for fn in ordered(BOOTSTRAPS):
            fn(w)
        for fn in ordered(ON_LOAD):
            fn(w)
        self.commit(start, start, mode="init", events=0, fresh=True)
        log.info("fresh simulation at %s (seed %s)", start.isoformat(), w.seed)

    def _load(self, sim_time: datetime, hist: datetime) -> None:
        w = self.world
        # Stream both tables (server-side cursors, JSON as text decoded row by
        # row): fetchall() on the jsonb would hold the whole result and every
        # parsed dict at once (about 0.9 GB for a 24-month state).
        with self.conn.cursor(name="cpgsim_load_entities") as cur:
            cur.itersize = LOAD_ITERSIZE
            cur.execute("SELECT kind, id, data::text FROM sim.entity_state")
            self.store.load(cur)
        with self.conn.cursor(name="cpgsim_load_queue") as cur:
            cur.itersize = LOAD_ITERSIZE
            cur.execute("SELECT key, due, priority, kind, data::text FROM sim.event_queue")
            self.queue.load(cur)
        self.conn.commit()
        self.clock = sim_time.astimezone(UTC)
        self._committed = sim_time
        w.now = self.clock
        w.history_start = hist.astimezone(UTC)
        self._reconcile_ledger()
        self.refresh_scenarios()
        for fn in ordered(ON_LOAD):
            fn(w)
        log.info("resumed at %s with %d queued events", self.clock.isoformat(), len(self.queue))

    # -- scenarios -----------------------------------------------------------
    def _reconcile_ledger(self) -> None:
        """On resume: create the scenario ledger for old states; spot new config windows.

        A config window the ledger does not know was added to the config after
        the simulation started. Its start (and its promotion's announcement)
        may lie behind the clock, where it could never be recorded: it is given
        ``known_at = clock`` (persisted), see ``ScenarioManager.set_known_at``.
        """
        from ..scenarios.manager import LEDGER_KIND, ScenarioLedger

        sm = self.world.scenarios
        if sm is None or not hasattr(sm, "config_window_starts"):
            return
        assert self.clock is not None
        sm.advance_to(self.clock)
        led = self.store.get(LEDGER_KIND, "all")
        starts = sm.config_window_starts()
        if led is None:
            # State written by a version without the ledger: everything it
            # had was already recorded.
            with self.conn.cursor() as cur:
                cur.execute("SELECT id FROM sim.scenario_overrides ORDER BY id")
                ids = [int(r[0]) for r in cur.fetchall()]
            self.conn.commit()
            self.store.put(
                LEDGER_KIND, "all",
                ScenarioLedger(overrides=ids, config={wid: [sig, None] for wid, sig in starts}),
            )
        else:
            new = [(wid, sig) for wid, sig in starts if wid not in led.config]
            for wid, sig in starts:
                old = led.config.get(wid)
                if old is not None and old[0] != sig:
                    log.warning(
                        "config scenario window %s changed its start (%s -> %s) after it was"
                        " recorded; the recorded truth keeps the old start", wid, old[0] or "history start",
                        sig or "history start",
                    )
            if new:
                led = self.store.mut(LEDGER_KIND, "all")
                for wid, sig in new:
                    led.config[wid] = [sig, self.clock.isoformat()]
                    log.warning(
                        "config scenario window %s is new since the simulation started; it applies"
                        " from %s at the earliest", wid, self.clock.isoformat(),
                    )
        led = self.store.get(LEDGER_KIND, "all")
        known = {wid: datetime.fromisoformat(v[1]) for wid, v in led.config.items() if v[1]}
        if known:
            sm.set_known_at(known)

    def refresh_scenarios(self) -> datetime:
        """Re-read ``sim.scenario_overrides``; returns the time to advance to.

        The read runs under the same advisory lock the CLI takes while it picks a
        switch time and inserts an override, and the returned time (the database
        clock, capped at this host's wall clock) is taken while the lock is held.
        The CLI picks ``at = max(database clock, committed sim clock + 1 us)``,
        so an override the read did not see lies after the returned time.

        A row first seen with ``at`` at or behind the engine clock (clock skew,
        manual SQL) is late: its window start would never be recorded. Its
        ``at`` is moved to ``clock + 1 us`` in the table (in this locked
        transaction), so every later read, a restart included, sees the same row.
        """
        from ..scenarios.manager import LEDGER_KIND

        sm = self.world.scenarios
        if sm is None or not hasattr(sm, "load_overrides"):
            return utcnow()
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (db.SCENARIO_LOCK,))
            cur.execute(
                "SELECT id, scenario_id, action, at, params FROM sim.scenario_overrides ORDER BY id"
            )
            rows = [list(r) for r in cur.fetchall()]
            led = self.store.get(LEDGER_KIND, "all")
            known = set(led.overrides) if led is not None else set()
            new_ids: list[int] = []
            for r in rows:
                oid = int(r[0])
                r[3] = r[3].astimezone(UTC)
                if oid in known:
                    continue
                new_ids.append(oid)
                if self.clock is not None and r[3] <= self.clock:
                    late = self.clock + EPS
                    log.warning(
                        "scenario override #%d (%s %s) has its switch time %s at or behind the"
                        " simulation clock; moved to %s", oid, r[2], r[1], r[3].isoformat(), late.isoformat(),
                    )
                    cur.execute("UPDATE sim.scenario_overrides SET at = %s WHERE id = %s", (late, oid))
                    r[3] = late
            cur.execute("SELECT clock_timestamp()")
            db_now = cur.fetchone()[0].astimezone(UTC)
        self.conn.commit()
        if new_ids and led is not None:
            led = self.store.mut(LEDGER_KIND, "all")
            led.overrides = sorted(known | set(new_ids))
        sm.load_overrides([tuple(r) for r in rows])
        return min(db_now, utcnow())

    # -- commit ------------------------------------------------------------
    def commit(self, sim_from: datetime, sim_to: datetime, mode: str, events: int, fresh=False) -> dict:
        t0 = time.perf_counter()
        try:
            with self.conn.cursor() as cur:
                ent = self.store.flush(cur)
                q = self.queue.flush(cur)
                # Live: contract columns carry the commit time (see module doc),
                # taken as late as possible, i.e. right before the business rows.
                stamp = max(utcnow(), sim_to) if self.commit_time_stamps else None
                rows = self.batch.flush(cur, stamp=stamp)
                if fresh:
                    cur.execute(
                        "INSERT INTO sim.clock (id, sim_time, history_start, seed, config_fingerprint,"
                        " started_wall, updated_wall) VALUES (1, %s, %s, %s, %s, now(), now())",
                        (sim_to, self.world.history_start, self.world.seed, self.cfg.fingerprint()),
                    )
                else:
                    # Fence: only the state this engine last committed may move on.
                    # The fingerprint the simulation started with is kept.
                    cur.execute(
                        "UPDATE sim.clock SET sim_time = %s, updated_wall = now()"
                        " WHERE id = 1 AND seed = %s AND sim_time = %s",
                        (sim_to, self.world.seed, self._committed),
                    )
                    if cur.rowcount != 1:
                        raise StateMismatch(
                            "sim.clock no longer holds the state this engine committed (reset, or a"
                            " second writer?); stopping without writing this slice"
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
        self._committed = sim_to
        self.batch.clear()
        self.store.mark_flushed()
        self.queue.mark_flushed()
        self._state_rows += sum(ent) + sum(q)
        if self._state_rows >= VACUUM_EVERY_STATE_ROWS:
            self.vacuum_state(skip_locked=True)  # never wait for a running autovacuum
        return {"rows": rows, "entities": ent, "queue": q}

    # -- maintenance -----------------------------------------------------------
    def vacuum_state(self, analyze: bool = False, skip_locked: bool = False) -> None:
        """VACUUM the state tables (``db.STATE_TABLES``) between commits.

        Pure database housekeeping: it changes no rows, so determinism is
        unaffected. A failure is logged, never fatal.
        """
        self._state_rows = 0
        t0 = time.perf_counter()
        try:
            db.vacuum(self.conn, db.STATE_TABLES, analyze=analyze, skip_locked=skip_locked)
        except psycopg.Error as e:
            log.warning("vacuum of the simulator state tables failed: %s", e)
            return
        log.debug("vacuumed state tables in %.2fs", time.perf_counter() - t0)

    def after_backfill(self) -> None:
        """After a backfill: VACUUM (ANALYZE) the state tables, ANALYZE business and truth.

        Fresh statistics right away, so BI queries plan well on the new history
        without waiting for autovacuum. Called by ``sim start`` / ``sim backfill``,
        not by the catch-up inside :meth:`run_live`.
        """
        t0 = time.perf_counter()
        self.vacuum_state(analyze=True)
        try:
            n = db.analyze(self.conn)
        except psycopg.Error as e:
            log.warning("ANALYZE after backfill failed: %s", e)
            return
        log.info("vacuumed state tables and analyzed %d tables in %.1fs", n, time.perf_counter() - t0)

    # -- advancing -----------------------------------------------------------
    def _process_slice(self, start: datetime, end: datetime) -> int:
        w = self.world
        sm = w.scenarios
        if sm is not None and hasattr(sm, "advance_to"):
            sm.advance_to(start)
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
        sm = self.world.scenarios
        switches = sm is not None and hasattr(sm, "next_switch")
        while self.clock < target and not self._stop.is_set():
            end = min(self.clock + slice_len, target)
            if switches:
                # Never straddle a scenario switch: the slice ends just before it.
                sw = sm.next_switch(self.clock)
                if sw is not None and sw < end:
                    end = sw
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
        """Advance at backfill speed to ``until`` (default: now, taken under the scenario lock)."""
        bf = self.cfg.backfill
        now = self.refresh_scenarios()
        return self.advance(
            until or now,
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
        """Run until stopped. Catches up at backfill speed first if behind.

        Everything committed here, the catch-up included, stamps
        ``created_at``/``updated_at`` with the commit time (see module doc).
        """
        tick = timedelta(seconds=self.cfg.tick_seconds)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except ValueError:  # not in main thread (tests)
                pass
        ticks = 0
        self.commit_time_stamps = True
        try:
            while not self._stop.is_set():
                now = self.refresh_scenarios()
                # The target runs on the database clock (capped at the host clock).
                # When the host clock is ahead of the database clock, the sleep below
                # must wait on the target's timeline, or it never sleeps and the loop
                # spins with millisecond slices.
                lag = max(timedelta(0), utcnow() - now)
                assert self.clock is not None
                if now - self.clock > 4 * tick:
                    log.info("catching up from %s to %s", self.clock.isoformat(), now.isoformat())
                    self.backfill(now, progress)
                    continue
                advanced = now > self.clock
                if advanced:
                    # One slice and one commit per tick: the sleep below wakes a few ms
                    # after clock + tick, and a fixed tick-length slice would leave a
                    # second, empty slice (and commit) for those milliseconds.
                    span = max(tick, now - self.clock)
                    self.advance(now, span, span, "live")
                    ticks += 1
                    if progress:
                        progress(self.clock, ticks)
                if max_ticks is not None and ticks >= max_ticks:
                    break
                # Wake when the target's clock (host clock - lag) reaches clock + tick.
                sleep_for = (self.clock + tick + lag - utcnow()).total_seconds()
                if sleep_for <= 0 and not advanced:
                    # Nothing to do (e.g. a clock stepped back): never spin.
                    sleep_for = min(tick.total_seconds(), MIN_IDLE_SECONDS)
                if sleep_for > 0:
                    self._stop.wait(sleep_for)
        finally:
            self.commit_time_stamps = False
