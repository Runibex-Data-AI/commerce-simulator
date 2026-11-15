"""Feeds writer: per-retailer layouts, load ids, restatement loads, insert-only (phase-2 design, 3.2 and 4.1)."""

from __future__ import annotations

import os
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cpgsim.config import load_config
from cpgsim.domain.events import FeedDelivered, MarketplaceInventoryRow, MarketplaceSalesRow, SelloutRow
from cpgsim.domain.reference import build_reference
from cpgsim.engine.sink import ContractViolation
from cpgsim.writers.base import Dispatcher, WriterContext
from cpgsim.writers.feeds import (
    FEED_LOADS,
    MP_INVENTORY,
    MP_SALES,
    RETAILER_SELLOUT,
    FeedsWriter,
    article_text,
    file_name,
    period_label,
)

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "sql" / "schemas" / "feeds.sql"
BERLIN = ZoneInfo("Europe/Berlin")
T0 = datetime(2026, 3, 9, 6, 41, tzinfo=timezone.utc)
T1 = datetime(2026, 3, 16, 7, 5, tzinfo=timezone.utc)
WS = date(2026, 3, 2)  # ISO week 10 of 2026
P03 = (date(2026, 2, 23), date(2026, 3, 29))  # retail period 2026P03 (ISO weeks 9-13)

_REF: dict = {}


def ref():
    if "ref" not in _REF:
        cfg = load_config(channels={"shopnova": {"enabled": True}, "belle": {"enabled": True}})
        _REF["cfg"], _REF["ref"] = cfg, build_reference(cfg)
    return _REF["cfg"], _REF["ref"]


def make_dispatcher() -> tuple[Dispatcher, FeedsWriter]:
    cfg, r = ref()
    w = FeedsWriter(WriterContext(seed=cfg.seed, cfg=cfg, ref=r, tz=BERLIN))
    return Dispatcher([w]), w


def skus(channel: str, n: int, d: date = WS) -> list[int]:
    return ref()[1].catalogue.retailer_assortment(channel, d)[:n]


def sellout_event(channel, feed_type, ps, pe, run=1, at=T0, dcs=None):
    _, r = ref()
    dcs = dcs or [a.account_id for a in r.accounts.dcs(channel)]
    ids = skus(channel, 3, ps)
    rows = []
    for i, dc in enumerate(dcs):
        for j, s in enumerate(ids):
            rows.append(SelloutRow(dc, "store", s, 10 * (i + 1) + j, 50.0 + j, 59.5 + j * 1.19, 7 - j))
        rows.append(SelloutRow(dc, "online", ids[0], 2 + i, 8.4, 10.0, None))
    return FeedDelivered(at=at, channel=channel, feed_type=feed_type, period_start=ps, period_end=pe, run=run,
                         restates_run=(run - 1) if run > 1 else None, sellout=rows)


def inserted(d: Dispatcher, table: str) -> list[dict]:
    return list(d.batch.table(table).inserts.values())


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------


def test_all_tables_are_insert_only():
    for spec in FeedsWriter.tables:
        assert spec.insert_only and not spec.soft_delete, spec.name
    assert [s.name for s in FeedsWriter.tables] == [FEED_LOADS, RETAILER_SELLOUT, MP_SALES, MP_INVENTORY]


def test_kranz_layout():
    d, _ = make_dispatcher()
    d.dispatch(sellout_event("drogerie_kranz", "sellout_weekly", WS, date(2026, 3, 8)))
    (load,) = inserted(d, FEED_LOADS)
    assert load == {
        "load_id": "KRZ_SO_20260302_01", "source": "KRANZ", "feed_type": "sellout_weekly",
        "period_start": WS, "period_end": date(2026, 3, 8), "file_name": "ABVERKAUF_KW10_2026_V1.csv",
        "delivered_at": T0, "row_count": 12, "is_restatement": False, "restates_load_id": None,
        "created_at": T0, "updated_at": T0,
    }
    rows = inserted(d, RETAILER_SELLOUT)
    assert len(rows) == 12
    # DC ordinals 1..3: Erfurt, Hamm, Günzburg
    assert sorted({r["region_code"] for r in rows}) == ["VZERF", "VZGUE", "VZHAM"]
    assert {r["sales_channel"] for r in rows} == {"STORE", "ONLINE"}
    assert {(r["retailer"], r["period_type"], r["period_label"], r["value_basis"], r["currency"])
            for r in rows} == {("KRANZ", "W", "2026-W10", "NET", "EUR")}
    cat = ref()[1].catalogue
    for r in rows:
        assert re.fullmatch(r"\d{6}", r["article_no"])
        assert r["article_text"] == r["article_text"].upper() and len(r["article_text"]) <= 40
        sku = next(s for s in cat.skus if cat.customer_article("drogerie_kranz", s.sku_id) == r["article_no"])
        assert r["article_text"] == article_text("drogerie_kranz", sku)
        if r["sales_channel"] == "ONLINE":
            assert r["stock_units"] is None and r["sales_value"] == 8.4  # net basis
        else:
            assert r["stock_units"] is not None
    # line_no 1..n in order region, sales channel, article_no
    assert [r["line_no"] for r in rows] == list(range(1, 13))
    order = [(r["region_code"], r["sales_channel"], r["article_no"]) for r in sorted(rows, key=lambda r: r["line_no"])]
    assert order == sorted(order)


def test_mylo_layout_and_restatement_load():
    d, _ = make_dispatcher()
    d.dispatch(sellout_event("mylo", "sellout_weekly", WS, date(2026, 3, 8)))
    d.dispatch(sellout_event("mylo", "sellout_weekly", WS, date(2026, 3, 8), run=2, at=T1))
    loads = {r["load_id"]: r for r in inserted(d, FEED_LOADS)}
    assert set(loads) == {"MYL_SO_20260302_01", "MYL_SO_20260302_02"}
    r1, r2 = loads["MYL_SO_20260302_01"], loads["MYL_SO_20260302_02"]
    assert r1["file_name"] == "mylo_sellout_2026-03-02_r1.txt" and not r1["is_restatement"]
    assert r2["file_name"] == "mylo_sellout_2026-03-02_r2.txt" and r2["is_restatement"]
    assert r2["restates_load_id"] == "MYL_SO_20260302_01" and r2["delivered_at"] == T1
    rows = inserted(d, RETAILER_SELLOUT)
    assert len(rows) == 2 * 8
    run1 = [r for r in rows if r["load_id"].endswith("_01")]
    assert sorted({r["region_code"] for r in run1}) == ["OST", "SUED"]
    assert {r["sales_channel"] for r in run1} == {"F", "O"}
    assert {(r["period_label"], r["value_basis"]) for r in run1} == {("KW10/2026", "GROSS")}
    assert all(r["article_no"].startswith("MY") and len(r["article_no"]) == 9 for r in run1)
    online = [r for r in run1 if r["sales_channel"] == "O"]
    assert {r["sales_value"] for r in online} == {10.0}  # gross basis
    cat = ref()[1].catalogue
    names = {s.name for s in cat.skus}
    assert all(r["article_text"] in names for r in run1)  # full material description, mixed case


def test_belle_layout():
    d, _ = make_dispatcher()
    d.dispatch(sellout_event("belle", "sellout_monthly", *P03))
    (load,) = inserted(d, FEED_LOADS)
    assert (load["load_id"], load["file_name"], load["feed_type"]) == (
        "BEL_SOM_20260223_01", "BELLE_ABS_2026P03_1.xlsx", "sellout_monthly")
    rows = inserted(d, RETAILER_SELLOUT)
    assert sorted({r["region_code"] for r in rows}) == ["BL-HE", "BL-NW"]  # Neu-Isenburg, Neuss
    assert {r["sales_channel"] for r in rows} == {"FIL", "ECOM"}
    assert {(r["period_type"], r["period_label"], r["value_basis"], r["period_start"], r["period_end"])
            for r in rows} == {("M", "2026P03", "GROSS", *P03)}
    cat = ref()[1].catalogue
    for r in rows:
        assert r["article_no"].startswith("BL")
        brand = r["article_text"].split(" - ", 1)[0]
        assert brand in {s.brand for s in cat.skus}


def test_marketplace_reports():
    d, _ = make_dispatcher()
    a, b = skus("shopnova", 2)
    day = date(2026, 3, 4)
    d.dispatch(FeedDelivered(at=T0, channel="shopnova", feed_type="mp_sales_daily", period_start=day,
                             period_end=day, run=1,
                             mp_sales=[MarketplaceSalesRow(b, 0, 0.0, 41), MarketplaceSalesRow(a, 3, 27.87, 30)]))
    d.dispatch(FeedDelivered(at=T0, channel="shopnova", feed_type="mp_inventory_daily", period_start=day,
                             period_end=day, run=1, mp_inventory=[MarketplaceInventoryRow(a, 120, 48)]))
    loads = {r["load_id"]: r for r in inserted(d, FEED_LOADS)}
    assert loads["SNV_SALES_20260304_01"]["file_name"] == "snv_vendor_sales_20260304_1.csv"
    assert loads["SNV_INV_20260304_01"]["file_name"] == "snv_vendor_inventory_20260304_1.csv"
    assert loads["SNV_SALES_20260304_01"]["row_count"] == 2 and loads["SNV_INV_20260304_01"]["row_count"] == 1
    assert {r["source"] for r in loads.values()} == {"SHOPNOVA"}
    sales = inserted(d, MP_SALES)
    cat = ref()[1].catalogue
    assert [r["product_code"] for r in sales] == [cat.customer_article("shopnova", a),
                                                 cat.customer_article("shopnova", b)]
    assert all(re.fullmatch(r"SNV\d{8}", r["product_code"]) for r in sales)
    assert sales[0]["units_ordered"] == 3 and sales[0]["ordered_revenue"] == 27.87
    assert sales[1]["units_ordered"] == 0 and sales[1]["glance_views"] == 41
    assert sales[0]["product_title"] == cat.get(a).name[:80] and sales[0]["currency"] == "EUR"
    (inv,) = inserted(d, MP_INVENTORY)
    assert (inv["report_date"], inv["sellable_on_hand"], inv["inbound_quantity"], inv["load_id"]) == (
        day, 120, 48, "SNV_INV_20260304_01")


def test_labels_and_names():
    assert period_label("drogerie_kranz", "sellout_weekly", date(2026, 1, 5)) == "2026-W02"
    assert period_label("mylo", "sellout_weekly", date(2025, 12, 29)) == "KW01/2026"
    assert period_label("belle", "sellout_monthly", date(2026, 11, 30)) == "2026P12"
    assert file_name("drogerie_kranz", "sellout_weekly", date(2025, 12, 29), 2) == "ABVERKAUF_KW01_2026_V2.csv"
    assert file_name("belle", "sellout_monthly", date(2026, 1, 26), 1) == "BELLE_ABS_2026P02_1.xlsx"


def test_writer_never_updates_and_rejects_a_repeated_load():
    d, _ = make_dispatcher()
    e = sellout_event("mylo", "sellout_weekly", WS, date(2026, 3, 8))
    d.dispatch(e)
    assert all(not d.batch.table(s.name).updates for s in FeedsWriter.tables)
    with pytest.raises(ContractViolation):
        d.dispatch(e)


# ---------------------------------------------------------------------------
# Postgres round trip (scratch database, dropped afterwards)
# ---------------------------------------------------------------------------

DSN = os.environ.get("CPGSIM_TEST_DSN")


@pytest.mark.db
@pytest.mark.skipif(not DSN, reason="CPGSIM_TEST_DSN not set")
def test_roundtrip_against_postgres():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"cpgsim_feeds_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        dsn = make_conninfo(**{**conninfo_to_dict(DSN), "dbname": name})
        with psycopg.connect(dsn, autocommit=True) as conn:
            ddl = SCHEMA_SQL.read_text()
            conn.execute(ddl)
            conn.execute(ddl)  # idempotent
        d, _ = make_dispatcher()
        day = date(2026, 3, 4)
        a = skus("shopnova", 1)[0]
        d.dispatch(sellout_event("drogerie_kranz", "sellout_weekly", WS, date(2026, 3, 8)))
        d.dispatch(sellout_event("belle", "sellout_monthly", *P03))
        d.dispatch(FeedDelivered(at=T0, channel="shopnova", feed_type="mp_sales_daily", period_start=day,
                                 period_end=day, run=1, mp_sales=[MarketplaceSalesRow(a, 3, 27.87, 30)]))
        d.dispatch(FeedDelivered(at=T0, channel="shopnova", feed_type="mp_inventory_daily", period_start=day,
                                 period_end=day, run=1, mp_inventory=[MarketplaceInventoryRow(a, 120, 0)]))
        with psycopg.connect(dsn) as conn:
            with conn.transaction(), conn.cursor() as cur:
                touched = d.batch.flush(cur)
        d.batch.clear()
        assert touched == {"feeds": 4 + 12 + 8 + 1 + 1}
        # a restatement arrives later as a new load; nothing is updated
        d.dispatch(sellout_event("drogerie_kranz", "sellout_weekly", WS, date(2026, 3, 8), run=2, at=T1))
        with psycopg.connect(dsn) as conn:
            with conn.transaction(), conn.cursor() as cur:
                d.batch.flush(cur)
        d.batch.clear()
        with psycopg.connect(dsn) as conn:
            loads = conn.execute(
                "SELECT load_id, is_restatement, restates_load_id, row_count, created_at = updated_at"
                " FROM feeds.feed_loads ORDER BY load_id").fetchall()
            assert loads == [
                ("BEL_SOM_20260223_01", False, None, 8, True),
                ("KRZ_SO_20260302_01", False, None, 12, True),
                ("KRZ_SO_20260302_02", True, "KRZ_SO_20260302_01", 12, True),
                ("SNV_INV_20260304_01", False, None, 1, True),
                ("SNV_SALES_20260304_01", False, None, 1, True),
            ]
            # latest load per period: the restatement's rows replace the original's
            latest = conn.execute(
                "SELECT DISTINCT ON (source, feed_type, period_start) load_id FROM feeds.feed_loads"
                " WHERE source = 'KRANZ' ORDER BY source, feed_type, period_start, delivered_at DESC, load_id DESC"
            ).fetchall()
            assert latest == [("KRZ_SO_20260302_02",)]
            n, online_stock = conn.execute(
                "SELECT count(*), count(stock_units) FILTER (WHERE sales_channel = 'ONLINE')"
                " FROM feeds.retailer_sellout WHERE load_id = 'KRZ_SO_20260302_01'").fetchone()
            assert (n, online_stock) == (12, 0)
            rev = conn.execute("SELECT ordered_revenue FROM feeds.marketplace_sales_daily").fetchone()[0]
            assert rev == Decimal("27.87")
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
