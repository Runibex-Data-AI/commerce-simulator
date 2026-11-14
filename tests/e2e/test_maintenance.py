"""Database housekeeping: state-table vacuum during a backfill, ANALYZE afterwards."""

from __future__ import annotations

import time
from datetime import date

import psycopg

from cpgsim.engine import runner

from .conftest import digest, simulation, small_cfg, utc

START = date(2025, 3, 3)


def _stats(dsn: str, table: str) -> tuple[int, object, object]:
    with psycopg.connect(dsn, autocommit=True) as c:
        for _ in range(50):  # vacuum/analyze counters are reported asynchronously
            c.execute("SELECT pg_stat_clear_snapshot()")
            row = c.execute(
                "SELECT vacuum_count, last_vacuum, coalesce(last_analyze, last_autoanalyze)"
                " FROM pg_stat_user_tables WHERE relid = %s::regclass",
                (table,),
            ).fetchone()
            if row[0] or row[2]:
                return row
            time.sleep(0.1)
        return row


def test_state_tables_are_vacuumed_and_business_tables_analyzed(fresh_db, monkeypatch):
    cfg = small_cfg()
    a, b = fresh_db(), fresh_db()
    monkeypatch.setattr(runner, "VACUUM_EVERY_STATE_ROWS", 2_000)
    with simulation(a, cfg, START) as sim:
        sim.backfill(utc(date(2025, 3, 17)))
        # The connection is left idle and usable: the engine keeps committing.
        assert sim.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        sim.backfill(utc(date(2025, 3, 20)))
        sim.after_backfill()
    vacuums, last_vacuum, _ = _stats(a, "sim.entity_state")
    assert vacuums >= 2 and last_vacuum is not None
    assert _stats(a, "sim.event_queue")[0] >= 2
    assert _stats(a, "shop.orders")[2] is not None  # after_backfill analyzed the business tables
    assert _stats(a, "erp.vbap")[2] is not None

    # Storage parameters from sim.sql (HOT-friendly fill factor, earlier autovacuum).
    with psycopg.connect(a) as c:
        opts = c.execute("SELECT reloptions FROM pg_class WHERE oid = 'sim.entity_state'::regclass").fetchone()[0]
    assert "fillfactor=70" in opts and "autovacuum_vacuum_scale_factor=0.05" in opts

    # Housekeeping changes no data: same rows as a run that never vacuums.
    monkeypatch.setattr(runner, "VACUUM_EVERY_STATE_ROWS", 10**12)
    with simulation(b, cfg, START) as sim:
        sim.backfill(utc(date(2025, 3, 20)))
    assert digest(a) == digest(b)
