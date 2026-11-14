import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cpgsim.domain.events import PlantedEffect, ScenarioWindowRecorded
from cpgsim.writers import keys
from cpgsim.writers.base import Dispatcher, WriterContext
from cpgsim.writers.truth import TruthWriter

SCHEMAS = Path(__file__).resolve().parents[2] / "sql" / "schemas"
T0 = datetime(2025, 3, 1, 8, tzinfo=timezone.utc)
T1 = datetime(2025, 3, 8, 8, tzinfo=timezone.utc)


def make_dispatcher(seed=7):
    sku = SimpleNamespace(ean="4000000000017")
    ref = SimpleNamespace(
        catalogue=SimpleNamespace(get=lambda i: sku if i == 5 else None),
        accounts=SimpleNamespace(numbers={}, get=lambda a: None),
    )
    ctx = WriterContext(seed=seed, cfg=None, ref=ref, tz=None)
    w = TruthWriter(ctx)
    return Dispatcher([w]), w


def window(at, end=None):
    return ScenarioWindowRecorded(
        at=at, scenario_id="carrier_decay", window_id="carrier_decay:config:0",
        params={"since": T0, "k": 2}, start=T0, end=end, source="config",
    )


def effect(at, eid, etype, entity, mag=1.5):
    return PlantedEffect(
        at=at, effect_id=eid, scenario_id="carrier_decay", window_id="carrier_decay:config:0",
        entity_type=etype, entity_id=entity, effect="delay_multiplier", magnitude=mag,
        detail={"when": T0},
    )


def test_system_refs():
    d, w = make_dispatcher()
    assert w.system_refs("web_order", "12") == {
        "shop_order_id": keys.shop_order_id(7, 12),
        "shop_order_number": keys.shop_order_number(12),
    }
    assert w.system_refs("delivery", "3") == {
        "erp_delivery": keys.vbeln_delivery(3), "wms_shipment_id": keys.wms_shipment_id(3)}
    assert w.system_refs("customer", "4") == {"shop_customer_id": keys.shop_customer_id(7, 4)}
    assert w.system_refs("sku", "5") == {"matnr": keys.matnr(5), "ean": "4000000000017"}
    assert w.system_refs("sku", "6") == {"matnr": keys.matnr(6)}
    assert w.system_refs("sales_doc", "1") == {"erp_sales_doc": keys.vbeln_sales(1)}
    assert w.system_refs("billing", "1") == {"erp_billing": keys.vbeln_billing(1)}
    assert w.system_refs("week", "2025-W10") == {}
    assert w.system_refs("delivery", "abc") == {}


def test_system_refs_phase2_entity_types():
    sku = SimpleNamespace(ean="4000000000017")
    accounts = {"KRANZ": SimpleNamespace(retailer="drogerie_kranz"),
                "MYLO-DC2": SimpleNamespace(retailer="mylo")}

    def get_account(a):
        if a not in accounts:
            raise KeyError(a)
        return accounts[a]

    ref = SimpleNamespace(
        catalogue=SimpleNamespace(
            get=lambda i: sku if i == 5 else None,
            customer_article=lambda ch, i: f"{ch[:2].upper()}{i:06d}",
        ),
        accounts=SimpleNamespace(numbers={"KRANZ": 100001, "MYLO-DC2": 200005}, get=get_account),
    )
    w = TruthWriter(WriterContext(seed=7, cfg=None, ref=ref, tz=None))
    from cpgsim.writers.wms import carrier_event_id, rma_no

    assert w.system_refs("carrier_scan", "3:2") == {
        "wms_event_id": carrier_event_id(3, 2), "wms_shipment_id": keys.wms_shipment_id(3),
        "erp_delivery": keys.vbeln_delivery(3)}
    assert w.system_refs("customer_material", "KRANZ:5") == {
        "kunnr": "0000100001", "matnr": keys.matnr(5), "kdmat": "DR000005", "ean": "4000000000017"}
    assert w.system_refs("feed_load", "KRZ_SO_20260302_01") == {
        "feeds_load_id": "KRZ_SO_20260302_01", "feeds_restatement_load_id": "KRZ_SO_20260302_02"}
    assert w.system_refs("feed_load", "BEL_SOM_20260302_02")["feeds_restatement_load_id"] == "BEL_SOM_20260302_03"
    assert w.system_refs("retailer_dc_week", "MYLO-DC2:2026-03-02") == {
        "kunnr": "0000200005", "week_start": "2026-03-02"}
    # a cut-phase row is keyed by the PO order day (a Friday here): week_start is still the Monday
    assert w.system_refs("retailer_dc_week", "MYLO-DC2:2026-04-10") == {
        "kunnr": "0000200005", "week_start": "2026-04-06", "order_date": "2026-04-10"}
    assert w.system_refs("deduction", "12") == {"erp_deduction": keys.belnr_deduction(12)}
    assert w.system_refs("campaign", "PS-2026Q1-01") == {"mkt_campaign_id": "PS-2026Q1-01"}
    assert w.system_refs("web_return", "42") == {"wms_return_id": keys.wms_return_id(42), "rma_no": rma_no(42)}
    assert w.system_refs("dst_transition", "2026-03-29") == {}
    # duplicate customers are customers with a derived id
    dup = keys.DUPLICATE_CUSTOMER_BASE + 4
    assert w.system_refs("customer", str(dup)) == {"shop_customer_id": keys.shop_customer_id(7, dup)}
    # unparsable ids give no refs instead of failing the commit
    for etype, eid in [("carrier_scan", "3"), ("customer_material", "KRANZ"), ("feed_load", "XYZ_SO_20260302_01"),
                       ("feed_load", "garbage"), ("retailer_dc_week", "MYLO-DC2:soon"), ("deduction", "x"),
                       ("web_return", "x")]:
        assert w.system_refs(etype, eid) == {}, (etype, eid)
    # an unknown account still gives the material refs
    assert w.system_refs("customer_material", "NOPE:5") == {"matnr": keys.matnr(5), "ean": "4000000000017"}


def test_window_update_in_same_batch():
    d, w = make_dispatcher()
    d.dispatch(window(T0))
    d.dispatch(window(T1, end=T1))
    row = d.batch.table("truth.scenario_windows").pending("carrier_decay:config:0")
    assert row["end_at"] == T1 and row["created_at"] == T0 and row["updated_at"] == T1
    assert len(d.batch) == 1


DSN = os.environ.get("CPGSIM_TEST_DSN")


@pytest.mark.skipif(not DSN, reason="CPGSIM_TEST_DSN not set")
def test_roundtrip_against_postgres():
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS truth CASCADE")
        for name in ("truth", "mkt", "feeds", "shelf"):
            sql = (SCHEMAS / f"{name}.sql").read_text()
            conn.execute(sql)
            conn.execute(sql)  # idempotent

    d, _ = make_dispatcher()
    d.dispatch(window(T0))
    d.dispatch(effect(T0, "e1", "web_order", "12"))
    d.dispatch(effect(T0, "e2", "week", "2025-W10", mag=None))
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            d.batch.flush(cur)
        conn.commit()
    d.batch.clear()

    d.dispatch(window(T1, end=T1))  # new batch: window not pending -> insert would collide
    # The contract has no upsert; closing a committed window is an open issue, so
    # exercise the update path through the buffer directly.
    d.batch.table("truth.scenario_windows").inserts.clear()
    d.batch.table("truth.scenario_windows").update("carrier_decay:config:0", {"end_at": T1}, T1)
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            d.batch.flush(cur)
        conn.commit()

    with psycopg.connect(DSN) as conn:
        w = conn.execute(
            "SELECT scenario_id, params, start_at, end_at, source, created_at, updated_at"
            " FROM truth.scenario_windows").fetchall()
        assert len(w) == 1
        assert w[0][0] == "carrier_decay" and w[0][1] == {"since": str(T0), "k": 2}
        assert w[0][2] == T0 and w[0][3] == T1 and w[0][4] == "config"
        assert w[0][5] == T0 and w[0][6] == T1
        e = conn.execute(
            "SELECT effect_id, system_refs, magnitude, detail, effective_at FROM truth.planted_effects"
            " ORDER BY effect_id").fetchall()
        assert e[0][0] == "e1" and e[0][1]["shop_order_number"] == "NV-10000012"
        assert e[0][2] == 1.5 and e[0][3] == {"when": str(T0)} and e[0][4] == T0
        assert e[1][1] == {} and e[1][2] is None
