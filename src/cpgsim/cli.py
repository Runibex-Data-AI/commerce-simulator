"""The ``sim`` command-line tool.

Global options (accepted before or after the command): ``--config``,
``--overlay``, ``--dsn``, ``-v``.

Config resolution: ``--config`` > env ``CPGSIM_CONFIG`` > ``config/default.yaml``
in the source checkout; overlay: ``--overlay`` > env ``CPGSIM_OVERLAY`` (empty
means none). DSN: ``--dsn`` > env ``CPGSIM_DSN`` > ``database.dsn`` in config.

Seed override: ``sim reset --seed N`` stores ``N`` in ``sim.settings``. Every
command that runs or inspects the simulation then uses that seed instead of
``seed`` from the config, so a reset with a new seed sticks across restarts
(also in Docker, where the config file is read-only). ``sim reset
--clear-seed`` removes the override.

The engine and domain are imported lazily inside the commands that need them,
so ``init``, ``status``, ``scenario`` and ``truth`` work without them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TextIO
from zoneinfo import ZoneInfo

import psycopg
import yaml

from . import db
from .config import DEFAULT_CONFIG, Config, ScenarioCfg, load_config

log = logging.getLogger("cpgsim.cli")

UTC = timezone.utc
ENV_CONFIG = "CPGSIM_CONFIG"
ENV_OVERLAY = "CPGSIM_OVERLAY"
ENV_READER_PASSWORD = "CPGSIM_READER_PASSWORD"

BACKFILL_PROGRESS_SECONDS = 5.0
LIVE_PROGRESS_SECONDS = 300.0


class CliError(RuntimeError):
    """A user-facing error: printed without a traceback, exit code 1."""


# -- config ------------------------------------------------------------------


def config_paths(
    args: argparse.Namespace, env: Mapping[str, str] | None = None
) -> tuple[Path, Path | None]:
    """(config path, overlay path) after applying CLI > env > default."""
    env = os.environ if env is None else env
    cfg = getattr(args, "config", None) or env.get(ENV_CONFIG) or None
    ov = getattr(args, "overlay", None) or env.get(ENV_OVERLAY) or None
    return (Path(cfg) if cfg else DEFAULT_CONFIG), (Path(ov) if ov else None)


def load_cfg(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> Config:
    path, overlay = config_paths(args, env)
    if not path.exists():
        raise CliError(f"config file not found: {path} (use --config or {ENV_CONFIG})")
    if overlay is not None and not overlay.exists():
        raise CliError(f"overlay file not found: {overlay}")
    return load_config(path, overlay)


def apply_seed_override(cfg: Config, seed: int | None) -> Config:
    """Return ``cfg`` with the seed persisted by ``sim reset --seed`` (if any)."""
    if seed is None or seed == cfg.seed:
        return cfg
    log.info("using seed %d from sim.settings (config has %d)", seed, cfg.seed)
    return cfg.model_copy(update={"seed": int(seed)})


def scenario_dir(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> Path:
    """Generic scenario definitions live next to the config: ``<config dir>/scenarios``."""
    return config_paths(args, env)[0].parent / "scenarios"


# -- scenarios -----------------------------------------------------------------


def parse_params(items: Sequence[str] | None) -> dict[str, Any]:
    """``["k=v", ...]`` -> dict; values are parsed as YAML scalars (2.5, true, DE-BY)."""
    out: dict[str, Any] = {}
    for item in items or ():
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise CliError(f"--param expects key=value, got {item!r}")
        try:
            value = yaml.safe_load(raw) if raw.strip() else ""
        except yaml.YAMLError:
            value = raw
        if isinstance(value, (dict, list)) or isinstance(value, (date, datetime)):
            value = raw  # keep structured-looking text and dates as plain strings
        out[key] = value
    return out


@dataclass
class ScenarioDef:
    """A generic scenario definition from ``config/scenarios/<id>.yaml``."""

    id: str
    description: str
    params: dict[str, Any]
    path: Path


def scenario_definitions(directory: Path) -> dict[str, ScenarioDef]:
    out: dict[str, ScenarioDef] = {}
    if not directory.is_dir():
        return out
    for p in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict):
            raise CliError(f"{p}: expected a mapping")
        sid = str(data.get("id") or p.stem)
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise CliError(f"{p}: params must be a mapping")
        out[sid] = ScenarioDef(sid, str(data.get("description") or "").strip(), dict(params), p)
    return out


@dataclass
class Window:
    """A scenario window as the CLI sees it (for display; the engine is authoritative)."""

    scenario_id: str
    source: str  # config | cli
    start: datetime | None  # None: from the start of history
    end: datetime | None  # exclusive; None: open
    params: dict[str, Any] = field(default_factory=dict)

    def contains(self, t: datetime) -> bool:
        return (self.start is None or self.start <= t) and (self.end is None or t < self.end)


def scenario_windows(
    scenarios: Sequence[ScenarioCfg],
    overrides: Sequence[tuple[Any, ...]],
    tz: ZoneInfo,
) -> list[Window]:
    """Windows from config plus ``sim.scenario_overrides`` rows.

    Config dates are local calendar days; ``end`` is inclusive (a window ending
    2026-03-29 covers that whole Sunday). Override rows are
    ``(id, scenario_id, action, at, params)`` in id order: ``enable`` opens a
    window at ``at``; ``disable`` closes every window of that scenario open at
    ``at``.
    """
    out: list[Window] = []
    for sc in scenarios:
        if not sc.enabled:
            continue
        start = datetime.combine(sc.start, datetime.min.time(), tz).astimezone(UTC) if sc.start else None
        end = (
            datetime.combine(sc.end + timedelta(days=1), datetime.min.time(), tz).astimezone(UTC)
            if sc.end
            else None
        )
        out.append(Window(sc.id, "config", start, end, sc.params()))
    for _id, sid, action, at, params in overrides:
        at = at.astimezone(UTC)
        if action == "enable":
            out.append(Window(sid, "cli", at, None, dict(params or {})))
        elif action == "disable":
            for w in out:
                if w.scenario_id == sid and w.contains(at):
                    w.end = at
    return out


def active_scenarios(windows: Sequence[Window], t: datetime) -> list[Window]:
    return [w for w in windows if w.contains(t)]


# -- progress ------------------------------------------------------------------


class Progress:
    """Throttled progress lines for ``Simulation.backfill`` / ``run_live``.

    The runner calls ``progress(sim_time, n)``; ``n`` is the cumulative event
    count of the current backfill pass, or the tick count in live mode. The
    mode is read from ``sim.world.mode`` at call time. Backfill lines are printed
    at most every ``every`` seconds of wall time, live lines every ``live_every``.
    """

    def __init__(
        self,
        sim: Any,
        tz: ZoneInfo,
        out: TextIO | None = None,
        every: float = BACKFILL_PROGRESS_SECONDS,
        live_every: float = LIVE_PROGRESS_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sim, self.tz, self.out = sim, tz, out or sys.stdout
        self.every, self.live_every, self._clock = every, live_every, clock
        self.started = clock()
        self.events = 0  # events across all backfill passes
        self._pass_n = 0  # last ``n`` seen in the current backfill pass
        self._last_print: float | None = None
        self._events_at_print = 0
        self._last_live: float | None = None

    def _fmt(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M")

    def __call__(self, sim_time: datetime, n: int) -> None:
        now = self._clock()
        mode = getattr(getattr(self.sim, "world", None), "mode", "backfill")
        if mode == "live":
            if self._last_live is None or now - self._last_live >= self.live_every:
                lag = (datetime.now(UTC) - sim_time).total_seconds()
                self._print(f"live      sim {self._fmt(sim_time)}  lag {lag:,.0f}s  ticks {n:,}")
                self._last_live = now
            return
        if n < self._pass_n:  # a new backfill pass (catch-up inside run_live)
            self._pass_n = 0
        self.events += n - self._pass_n
        self._pass_n = n
        if self._last_print is not None and now - self._last_print < self.every:
            return
        since = self._last_print if self._last_print is not None else self.started
        rate = (self.events - self._events_at_print) / max(now - since, 1e-9)
        self._print(f"backfill  sim {self._fmt(sim_time)}  events {self.events:,}  {rate:,.0f} ev/s")
        self._last_print = now
        self._events_at_print = self.events

    def done(self, label: str = "backfill") -> None:
        el = self._clock() - self.started
        rate = self.events / el if el > 0 else 0.0
        self._print(f"{label} done: {self.events:,} events in {el:,.0f}s ({rate:,.0f} ev/s)")

    def _print(self, line: str) -> None:
        print(line, file=self.out, flush=True)


# -- helpers -------------------------------------------------------------------


def _table(rows: Sequence[Sequence[Any]], headers: Sequence[str], out: TextIO | None = None) -> None:
    out = out or sys.stdout
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    for j, r in enumerate(cells):
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip(), file=out)
        if j == 0:
            print("  ".join("-" * w for w in widths), file=out)


def _short(v: Any, width: int = 60) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str, sort_keys=True)
    return s if len(s) <= width else s[: width - 3] + "..."


def _fmt_td(td: timedelta) -> str:
    secs = int(td.total_seconds())
    sign = "-" if secs < 0 else ""
    secs = abs(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{sign}{h:02d}:{m:02d}:{s:02d}"


def _connect(args: argparse.Namespace, cfg: Config):
    return db.connect(db.resolve_dsn(getattr(args, "dsn", None), cfg))


def _cfg_for_db(args: argparse.Namespace, conn) -> Config:
    return apply_seed_override(load_cfg(args), db.persisted_seed(conn))


def _parse_until(s: str | None, tz: ZoneInfo) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise CliError(f"--until: not an ISO date/time: {s!r}") from e
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(UTC)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise CliError(f"--start: expected YYYY-MM-DD, got {s!r}") from e


def _read_clock(conn) -> tuple | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sim_time, history_start, seed, config_fingerprint, started_wall, updated_wall"
            " FROM sim.clock WHERE id = 1"
        )
        row = cur.fetchone()
    conn.commit()
    return row


def _read_overrides(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, scenario_id, action, at, params FROM sim.scenario_overrides ORDER BY id"
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


class _StopFlag:
    """SIGINT/SIGTERM during backfill: finish the current slice, commit, stop."""

    def __init__(self, sim: Any) -> None:
        self.sim = sim
        self.stopped = False
        self._old: dict[int, Any] = {}

    def __enter__(self) -> "_StopFlag":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._old[sig] = signal.signal(sig, self._handle)
            except ValueError:  # not in the main thread
                pass
        return self

    def _handle(self, *_a: Any) -> None:
        if self.stopped:  # second signal: give up immediately
            raise KeyboardInterrupt
        self.stopped = True
        log.warning("stop requested; committing the current slice")
        self.sim.stop()

    def __exit__(self, *_exc: Any) -> None:
        for sig, old in self._old.items():
            signal.signal(sig, old)


def _new_simulation(cfg: Config, conn):
    from .engine.runner import Simulation  # lazy: needs the full domain

    return Simulation(cfg, conn)


def _open(sim: Any, **kw: Any) -> bool:
    from .engine.runner import StateMismatch

    try:
        return bool(sim.open(**kw))
    except StateMismatch as e:
        raise CliError(str(e)) from e


def _backfill(sim: Any, cfg: Config, until: datetime | None) -> bool:
    """Run backfill with progress; returns False if stopped by a signal."""
    progress = Progress(sim, ZoneInfo(cfg.timezone))
    with _StopFlag(sim) as flag:
        sim.backfill(until, progress)
    progress.done()
    return not flag.stopped


# -- commands ------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    cfg = load_cfg(args)
    with _connect(args, cfg) as conn:
        db.init_db(conn, reader_password=os.environ.get(ENV_READER_PASSWORD) or None)
    print("database initialised")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    cfg = load_cfg(args)
    with _connect(args, cfg) as conn:
        target = f"{conn.info.dbname} on {conn.info.host}"
        if not args.yes:
            if not sys.stdin.isatty():
                raise CliError("refusing to reset without confirmation; pass --yes")
            answer = input(f"Empty all simulated data in {target}? Type 'yes' to continue: ")
            if answer.strip().lower() != "yes":
                print("aborted")
                return 1
        tables = db.reset_data(conn, seed=args.seed, clear_seed=args.clear_seed)
    print(f"reset {len(tables)} tables in {target}")
    if args.seed is not None:
        print(f"seed {args.seed} stored in sim.settings; it overrides the config seed")
    elif args.clear_seed:
        print("seed override removed; the config seed applies")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    cfg0 = load_cfg(args)
    with _connect(args, cfg0) as conn:
        cfg = _cfg_for_db(args, conn)
        tz = ZoneInfo(cfg.timezone)
        until = _parse_until(args.until, tz)
        start = _parse_date(args.start)
        if start is not None and start > datetime.now(tz).date():
            raise CliError(f"--start {start} lies in the future")
        if args.months is not None and args.months < 0:
            raise CliError("--months must be >= 0")
        sim = _new_simulation(cfg, conn)
        fresh = _open(sim, history_start=start, months=args.months)
        if not fresh and (start or args.months is not None):
            log.warning("a simulation already exists; --start/--months ignored (sim reset starts over)")
        _backfill(sim, cfg, until)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg0 = load_cfg(args)
    with _connect(args, cfg0) as conn:
        cfg = _cfg_for_db(args, conn)
        sim = _new_simulation(cfg, conn)
        _open(sim)
        progress = Progress(sim, ZoneInfo(cfg.timezone))
        sim.run_live(max_ticks=args.max_ticks, progress=progress)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Container entrypoint: wait for the DB, init, backfill if new, then run live."""
    cfg0 = load_cfg(args)
    dsn = db.resolve_dsn(getattr(args, "dsn", None), cfg0)
    conn = db.wait_for_db(dsn, attempts=args.db_attempts)
    with conn:
        db.init_db(conn, reader_password=os.environ.get(ENV_READER_PASSWORD) or None)
        cfg = _cfg_for_db(args, conn)
        sim = _new_simulation(cfg, conn)
        clock_missing = _read_clock(conn) is None
        _open(sim, months=cfg.history_months)
        if clock_missing:
            log.info("new simulation: backfilling %d months", cfg.history_months)
            if not _backfill(sim, cfg, None):
                return 0
        progress = Progress(sim, ZoneInfo(cfg.timezone))
        sim.run_live(progress=progress)
    return 0


def cmd_status(args: argparse.Namespace, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    cfg0 = load_cfg(args)
    with _connect(args, cfg0) as conn:
        cfg = _cfg_for_db(args, conn)
        tz = ZoneInfo(cfg.timezone)
        p = lambda *a: print(*a, file=out)  # noqa: E731
        p(f"database        {conn.info.dbname} on {conn.info.host}:{conn.info.port} as {conn.info.user}")
        try:
            clock = _read_clock(conn)
        except psycopg.errors.UndefinedTable:
            conn.rollback()
            p("state           not initialised (run: sim init)")
            return 1
        if clock is None:
            p("state           initialised, no simulation yet (run: sim backfill or sim start)")
            return 0
        sim_time, hist, seed, fp, started_wall, updated_wall = clock
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            wall = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM sim.event_queue")
            depth = cur.fetchone()[0]
            cur.execute("SELECT rows FROM sim.run_log WHERE wall_time > now() - interval '1 hour'")
            per_schema: dict[str, int] = {}
            for (rows,) in cur.fetchall():
                for k, v in (rows or {}).items():
                    per_schema[k] = per_schema.get(k, 0) + int(v)
            cur.execute(
                "SELECT wall_time, mode, sim_from, sim_to, events, rows, queue_depth, duration_ms"
                " FROM sim.run_log ORDER BY id DESC LIMIT 1"
            )
            last = cur.fetchone()
        conn.commit()
        loc = lambda t: t.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")  # noqa: E731
        p(f"sim clock       {loc(sim_time)}  (lag vs wall {_fmt_td(wall - sim_time)})")
        p(f"history start   {loc(hist)}")
        p(f"last commit     {loc(updated_wall)} (wall), started {loc(started_wall)}")
        fp_note = "matches config" if fp == cfg.fingerprint() else "DIFFERS from current config"
        p(f"seed            {seed}  (config fingerprint {fp_note})")
        p(f"queue depth     {depth:,}")
        if per_schema:
            parts = ", ".join(f"{k} {v:,}" for k, v in sorted(per_schema.items()))
            p(f"rows last hour  {parts}  (total {sum(per_schema.values()):,}, wall clock)")
        else:
            p("rows last hour  none")
        windows = scenario_windows(cfg.scenarios, _read_overrides(conn), tz)
        active = active_scenarios(windows, sim_time.astimezone(UTC))
        if active:
            p("active scenarios at sim time:")
            for w in active:
                since = loc(w.start) if w.start else "history start"
                until = loc(w.end) if w.end else "open"
                p(f"  {w.scenario_id:<22} {w.source:<6} {since} -> {until}  {_short(w.params)}")
        else:
            p("active scenarios at sim time: none")
        if last:
            lw, mode, sf, st, ev, rows, qd, ms = last
            p(
                f"last run        {loc(lw)} {mode}: {loc(sf)} -> {loc(st)}, {ev:,} events,"
                f" rows {_short(rows)}, queue {qd:,}, {ms:,} ms"
            )
    return 0


def _scenario_db_state(args: argparse.Namespace, cfg: Config) -> tuple[Config, datetime | None, list[tuple]] | None:
    """(cfg with seed override, sim clock, overrides), or None if no DB is reachable."""
    try:
        conn = _connect(args, cfg)
    except db.DsnError:
        return None
    except psycopg.OperationalError as e:
        log.warning("database not reachable, showing config only: %s", str(e).strip())
        return None
    with conn:
        try:
            clock = _read_clock(conn)
            overrides = _read_overrides(conn)
        except psycopg.errors.UndefinedTable:
            conn.rollback()
            return cfg, None, []
        return _cfg_for_db(args, conn), (clock[0].astimezone(UTC) if clock else None), overrides


def cmd_scenario_list(args: argparse.Namespace, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    cfg = load_cfg(args)
    tz = ZoneInfo(cfg.timezone)
    defs = scenario_definitions(scenario_dir(args))
    state = _scenario_db_state(args, cfg)
    clock: datetime | None = None
    overrides: list[tuple] = []
    if state is not None:
        cfg, clock, overrides = state
    windows = scenario_windows(cfg.scenarios, overrides, tz)
    fmt = lambda t: t.astimezone(tz).strftime("%Y-%m-%d %H:%M") if t else None  # noqa: E731
    ids = sorted({w.scenario_id for w in windows} | set(defs) | {s.id for s in cfg.scenarios})
    rows = []
    for sid in ids:
        ws = [w for w in windows if w.scenario_id == sid]
        disabled_cfg = [s for s in cfg.scenarios if s.id == sid and not s.enabled]
        sources = []
        if any(s.id == sid for s in cfg.scenarios):
            sources.append("config")
        if sid in defs:
            sources.append("generic")
        if any(w.source == "cli" for w in ws):
            sources.append("cli")
        wtxt = "; ".join(
            f"{fmt(w.start) or 'history start'} -> {fmt(w.end) or 'open'} ({w.source})" for w in ws
        )
        if not wtxt:
            wtxt = "disabled in config" if disabled_cfg else "-"
        if clock is None:
            st = "unknown" if state is None else "not started"
        else:
            st = "ACTIVE" if any(w.contains(clock) for w in ws) else "inactive"
        params = ws[-1].params if ws else (defs[sid].params if sid in defs else {})
        rows.append((sid, ",".join(sources), st, wtxt, _short(params, 70)))
    _table(rows, ("ID", "SOURCE", "STATE", "WINDOWS (local, end exclusive)", "PARAMS"), out)
    if clock is not None:
        print(f"\nstate at sim time {fmt(clock)}", file=out)
    return 0


def _scenario_params(args: argparse.Namespace, cfg: Config, sid: str) -> dict[str, Any]:
    defs = scenario_definitions(scenario_dir(args))
    if sid in defs:
        base = dict(defs[sid].params)
    else:
        from_cfg = [s for s in cfg.scenarios if s.id == sid]
        if not from_cfg:
            known = sorted(set(defs) | {s.id for s in cfg.scenarios})
            raise CliError(f"unknown scenario {sid!r}; known: {', '.join(known) or 'none'}")
        base = from_cfg[-1].params()
    base.update(parse_params(args.param))
    return base


def _switch_at(conn) -> datetime:
    """max(wall now, sim clock): never in the simulation's past."""
    now = datetime.now(UTC)
    clock = _read_clock(conn)
    return max(now, clock[0].astimezone(UTC)) if clock else now


def cmd_scenario_switch(args: argparse.Namespace) -> int:
    cfg0 = load_cfg(args)
    action = args.scenario_cmd
    with _connect(args, cfg0) as conn:
        cfg = _cfg_for_db(args, conn)
        if action == "enable":
            params = _scenario_params(args, cfg, args.id)
        else:
            known = set(scenario_definitions(scenario_dir(args))) | {s.id for s in cfg.scenarios}
            if args.id not in known:
                raise CliError(f"unknown scenario {args.id!r}")
            params = {}
        at = _switch_at(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sim.scenario_overrides (scenario_id, action, at, params)"
                " VALUES (%s, %s, %s, %s) RETURNING id",
                (args.id, action, at, json.dumps(params)),
            )
            oid = cur.fetchone()[0]
        conn.commit()
    tz = ZoneInfo(cfg.timezone)
    print(f"{action}d {args.id} at {at.astimezone(tz).isoformat(timespec='seconds')} (override #{oid})")
    if params:
        print(f"params: {json.dumps(params, sort_keys=True)}")
    return 0


def cmd_truth(args: argparse.Namespace, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    cfg0 = load_cfg(args)
    sid = args.scenario_id
    with _connect(args, cfg0) as conn:
        tz = ZoneInfo(cfg0.timezone)
        loc = lambda t: t.astimezone(tz).strftime("%Y-%m-%d %H:%M") if t else "open"  # noqa: E731
        with conn.cursor() as cur:
            cur.execute(
                "SELECT window_id, source, start_at, end_at, params FROM truth.scenario_windows"
                " WHERE scenario_id = %s ORDER BY start_at, window_id",
                (sid,),
            )
            windows = cur.fetchall()
            cur.execute(
                "SELECT entity_type, effect, count(*), count(DISTINCT entity_id),"
                " min(magnitude), avg(magnitude), max(magnitude),"
                " min(effective_at), max(effective_at)"
                " FROM truth.planted_effects WHERE scenario_id = %s"
                " GROUP BY entity_type, effect ORDER BY entity_type, effect",
                (sid,),
            )
            summary = cur.fetchall()
            cur.execute(
                "SELECT effective_at, entity_type, entity_id, effect, magnitude, system_refs, detail"
                " FROM truth.planted_effects WHERE scenario_id = %s"
                " ORDER BY effective_at, effect_id LIMIT 10",
                (sid,),
            )
            samples = cur.fetchall()
        conn.commit()
    if not windows and not summary:
        print(f"no ground truth recorded for {sid!r}", file=out)
        return 1
    num = lambda x: "" if x is None else f"{x:,.3f}"  # noqa: E731
    print(f"scenario windows for {sid}:", file=out)
    _table(
        [(w[0], w[1], loc(w[2]), loc(w[3]), _short(w[4], 80)) for w in windows],
        ("WINDOW", "SOURCE", "START", "END", "PARAMS"),
        out,
    )
    print("\nplanted effects:", file=out)
    _table(
        [
            (s[0], s[1], f"{s[2]:,}", f"{s[3]:,}", num(s[4]), num(s[5]), num(s[6]), loc(s[7]), loc(s[8]))
            for s in summary
        ],
        ("ENTITY_TYPE", "EFFECT", "ROWS", "ENTITIES", "MIN", "AVG", "MAX", "FIRST", "LAST"),
        out,
    )
    if samples:
        print("\nsample rows:", file=out)
        _table(
            [
                (loc(r[0]), r[1], r[2], r[3], num(r[4]), _short(r[5], 70), _short(r[6], 50))
                for r in samples
            ],
            ("EFFECTIVE", "ENTITY_TYPE", "ENTITY_ID", "EFFECT", "MAGNITUDE", "SYSTEM_REFS", "DETAIL"),
            out,
        )
    return 0


# -- parser --------------------------------------------------------------------


def _add_globals(p: argparse.ArgumentParser, suppress: bool) -> None:
    d = argparse.SUPPRESS if suppress else None
    p.add_argument("--config", metavar="PATH", default=d, help=f"config YAML (env {ENV_CONFIG})")
    p.add_argument("--overlay", metavar="PATH", default=d, help=f"private overlay YAML (env {ENV_OVERLAY})")
    p.add_argument("--dsn", default=d, help=f"Postgres connection string (env {db.ENV_DSN})")
    p.add_argument(
        "-v", "--verbose", action="count", default=argparse.SUPPRESS if suppress else 0,
        help="more logging (-v debug)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sim",
        description="cpg-commerce-simulator: realistic, continuously changing commerce data in Postgres.",
    )
    _add_globals(parser, suppress=False)
    common = argparse.ArgumentParser(add_help=False)
    _add_globals(common, suppress=True)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("init", parents=[common], help="create schemas, tables, indexes and roles")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("reset", parents=[common], help="empty all simulated data, keep the structure")
    p.add_argument("--seed", type=int, help="store a new seed (overrides the config seed)")
    p.add_argument("--clear-seed", action="store_true", help="remove a stored seed override")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("backfill", parents=[common], help="simulate history up to now (or --until)")
    p.add_argument("--months", type=int, help="history length for a new simulation (default: history_months)")
    p.add_argument("--start", metavar="YYYY-MM-DD", help="history start date for a new simulation")
    p.add_argument("--until", metavar="ISO", help="stop at this time (local time if no offset)")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("run", parents=[common], help="run live until stopped (catches up first)")
    p.add_argument("--max-ticks", type=int, help="stop after N live ticks")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("start", parents=[common], help="container entrypoint: wait, init, backfill if new, run")
    p.add_argument("--db-attempts", type=int, default=60, help="connection attempts before giving up")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("status", parents=[common], help="clock, lag, queue, recent rows, scenarios")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("scenario", parents=[common], help="list scenarios or switch one on or off")
    ssub = p.add_subparsers(dest="scenario_cmd", required=True, metavar="ACTION")
    sp = ssub.add_parser("list", parents=[common], help="scenarios in config and config/scenarios/")
    sp.set_defaults(func=cmd_scenario_list)
    sp = ssub.add_parser("enable", parents=[common], help="switch a scenario on from now")
    sp.add_argument("id")
    sp.add_argument(
        "--param", action="append", metavar="KEY=VALUE", help="override a parameter (repeatable)"
    )
    sp.set_defaults(func=cmd_scenario_switch)
    sp = ssub.add_parser("disable", parents=[common], help="switch a scenario off from now")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_scenario_switch)

    p = sub.add_parser("truth", parents=[common], help="print what a scenario planted")
    p.add_argument("scenario_id")
    p.set_defaults(func=cmd_truth)
    return parser


def _setup_logging(verbose: int) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: Sequence[str] | None = None) -> int:
    from pydantic import ValidationError

    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", 0) or 0)
    try:
        return int(args.func(args) or 0)
    except (CliError, db.DsnError, db.InitError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValidationError as e:
        print(f"error: invalid config:\n{e}", file=sys.stderr)
        return 1
    except psycopg.OperationalError as e:
        print(f"error: database: {str(e).strip()}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
