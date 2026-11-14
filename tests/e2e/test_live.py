"""Live operation against a real database: watermark-safe stamping, restarts under a
non-UTC session, causal scenario switches, the single-writer lock, config changes and
customer eviction."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from cpgsim import cli, db
from cpgsim.domain import customers
from cpgsim.engine.runner import EngineLocked, Simulation, StateMismatch, utcnow
from cpgsim.engine.world import UTC
from cpgsim.writers import keys

from .conftest import BUSINESS, business_tables, digest, primary_key, simulation, small_cfg, utc

pytestmark = pytest.mark.db

EPS = timedelta(microseconds=1)
CONTRACT = ("created_at", "updated_at")


@contextmanager
def sim_on(dsn: str, cfg, start: date | None = None, **conn_kw):
    conn = psycopg.connect(dsn, **conn_kw)
    try:
        sim = Simulation(cfg, conn)
        sim.open(history_start=start)
        yield sim
    finally:
        conn.close()


def q(dsn: str, query, params=()):
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def business_digest(dsn: str) -> dict[str, tuple]:
    """Like ``digest`` but without the contract timestamp columns."""
    out = {}
    with psycopg.connect(dsn) as conn:
        for s, t in business_tables(conn):
            cols = [
                r[0] for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = %s"
                    " AND table_name = %s AND column_name <> ALL(%s) ORDER BY ordinal_position",
                    (s, t, list(CONTRACT)),
                ).fetchall()
            ]
            pk = sql.SQL(", ").join(sql.Identifier(c) for c in primary_key(conn, s, t))
            row = sql.SQL("ROW({})::text").format(sql.SQL(", ").join(sql.Identifier(c) for c in cols))
            qq = sql.SQL(
                "SELECT count(*), md5(coalesce(string_agg({row}, E'\\n' ORDER BY {pk}), '')) FROM {s}.{t}"
            ).format(row=row, pk=pk, s=sql.Identifier(s), t=sql.Identifier(t))
            out[f"{s}.{t}"] = tuple(conn.execute(qq).fetchone())
    return out


def assert_same(a: dict, b: dict) -> None:
    diff = {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}
    assert not diff, f"tables differ: {diff}"


def insert_override(dsn: str, scenario_id: str, action: str, at: datetime, params: str = "{}") -> int:
    with psycopg.connect(dsn) as conn:
        oid = conn.execute(
            "INSERT INTO sim.scenario_overrides (scenario_id, action, at, params) VALUES (%s, %s, %s, %s)"
            " RETURNING id",
            (scenario_id, action, at, params),
        ).fetchone()[0]
        conn.commit()
    return oid


# -- (e) contract timestamps in live mode ---------------------------------------------


@pytest.mark.slow
def test_live_stamps_commit_time_and_leaves_business_columns_alone(fresh_db):
    a, b = fresh_db(), fresh_db()
    cfg = small_cfg()
    start = (utcnow() - timedelta(days=3)).date()
    t_backfill = utcnow() - timedelta(hours=4)
    with simulation(a, cfg, start) as sim:
        sim.backfill(t_backfill)
        before_live = utcnow()
        sim.run_live(max_ticks=1)  # catches up about 4 h (one backfill-sized commit), then one tick
        clock = sim.clock
    assert clock >= before_live

    with psycopg.connect(a) as conn:
        n_live = n_old_business = 0
        for s, t in business_tables(conn):
            if s not in BUSINESS:
                continue
            ident = sql.Identifier(s, t)
            late = conn.execute(
                sql.SQL("SELECT count(*), min(created_at), min(updated_at) FROM {} WHERE updated_at > %s").format(ident),
                (t_backfill,),
            ).fetchone()
            if late[0]:
                n_live += late[0]
                # Everything committed by run_live carries the commit time, never an
                # earlier event time: a load that started before that commit misses nothing.
                assert late[2] >= before_live, (s, t, late)
            new = conn.execute(
                sql.SQL("SELECT min(created_at) FROM {} WHERE created_at > %s").format(ident), (t_backfill,)
            ).fetchone()[0]
            assert new is None or new >= before_live, (s, t, new)
        # ... while business timestamps keep the simulated event time
        n_old_business = conn.execute(
            "SELECT count(*) FROM shop.orders WHERE created_at >= %s AND placed_at < %s",
            (before_live, before_live - timedelta(minutes=30)),
        ).fetchone()[0]
    assert n_live > 100 and n_old_business > 0

    # The business is the one a plain backfill to the same clock produces.
    with simulation(b, cfg, start) as sim:
        sim.backfill(clock)
    assert_same(business_digest(a), business_digest(b))


# -- (a) restart under a DST session time zone ----------------------------------------


@pytest.mark.slow
def test_restart_on_a_berlin_session_gives_the_same_rows(fresh_db):
    a, b = fresh_db(), fresh_db()
    start, until = date(2025, 10, 13), utc(date(2025, 11, 3))  # DST ends 26 October
    with sim_on(a, small_cfg(), start) as sim:
        sim.backfill(until)
    berlin = {"options": "-c TimeZone=Europe/Berlin"}
    with sim_on(b, small_cfg(), start, **berlin) as sim:
        sim.backfill(utc(date(2025, 10, 22), 9.25))
    with sim_on(b, small_cfg(), start, **berlin) as sim:
        assert sim.world.now.tzinfo is UTC
        assert all(ev.due.tzinfo is UTC for _, _, _, ev in sim.queue._heap)
        sim.backfill(utc(date(2025, 10, 27), 2.5))
    with sim_on(b, small_cfg(), start, **berlin) as sim:
        sim.backfill(until)
    assert_same(digest(a), digest(b))


# -- (b, f) scenario switches -------------------------------------------------------


@pytest.mark.slow
def test_scenario_switches_do_not_depend_on_when_the_engine_saw_them(fresh_db):
    """Same override rows: engine running up to the switch, engine knowing them from
    the start, engine down for hours across each switch. Identical rows."""
    start, until = date(2025, 11, 10), utc(date(2025, 11, 24))
    t1 = utc(date(2025, 11, 14), 14)  # enable promo_margin_trap
    t2 = utc(date(2025, 11, 18), 10)  # disable it
    cfg = small_cfg(scenarios=[])
    e, f, g = fresh_db(), fresh_db(), fresh_db()

    with simulation(e, cfg, start) as sim:  # running: sees each switch a second before
        sim.backfill(t1 - timedelta(seconds=1))
        insert_override(e, "promo_margin_trap", "enable", t1)
        sim.backfill(t2 - timedelta(seconds=1))
        insert_override(e, "promo_margin_trap", "disable", t2)
        sim.backfill(until)

    insert_override(f, "promo_margin_trap", "enable", t1)  # known before the first run
    insert_override(f, "promo_margin_trap", "disable", t2)
    with simulation(f, cfg, start) as sim:
        sim.backfill(until)

    with simulation(g, cfg, start) as sim:  # down 8 h before each switch, catches up after
        sim.backfill(t1 - timedelta(hours=8))
    insert_override(g, "promo_margin_trap", "enable", t1)
    with simulation(g, cfg, start) as sim:
        sim.backfill(t2 - timedelta(hours=8))
    insert_override(g, "promo_margin_trap", "disable", t2)
    with simulation(g, cfg, start) as sim:
        sim.backfill(until)

    de = digest(e)
    assert_same(de, digest(f))
    assert_same(de, digest(g))

    # (f) the shop promotion ends at the disable, as an update incremental loads pick up
    pid = keys.shop_promotion_id(cfg.seed, 900_000 + 1000 + 1)
    (starts, ends, created, updated), = q(
        e, "SELECT starts_at, ends_at, created_at, updated_at FROM shop.promotions WHERE id = %s", (pid,)
    )
    assert starts == t1 and created == t1
    assert ends == t2 - timedelta(seconds=1) and updated == t2
    ((end_at,),) = q(e, "SELECT end_at FROM truth.scenario_windows WHERE window_id = 'promo_margin_trap:cli:1'")
    assert end_at == t2
    last, first = q(
        e, "SELECT max(o.placed_at), min(o.placed_at) FROM shop.promo_redemptions r"
        " JOIN shop.orders o ON o.id = r.order_id WHERE r.promotion_id = %s", (pid,)
    )[0]
    assert first >= t1 and last < t2


def test_late_override_is_moved_after_the_clock_recorded_and_announced(fresh_db):
    dsn = fresh_db()
    cfg = small_cfg(scenarios=[])
    start = date(2025, 11, 10)
    t0 = utc(date(2025, 11, 12), 9)
    with simulation(dsn, cfg, start) as sim:
        sim.backfill(t0)
        # A switch time behind the clock (CLI host clock behind, manual SQL ...).
        oid = insert_override(dsn, "promo_margin_trap", "enable", t0 - timedelta(hours=2))
        sim.backfill(t0 + timedelta(days=2))
    ((at,),) = q(dsn, "SELECT at FROM sim.scenario_overrides WHERE id = %s", (oid,))
    assert at == t0 + EPS  # persisted: a restart rebuilds the same window
    ((start_at, source),) = q(
        dsn, "SELECT start_at, source FROM truth.scenario_windows WHERE window_id = %s", (f"promo_margin_trap:cli:{oid}",)
    )
    assert start_at == t0 + EPS and source == "cli"
    pid = keys.shop_promotion_id(cfg.seed, 900_000 + 1000 + oid)
    ((n_promo,),) = q(dsn, "SELECT count(*) FROM shop.promotions WHERE id = %s AND starts_at = %s", (pid, t0 + EPS))
    ((n_red,),) = q(dsn, "SELECT count(*) FROM shop.promo_redemptions WHERE promotion_id = %s", (pid,))
    assert n_promo == 1 and n_red > 0
    ((orphans,),) = q(
        dsn, "SELECT count(*) FROM shop.promo_redemptions r WHERE NOT EXISTS"
        " (SELECT 1 FROM shop.promotions p WHERE p.id = r.promotion_id)"
    )
    assert orphans == 0


def test_cli_switch_time_is_after_the_committed_clock(fresh_db, monkeypatch, capsys):
    dsn = fresh_db()
    for k in ("CPGSIM_DSN", "CPGSIM_CONFIG", "CPGSIM_OVERLAY"):
        monkeypatch.delenv(k, raising=False)
    with simulation(dsn, small_cfg(), date(2025, 11, 10)) as sim:
        sim.backfill(utc(date(2025, 11, 11)))
        future = utcnow() + timedelta(hours=1)  # a clock ahead of the database clock
        with psycopg.connect(dsn) as conn:
            conn.execute("UPDATE sim.clock SET sim_time = %s", (future,))
            conn.commit()
        assert cli.main(["--dsn", dsn, "scenario", "enable", "carrier_decay"]) == 0
    ((at,),) = q(dsn, "SELECT at FROM sim.scenario_overrides")
    assert at == future + EPS


# -- (c) single writer ------------------------------------------------------------


def test_one_writer_reset_refuses_and_commits_are_fenced(fresh_db, monkeypatch, capsys):
    dsn = fresh_db()
    for k in ("CPGSIM_DSN", "CPGSIM_CONFIG", "CPGSIM_OVERLAY"):
        monkeypatch.delenv(k, raising=False)
    cfg = small_cfg()
    start = date(2025, 11, 10)
    conn_a = psycopg.connect(dsn)
    try:
        a = Simulation(cfg, conn_a)
        a.open(history_start=start)
        a.backfill(utc(date(2025, 11, 11)))

        # a second engine refuses to start
        with psycopg.connect(dsn) as conn_b:
            with pytest.raises(EngineLocked, match="another generator is running"):
                Simulation(cfg, conn_b).open(history_start=start)
        assert cli.main(["--dsn", dsn, "backfill"]) == 1
        assert "another generator is running" in capsys.readouterr().err

        # reset refuses while the engine runs
        assert cli.main(["--dsn", dsn, "reset", "--yes"]) == 1
        assert "generator is running" in capsys.readouterr().err
        assert q(dsn, "SELECT count(*) FROM sim.clock")[0][0] == 1

        # releasing the lock (or ending the process) lets reset through; the stale
        # engine then cannot write into the new, empty timeline
        a.close()
        assert cli.main(["--dsn", dsn, "reset", "--yes"]) == 0
        orders = q(dsn, "SELECT count(*) FROM shop.orders")[0][0]
        assert orders == 0
        with pytest.raises(StateMismatch, match="sim.clock no longer holds"):
            a.backfill(utc(date(2025, 11, 12)))
        assert q(dsn, "SELECT count(*) FROM shop.orders")[0][0] == 0
        assert q(dsn, "SELECT count(*) FROM sim.entity_state")[0][0] == 0
    finally:
        conn_a.close()

    # leftovers without a clock are refused instead of merged into a new timeline
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO sim.entity_state VALUES ('customer', '1', '{}')")
        conn.commit()
    with pytest.raises(StateMismatch, match="run `sim reset`"):
        with simulation(dsn, cfg, start):
            pass
    assert cli.main(["--dsn", dsn, "reset", "--yes"]) == 0


def test_reset_force_terminates_the_running_engine(fresh_db, monkeypatch, capsys):
    dsn = fresh_db()
    for k in ("CPGSIM_DSN", "CPGSIM_CONFIG", "CPGSIM_OVERLAY"):
        monkeypatch.delenv(k, raising=False)
    conn_a = psycopg.connect(dsn)
    try:
        a = Simulation(small_cfg(), conn_a)
        a.open(history_start=date(2025, 11, 10))
        assert cli.main(["--dsn", dsn, "reset", "--yes", "--force"]) == 0
        assert q(dsn, "SELECT count(*) FROM sim.clock")[0][0] == 0
        with pytest.raises(psycopg.OperationalError):
            a.backfill(utc(date(2025, 11, 11)))
    finally:
        conn_a.close()


# -- (d) config changes -------------------------------------------------------------


def test_config_change_is_recorded_once_and_the_start_fingerprint_kept(fresh_db, monkeypatch, capsys):
    dsn = fresh_db()
    for k in ("CPGSIM_DSN", "CPGSIM_CONFIG", "CPGSIM_OVERLAY"):
        monkeypatch.delenv(k, raising=False)
    start = date(2025, 11, 10)
    cfg_a = small_cfg()
    cfg_b = small_cfg(channels={"web": {"base_orders_per_day": 61}})
    with simulation(dsn, cfg_a, start) as sim:
        sim.backfill(utc(date(2025, 11, 11)))
    # tick/backfill settings are not a config change
    with simulation(dsn, small_cfg(tick_seconds=5, backfill={"tick_minutes": 15}), start) as sim:
        sim.backfill(utc(date(2025, 11, 11), 6))
    assert q(dsn, "SELECT count(*) FROM sim.config_history")[0][0] == 0
    for _ in range(2):  # restarted twice with the changed config: one record
        with simulation(dsn, cfg_b, start) as sim:
            sim.backfill(sim.clock + timedelta(hours=3))
    hist = q(dsn, "SELECT old_fingerprint, new_fingerprint FROM sim.config_history")
    assert hist == [(cfg_a.fingerprint(), cfg_b.fingerprint())]
    assert q(dsn, "SELECT config_fingerprint FROM sim.clock")[0][0] == cfg_a.fingerprint()

    # an old version stored the legacy fingerprint: same config, upgraded silently
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE sim.clock SET config_fingerprint = %s", (cfg_a.legacy_fingerprint(),))
        conn.commit()
    with simulation(dsn, cfg_a, start):
        pass
    assert q(dsn, "SELECT config_fingerprint FROM sim.clock")[0][0] == cfg_a.fingerprint()
    assert q(dsn, "SELECT count(*) FROM sim.config_history")[0][0] == 1

    # sim status keeps showing that the history mixes configs
    capsys.readouterr()
    assert cli.main(["--dsn", dsn, "status"]) == 0
    out = capsys.readouterr().out
    assert "config changes  1 recorded" in out


# -- (g) customer eviction ---------------------------------------------------------


@pytest.mark.slow
def test_evicting_customers_changes_nothing_in_the_data(fresh_db, monkeypatch):
    """Customers who can never act again leave the store; nobody looks them up."""
    a, b = fresh_db(), fresh_db()
    start, until = date(2025, 9, 1), utc(date(2025, 10, 27))  # > 30 days: orders get cleaned up
    with simulation(a, small_cfg(), start) as sim:
        sim.backfill(utc(date(2025, 10, 6), 7.5))
    with simulation(a, small_cfg(), start) as sim:  # restart: eviction is restart-safe
        sim.backfill(until)
        kept = sim.store.count("customer")
    with monkeypatch.context() as m:
        m.setattr(customers, "maybe_evict", lambda world, c: None)
        with simulation(b, small_cfg(), start) as sim:
            sim.backfill(until)
            all_customers = sim.store.count("customer")
            dup_entities = sum(1 for cid, _ in sim.store.items("customer") if int(cid) >= keys.DUPLICATE_CUSTOMER_BASE)
    assert_same(digest(a), digest(b))
    ((n_shop,),) = q(a, "SELECT count(*) FROM shop.customers")
    # duplicate accounts (chaos) are shop records; only those that took over a person become entities
    ((n_dup,),) = q(a, "SELECT count(*) FROM truth.planted_effects WHERE effect = 'duplicate_record'")
    assert all_customers - dup_entities == n_shop - n_dup
    assert kept < 0.8 * n_shop, (kept, n_shop)
    ((stored,),) = q(a, "SELECT count(*) FROM sim.entity_state WHERE kind = 'customer'")
    assert stored == kept
