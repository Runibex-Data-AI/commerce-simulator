import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from cpgsim.domain.events import (
    CarrierScan,
    DeliveryChanged,
    DeliveryCreated,
    ReturnChanged,
    ReturnCreated,
    StockMoved,
    StockSnapshotRow,
    StockSnapshotTaken,
)
from cpgsim.domain.model import CARRIER_SCAN_CODES as NEUTRAL_SCAN_CODES
from cpgsim.domain.model import (
    DELIVERY_STATUSES,
    RETURN_REASONS,
    RETURN_STATUSES,
    STOCK_MOVEMENT_TYPES,
    Address,
    B2BAccount,
    Delivery,
    DeliveryLine,
    ReturnLine,
    SalesDoc,
    WebReturn,
)
from cpgsim.engine.sink import ContractViolation
from cpgsim.writers import keys
from cpgsim.writers.base import Dispatcher, WriterContext
from cpgsim.writers import wms
from cpgsim.writers.wms import (
    CARRIER_SCAN_CODES,
    SHIPMENT_STATUS_CODES,
    WmsWriter,
    carrier_code,
    carrier_event_id,
    item_code,
    snapshot_load_id,
)

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "sql" / "schemas" / "wms.sql"
BERLIN = ZoneInfo("Europe/Berlin")
# 2025-03-28 is before the DST change (UTC+1), 2025-04-01 after it (UTC+2).
T0 = datetime(2025, 3, 28, 9, 30, tzinfo=timezone.utc)
T1 = datetime(2025, 3, 31, 7, 15, tzinfo=timezone.utc)
T2 = datetime(2025, 4, 2, 12, 0, tzinfo=timezone.utc)


# -- stub reference data ---------------------------------------------------------

SKUS = {
    1: SimpleNamespace(sku_id=1, ean="2000000000015", weight_g=420, units_per_case=12),
    2: SimpleNamespace(sku_id=2, ean="2000000000022", weight_g=55, units_per_case=24),
}
DC_ADDR = Address(country="DE", region="DE-NW", postcode="44137", city="Dortmund")
ACCOUNTS = {
    "WEB-DE": B2BAccount("WEB-DE", "web_collective", "Webshop Endkunden DE", None, DC_ADDR),
    "KRANZ-DC1": B2BAccount("KRANZ-DC1", "retailer_dc", "Drogerie Kranz DC West", "drogerie_kranz", DC_ADDR),
}


def make_dispatcher(seed: int = 7) -> tuple[Dispatcher, WmsWriter]:
    ref = SimpleNamespace(
        catalogue=SimpleNamespace(get=SKUS.get),
        accounts=SimpleNamespace(numbers={"WEB-DE": 900001, "KRANZ-DC1": 200011}, get=ACCOUNTS.get),
    )
    w = WmsWriter(WriterContext(seed=seed, cfg=None, ref=ref, tz=BERLIN))
    return Dispatcher([w]), w


def sales_doc(kind: str) -> SalesDoc:
    return SalesDoc(
        sales_doc_id=9, doc_type=kind, sold_to="WEB-DE", ship_to="WEB-DE", currency="EUR",
        fx_rate_eur=1.0, created_at=T0, requested_date=T0.date(), external_ref=None, items=[],
        channel="web",
    )


def web_delivery(delivery_id: int = 42) -> Delivery:
    return Delivery(
        delivery_id=delivery_id, kind="web", sales_doc_id=9, ship_to_account="WEB-DE",
        ship_to=Address(country="DE", region="DE-BY", postcode="80331", city="München",
                        street="Lindenweg", house_number="4"),
        carrier="Swiftbox",
        lines=[DeliveryLine(1, 1, 2, 10), DeliveryLine(2, 2, 3, 20)],
        created_at=T0, planned_ship_date=date(2025, 3, 31), order_id=77,
    )


def retail_delivery(delivery_id: int = 43) -> Delivery:
    return Delivery(
        delivery_id=delivery_id, kind="retail", sales_doc_id=10, ship_to_account="KRANZ-DC1",
        ship_to=DC_ADDR, carrier="Frachtwerk Logistik",
        lines=[DeliveryLine(1, 1, 1200, 10), DeliveryLine(2, 2, 480, 20)],
        created_at=T0, planned_ship_date=date(2025, 4, 1),
    )


def web_return(return_id: int = 5, delivery_id: int = 42) -> WebReturn:
    return WebReturn(
        return_id=return_id, order_id=77, delivery_id=delivery_id, customer_id=3,
        reason="did_not_like", lines=[ReturnLine(1, 1, 1, 1), ReturnLine(2, 2, 2, 2)],
        requested_at=T1,
    )


def local(t: datetime) -> datetime:
    return t.astimezone(BERLIN).replace(tzinfo=None)


# -- unit tests ------------------------------------------------------------------


def test_code_mappings_cover_neutral_vocabularies():
    assert set(SHIPMENT_STATUS_CODES) == set(DELIVERY_STATUSES)
    assert set(CARRIER_SCAN_CODES) == set(NEUTRAL_SCAN_CODES)
    assert set(wms.RETURN_STATUS_CODES) == set(RETURN_STATUSES)
    assert set(wms.RETURN_REASON_CODES) == set(RETURN_REASONS)
    assert set(wms.STOCK_MOVEMENT_CODES) == set(STOCK_MOVEMENT_TYPES)
    for mapping in (SHIPMENT_STATUS_CODES, CARRIER_SCAN_CODES, wms.RETURN_STATUS_CODES,
                    wms.RETURN_REASON_CODES, wms.STOCK_MOVEMENT_CODES, wms.RETURN_CONDITION_CODES):
        codes = list(mapping.values())
        assert all(len(c) == 2 for c in codes) and len(set(codes)) == len(codes)


def test_helpers():
    assert item_code(5) == "100005" and keys.matnr(5) == "0" * 12 + "100005"
    assert carrier_code("Paketblitz") == "PBL" and carrier_code("Nordpost") == "NPT"
    assert carrier_code("Swiftbox") == "SWB" and carrier_code("Frachtwerk Logistik") == "FRW"
    assert carrier_code("Kurier Eins") == "KUR"
    assert carrier_event_id(42, 3) == 500_042_003
    with pytest.raises(ValueError):
        carrier_event_id(42, 1000)
    assert snapshot_load_id(date(2025, 3, 30)) == 2025033001


def test_delivery_created_web_and_retail():
    d, _ = make_dispatcher()
    d.dispatch(DeliveryCreated(at=T0, delivery=web_delivery(), sales_doc=sales_doc("web")))
    d.dispatch(DeliveryCreated(at=T0, delivery=retail_delivery(), sales_doc=sales_doc("retail")))
    ships = d.batch.table("wms.shipments")
    web = ships.pending(keys.wms_shipment_id(42))
    assert web["delivery_no"] == "0080000042"
    assert (web["shipment_type"], web["status_code"], web["carrier_code"]) == ("PA", "OP", "SWB")
    assert web["consignee_no"] == "0000900001" and web["consignee_name"] is None
    assert (web["dest_country"], web["dest_region"], web["dest_postcode"]) == ("DE", "DE-BY", "80331")
    assert web["weight_kg"] == round((420 * 2 + 55 * 3) / 1000, 3)
    assert web["handling_units"] == 1 and web["freight_cost"] is None
    assert web["created_local"] == datetime(2025, 3, 28, 10, 30)  # UTC+1
    assert web["created_at"] == T0 and web["is_deleted"] is False
    retail = ships.pending(keys.wms_shipment_id(43))
    assert (retail["shipment_type"], retail["carrier_code"]) == ("PL", "FRW")
    assert retail["consignee_name"] == "Drogerie Kranz DC West"
    assert retail["handling_units"] == 2  # 100 + 20 cases at 60 cases per pallet
    line = d.batch.table("wms.shipment_lines").pending((keys.wms_shipment_id(42), 1))
    assert line["item_code"] == "100001" and line["ean"] == "2000000000015"
    assert line["qty_ordered"] == 2 and line["qty_picked"] == 0


def test_delivery_changes_coalesce_into_pending_insert():
    d, _ = make_dispatcher()
    sid = keys.wms_shipment_id(42)
    d.dispatch(DeliveryCreated(at=T0, delivery=web_delivery(), sales_doc=sales_doc("web")))
    d.dispatch(DeliveryChanged(at=T1, delivery_id=42, status="picked", fields={"picked": {1: 2, 2: 1}}))
    d.dispatch(DeliveryChanged(at=T2, delivery_id=42, status="cancelled"))
    row = d.batch.table("wms.shipments").pending(sid)
    assert row["status_code"] == "CX" and row["is_deleted"] is False  # cancellation is a status
    assert row["picked_local"] == local(T1) and row["cancelled_local"] == local(T2)
    assert row["updated_at"] == T2 and row["created_at"] == T0
    assert d.batch.table("wms.shipment_lines").pending((sid, 2))["qty_picked"] == 1
    assert len(d.batch.table("wms.shipments")) == 1


def test_snapshot_is_insert_only():
    d, w = make_dispatcher()
    snap = StockSnapshotTaken(at=T1, snapshot_date=date(2025, 3, 30),
                              rows=[StockSnapshotRow(2, 50, 60, 0), StockSnapshotRow(1, 100, 10, 500)])
    d.dispatch(snap)
    buf = d.batch.table("wms.stock_levels_daily")
    row = buf.pending((date(2025, 3, 30), "WH01", "100002", 2025033001))
    assert row["qty_available"] == -10 and "is_deleted" not in row
    with pytest.raises(ContractViolation):
        buf.update((date(2025, 3, 30), "WH01", "100002", 2025033001), {"qty_on_hand": 1}, T2)


def test_unknown_codes_fail_loudly():
    d, _ = make_dispatcher()
    with pytest.raises(ValueError):
        d.dispatch(CarrierScan(at=T1, delivery_id=42, scan_seq=1, code="teleported",
                               carrier="Swiftbox", location="Kassel"))


# -- Postgres round trip -----------------------------------------------------------

DSN = os.environ.get("CPGSIM_TEST_DSN")


def _rows(cur, sql: str, *params) -> list[dict]:
    cur.execute(sql, params)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@pytest.mark.db
@pytest.mark.skipif(not DSN, reason="CPGSIM_TEST_DSN not set")
def test_roundtrip_against_postgres():
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS wms CASCADE")
        ddl = SCHEMA_SQL.read_text()
        conn.execute(ddl)
        conn.execute(ddl)  # idempotent

    d, _ = make_dispatcher()
    sid, rid = keys.wms_shipment_id(42), keys.wms_return_id(5)

    # -- batch 1: creations -------------------------------------------------------
    d.dispatch(DeliveryCreated(at=T0, delivery=web_delivery(), sales_doc=sales_doc("web")))
    d.dispatch(DeliveryCreated(at=T0, delivery=retail_delivery(), sales_doc=sales_doc("retail")))
    d.dispatch(CarrierScan(at=T0, delivery_id=42, scan_seq=1, code="label_created",
                           carrier="Swiftbox", location="Kassel"))
    d.dispatch(StockMoved(at=T0, movement_id=901, sku_id=1, movement_type="goods_receipt", qty=600,
                          reference="PRD-0001"))
    d.dispatch(StockSnapshotTaken(at=T0, snapshot_date=date(2025, 3, 27),
                                  rows=[StockSnapshotRow(1, 1000, 40, 600), StockSnapshotRow(2, 50, 0, 0)]))
    with psycopg.connect(DSN) as conn:
        with conn.transaction(), conn.cursor() as cur:
            touched = d.batch.flush(cur)
    d.batch.clear()
    assert touched == {"wms": 2 + 4 + 1 + 1 + 2}

    # -- batch 2: updates to committed rows, plus new rows ------------------------
    d.dispatch(DeliveryChanged(at=T1, delivery_id=42, status="picked", fields={"picked": {"1": 2, "2": 3}}))
    d.dispatch(DeliveryChanged(at=T1, delivery_id=42, status="shipped", fields={
        "tracking_no": keys.tracking_number("Swiftbox", 42), "freight_cost_eur": 4.4, "shipped_at": T1}))
    d.dispatch(StockMoved(at=T1, movement_id=902, sku_id=1, movement_type="goods_issue", qty=-2,
                          delivery_id=42))
    d.dispatch(CarrierScan(at=T2, delivery_id=42, scan_seq=2, code="delivered", carrier="Swiftbox",
                           location="München", detail="Haustür"))
    d.dispatch(DeliveryChanged(at=T2, delivery_id=42, status="delivered", fields={"delivered_at": T2}))
    d.dispatch(DeliveryChanged(at=T1, delivery_id=43, status="cancelled"))
    d.dispatch(ReturnCreated(at=T2, ret=web_return()))
    d.dispatch(StockSnapshotTaken(at=T2, snapshot_date=date(2025, 4, 1),
                                  rows=[StockSnapshotRow(1, 1598, 0, 0)]))
    with psycopg.connect(DSN) as conn:
        with conn.transaction(), conn.cursor() as cur:
            d.batch.flush(cur)
    d.batch.clear()

    # -- batch 3: return lifecycle on committed rows ------------------------------
    d.dispatch(ReturnChanged(at=T2.replace(hour=14), return_id=5, status="received"))
    d.dispatch(ReturnChanged(at=T2.replace(hour=15), return_id=5, status="inspected",
                             line_conditions={1: "resaleable", 2: "damaged"}))
    d.dispatch(StockMoved(at=T2.replace(hour=15), movement_id=903, sku_id=2, movement_type="scrap",
                          qty=-2, return_id=5))
    with psycopg.connect(DSN) as conn:
        with conn.transaction(), conn.cursor() as cur:
            d.batch.flush(cur)
    d.batch.clear()

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        ship = _rows(cur, "SELECT * FROM wms.shipments WHERE shipment_id = %s", sid)[0]
        assert ship["status_code"] == "DL" and ship["delivery_no"] == "0080000042"
        assert ship["tracking_no"] == keys.tracking_number("Swiftbox", 42)
        assert ship["freight_cost"] == Decimal("4.40") and ship["weight_kg"] == Decimal("1.005")
        assert ship["created_local"] == datetime(2025, 3, 28, 10, 30)
        assert ship["picked_local"] == datetime(2025, 3, 31, 9, 15)  # after DST: UTC+2
        assert ship["shipped_local"] == local(T1) and ship["delivered_local"] == local(T2)
        assert ship["status_local"] == local(T2)
        assert ship["created_at"] == T0 and ship["updated_at"] == T2
        assert ship["is_deleted"] is False and ship["deleted_at"] is None

        cancelled = _rows(cur, "SELECT * FROM wms.shipments WHERE shipment_id = %s",
                          keys.wms_shipment_id(43))[0]
        assert cancelled["status_code"] == "CX" and cancelled["cancelled_local"] == local(T1)
        assert cancelled["is_deleted"] is False

        lines = _rows(cur, "SELECT * FROM wms.shipment_lines WHERE shipment_id = %s ORDER BY line_no", sid)
        assert [(l["item_code"], l["qty_ordered"], l["qty_picked"]) for l in lines] == [
            ("100001", 2, 2), ("100002", 3, 3)]
        assert lines[0]["updated_at"] == T1 and lines[0]["created_at"] == T0

        scans = _rows(cur, "SELECT * FROM wms.carrier_events ORDER BY event_id")
        assert [s["event_id"] for s in scans] == [500_042_001, 500_042_002]
        assert [s["event_code"] for s in scans] == ["LC", "DL"]
        assert scans[1]["carrier_code"] == "SWB" and scans[1]["detail"] == "Haustür"
        assert scans[1]["tracking_no"] == ship["tracking_no"]

        ret = _rows(cur, "SELECT * FROM wms.returns WHERE return_id = %s", rid)[0]
        assert ret["rma_no"] == "RMA700005" and ret["shipment_id"] == sid
        assert ret["delivery_no"] == "0080000042" and ret["reason_code"] == "NL"
        assert ret["status_code"] == "IN" and ret["requested_local"] == local(T1)
        assert ret["received_local"] == local(T2.replace(hour=14))
        assert ret["inspected_local"] == local(T2.replace(hour=15))
        assert ret["created_at"] == T2 and ret["updated_at"] == T2.replace(hour=15)

        rlines = _rows(cur, "SELECT * FROM wms.return_lines WHERE return_id = %s ORDER BY line_no", rid)
        assert [(l["item_code"], l["qty"], l["condition_code"]) for l in rlines] == [
            ("100001", 1, "OK"), ("100002", 2, "DM")]

        moves = _rows(cur, "SELECT * FROM wms.stock_movements ORDER BY movement_id")
        assert [(m["movement_code"], m["qty"], m["delivery_no"], m["return_id"]) for m in moves] == [
            ("GR", 600, None, None), ("GI", -2, "0080000042", None), ("SC", -2, None, rid)]
        assert moves[0]["reference"] == "PRD-0001" and moves[0]["ean"] == "2000000000015"

        snaps = _rows(cur, "SELECT * FROM wms.stock_levels_daily ORDER BY snapshot_date, item_code")
        assert [(s["snapshot_date"], s["item_code"], s["load_id"], s["qty_available"]) for s in snaps] == [
            (date(2025, 3, 27), "100001", 2025032701, 960),
            (date(2025, 3, 27), "100002", 2025032701, 50),
            (date(2025, 4, 1), "100001", 2025040101, 1598),
        ]
        assert snaps[0]["qty_in_production"] == 600 and snaps[0]["created_at"] == T0

        # Loading contract: every table has updated_at indexed and contract columns typed.
        cur.execute(
            """SELECT c.table_name, c.column_name, c.data_type FROM information_schema.columns c
               WHERE c.table_schema = 'wms'
                 AND c.column_name IN ('created_at', 'updated_at', 'deleted_at', 'is_deleted', 'load_id')"""
        )
        cols = {(t, c): dt for t, c, dt in cur.fetchall()}
        tables = {t for t, _ in cols}
        assert len(tables) == 7
        for t in tables:
            assert cols[(t, "created_at")] == "timestamp with time zone"
            assert cols[(t, "updated_at")] == "timestamp with time zone"
            if t == "stock_levels_daily":
                assert (t, "is_deleted") not in cols and (t, "load_id") in cols
            else:
                assert cols[(t, "is_deleted")] == "boolean"
                assert cols[(t, "deleted_at")] == "timestamp with time zone"
        cur.execute(
            "SELECT tablename FROM pg_indexes WHERE schemaname = 'wms' AND indexdef LIKE '%(updated_at)%'"
        )
        assert {r[0] for r in cur.fetchall()} == tables
