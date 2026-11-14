import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from cpgsim.domain.events import (
    CustomerAnonymised,
    CustomerRegistered,
    PaymentRecorded,
    PaymentStatusChanged,
    PromotionDefined,
    RefundChanged,
    RefundIssued,
    SubscriptionChanged,
    SubscriptionCreated,
    WebOrderLinesChanged,
    WebOrderPlaced,
    WebOrderMilestoneReached,
    WebOrderStatusChanged,
)
from cpgsim.domain.model import (
    Address,
    Customer,
    OrderLine,
    Payment,
    Promotion,
    Refund,
    Subscription,
    WebOrder,
)
from cpgsim.models.pricing import split_gross
from cpgsim.writers import keys
from cpgsim.writers.base import Dispatcher, WriterContext
from cpgsim.writers.shop import ShopWriter, included_tax

SHOP_SQL = Path(__file__).resolve().parents[2] / "sql" / "schemas" / "shop.sql"
SEED = 11
T0 = datetime(2025, 3, 1, 9, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=2)
T2 = T0 + timedelta(days=1)
T3 = T0 + timedelta(days=3)

SKUS = {
    1: SimpleNamespace(code="VEL-BOD-HYB-400", ean="2000000000015",
                       name="Velmora Hydra Balance Body Lotion 400 ml"),
    2: SimpleNamespace(code="CUT-LIP-SOF-5", ean="2000000000022",
                       name="Cutara Soft Lip Balm 5 g"),
}


def make_dispatcher(seed=SEED):
    ref = SimpleNamespace(
        catalogue=SimpleNamespace(get=lambda i: SKUS[i]),
        accounts=SimpleNamespace(numbers={}, get=lambda a: None),
    )
    ctx = WriterContext(seed=seed, cfg=None, ref=ref, tz=None)
    w = ShopWriter(ctx)
    return Dispatcher([w]), w


ADDR = Address(country="DE", region="DE-BY", postcode="80331", city="Muenchen",
               street="Musterstrasse", house_number="12a")


def customer(cid=1):
    return Customer(customer_id=cid, country="DE", address=ADDR, created_at=T0,
                    acquisition="influencer", influencer_code="GLOW10")


def registered(cid=1, at=T0):
    return CustomerRegistered(
        at=at, customer=customer(cid), first_name="Lena", last_name="Beispiel",
        email="lena.beispiel@example.de", phone="+49 151 0000000", birth_year=1990,
        marketing_opt_in=True, locale="de_DE", address=ADDR,
    )


def promotion(pid=3, channel="web", kind="code", free_shipping=False):
    return Promotion(
        promo_id=pid, channel=channel, name="Spring Glow", kind=kind,
        code="SPRING20" if kind == "code" else None, discount_pct=0.2,
        free_shipping=free_shipping, sku_ids=[1, 2], brand="Velmora",
        start=T0, end=T0 + timedelta(days=7), announced_at=T0 - timedelta(days=2),
    )


def order(oid=5, promo_id=3, discount=4.0, shipping=0.0, subscription_id=None):
    return WebOrder(
        order_id=oid, customer_id=1, country="DE", currency="EUR", placed_at=T0,
        ship_to=ADDR,
        lines=[
            OrderLine(line_no=1, sku_id=1, qty=2, unit_price=9.99, discount=discount,
                      tax_rate=0.19),
            OrderLine(line_no=2, sku_id=2, qty=1, unit_price=3.49, discount=0.0, tax_rate=0.19),
        ],
        shipping_fee=shipping, payment_method="paypal", promo_id=promo_id,
        subscription_id=subscription_id, is_repeat=True,
    )


@dataclass(slots=True)
class PlacedWithPromo(WebOrderPlaced):
    """WebOrderPlaced plus the requested ``promotion`` field (contract change request)."""

    promotion: Promotion | None = None


def placed(o, promo=None, at=T0):
    if promo is not None:
        # Not routed by the Dispatcher (exact-type routing): call the handler directly.
        return PlacedWithPromo(at=at, order=o, customer=customer(), source="influencer",
                               promotion=promo)
    return WebOrderPlaced(at=at, order=o, customer=customer(), source="influencer")


def rows(d, table):
    return list(d.batch.table(table).inserts.values())


# -- pure / buffer-level tests ------------------------------------------------


def test_included_tax():
    assert included_tax(Decimal("119.00"), Decimal("0.19")) == Decimal("19.00")
    assert included_tax(Decimal("3.49"), Decimal("0.19")) == Decimal("0.56")
    assert included_tax(Decimal("0.00"), Decimal("0.19")) == Decimal("0.00")
    # half-cent net amounts: net rounds half-up, tax is the remainder (like the ERP item)
    assert included_tax(Decimal("3.45"), Decimal("0.20")) == Decimal("0.57")
    assert included_tax(Decimal("4.95"), Decimal("0.20")) == Decimal("0.82")
    assert included_tax(Decimal("34.95"), Decimal("0.20")) == Decimal("5.82")


def test_included_tax_matches_domain_split():
    """Shop and ERP use one net/tax split: no one-cent disagreements."""
    for cents in range(1, 20001, 7):
        gross = cents / 100
        for vat in (0.19, 0.20, 0.081):
            _, tax = split_gross(gross, vat)
            assert included_tax(Decimal(str(gross)), Decimal(str(vat))) == Decimal(str(tax)).quantize(Decimal("0.01"))


def test_late_status_uses_effective_time_and_refund_completion():
    d, _ = make_dispatcher()
    d.dispatch(WebOrderStatusChanged(at=T3, order_id=5, status="shipped", effective_at=T1))
    d.dispatch(WebOrderStatusChanged(at=T3, order_id=5, status="delivered", effective_at=T2))
    ch = d.batch.table("shop.orders").updates[(keys.shop_order_id(SEED, 5),)]
    assert ch["shipped_at"] == T1 and ch["delivered_at"] == T2 and ch["status"] == "delivered"
    assert ch["updated_at"] == T3
    d.dispatch(WebOrderStatusChanged(at=T3, order_id=6, status="return_rejected"))
    ch = d.batch.table("shop.orders").updates[(keys.shop_order_id(SEED, 6),)]
    assert ch["return_rejected_at"] == T3
    d.dispatch(RefundChanged(at=T3, refund_id=4, status="completed"))
    rc = d.batch.table("shop.refunds").updates[(keys.shop_refund_id(SEED, 4),)]
    assert rc["status"] == "completed" and rc["completed_at"] == T3 and rc["updated_at"] == T3
    with pytest.raises(ValueError, match="refund status"):
        d.dispatch(RefundChanged(at=T3, refund_id=4, status="bogus"))


def test_milestone_sets_timestamp_without_status():
    """A return status hides the later shipment: only shipped_at / delivered_at are set."""
    d, _ = make_dispatcher()
    d.dispatch(WebOrderStatusChanged(at=T1, order_id=7, status="refunded"))
    d.dispatch(WebOrderMilestoneReached(at=T3, order_id=7, milestone="shipped", effective_at=T2))
    d.dispatch(WebOrderMilestoneReached(at=T3, order_id=7, milestone="delivered"))
    ch = d.batch.table("shop.orders").updates[(keys.shop_order_id(SEED, 7),)]
    assert ch["status"] == "refunded" and ch["refunded_at"] == T1
    assert ch["shipped_at"] == T2 and ch["delivered_at"] == T3 and ch["updated_at"] == T3
    with pytest.raises(ValueError, match="milestone"):
        d.dispatch(WebOrderMilestoneReached(at=T3, order_id=7, milestone="returned"))


def test_customer_and_default_address():
    d, _ = make_dispatcher()
    d.dispatch(registered())
    (c,) = rows(d, "shop.customers")
    (a,) = rows(d, "shop.addresses")
    assert c["id"] == keys.shop_customer_id(SEED, 1)
    assert c["status"] == "active" and c["signup_source"] == "influencer"
    assert c["influencer_code"] == "GLOW10" and c["registered_at"] == T0
    assert a["id"] == keys.shop_address_id(SEED, 1, 1)
    assert a["customer_id"] == c["id"] and a["is_default"] is True
    assert "region" not in a
    assert c["created_at"] == T0 and c["is_deleted"] is False


def test_anonymisation_in_same_batch_coalesces():
    d, _ = make_dispatcher()
    d.dispatch(registered())
    d.dispatch(CustomerAnonymised(at=T1, customer_id=1))
    (c,) = rows(d, "shop.customers")
    (a,) = rows(d, "shop.addresses")
    assert c["status"] == "anonymised" and c["first_name"] == "anonymised"
    assert c["email"].endswith("@anonymised.invalid") and c["phone"] is None
    assert c["anonymised_at"] == T1 and c["updated_at"] == T1 and c["created_at"] == T0
    assert a["street"] == "anonymised" and a["postcode"] == "80331"


def test_only_web_promotions():
    d, _ = make_dispatcher()
    d.dispatch(PromotionDefined(at=T0, promotion=promotion(pid=1, channel="drogerie_kranz")))
    d.dispatch(PromotionDefined(at=T0, promotion=promotion(pid=2)))
    (p,) = rows(d, "shop.promotions")
    assert p["id"] == keys.shop_promotion_id(SEED, 2)
    assert p["skus"] == ["VEL-BOD-HYB-400", "CUT-LIP-SOF-5"]
    assert p["code"] == "SPRING20" and p["discount_pct"] == Decimal("0.2000")


def test_order_amounts_and_lines():
    d, _ = make_dispatcher()
    d.dispatch(placed(order(shipping=4.95, subscription_id=8)))
    (o,) = rows(d, "shop.orders")
    lines = rows(d, "shop.order_lines")
    assert o["order_number"] == "NV-10000005"
    assert o["items_subtotal"] == Decimal("23.47")
    assert o["discount_total"] == Decimal("4.00")
    assert o["shipping_fee"] == Decimal("4.95")
    assert o["total_amount"] == Decimal("24.42")
    # line taxes 15.98 -> 2.55, 3.49 -> 0.56; shipping 4.95 -> 0.79
    assert o["tax_total"] == Decimal("3.90")
    assert o["item_count"] == 3 and o["is_repeat_customer"] is True
    assert o["subscription_id"] == keys.shop_subscription_id(SEED, 8)
    assert o["shipping_address_id"] == o["billing_address_id"] == keys.shop_address_id(SEED, 1, 1)
    assert [ln["line_total"] for ln in lines] == [Decimal("15.98"), Decimal("3.49")]
    assert lines[0]["id"] == keys.shop_order_line_id(SEED, 5, 1)
    assert lines[0]["sku"] == "VEL-BOD-HYB-400" and lines[0]["tax_rate"] == Decimal("0.1900")
    # discount applied but promotion details unknown: redemption without code
    (r,) = rows(d, "shop.promo_redemptions")
    assert r["discount_amount"] == Decimal("4.00") and r["code"] is None
    assert o["promotion_id"] == keys.shop_promotion_id(SEED, 3) and o["promo_code"] is None


def test_promo_code_when_promotion_known():
    d, w = make_dispatcher()
    w.on_order_placed(placed(order(), promo=promotion()))
    (o,) = rows(d, "shop.orders")
    (r,) = rows(d, "shop.promo_redemptions")
    assert o["promo_code"] == "SPRING20" and r["code"] == "SPRING20"


def test_no_redemption_without_applied_discount():
    d, w = make_dispatcher()
    d.dispatch(placed(order(oid=6, discount=0.0, shipping=4.95)))
    assert rows(d, "shop.promo_redemptions") == []
    (o,) = rows(d, "shop.orders")
    assert o["promotion_id"] is None
    # free-shipping promotion with zero line discount counts when the promotion is known
    w.on_order_placed(placed(order(oid=7, discount=0.0, shipping=0.0),
                             promo=promotion(kind="automatic", free_shipping=True)))
    (r,) = rows(d, "shop.promo_redemptions")
    assert r["discount_amount"] == Decimal("0.00") and r["code"] is None


def test_status_timestamps_and_validation():
    d, _ = make_dispatcher()
    d.dispatch(placed(order()))
    d.dispatch(WebOrderStatusChanged(at=T1, order_id=5, status="authorised"))
    d.dispatch(WebOrderStatusChanged(at=T2, order_id=5, status="cancelled", reason="customer"))
    (o,) = rows(d, "shop.orders")
    assert o["status"] == "cancelled" and o["paid_at"] == T1
    assert o["cancelled_at"] == T2 and o["cancel_reason"] == "customer"
    with pytest.raises(ValueError):
        d.dispatch(WebOrderStatusChanged(at=T2, order_id=5, status="teleported"))
    with pytest.raises(ValueError):
        d.dispatch(WebOrderLinesChanged(at=T2, order_id=5, lines={1: {"picked_qty": 1}}))


def test_payment_lifecycle_in_buffer():
    d, _ = make_dispatcher()
    pay = Payment(payment_id=9, order_id=5, attempt=1, method="invoice_bnpl", amount=24.42,
                  currency="EUR", status="failed", created_at=T0, provider_ref="")
    d.dispatch(PaymentRecorded(at=T0, payment=pay))
    (p,) = rows(d, "shop.payments")
    assert p["status"] == "failed" and p["failed_at"] == T0 and p["failure_reason"]
    assert p["provider"] == "Kassiro" and p["provider_reference"] is None
    assert p["refunded_amount"] == Decimal("0.00")


# -- Postgres integration --------------------------------------------------------

DSN = os.environ.get("CPGSIM_TEST_DSN")
TABLES = ("customers", "addresses", "promotions", "subscriptions", "orders", "order_lines",
          "payments", "refunds", "promo_redemptions")


@pytest.fixture
def conn():
    if not DSN:
        pytest.skip("CPGSIM_TEST_DSN not set")
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN) as c:
        c.execute("DROP SCHEMA IF EXISTS shop CASCADE")
        ddl = SHOP_SQL.read_text()
        c.execute(ddl)
        c.execute(ddl)  # idempotent
        c.commit()
        yield c
        c.rollback()
        c.execute("DROP SCHEMA IF EXISTS shop CASCADE")
        c.commit()


@pytest.mark.db
def test_ddl_loading_contract(conn):
    cur = conn.cursor()
    for t in TABLES:
        cur.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
            " WHERE table_schema = 'shop' AND table_name = %s",
            (t,),
        )
        cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        assert cols["created_at"] == ("timestamp with time zone", "NO"), t
        assert cols["updated_at"] == ("timestamp with time zone", "NO"), t
        assert cols["is_deleted"] == ("boolean", "NO"), t
        assert cols["deleted_at"][0] == "timestamp with time zone", t
        cur.execute(
            "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'shop' AND c.relname = %s AND i.indisprimary",
            (t,),
        )
        assert cur.fetchone()[0] == 1, f"{t} has no primary key"
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'shop' AND tablename = %s",
            (t,),
        )
        assert any("(updated_at)" in r[0] for r in cur.fetchall()), f"{t} lacks updated_at index"
    # writer specs and DDL agree on the table set
    assert {s.name for s in ShopWriter.tables} == {f"shop.{t}" for t in TABLES}


@pytest.mark.db
def test_write_and_update_roundtrip(conn):
    d, _ = make_dispatcher()

    # -- batch 1: inserts --------------------------------------------------
    d.dispatch(registered(1))
    d.dispatch(registered(2))
    d.dispatch(PromotionDefined(at=T0, promotion=promotion()))
    sub = Subscription(subscription_id=8, customer_id=1, sku_ids=[1, 2], qtys=[1, 2],
                       interval_weeks=8, status="active", created_at=T0,
                       next_order_at=T0 + timedelta(weeks=8))
    d.dispatch(SubscriptionCreated(at=T0, subscription=sub))
    d.dispatch(placed(order(subscription_id=8)))
    d.dispatch(PaymentRecorded(at=T0, payment=Payment(
        payment_id=9, order_id=5, attempt=1, method="card", amount=19.47, currency="EUR",
        status="pending", created_at=T0, provider_ref="PL-0001")))
    d.dispatch(PaymentStatusChanged(at=T1, payment_id=9, status="authorised"))
    with conn.transaction():
        counts = d.batch.flush(conn.cursor())
    d.batch.clear()
    assert counts["shop"] == 2 + 2 + 1 + 1 + 1 + 2 + 1 + 1

    # -- batch 2: updates to committed rows ---------------------------------
    d.dispatch(WebOrderStatusChanged(at=T1, order_id=5, status="authorised"))
    d.dispatch(WebOrderStatusChanged(at=T2, order_id=5, status="partially_shipped"))
    d.dispatch(WebOrderLinesChanged(at=T2, order_id=5,
                                    lines={1: {"shipped_qty": 2}, 2: {"cancelled_qty": 1}}))
    d.dispatch(PaymentStatusChanged(at=T2, payment_id=9, status="captured", amount=15.98))
    d.dispatch(PaymentStatusChanged(at=T3, payment_id=9, status="partially_refunded",
                                    amount=7.99))
    d.dispatch(RefundIssued(at=T3, refund=Refund(
        refund_id=4, order_id=5, payment_id=9, amount=7.99, currency="EUR", reason="return",
        created_at=T3)))
    d.dispatch(SubscriptionChanged(at=T3, subscription_id=8, status="cancelled"))
    d.dispatch(CustomerAnonymised(at=T3, customer_id=2))
    with conn.transaction():
        d.batch.flush(conn.cursor())
    d.batch.clear()
    T4 = T3 + timedelta(days=2)
    d.dispatch(RefundChanged(at=T4, refund_id=4, status="completed"))
    with conn.transaction():
        d.batch.flush(conn.cursor())
    d.batch.clear()

    cur = conn.cursor()
    oid = keys.shop_order_id(SEED, 5)
    cur.execute(
        "SELECT status, paid_at, partially_shipped_at, shipped_at, total_amount, tax_total,"
        " promo_code, promotion_id::text, subscription_id::text, created_at, updated_at,"
        " is_deleted FROM shop.orders WHERE id = %s",
        (oid,),
    )
    o = cur.fetchone()
    assert o[0] == "partially_shipped" and o[1] == T1 and o[2] == T2 and o[3] is None
    assert o[4] == Decimal("19.47") and o[5] == Decimal("3.11")
    assert o[6] is None and o[7] == keys.shop_promotion_id(SEED, 3)
    assert o[8] == keys.shop_subscription_id(SEED, 8)
    assert o[9] == T0 and o[10] == T2 and o[11] is False

    cur.execute("SELECT line_no, qty_shipped, qty_cancelled, qty_returned, updated_at"
                " FROM shop.order_lines WHERE order_id = %s ORDER BY line_no", (oid,))
    assert cur.fetchall() == [(1, 2, 0, 0, T2), (2, 0, 1, 0, T2)]

    cur.execute("SELECT status, authorised_at, captured_at, captured_amount, refunded_at,"
                " refunded_amount, provider, provider_reference FROM shop.payments")
    assert cur.fetchall() == [("partially_refunded", T1, T2, Decimal("15.98"), T3,
                               Decimal("7.99"), "Paylance", "PL-0001")]

    cur.execute("SELECT payment_id::text, amount, reason, status, issued_at, completed_at, updated_at"
                " FROM shop.refunds")
    assert cur.fetchall() == [(keys.shop_payment_id(SEED, 9), Decimal("7.99"), "return",
                               "completed", T3, T3 + timedelta(days=2), T3 + timedelta(days=2))]

    cur.execute("SELECT status, skus, quantities, next_order_at, cancelled_at"
                " FROM shop.subscriptions")
    assert cur.fetchall() == [("cancelled", ["VEL-BOD-HYB-400", "CUT-LIP-SOF-5"], [1, 2], None,
                               T3)]

    cur.execute("SELECT skus, discount_pct, promo_type FROM shop.promotions")
    assert cur.fetchall() == [(["VEL-BOD-HYB-400", "CUT-LIP-SOF-5"], Decimal("0.2000"), "code")]

    cur.execute("SELECT discount_amount, code, redeemed_at FROM shop.promo_redemptions")
    assert cur.fetchall() == [(Decimal("4.00"), None, T0)]

    cur.execute("SELECT id::text, status, email, phone, anonymised_at, updated_at"
                " FROM shop.customers ORDER BY registered_at, email")
    by_id = {r[0]: r[1:] for r in cur.fetchall()}
    c2 = by_id[keys.shop_customer_id(SEED, 2)]
    assert c2[0] == "anonymised" and c2[1].endswith("@anonymised.invalid") and c2[2] is None
    assert c2[3] == T3 and c2[4] == T3
    assert by_id[keys.shop_customer_id(SEED, 1)][0] == "active"

    cur.execute("SELECT street, house_number, postcode FROM shop.addresses WHERE id = %s",
                (keys.shop_address_id(SEED, 2, 1),))
    assert cur.fetchone() == ("anonymised", None, "80331")
