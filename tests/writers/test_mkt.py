"""Marketing writer (``mkt``): rows, keys and the loading contract."""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cpgsim.domain.events import (
    AttributionRow,
    CampaignDefined,
    CampaignSpendReported,
    CampaignSpendRow,
    CampaignStatusChanged,
    InfluencerCodeRegistered,
    OrdersAttributed,
    SessionsReported,
    SessionsRow,
)
from cpgsim.domain.model import Campaign
from cpgsim.writers import keys
from cpgsim.writers.base import Dispatcher, WriterContext
from cpgsim.writers.mkt import ATTRIBUTIONS, CAMPAIGN_SPEND, CAMPAIGNS, INFLUENCER_CODES, SESSIONS, MktWriter

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
SCHEMAS = Path(__file__).resolve().parents[2] / "sql" / "schemas"
T0 = datetime(2026, 2, 15, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 3, 2, 23, 5, tzinfo=UTC)
T2 = datetime(2026, 4, 1, 22, 5, tzinfo=UTC)
SEED = 7


def make_dispatcher():
    w = MktWriter(WriterContext(seed=SEED, cfg=None, ref=None, tz=BERLIN))
    return Dispatcher([w]), w


def campaign(cid="PS-2026Q1-01", promo_id=None, code=None, channel="paid_search"):
    return Campaign(
        campaign_id=cid, name="Brand search 2026 Q1", channel=channel, objective="acquisition",
        start=date(2026, 3, 3), end=date(2026, 3, 31), daily_budget_eur=270.954, created_on=date(2026, 2, 15),
        defined_at=T0, influencer_code=code, promo_id=promo_id,
    )


def events():
    return [
        InfluencerCodeRegistered(at=T0, code="LENA10", handle="lena.glow", audience_platform="short_video",
                                 commission_pct=10.5, fixed_fee_eur=3550.0, valid_from=date(2023, 5, 1)),
        CampaignDefined(at=T0, campaign=campaign(), status="planned"),
        CampaignDefined(at=T0, campaign=campaign("EM-P20260305", promo_id=20260305, channel="email"), status="planned"),
        CampaignDefined(at=T0, campaign=campaign("IN-2026Q1-01", code="LENA10", channel="influencer"),
                        status="active"),
    ]


def test_rows_and_keys():
    d, _ = make_dispatcher()
    for e in events():
        d.dispatch(e)
    code = d.batch.table(INFLUENCER_CODES).pending("LENA10")
    assert code["influencer_handle"] == "lena.glow" and code["status"] == "active" and code["valid_to"] is None
    assert code["commission_pct"] == 10.5 and code["created_at"] == T0 and code["is_deleted"] is False
    ps = d.batch.table(CAMPAIGNS).pending("PS-2026Q1-01")
    assert ps["promo_ref"] is None and ps["daily_budget_eur"] == 270.95 and ps["status"] == "planned"
    assert (ps["start_date"], ps["end_date"], ps["created_date"]) == (date(2026, 3, 3), date(2026, 3, 31),
                                                                       date(2026, 2, 15))
    em = d.batch.table(CAMPAIGNS).pending("EM-P20260305")
    assert em["promo_ref"] == keys.shop_promotion_id(SEED, 20260305) and len(em["promo_ref"]) == 36
    assert d.batch.table(CAMPAIGNS).pending("IN-2026Q1-01")["influencer_code"] == "LENA10"

    d.dispatch(CampaignSpendReported(at=T1, rows=[CampaignSpendRow("PS-2026Q1-01", date(2026, 3, 2), 30100, 1053,
                                                                   271.234, 41)]))
    d.dispatch(SessionsReported(at=T1, session_date=date(2026, 3, 2), rows=[
        SessionsRow("paid_search", "DE", "mobile", 812, 590, 300, 24, 1234.567),
        SessionsRow("paid_search", "DE", "tablet", 90, 70, 30, 0, 0.0)]))
    d.dispatch(OrdersAttributed(at=T1, rows=[
        AttributionRow(order_id=4711, customer_id=88, attribution_date=date(2026, 3, 2), channel="paid_search",
                       campaign_id="PS-2026Q1-01", influencer_code=None, is_new_customer=True, revenue_eur=41.9),
        AttributionRow(order_id=4712, customer_id=50_000_088, attribution_date=date(2026, 3, 2), channel="direct",
                       campaign_id=None, influencer_code=None, is_new_customer=False, revenue_eur=12.5)]))
    sp = d.batch.table(CAMPAIGN_SPEND).pending(("PS-2026Q1-01", date(2026, 3, 2)))
    assert sp["spend_eur"] == 271.23 and sp["clicks"] == 1053 and sp["platform_conversions"] == 41
    s = d.batch.table(SESSIONS).pending((date(2026, 3, 2), "paid_search", "DE", "mobile"))
    assert s["revenue_eur"] == 1234.57 and s["orders"] == 24 and s["created_at"] == T1
    a = d.batch.table(ATTRIBUTIONS).pending("ATT-NV-10004711")
    assert a["order_ref"] == keys.shop_order_id(SEED, 4711) and a["customer_ref"] == keys.shop_customer_id(SEED, 88)
    assert a["model"] == "last_click" and a["is_new_customer"] is True and a["campaign_id"] == "PS-2026Q1-01"
    dup = d.batch.table(ATTRIBUTIONS).pending("ATT-NV-10004712")
    assert dup["customer_ref"] == keys.shop_customer_id(SEED, 50_000_088) and dup["campaign_id"] is None


def test_status_updates_coalesce_and_carry_the_end_date():
    d, _ = make_dispatcher()
    d.dispatch(CampaignDefined(at=T0, campaign=campaign(), status="planned"))
    d.dispatch(CampaignStatusChanged(at=T1, campaign_id="PS-2026Q1-01", status="active"))
    row = d.batch.table(CAMPAIGNS).pending("PS-2026Q1-01")
    assert row["status"] == "active" and row["created_at"] == T0 and row["updated_at"] == T1
    assert len(d.batch) == 1
    d.batch.clear()
    d.dispatch(CampaignStatusChanged(at=T2, campaign_id="PS-2026Q1-01", status="ended", end_date=date(2026, 3, 20)))
    upd = d.batch.table(CAMPAIGNS).updates[("PS-2026Q1-01",)]
    assert upd == {"status": "ended", "end_date": date(2026, 3, 20), "updated_at": T2}


def test_budget_update_moves_daily_budget_and_updated_at():
    d, _ = make_dispatcher()
    d.dispatch(CampaignDefined(at=T0, campaign=campaign(), status="active"))
    d.batch.clear()
    d.dispatch(CampaignStatusChanged(at=T1, campaign_id="PS-2026Q1-01", status="active", daily_budget_eur=141.756))
    upd = d.batch.table(CAMPAIGNS).updates[("PS-2026Q1-01",)]
    assert upd == {"status": "active", "daily_budget_eur": 141.76, "updated_at": T1}


DSN = os.environ.get("CPGSIM_TEST_DSN")


@pytest.mark.skipif(not DSN, reason="CPGSIM_TEST_DSN not set")
def test_roundtrip_against_postgres():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"cpgsim_mkt_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(**{**conninfo_to_dict(DSN), "dbname": name})
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            schema = (SCHEMAS / "mkt.sql").read_text()
            conn.execute(schema)
            conn.execute(schema)  # idempotent
            tables = {r[0] for r in conn.execute(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables WHERE table_schema = 'mkt'")}
        assert tables == {s.name for s in MktWriter.tables}

        d, _ = make_dispatcher()
        for e in events():
            d.dispatch(e)
        d.dispatch(CampaignSpendReported(at=T1, rows=[
            CampaignSpendRow("PS-2026Q1-01", date(2026, 3, 2), 30100, 1053, 271.23, 41)]))
        d.dispatch(SessionsReported(at=T1, session_date=date(2026, 3, 2), rows=[
            SessionsRow("social", "AT", "desktop", 120, 90, 50, 2, 77.7)]))
        d.dispatch(OrdersAttributed(at=T1, rows=[AttributionRow(
            order_id=4711, customer_id=88, attribution_date=date(2026, 3, 2), channel="influencer",
            campaign_id="IN-2026Q1-01", influencer_code="LENA10", is_new_customer=True, revenue_eur=41.9)]))
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                d.batch.flush(cur)
            conn.commit()
        d.batch.clear()
        d.dispatch(CampaignStatusChanged(at=T1 + timedelta(hours=1), campaign_id="PS-2026Q1-01", status="active"))
        d.dispatch(CampaignStatusChanged(at=T2, campaign_id="PS-2026Q1-01", status="ended",
                                         end_date=date(2026, 3, 20)))
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                d.batch.flush(cur)
            conn.commit()

        with psycopg.connect(dsn) as conn:
            c = conn.execute("SELECT status, end_date, created_at, updated_at, promo_ref, is_deleted"
                             " FROM mkt.campaigns WHERE campaign_id = 'PS-2026Q1-01'").fetchone()
            assert c == ("ended", date(2026, 3, 20), T0, T2, None, False)
            assert conn.execute("SELECT promo_ref FROM mkt.campaigns WHERE campaign_id = 'EM-P20260305'"
                                ).fetchone()[0] == keys.shop_promotion_id(SEED, 20260305)
            a = conn.execute("SELECT attribution_id, influencer_code, model, attributed_revenue_eur::float"
                             " FROM mkt.attributions").fetchall()
            assert a == [("ATT-NV-10004711", "LENA10", "last_click", 41.9)]
            assert conn.execute("SELECT count(*) FROM mkt.sessions_daily").fetchone()[0] == 1
            assert conn.execute("SELECT spend_eur::float FROM mkt.campaign_spend_daily").fetchone()[0] == 271.23
            for t in MktWriter.tables:  # loading contract: updated_at indexed on every table
                idx = conn.execute("SELECT count(*) FROM pg_indexes WHERE schemaname = 'mkt' AND tablename = %s"
                                   " AND indexdef LIKE '%%(updated_at)%%'", (t.name.split(".")[1],)).fetchone()[0]
                assert idx == 1, t.name
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
