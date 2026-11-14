"""The loading contract for BI tools (SPEC: Loading contract for BI tools).

* every business table has a primary key, created_at/updated_at timestamptz
  NOT NULL and an index on updated_at;
* no hard deletes;
* snapshot tables are insert-only;
* a plain watermark load (``updated_at > last max``) run between commits
  reproduces every table exactly.
"""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
import pytest
from psycopg import sql

from .conftest import BUSINESS, READER, business_tables, primary_key, simulation, small_cfg, utc

pytestmark = [pytest.mark.db, pytest.mark.slow]

START = date(2025, 3, 3)
SNAPSHOT_TABLES = {"wms.stock_levels_daily"}


def test_schema_contract(fresh_db):
    dsn = fresh_db()
    with psycopg.connect(dsn) as conn:
        tables = [(s, t) for s, t in business_tables(conn) if s in BUSINESS]
        assert len(tables) >= 25
        for s, t in tables:
            assert primary_key(conn, s, t), f"{s}.{t} has no primary key"
            cols = dict(
                conn.execute(
                    "SELECT column_name, data_type || ':' || is_nullable FROM information_schema.columns"
                    " WHERE table_schema = %s AND table_name = %s",
                    (s, t),
                ).fetchall()
            )
            for c in ("created_at", "updated_at"):
                assert cols.get(c) == "timestamp with time zone:NO", f"{s}.{t}.{c}: {cols.get(c)}"
            if f"{s}.{t}" not in SNAPSHOT_TABLES:
                assert "is_deleted" in cols and "deleted_at" in cols, f"{s}.{t} lacks soft-delete columns"
            idx = conn.execute(
                "SELECT count(*) FROM pg_indexes WHERE schemaname = %s AND tablename = %s"
                " AND indexdef ~ '\\(updated_at[,)]'",
                (s, t),
            ).fetchone()[0]
            assert idx >= 1, f"{s}.{t} has no index on updated_at"


def test_reader_role_sees_business_schemas_only(fresh_db):
    dsn = fresh_db()
    with psycopg.connect(dsn) as conn:
        def can(priv: str, obj: str) -> bool:
            return conn.execute("SELECT has_schema_privilege(%s, %s, %s)", (READER, obj, priv)).fetchone()[0]

        for s in ("shop", "wms", "erp", "mkt", "feeds", "shelf"):
            assert can("USAGE", s) and not can("CREATE", s)
        for s in ("sim", "truth"):
            assert not can("USAGE", s)
        assert conn.execute(
            "SELECT has_table_privilege(%s, 'shop.orders', 'SELECT'),"
            " has_table_privilege(%s, 'shop.orders', 'UPDATE')",
            (READER, READER),
        ).fetchone() == (True, False)


def test_watermark_load_replicates_every_table_and_nothing_is_deleted(fresh_db):
    dsn = fresh_db()
    replica: dict[str, dict[tuple, tuple]] = {}
    watermark: dict[str, object] = {}
    snapshot_seen: dict[str, dict[tuple, tuple]] = {}

    def bi_load(conn) -> None:
        for s, t in business_tables(conn):
            name = f"{s}.{t}"
            pk = primary_key(conn, s, t)
            cols = [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position", (s, t)).fetchall()]
            pki = [cols.index(c) for c in pk]
            wm = watermark.get(name)
            q = sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(s), sql.Identifier(t))
            rows = conn.execute(q + sql.SQL(" WHERE updated_at > %s"), (wm,)).fetchall() if wm else conn.execute(q).fetchall()
            ui = cols.index("updated_at")
            store = replica.setdefault(name, {})
            for r in rows:
                key = tuple(r[i] for i in pki)
                if name in SNAPSHOT_TABLES and key in store:
                    raise AssertionError(f"snapshot row changed: {name} {key}")
                store[key] = tuple(r)
                if wm is None or r[ui] > wm:
                    wm = r[ui]
            if wm is not None:
                watermark[name] = wm
        conn.commit()

    cfg = small_cfg()
    with simulation(dsn, cfg, START) as sim:
        t = utc(START)
        end = utc(START + timedelta(days=12))
        with psycopg.connect(dsn) as bi:
            while t < end:
                t = min(t + timedelta(hours=5, minutes=17), end)  # deliberately off the hour
                sim.backfill(t)
                bi_load(bi)

    with psycopg.connect(dsn) as conn:
        for s, tname in business_tables(conn):
            name = f"{s}.{tname}"
            pk = primary_key(conn, s, tname)
            cols = [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position", (s, tname)).fetchall()]
            pki = [cols.index(c) for c in pk]
            full = {tuple(r[i] for i in pki): tuple(r) for r in conn.execute(
                sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(s), sql.Identifier(tname))).fetchall()}
            got = replica.get(name, {})
            assert got.keys() == full.keys(), f"{name}: replica has {len(got)} rows, table {len(full)}"
            stale = [k for k in full if full[k] != got[k]]
            assert not stale, f"{name}: {len(stale)} rows changed without moving updated_at, e.g. {stale[:3]}"
            later = conn.execute(
                sql.SQL("SELECT count(*) FROM {}.{} WHERE updated_at < created_at").format(
                    sql.Identifier(s), sql.Identifier(tname))).fetchone()[0]
            assert later == 0, f"{name}: updated_at before created_at"
        deleted = conn.execute(
            "SELECT relname, n_tup_del FROM pg_stat_user_tables"
            " WHERE schemaname = ANY(%s) AND n_tup_del > 0", (list(BUSINESS) + ["truth"],)).fetchall()
        assert not deleted, f"hard deletes in business schemas: {deleted}"
        assert sum(len(v) for v in replica.values()) > 20_000
