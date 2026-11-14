import os
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from cpgsim.domain.events import (
    B2BAccountCreated,
    BillingDocChanged,
    BillingDocCreated,
    CustomerMaterialListed,
    DeliveryChanged,
    DeliveryCreated,
    FxRatePublished,
    SalesDocChanged,
    SalesDocCreated,
    ServiceMaterialListed,
    SkuListed,
    StandardCostRevised,
)
from cpgsim.domain.model import (
    Address,
    B2BAccount,
    BillingDoc,
    BillingItem,
    Condition,
    Delivery,
    DeliveryLine,
    SalesDoc,
    SalesItem,
    Sku,
)
from cpgsim.writers import keys
from cpgsim.writers.base import Dispatcher, WriterContext
from cpgsim.writers.erp import ErpWriter, _matkl, fictional_vat_id

SCHEMAS = Path(__file__).resolve().parents[2] / "sql" / "schemas"
BERLIN = ZoneInfo("Europe/Berlin")
SEED = 7
T0 = datetime(2025, 3, 1, 22, 30, tzinfo=timezone.utc)  # 23:30 local, same day
T1 = datetime(2025, 3, 3, 23, 15, tzinfo=timezone.utc)  # 00:15 local next day
T2 = datetime(2025, 4, 2, 9, 0, tzinfo=timezone.utc)


def sku(sku_id, name="Velmora Hydra Balance Body Lotion 400 ml", delist=None):
    return Sku(
        sku_id=sku_id, code=f"VEL-BOD-{sku_id}", ean=f"20000000000{sku_id:02d}", brand="Velmora",
        category="body_care", line="Hydra Balance", name=name, pack_size=400.0, pack_unit="ml",
        usage_days=40.0, rrp_eur=5.95, rrp_chf=6.5, list_price_eur=3.1, std_cost_eur=1.234,
        weight_g=450, units_per_case=12, launch_date=date(2020, 1, 1), delist_date=delist,
        hero=True, popularity=1.0, season_profile="flat",
    )


SKUS = {1: sku(1), 2: sku(2, "Velmora Sun Spray SPF 50 200 ml", delist=date(2026, 6, 30))}
ACCOUNTS = {
    "KRANZ": B2BAccount("KRANZ", "retailer", "Drogerie Kranz GmbH", "drogerie_kranz",
                        Address("DE", "DE-BY", "80331", "München", "Handelsweg", "5"),
                        payment_terms_days=45),
    "KRANZ-DC1": B2BAccount("KRANZ-DC1", "retailer_dc", "Drogerie Kranz DC Süd", "drogerie_kranz",
                            Address("DE", "DE-BY", "85748", "Garching", "Lagerstraße", "12")),
    "WEB-AT": B2BAccount("WEB-AT", "web_collective", "Webshop Endkunden AT", None,
                         Address("AT", "AT-9", "1010", "Wien")),
}
NUMBERS = {"KRANZ": 100001, "KRANZ-DC1": 100002, "WEB-AT": 100003}


def make_dispatcher():
    ref = SimpleNamespace(
        catalogue=SimpleNamespace(get=lambda i: SKUS[i]),
        accounts=SimpleNamespace(numbers=NUMBERS, get=lambda a: ACCOUNTS[a]),
    )
    w = ErpWriter(WriterContext(seed=SEED, cfg=None, ref=ref, tz=BERLIN))
    return Dispatcher([w]), w


def retail_doc():
    return SalesDoc(
        sales_doc_id=5, doc_type="retail", sold_to="KRANZ", ship_to="KRANZ-DC1", currency="EUR",
        fx_rate_eur=1.0, created_at=T0, requested_date=date(2025, 3, 5),
        external_ref="KR-4500012345", channel="drogerie_kranz", order_id=3,
        items=[
            SalesItem(10, 1, qty=24, confirmed_qty=24, net_value=66.96, tax=12.72, cost=29.62,
                      customer_article="KR-778812",
                      conditions=[
                          Condition("list_price", 3.1, 74.4),
                          Condition("customer_discount", 0.1, -7.44),
                          Condition("tax", 0.19, 12.72),
                      ]),
            SalesItem(20, 2, qty=12, confirmed_qty=6, net_value=18.0, tax=3.42, cost=7.4,
                      customer_article="KR-778813",
                      conditions=[Condition("list_price", 3.0, 18.0)]),
        ],
    )


def web_doc():
    return SalesDoc(
        sales_doc_id=6, doc_type="web", sold_to="WEB-AT", ship_to="WEB-AT", currency="EUR",
        fx_rate_eur=1.0, created_at=T0, requested_date=date(2025, 3, 2), external_ref="4711",
        channel="web", order_id=4711,
        items=[SalesItem(10, 1, qty=2, confirmed_qty=2, net_value=6.94, tax=1.39, cost=2.47,
                         conditions=[
                             Condition("list_price", 4.96, 9.92),
                             Condition("promo_discount", 0.25, -2.48),
                             Condition("promo_discount", 0.05, -0.5),
                             Condition("tax", 0.2, 1.39),
                         ]),
               # shipping charge: freight item of its own (gross 3.95 at 20 %)
               SalesItem(900, 0, qty=1, confirmed_qty=1, net_value=3.29, tax=0.66, cost=0.0,
                         conditions=[Condition("shipping", 3.29, 3.29), Condition("tax", 0.2, 0.66)],
                         item_type="freight")],
    )


def retail_delivery(delivery_id=8, status="open"):
    return Delivery(
        delivery_id=delivery_id, kind="retail", sales_doc_id=5, ship_to_account="KRANZ-DC1",
        ship_to=ACCOUNTS["KRANZ-DC1"].address, carrier="Trans Alpin Logistik",
        lines=[DeliveryLine(1, 1, 24, 10), DeliveryLine(2, 2, 6, 20)],
        created_at=T0, planned_ship_date=date(2025, 3, 3), status=status, order_id=3,
    )


def invoice():
    return BillingDoc(
        billing_id=11, doc_kind="invoice", sales_doc_id=5, payer="KRANZ", currency="EUR",
        fx_rate_eur=1.0, billing_date=date(2025, 3, 6), created_at=T1, due_date=date(2025, 4, 20),
        items=[BillingItem(10, 1, 24, 66.96, 12.72, 29.62, 10, delivery_id=8),
               BillingItem(20, 2, 6, 18.0, 3.42, 7.4, 20, delivery_id=8)],
    )


def credit_memo():
    return BillingDoc(
        billing_id=12, doc_kind="credit_memo", sales_doc_id=6, payer="WEB-AT", currency="EUR",
        fx_rate_eur=1.0, billing_date=date(2025, 3, 10), created_at=T1, due_date=date(2025, 3, 10),
        items=[BillingItem(10, 1, 1, 4.17, 0.83, 1.23, 10, delivery_id=9)],
        reference_billing_id=10, reason="return", paid_at=T1, paid_amount=5.0,
    )


def master_events():
    return [
        SkuListed(at=T0, sku=SKUS[1], listed_on=date(2019, 11, 2)),
        SkuListed(at=T0, sku=SKUS[2]),
        ServiceMaterialListed(at=T0, service="shipping", listed_on=date(2016, 5, 1)),
        B2BAccountCreated(at=T0, account=ACCOUNTS["KRANZ"], opened_on=date(2014, 2, 3)),
        B2BAccountCreated(at=T0, account=ACCOUNTS["KRANZ-DC1"]),
        B2BAccountCreated(at=T0, account=ACCOUNTS["WEB-AT"]),
        CustomerMaterialListed(at=T0, account_id="KRANZ", sku_id=1, customer_article="KR-778812"),
        FxRatePublished(at=T0, from_currency="CHF", to_currency="EUR", valid_from=date(2025, 3, 1),
                        rate=1.0532),
    ]


def doc_events():
    return [
        SalesDocCreated(at=T0, doc=retail_doc()),
        SalesDocCreated(at=T0, doc=web_doc()),
        DeliveryCreated(at=T0, delivery=retail_delivery(), sales_doc=retail_doc()),
        DeliveryCreated(at=T0, delivery=retail_delivery(9), sales_doc=retail_doc()),
        BillingDocCreated(at=T1, doc=invoice()),
        BillingDocCreated(at=T1, doc=credit_memo()),
    ]


def update_events():
    return [
        StandardCostRevised(at=T2, sku_id=1, std_cost_eur=1.301, valid_from=date(2025, 4, 1)),
        SalesDocChanged(at=T2, sales_doc_id=5,
                        header={"status": "in_process", "delivery_status": "full"},
                        items={10: {"delivered_qty": 24},
                               20: {"confirmed_qty": 6, "delivered_qty": 6, "billed_qty": 6}}),
        DeliveryChanged(at=T2, delivery_id=8, status="picked", fields={"picked": {1: 24, 2: 5}}),
        DeliveryChanged(at=T2, delivery_id=8, status="shipped",
                        fields={"tracking_no": "TA123", "freight_cost_eur": 84.5, "shipped_at": T2}),
        DeliveryChanged(at=T2, delivery_id=8, status="in_transit"),
        DeliveryChanged(at=T2, delivery_id=8, status="delivered",
                        fields={"delivered_at": T2, "pod_signed_by": "A. Lager"}),
        DeliveryChanged(at=T2, delivery_id=9, status="cancelled"),
        BillingDocChanged(at=T2, billing_id=11,
                          fields={"paid_at": T2, "paid_amount": 99.1, "status": "cleared"}),
    ]


# -- unit tests ------------------------------------------------------------------


def test_material_master_rows():
    d, _ = make_dispatcher()
    for ev in master_events():
        d.dispatch(ev)
    b = d.batch
    mara = b.table("erp.mara").pending(keys.matnr(2))
    assert mara["matnr"] == "000000000000100002" and mara["mtart"] == "FERT"
    assert mara["matkl"] == "KP1010" and mara["meins"] == "ST" and mara["numtp"] == "HE"
    assert mara["prdha"] == "VELMOBODYCHYDRABAL" and len(mara["prdha"]) == 18
    assert mara["brand_id"] == "VEL" and mara["groes"] == "400 ML"
    assert mara["brgew"] == 0.45 and mara["ntgew"] == 0.405 and mara["gewei"] == "KG"
    assert mara["ersda"] == date(2025, 3, 1)  # 23:30 Berlin
    assert mara["mstae"] == "Z1" and mara["mstde"] == date(2026, 6, 30)
    m1 = b.table("erp.mara").pending(keys.matnr(1))
    assert m1["mstae"] == ""
    # seeded at history start: created on its listing date, changed (replicated) today
    assert m1["ersda"] == date(2019, 11, 2) and m1["laeda"] == date(2025, 3, 1)
    # the shipping service material: no stock, no valuation
    srv = b.table("erp.mara").pending("SHIPPING")
    assert (srv["mtart"], srv["meins"], srv["matkl"], srv["ersda"]) == ("DIEN", "LE", "KP9910", date(2016, 5, 1))
    assert b.table("erp.makt").pending(("SHIPPING", "D"))["maktx"] == "Versandkosten"
    assert b.table("erp.mbew").pending(("SHIPPING", "1000")) is None
    makt = b.table("erp.makt")
    assert makt.pending((keys.matnr(1), "D"))["maktx"] == "Velmora Hydra Balance Körperlotion 400 m"
    assert makt.pending((keys.matnr(1), "E"))["maktx"] == "Velmora Hydra Balance Body Lotion 400 ml"
    assert makt.pending((keys.matnr(2), "D"))["maktx"] == "Velmora Sonnenspray SPF 50 200 ml"
    mbew = b.table("erp.mbew").pending((keys.matnr(1), "1000"))
    assert mbew["stprs"] == 1.23 and mbew["vprsv"] == "S" and mbew["peinh"] == 1
    tcurr = b.table("erp.tcurr").pending(("M", "CHF", "EUR", "20250301"))
    assert tcurr["ukurs"] == 1.0532 and tcurr["ffact"] == 1


def test_customer_master_rows():
    d, _ = make_dispatcher()
    for ev in master_events():
        d.dispatch(ev)
    kna1 = d.batch.table("erp.kna1")
    kranz = kna1.pending("0000100001")
    assert kranz["ktokd"] == "Z001" and kranz["regio"] == "BY" and kranz["konzs"] == "DROGERIE_K"
    assert kranz["erdat"] == date(2014, 2, 3)  # account opened long before the event
    assert kna1.pending("0000100002")["erdat"] == date(2025, 3, 1)  # no opening date: event date
    assert kranz["stras"] == "Handelsweg 5" and kranz["sortl"] == "KRANZ"
    assert kranz["stceg"].startswith("DE") and len(kranz["stceg"]) == 11
    assert kna1.pending("0000100002")["ktokd"] == "Z002"
    web = kna1.pending("0000100003")
    assert web["ktokd"] == "Z010" and web["regio"] == "09" and web["stras"] is None
    assert web["konzs"] is None and web["stceg"].startswith("ATU")
    knmt = d.batch.table("erp.knmt").pending(("1000", "10", "0000100001", keys.matnr(1)))
    assert knmt["kdmat"] == "KR-778812"


def _de_vat_valid(digits: str) -> bool:
    product = 10
    for ch in digits[:8]:
        s = (int(ch) + product) % 10 or 10
        product = (2 * s) % 11
    return (11 - product) % 10 == int(digits[8])


def test_fictional_vat_ids_are_invalid_and_stable():
    for i in range(200):
        vat = fictional_vat_id(SEED, f"ACC{i}", "DE")
        assert vat == fictional_vat_id(SEED, f"ACC{i}", "DE")
        assert not _de_vat_valid(vat[2:])
    assert fictional_vat_id(SEED, "X", "CH").endswith("MWST")


def test_sales_doc_rows_and_conditions():
    d, _ = make_dispatcher()
    d.dispatch(SalesDocCreated(at=T0, doc=retail_doc()))
    d.dispatch(SalesDocCreated(at=T0, doc=web_doc()))
    b = d.batch
    vbak = b.table("erp.vbak").pending("0010000005")
    assert vbak["auart"] == "ZEDI" and vbak["vtweg"] == "10" and vbak["ernam"] == "EDI_IN"
    assert vbak["bstnk"] == "KR-4500012345" and vbak["kunnr"] == "0000100001"
    assert vbak["zzkunwe"] == "0000100002" and vbak["netwr"] == 84.96
    assert vbak["erdat"] == date(2025, 3, 1) and vbak["erzet"] == time(23, 30)
    assert (vbak["gbstk"], vbak["lfstk"], vbak["fkstk"]) == ("A", "A", "A")
    web = b.table("erp.vbak").pending("0010000006")
    assert web["auart"] == "ZWEB" and web["vtweg"] == "20" and web["ernam"] == "WEBSHOP_IF"
    assert web["bstnk"] == keys.shop_order_id(SEED, 4711)
    item = b.table("erp.vbap").pending(("0010000005", "000020"))
    assert item["kwmeng"] == 12 and item["kbmeng"] == 6 and item["kdmat"] == "KR-778813"
    assert (item["gbsta"], item["lfsta"], item["fksta"], item["abgru"]) == ("A", "A", "A", "")
    konv = b.table("erp.konv")
    knumv = keys.knumv_sales(6)
    assert konv.pending((knumv, "000010", "010", "01"))["kbetr"] == 4.96
    p1 = konv.pending((knumv, "000010", "030", "01"))
    p2 = konv.pending((knumv, "000010", "030", "02"))
    assert p1["kschl"] == "ZPRO" and p1["kbetr"] == -25.0 and p1["kwert"] == -2.48
    assert p2["kbetr"] == -5.0
    assert konv.pending((knumv, "000010", "050", "01")) is None  # freight is not on the goods item
    hd = konv.pending((knumv, "000900", "050", "01"))
    assert hd["kschl"] == "HD00" and hd["krech"] == "B" and hd["kbetr"] == 3.29
    tax = konv.pending((knumv, "000010", "900", "01"))
    assert tax["kschl"] == "MWST" and tax["kbetr"] == 20.0 and tax["kpein"] is None


def test_web_freight_item_reconciles():
    d, _ = make_dispatcher()
    doc = web_doc()
    d.dispatch(SalesDocCreated(at=T0, doc=doc))
    b = d.batch
    vbak = b.table("erp.vbak").pending("0010000006")
    fr = b.table("erp.vbap").pending(("0010000006", "000900"))
    assert (fr["matnr"], fr["pstyv"], fr["vrkme"], fr["meins"]) == ("SHIPPING", "TAD", "LE", "LE")
    assert (fr["lgort"], fr["vstel"], fr["arktx"]) == ("", "", "Versandkosten")
    assert (fr["gbsta"], fr["lfsta"], fr["fksta"]) == ("A", "", "A")  # not delivery-relevant
    items = {k[1]: r for k, r in b.table("erp.vbap").inserts.items() if k[0] == "0010000006"}
    assert vbak["netwr"] == round(sum(r["netwr"] for r in items.values()), 2) == 10.23
    # per item: non-tax conditions add up to the item net value, MWST to its tax
    for posnr, row in items.items():
        conds = [r for k, r in b.table("erp.konv").inserts.items() if k[0] == keys.knumv_sales(6) and k[1] == posnr]
        assert round(sum(c["kwert"] for c in conds if c["kschl"] != "MWST"), 2) == row["netwr"]
        assert round(sum(c["kwert"] for c in conds if c["kschl"] == "MWST"), 2) == row["mwsbp"]
    # billed on the first invoice -> complete; never a delivery status
    ch = ErpWriter._item_changes({"billed_qty": 1, "confirmed_qty": 1}, {"billing_status": "partial"})
    assert ch == {"kbmeng": 1, "zzfkimg": 1, "fksta": "C", "gbsta": "C"}
    inv = BillingDoc(
        billing_id=13, doc_kind="invoice", sales_doc_id=6, payer="WEB-AT", currency="EUR", fx_rate_eur=1.0,
        billing_date=date(2025, 3, 4), created_at=T1, due_date=date(2025, 3, 4),
        items=[BillingItem(10, 1, 2, 6.94, 1.39, 2.47, 10, delivery_id=9, delivery_line_no=1),
               BillingItem(900, 0, 1, 3.29, 0.66, 0.0, 900, delivery_id=9)],
    )
    d.dispatch(BillingDocCreated(at=T1, doc=inv))
    ship = b.table("erp.vbrp").pending(("0090000013", "000900"))
    assert (ship["matnr"], ship["aupos"], ship["meins"]) == ("SHIPPING", "000900", "LE")
    vbrk = b.table("erp.vbrk").pending("0090000013")
    assert vbrk["netwr"] == vbak["netwr"]


def test_material_group_mapping():
    assert _matkl("gift_set") == "KP9010" and _matkl("Gift Sets") == "KP9010"
    with pytest.raises(ValueError, match="material group"):
        _matkl("no_such_category")


def test_item_change_rules():
    f = ErpWriter._item_changes
    assert f({"confirmed_qty": 10, "delivered_qty": 4}, {}) == {
        "kbmeng": 10, "zzlfimg": 4, "lfsta": "B", "gbsta": "B"}
    # target unknown: header full in the same event -> complete
    assert f({"delivered_qty": 4}, {"delivery_status": "full"})["lfsta"] == "C"
    assert f({"delivered_qty": 4}, {"delivery_status": "partial"})["lfsta"] == "B"
    billed = f({"confirmed_qty": 4, "billed_qty": 4}, {})
    assert billed["fksta"] == "C" and billed["gbsta"] == "C"
    # backordered remainder cut: the new target travels with delivered/billed -> complete
    cut = f({"confirmed_qty": 1, "delivered_qty": 1, "billed_qty": 1}, {"status": "completed"})
    assert (cut["kbmeng"], cut["lfsta"], cut["fksta"], cut["gbsta"]) == (1, "C", "C", "C")
    rej = f({"rejection_reason": "out_of_stock"}, {})
    assert rej == {"abgru": "Z2", "gbsta": "C", "lfsta": "", "fksta": ""}
    assert f({"rejection_reason": "something_new"}, {})["abgru"] == "ZZ"
    assert f({}, {}) == {}


def test_unknown_status_fails_fast():
    d, _ = make_dispatcher()
    with pytest.raises(ValueError, match="sales doc status"):
        d.dispatch(SalesDocChanged(at=T2, sales_doc_id=5, header={"status": "bogus"}))


def test_delivery_rows_and_cancellation():
    d, _ = make_dispatcher()
    d.dispatch(DeliveryCreated(at=T0, delivery=retail_delivery(), sales_doc=retail_doc()))
    likp = d.batch.table("erp.likp").pending("0080000008")
    assert likp["kunnr"] == "0000100002" and likp["kunag"] == "0000100001"
    assert likp["lfdat"] == date(2025, 3, 5) and likp["wadat"] == date(2025, 3, 3)
    assert likp["btgew"] == 13.5 and likp["ntgew"] == 12.15 and likp["vsbed"] == "02"
    assert (likp["kostk"], likp["wbstk"], likp["pdstk"]) == ("A", "A", "A")
    lips = d.batch.table("erp.lips").pending(("0080000008", "000020"))
    assert lips["vgbel"] == "0010000005" and lips["vgpos"] == "000020" and lips["lfimg"] == 6
    d.batch.clear()
    d.dispatch(DeliveryChanged(at=T2, delivery_id=8, status="cancelled"))
    upd = d.batch.table("erp.likp").updates
    assert upd == {("0080000008",): {"is_deleted": True, "deleted_at": T2, "updated_at": T2}}
    assert len(d.batch.table("erp.lips")) == 0
    # carrier progress without new facts does not touch the ERP
    d.batch.clear()
    d.dispatch(DeliveryChanged(at=T2, delivery_id=8, status="out_for_delivery"))
    assert len(d.batch) == 0


def test_delivery_change_fields_accept_iso_strings():
    d, _ = make_dispatcher()
    d.dispatch(DeliveryChanged(at=T2, delivery_id=8, status="shipped",
                               fields={"shipped_at": T1.isoformat()}))
    ch = d.batch.table("erp.likp").updates[("0080000008",)]
    assert ch["wadat_ist"] == date(2025, 3, 4) and ch["wbstk"] == "C"


def test_billing_rows():
    d, _ = make_dispatcher()
    d.dispatch(BillingDocCreated(at=T1, doc=invoice()))
    d.dispatch(BillingDocCreated(at=T1, doc=credit_memo()))
    inv = d.batch.table("erp.vbrk").pending("0090000011")
    assert (inv["fkart"], inv["vbtyp"], inv["fktyp"], inv["vtweg"]) == ("F2", "M", "L", "10")
    assert inv["zterm"] == "Z045" and inv["netwr"] == 84.96 and inv["mwsbk"] == 16.14
    assert inv["erdat"] == date(2025, 3, 4) and inv["erzet"] == time(0, 15)
    assert inv["zzpay_status"] == "A" and inv["zzaugru"] is None
    cm = d.batch.table("erp.vbrk").pending("0090000012")
    assert (cm["fkart"], cm["vbtyp"], cm["zterm"], cm["vtweg"]) == ("G2", "O", "Z000", "20")
    assert cm["zzaugru"] == "R01" and cm["zzpay_status"] == "C" and cm["zzpaid_amount"] == 5.0
    vbrp = d.batch.table("erp.vbrp")
    i = vbrp.pending(("0090000011", "000020"))
    assert (i["aubel"], i["aupos"], i["vgbel"], i["vgtyp"]) == ("0010000005", "000020", "0080000008", "J")
    c = vbrp.pending(("0090000012", "000010"))
    assert (c["vgbel"], c["vgtyp"], c["aubel"]) == ("0090000010", "M", "0010000006")


def _final_rows(batches):
    """Apply batches the way Postgres would (insert, then UPDATE by pk)."""
    tables: dict[str, dict] = {}
    for events in batches:
        d, _ = make_dispatcher()
        for ev in events:
            d.dispatch(ev)
        for name, buf in d.batch.buffers.items():
            t = tables.setdefault(name, {})
            for k, row in buf.inserts.items():
                t[k] = dict(row)
            for k, ch in buf.updates.items():
                if k in t:
                    t[k].update(ch)
    return tables


def test_final_state_independent_of_batching():
    events = master_events() + doc_events() + update_events()
    one = _final_rows([events])
    split = _final_rows([master_events() + doc_events(), update_events()])
    each = _final_rows([[ev] for ev in events])
    assert one == split == each


# -- Postgres integration ----------------------------------------------------------

DSN = os.environ.get("CPGSIM_TEST_DSN")


def _flush(d):
    import psycopg

    with psycopg.connect(DSN) as conn:
        with conn.transaction(), conn.cursor() as cur:
            d.batch.flush(cur)
    d.batch.clear()


@pytest.mark.skipif(not DSN, reason="CPGSIM_TEST_DSN not set")
def test_roundtrip_against_postgres():
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS erp CASCADE")
        sql = (SCHEMAS / "erp.sql").read_text()
        conn.execute(sql)
        conn.execute(sql)  # idempotent

    d, w = make_dispatcher()
    for ev in master_events() + doc_events():
        d.dispatch(ev)
    _flush(d)
    for ev in update_events():
        d.dispatch(ev)
    _flush(d)

    with psycopg.connect(DSN) as conn:
        def one(sql, *args):
            return conn.execute(sql, args).fetchone()

        assert one("SELECT stprs, laepr, created_at, updated_at FROM erp.mbew WHERE matnr = %s",
                   keys.matnr(1)) == (Decimal("1.30"), date(2025, 4, 1), T0, T2)
        assert one("SELECT maktx FROM erp.makt WHERE matnr = %s AND spras = 'D'", keys.matnr(2)) == (
            "Velmora Sonnenspray SPF 50 200 ml",)
        assert one("SELECT gdatu, ukurs FROM erp.tcurr") == ("20250301", Decimal("1.05320"))
        assert one("SELECT gbstk, lfstk, fkstk, netwr, netwr_eur, updated_at FROM erp.vbak"
                   " WHERE vbeln = '0010000005'") == ("B", "C", "A", Decimal("84.96"),
                                                      Decimal("84.96"), T2)
        assert one("SELECT bstnk FROM erp.vbak WHERE vbeln = '0010000006'") == (
            keys.shop_order_id(SEED, 4711),)
        rows = conn.execute(
            "SELECT posnr, kbmeng, zzlfimg, zzfkimg, gbsta, lfsta, fksta FROM erp.vbap"
            " WHERE vbeln = '0010000005' ORDER BY posnr").fetchall()
        assert rows == [("000010", Decimal("24.000"), Decimal("24.000"), Decimal("0.000"),
                         "B", "C", "A"),
                        ("000020", Decimal("6.000"), Decimal("6.000"), Decimal("6.000"),
                         "C", "C", "C")]
        assert one("SELECT count(*), sum(kwert) FROM erp.konv WHERE knumv = %s",
                   keys.knumv_sales(6)) == (6, Decimal("12.28"))
        assert one("SELECT mtart, ersda FROM erp.mara WHERE matnr = 'SHIPPING'") == ("DIEN", date(2016, 5, 1))
        # every web item joins to the material master, the freight item included
        assert one("SELECT count(*) FROM erp.vbap p LEFT JOIN erp.mara m USING (matnr)"
                   " WHERE p.vbeln = '0010000006' AND m.matnr IS NULL") == (0,)
        likp = one("SELECT kostk, wbstk, pdstk, wadat_ist, podat, potim, bolnr, zzfreight_eur,"
                   " zzpod_signer, is_deleted, updated_at FROM erp.likp WHERE vbeln = '0080000008'")
        assert likp == ("C", "C", "C", date(2025, 4, 2), date(2025, 4, 2), time(11, 0), "TA123",
                        Decimal("84.50"), "A. Lager", False, T2)
        assert conn.execute("SELECT posnr, pikmg FROM erp.lips WHERE vbeln = '0080000008'"
                            " ORDER BY posnr").fetchall() == [("000010", Decimal("24.000")),
                                                              ("000020", Decimal("5.000"))]
        assert one("SELECT is_deleted, deleted_at FROM erp.likp WHERE vbeln = '0080000009'") == (
            True, T2)
        assert one("SELECT zzpay_status, zzaugdt, zzpaid_amount, zzdue_date FROM erp.vbrk"
                   " WHERE vbeln = '0090000011'") == ("C", date(2025, 4, 2), Decimal("99.10"),
                                                      date(2025, 4, 20))
        assert one("SELECT fkart, vbtyp FROM erp.vbrk WHERE vbeln = '0090000012'") == ("G2", "O")
        assert one("SELECT count(*) FROM erp.vbrp") == (3,)

        # loading contract, checked against the catalogue
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'erp'")}
        assert tables == {s.name.split(".")[1] for s in w.tables}
        for spec in w.tables:
            tname = spec.name.split(".")[1]
            pk = [r[0] for r in conn.execute(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid"
                " AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary"
                " ORDER BY array_position(i.indkey, a.attnum)", (spec.name,))]
            assert tuple(pk) == spec.pk, spec.name
            cols = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
                " WHERE table_schema = 'erp' AND table_name = %s", (tname,))}
            assert cols["created_at"] == ("timestamp with time zone", "NO")
            assert cols["updated_at"] == ("timestamp with time zone", "NO")
            assert cols["is_deleted"] == ("boolean", "NO") and "deleted_at" in cols
            idx = conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'erp'"
                               " AND tablename = %s", (tname,)).fetchall()
            assert any("(updated_at)" in r[0] for r in idx), spec.name


# -- phase 2: trade promotion agreements and deductions ------------------------------

from cpgsim.domain.events import (  # noqa: E402
    DeductionChanged,
    DeductionClaimed,
    DeductionCleared,
    TradeAgreementDefined,
    TradeAgreementItem,
)
from cpgsim.domain.model import Deduction, DeductionItem  # noqa: E402

T3 = datetime(2025, 5, 12, 8, 30, tzinfo=timezone.utc)  # 10:30 local
T4 = datetime(2025, 6, 2, 13, 0, tzinfo=timezone.utc)


def agreement():
    return TradeAgreementDefined(
        at=T0, promo_id=80_251_100, channel="drogerie_kranz", account_id="KRANZ",
        text="AKTION KW15/2025", start=date(2025, 4, 7), end=date(2025, 4, 13), discount_pct=0.25,
        off_invoice_share=0.5, scanback_share=0.25, advertising_fee_eur=2500.0,
        items=[TradeAgreementItem(1, "KR-778812", 0.3125), TradeAgreementItem(2, "KR-778813", 0.12345)],
    )


def promo_claim(status="claimed"):
    return Deduction(
        deduction_id=12, kind="promotion", channel="drogerie_kranz", payer="KRANZ",
        claimed_on=date(2025, 5, 12), claimed_at=T3, amount=9.11, validated_amount=8.0, status=status,
        items=[DeductionItem(1, "KR-778812", True, 20, 0.3125, 7.44),
               DeductionItem(2, "KR-778813", False, 11, 0.1276, 1.67)],
        promo_id=80_251_100, promo_text="AKTION KW15/2025", period_from=date(2025, 4, 7),
        period_to=date(2025, 4, 13),
    )


def coop_claim():
    return Deduction(
        deduction_id=13, kind="coop", channel="drogerie_kranz", payer="KRANZ", claimed_on=date(2025, 6, 2),
        claimed_at=T4, amount=40.0, validated_amount=40.0, status="accepted", billing_id=11,
        accepted_amount=40.0,
    )


def deduction_events():
    return [
        agreement(),
        DeductionClaimed(at=T3, deduction=promo_claim()),
        DeductionCleared(at=T4, deduction_id=12, seq=1, kind="taken", billing_id=11, amount=9.11,
                         on=date(2025, 6, 2), taken_total=9.11, repaid_total=0.0),
        DeductionChanged(at=T4, deduction_id=12, status="disputed",
                         fields={"disputed_on": date(2025, 6, 2), "dispute_reason": "D01"}),
        DeductionClaimed(at=T4, deduction=coop_claim()),
        DeductionCleared(at=T4, deduction_id=13, seq=1, kind="taken", billing_id=11, amount=40.0,
                         on=date(2025, 6, 2), taken_total=40.0, repaid_total=0.0),
        DeductionChanged(at=T4, deduction_id=13, fields={"closed_on": date(2025, 6, 2)}),
    ]


def deduction_updates():
    return [
        DeductionChanged(at=T4, deduction_id=12, status="accepted",
                         fields={"resolved_on": "2025-06-20", "accepted_amount": 8.0}),
        DeductionCleared(at=T4, deduction_id=12, seq=2, kind="repaid", billing_id=None, amount=1.11,
                         on="2025-07-01", taken_total=9.11, repaid_total=1.11),
        DeductionChanged(at=T4, deduction_id=12, fields={"closed_on": date(2025, 7, 1)}),
    ]


def test_trade_agreement_rows():
    d, _ = make_dispatcher()
    d.dispatch(agreement())
    k = d.batch.table("erp.kona").pending("0080251100")
    assert k is not None and k["knuma"] == keys.knuma_promotion(80_251_100)
    assert (k["boart"], k["kunnr"], k["botext"], k["datab"], k["datbi"]) == (
        "ZPRO", "0000100001", "AKTION KW15/2025", date(2025, 4, 7), date(2025, 4, 13))
    assert (k["zzdisc_pct"], k["zzofi_pct"], k["zzscan_pct"], k["zzwkz_eur"]) == (25.0, 50.0, 25.0, 2500.0)
    assert k["erdat"] == date(2025, 3, 1) and k["ernam"] == "TPM_IF"
    i2 = d.batch.table("erp.zkona_item").pending((k["knuma"], "000020"))
    assert (i2["matnr"], i2["kdmat"], i2["zzscan_eur"]) == (keys.matnr(2), "KR-778813", 0.1235)


def test_deduction_rows_items_and_clearing():
    d, _ = make_dispatcher()
    for ev in deduction_events():
        d.dispatch(ev)
    zd = d.batch.table("erp.zdeduct")
    h = zd.pending("0070000012")
    assert (h["bukrs"], h["gjahr"], h["kunnr"], h["zztype"], h["xblnr"]) == (
        "1000", "2025", "0000100001", "ZP01", "BL2500000012")
    assert (h["wrbtr"], h["dmbtr"], h["waers"], h["bldat"], h["budat"]) == (
        9.11, 9.11, "EUR", date(2025, 5, 12), date(2025, 5, 12))
    assert h["knuma"] == keys.knuma_promotion(80_251_100) and h["zzpromo_text"] == "AKTION KW15/2025"
    assert (h["zzperiod_from"], h["zzperiod_to"]) == (date(2025, 4, 7), date(2025, 4, 13))
    assert (h["vbeln_vl"], h["vbeln_va"], h["vbeln_vf"]) == (None, None, None)
    assert (h["zzstatus"], h["zzdisp_date"], h["zzdisp_reason"], h["zztaken"], h["zzrepaid"]) == (
        "D", date(2025, 6, 2), "D01", 9.11, 0.0)
    assert (h["erdat"], h["erzet"], h["ernam"]) == (date(2025, 5, 12), time(10, 30), "DEDUCT_IN")
    items = d.batch.table("erp.zdeduct_item")
    i1, i2 = items.pending(("0070000012", "000010")), items.pending(("0070000012", "000020"))
    assert (i1["matnr"], i1["kdmat"], i1["zzunits"], i1["zzrate"], i1["wrbtr"]) == (
        keys.matnr(1), "KR-778812", 20, 0.3125, 7.44)
    assert i2["matnr"] is None and i2["kdmat"] == "KR-778813"  # not mapped in knmt
    assert h["wrbtr"] == round(i1["wrbtr"] + i2["wrbtr"], 2)  # R3
    clr = d.batch.table("erp.zdeduct_clr").pending(("0070000012", "001"))
    assert (clr["zzkind"], clr["vbeln_vf"], clr["augdt"], clr["dmbtr"]) == (
        "T", "0090000011", date(2025, 6, 2), 9.11)
    # contractual co-op: accepted, taken and closed in one go
    c = zd.pending("0070000013")
    assert (c["zztype"], c["zzstatus"], c["vbeln_vf"], c["zzaccepted"], c["zzres_date"]) == (
        "ZK01", "A", "0090000011", 40.0, date(2025, 6, 2))
    assert (c["zztaken"], c["zzclose_date"], c["knuma"]) == (40.0, date(2025, 6, 2), None)


def test_deduction_type_and_reference_mapping():
    d, _ = make_dispatcher()
    base = dict(channel="mylo", payer="KRANZ", claimed_on=date(2026, 1, 9), claimed_at=T3, amount=1.0,
                validated_amount=1.0)
    cases = [
        (Deduction(1, "shortage", delivery_id=8, **base), "ZS01", "vbeln_vl", "0080000008"),
        (Deduction(2, "service_penalty", sales_doc_id=5, **base), "ZL01", "vbeln_va", "0010000005"),
        (Deduction(3, "clearance", window_id="w", **base), "ZP05", "knuma", None),
        (Deduction(4, "unexplained", promo_id=7, **base), "ZX99", "knuma", "0000000007"),
        (Deduction(5, "advertising", promo_id=7, **base), "ZW01", "knuma", "0000000007"),
    ]
    for ded, zztype, col, val in cases:
        d.dispatch(DeductionClaimed(at=T3, deduction=ded))
        row = d.batch.table("erp.zdeduct").pending(keys.belnr_deduction(ded.deduction_id))
        assert row["zztype"] == zztype and row[col] == val and row["zzstatus"] == "O"
        assert row["xblnr"] == f"MY-DED-{ded.deduction_id:07d}"
    with pytest.raises(ValueError):
        d.dispatch(DeductionClaimed(at=T3, deduction=Deduction(9, "bogus", **base)))


def _deduction_final_rows(batches):
    tables: dict[str, dict] = {}
    for events in batches:
        d, _ = make_dispatcher()
        for ev in events:
            d.dispatch(ev)
        for name, buf in d.batch.buffers.items():
            t = tables.setdefault(name, {})
            for k, row in buf.inserts.items():
                t[k] = dict(row)
            for k, ch in buf.updates.items():
                if k in t:
                    t[k].update(ch)
    return tables


def test_deduction_final_state_independent_of_batching():
    events = deduction_events() + deduction_updates()
    one = _deduction_final_rows([events])
    split = _deduction_final_rows([deduction_events(), deduction_updates()])
    each = _deduction_final_rows([[ev] for ev in events])
    assert one == split == each
    h = one["erp.zdeduct"][("0070000012",)]
    assert (h["zzstatus"], h["zzaccepted"], h["zzres_date"], h["zztaken"], h["zzrepaid"], h["zzclose_date"]) == (
        "A", 8.0, date(2025, 6, 20), 9.11, 1.11, date(2025, 7, 1))
    p = one["erp.zdeduct_clr"][("0070000012", "002")]
    assert (p["zzkind"], p["vbeln_vf"], p["augdt"], p["dmbtr"]) == ("P", None, date(2025, 7, 1), 1.11)


@pytest.mark.skipif(not DSN, reason="CPGSIM_TEST_DSN not set")
def test_deduction_roundtrip_against_postgres():
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS erp CASCADE")
        conn.execute((SCHEMAS / "erp.sql").read_text())

    d, _ = make_dispatcher()
    for ev in deduction_events():
        d.dispatch(ev)
    _flush(d)
    for ev in deduction_updates():
        d.dispatch(ev)
    _flush(d)

    with psycopg.connect(DSN) as conn:
        def one(sql, *args):
            return conn.execute(sql, args).fetchone()

        assert one("SELECT zzstatus, zzaccepted, zztaken, zzrepaid, zzclose_date, created_at, updated_at"
                   " FROM erp.zdeduct WHERE belnr = '0070000012'") == (
            "A", Decimal("8.00"), Decimal("9.11"), Decimal("1.11"), date(2025, 7, 1), T3, T4)
        # R2 for closed cases
        bad = conn.execute(
            "SELECT z.belnr FROM erp.zdeduct z LEFT JOIN erp.zdeduct_clr c USING (belnr)"
            " WHERE z.zzclose_date IS NOT NULL GROUP BY z.belnr, z.zzaccepted, z.zztaken, z.zzrepaid"
            " HAVING coalesce(sum(c.dmbtr) FILTER (WHERE c.zzkind = 'T'), 0)"
            "      - coalesce(sum(c.dmbtr) FILTER (WHERE c.zzkind = 'P'), 0) <> coalesce(z.zzaccepted, 0)"
            "   OR coalesce(sum(c.dmbtr) FILTER (WHERE c.zzkind = 'T'), 0) <> z.zztaken"
            "   OR coalesce(sum(c.dmbtr) FILTER (WHERE c.zzkind = 'P'), 0) <> z.zzrepaid").fetchall()
        assert bad == []
        # R3: promotion claim amount = sum of items, agreement exists
        assert one("SELECT z.wrbtr = sum(i.wrbtr), bool_and(k.knuma IS NOT NULL) FROM erp.zdeduct z"
                   " JOIN erp.zdeduct_item i USING (belnr) LEFT JOIN erp.kona k ON k.knuma = z.knuma"
                   " WHERE z.zztype = 'ZP01' GROUP BY z.belnr, z.wrbtr") == (True, True)
        assert one("SELECT count(*) FROM erp.zdeduct_item WHERE matnr IS NULL") == (1,)
        assert one("SELECT count(*), sum(dmbtr) FROM erp.zdeduct_clr WHERE vbeln_vf = '0090000011'") == (
            2, Decimal("49.11"))
        assert one("SELECT count(*) FROM erp.zkona_item") == (2,)


def test_kna1_search_terms_are_unique_for_all_accounts():
    """erp.kna1.sortl (10 characters) tells every account apart, also the two marketplace FCs."""
    from cpgsim.config import Config
    from cpgsim.domain.accounts import build_accounts
    from cpgsim.writers.erp import _sortl

    accounts = build_accounts(Config()).all
    terms = [_sortl(a.account_id) for a in accounts]
    assert all(len(t) <= 10 for t in terms)
    assert len(set(terms)) == len(terms), sorted(terms)
    assert _sortl("SHOPNOVA") == "SHOPNOVA" and _sortl("SHOPNOVA-DC2") == "SHOPNO-DC2"
    assert _sortl("KRANZ-DC1") == "KRANZ-DC1"
