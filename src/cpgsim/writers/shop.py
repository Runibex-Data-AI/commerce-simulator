"""Writer for the web shop platform schema (``shop``).

Conventions: UUID keys (``writers/keys.py``), English snake_case,
``timestamptz`` in UTC, statuses as the neutral lower-case words from
``domain/model.py``, money as ``numeric(12,2)`` gross (incl. VAT) with an ISO
currency column. Money is computed with ``Decimal`` from the domain's
cent-rounded floats so totals add up exactly.

Each order status sets a matching business timestamp column (``paid_at``,
``shipped_at``...). The writer keeps no state between events: everything it
writes is derived from the event, the seed and reference data, so output does
not depend on commit batching or restarts.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from ..domain.events import (
    CustomerAnonymised,
    CustomerRegistered,
    PaymentRecorded,
    PaymentStatusChanged,
    PromotionChanged,
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
from ..domain.model import (
    PAYMENT_STATUSES,
    REFUND_STATUSES,
    SUBSCRIPTION_STATUSES,
    WEB_ORDER_STATUSES,
    OrderLine,
    Promotion,
    WebOrder,
)
from ..engine.sink import TableSpec
from ..models.pricing import split_gross
from . import keys
from .base import Writer

CUSTOMERS = "shop.customers"
ADDRESSES = "shop.addresses"
PROMOTIONS = "shop.promotions"
SUBSCRIPTIONS = "shop.subscriptions"
ORDERS = "shop.orders"
ORDER_LINES = "shop.order_lines"
PAYMENTS = "shop.payments"
REFUNDS = "shop.refunds"
PROMO_REDEMPTIONS = "shop.promo_redemptions"

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

#: Order status -> business timestamp column set when the order enters it.
ORDER_STATUS_TIMESTAMPS: dict[str, str] = {
    "authorised": "paid_at",
    "payment_failed": "payment_failed_at",
    "released": "released_at",
    "cancelled": "cancelled_at",
    "partially_shipped": "partially_shipped_at",
    "shipped": "shipped_at",
    "delivered": "delivered_at",
    "return_requested": "return_requested_at",
    "returned": "returned_at",
    "return_rejected": "return_rejected_at",
    "partially_refunded": "refunded_at",
    "refunded": "refunded_at",
}

#: Payment status -> business timestamp column set when the payment enters it.
PAYMENT_STATUS_TIMESTAMPS: dict[str, str] = {
    "authorised": "authorised_at",
    "captured": "captured_at",
    "failed": "failed_at",
    "voided": "voided_at",
    "partially_refunded": "refunded_at",
    "refunded": "refunded_at",
}

#: Neutral line field (WebOrderLinesChanged) -> order_lines column.
LINE_FIELDS: dict[str, str] = {
    "shipped_qty": "qty_shipped",
    "cancelled_qty": "qty_cancelled",
    "returned_qty": "qty_returned",
}

#: Fictional payment service providers by payment method.
PAYMENT_PROVIDERS: dict[str, str] = {
    "card": "Paylance",
    "paypal": "Paylance",
    "direct_debit": "Paylance",
    "invoice_bnpl": "Kassiro",
    "other": "Paylance",
}

#: Failure reason when the event carries none (PaymentStatusChanged has no reason field yet).
DEFAULT_FAILURE_REASON = "declined"

ANONYMISED = "anonymised"


def money(x: float | int | Decimal) -> Decimal:
    """Cent-exact Decimal from a domain amount (floats go through ``str`` to avoid binary noise)."""
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def rate(x: float) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def included_tax(gross: Decimal, tax_rate: Decimal) -> Decimal:
    """VAT contained in a gross amount: ``gross - net``, net rounded half-up to cents.

    Uses the domain's ``models.pricing.split_gross``, the same split the ERP
    sales items carry, so shop and ERP agree on net and tax to the cent.
    """
    if gross == 0 or tax_rate == 0:
        return ZERO
    _, tax = split_gross(float(gross), float(tax_rate))
    return money(tax)


def _check(value: str, allowed: tuple[str, ...], what: str) -> str:
    if value not in allowed:
        raise ValueError(f"unknown {what} {value!r}")
    return value


class ShopWriter(Writer):
    tables = (
        TableSpec(CUSTOMERS, ("id",)),
        TableSpec(ADDRESSES, ("id",)),
        TableSpec(PROMOTIONS, ("id",)),
        TableSpec(SUBSCRIPTIONS, ("id",)),
        TableSpec(ORDERS, ("id",)),
        TableSpec(ORDER_LINES, ("id",)),
        TableSpec(PAYMENTS, ("id",)),
        TableSpec(REFUNDS, ("id",)),
        TableSpec(PROMO_REDEMPTIONS, ("promotion_id", "order_id")),
    )

    def handlers(self) -> dict[type, Callable[[Any], None]]:
        return {
            CustomerRegistered: self.on_customer_registered,
            CustomerAnonymised: self.on_customer_anonymised,
            PromotionDefined: self.on_promotion_defined,
            PromotionChanged: self.on_promotion_changed,
            WebOrderPlaced: self.on_order_placed,
            WebOrderStatusChanged: self.on_order_status_changed,
            WebOrderMilestoneReached: self.on_order_milestone,
            WebOrderLinesChanged: self.on_order_lines_changed,
            PaymentRecorded: self.on_payment_recorded,
            PaymentStatusChanged: self.on_payment_status_changed,
            RefundIssued: self.on_refund_issued,
            RefundChanged: self.on_refund_changed,
            SubscriptionCreated: self.on_subscription_created,
            SubscriptionChanged: self.on_subscription_changed,
        }

    # -- helpers -----------------------------------------------------------
    def _sku_codes(self, sku_ids: list[int]) -> list[str]:
        cat = self.ctx.ref.catalogue
        return [cat.get(s).code for s in sku_ids]

    # -- customers ---------------------------------------------------------
    def on_customer_registered(self, e: CustomerRegistered) -> None:
        c = e.customer
        cid = keys.shop_customer_id(self.seed, c.customer_id)
        self.t(CUSTOMERS).insert(
            {
                "id": cid,
                "email": e.email,
                "first_name": e.first_name,
                "last_name": e.last_name,
                "phone": e.phone,
                "birth_year": e.birth_year,
                "locale": e.locale,
                "country": c.country,
                "marketing_opt_in": e.marketing_opt_in,
                "signup_source": c.acquisition,
                "influencer_code": c.influencer_code,
                "status": "active",
                "registered_at": c.created_at,
                "anonymised_at": None,
            },
            e.at,
        )
        a = e.address
        self.t(ADDRESSES).insert(
            {
                "id": keys.shop_address_id(self.seed, c.customer_id, 1),
                "customer_id": cid,
                "address_type": "shipping",
                "is_default": True,
                "first_name": e.first_name,
                "last_name": e.last_name,
                "street": a.street,
                "house_number": a.house_number or None,
                "postcode": a.postcode or None,
                "city": a.city,
                "country": a.country,
            },
            e.at,
        )

    def on_customer_anonymised(self, e: CustomerAnonymised) -> None:
        """GDPR erasure: personal data blanked in place, rows (and orders) kept.

        Postcode, city and country stay: they are not identifying on their own
        and keep regional reporting intact.
        """
        cid = keys.shop_customer_id(self.seed, e.customer_id)
        self.t(CUSTOMERS).update(
            cid,
            {
                "email": f"{cid}@{ANONYMISED}.invalid",
                "first_name": ANONYMISED,
                "last_name": ANONYMISED,
                "phone": None,
                "birth_year": None,
                "marketing_opt_in": False,
                "influencer_code": None,
                "status": ANONYMISED,
                "anonymised_at": e.at,
            },
            e.at,
        )
        self.t(ADDRESSES).update(
            keys.shop_address_id(self.seed, e.customer_id, 1),
            {
                "first_name": ANONYMISED,
                "last_name": ANONYMISED,
                "street": ANONYMISED,
                "house_number": None,
            },
            e.at,
        )

    # -- promotions --------------------------------------------------------
    def on_promotion_defined(self, e: PromotionDefined) -> None:
        p = e.promotion
        if p.channel != "web":
            return  # retailer promotions live in the retailers' systems, not the shop
        self.t(PROMOTIONS).insert(
            {
                "id": keys.shop_promotion_id(self.seed, p.promo_id),
                "name": p.name,
                "promo_type": p.kind,
                "code": p.code if p.kind == "code" else None,
                "discount_pct": rate(p.discount_pct),
                "free_shipping": p.free_shipping,
                "skus": self._sku_codes(p.sku_ids),
                "brand": p.brand,
                "starts_at": p.start,
                "ends_at": p.end,
            },
            e.at,
        )

    def on_promotion_changed(self, e: PromotionChanged) -> None:
        """End moved (e.g. ``sim scenario disable``): update ``ends_at``; withdrawn -> soft delete."""
        p = e.promotion
        if p.channel != "web":
            return
        pk = keys.shop_promotion_id(self.seed, p.promo_id)
        buf = self.t(PROMOTIONS)
        buf.update(pk, {"ends_at": p.end}, e.at)
        if e.withdrawn:
            buf.soft_delete(pk, e.at)

    # -- orders ------------------------------------------------------------
    def on_order_placed(self, e: WebOrderPlaced) -> None:
        o = e.order
        seed = self.seed
        oid = keys.shop_order_id(seed, o.order_id)
        cid = keys.shop_customer_id(seed, o.customer_id)
        address_id = keys.shop_address_id(seed, o.customer_id, 1)
        cat = self.ctx.ref.catalogue

        lines = self.t(ORDER_LINES)
        subtotal = discount = tax = ZERO
        items = 0
        for ln in o.lines:
            row = self._line_row(o, oid, ln, cat.get(ln.sku_id))
            lines.insert(row, e.at)
            subtotal += money(ln.unit_price) * ln.qty
            discount += row["discount_amount"]
            tax += row["tax_amount"]
            items += ln.qty

        shipping = money(o.shipping_fee)
        if o.lines:
            tax += included_tax(shipping, max(rate(ln.tax_rate) for ln in o.lines))

        # Promotion details are only known if the event carries the promotion
        # (requested contract addition); the promotion id always is.
        promo: Promotion | None = getattr(e, "promotion", None)
        applied = o.promo_id is not None and (
            discount > 0 or (promo is not None and promo.free_shipping and shipping == 0)
        )
        promo_id = keys.shop_promotion_id(seed, o.promo_id) if applied else None
        promo_code = promo.code if applied and promo is not None and promo.kind == "code" else None

        self.t(ORDERS).insert(
            {
                "id": oid,
                "order_number": keys.shop_order_number(o.order_id),
                "customer_id": cid,
                "status": _check(o.status, WEB_ORDER_STATUSES, "order status"),
                "currency": o.currency,
                "country": o.country,
                "placed_at": o.placed_at,
                "items_subtotal": subtotal,
                "discount_total": discount,
                "shipping_fee": shipping,
                "tax_total": tax,
                "total_amount": subtotal - discount + shipping,
                "promo_code": promo_code,
                "promotion_id": promo_id,
                "source": e.source,
                "payment_method": o.payment_method,
                "shipping_address_id": address_id,
                "billing_address_id": address_id,
                "subscription_id": (
                    keys.shop_subscription_id(seed, o.subscription_id)
                    if o.subscription_id is not None
                    else None
                ),
                "is_repeat_customer": o.is_repeat,
                "item_count": items,
                **{col: None for col in sorted(set(ORDER_STATUS_TIMESTAMPS.values()))},
                "cancel_reason": None,
            },
            e.at,
        )

        if applied:
            self.t(PROMO_REDEMPTIONS).insert(
                {
                    "promotion_id": promo_id,
                    "order_id": oid,
                    "customer_id": cid,
                    "code": promo_code,
                    "discount_amount": discount,
                    "currency": o.currency,
                    "redeemed_at": o.placed_at,
                },
                e.at,
            )

    def _line_row(self, o: WebOrder, oid: str, ln: OrderLine, sku: Any) -> dict[str, Any]:
        unit = money(ln.unit_price)
        disc = money(ln.discount)
        total = unit * ln.qty - disc
        tr = rate(ln.tax_rate)
        return {
            "id": keys.shop_order_line_id(self.seed, o.order_id, ln.line_no),
            "order_id": oid,
            "line_no": ln.line_no,
            "sku": sku.code,
            "ean": sku.ean,
            "product_name": sku.name,
            "quantity": ln.qty,
            "unit_price": unit,
            "discount_amount": disc,
            "tax_rate": tr,
            "tax_amount": included_tax(total, tr),
            "line_total": total,
            "qty_shipped": ln.shipped_qty,
            "qty_cancelled": ln.cancelled_qty,
            "qty_returned": ln.returned_qty,
        }

    def on_order_status_changed(self, e: WebOrderStatusChanged) -> None:
        status = _check(e.status, WEB_ORDER_STATUSES, "order status")
        changes: dict[str, Any] = {"status": status}
        col = ORDER_STATUS_TIMESTAMPS.get(status)
        if col is not None:
            # A transition recognised late carries its business time; updated_at stays e.at.
            eff = getattr(e, "effective_at", None)
            changes[col] = eff if eff is not None else e.at
        if status == "cancelled":
            changes["cancel_reason"] = e.reason
        self.t(ORDERS).update(keys.shop_order_id(self.seed, e.order_id), changes, e.at)

    def on_order_milestone(self, e: WebOrderMilestoneReached) -> None:
        if e.milestone not in ("shipped", "delivered"):
            raise ValueError(f"unknown order milestone {e.milestone!r}")
        col = ORDER_STATUS_TIMESTAMPS[e.milestone]
        eff = e.effective_at if e.effective_at is not None else e.at
        self.t(ORDERS).update(keys.shop_order_id(self.seed, e.order_id), {col: eff}, e.at)

    def on_order_lines_changed(self, e: WebOrderLinesChanged) -> None:
        buf = self.t(ORDER_LINES)
        for line_no in sorted(e.lines):
            fields = e.lines[line_no]
            unknown = set(fields) - LINE_FIELDS.keys()
            if unknown:
                raise ValueError(f"unknown order line fields {sorted(unknown)}")
            changes = {LINE_FIELDS[k]: v for k, v in fields.items()}
            if changes:
                buf.update(keys.shop_order_line_id(self.seed, e.order_id, line_no), changes, e.at)

    # -- payments and refunds -------------------------------------------------
    def on_payment_recorded(self, e: PaymentRecorded) -> None:
        p = e.payment
        status = _check(p.status, PAYMENT_STATUSES, "payment status")
        row: dict[str, Any] = {
            "id": keys.shop_payment_id(self.seed, p.payment_id),
            "order_id": keys.shop_order_id(self.seed, p.order_id),
            "attempt_no": p.attempt,
            "method": p.method,
            "provider": PAYMENT_PROVIDERS.get(p.method, PAYMENT_PROVIDERS["other"]),
            "status": status,
            "amount": money(p.amount),
            "currency": p.currency,
            "provider_reference": p.provider_ref or None,
            "authorised_at": None,
            "captured_at": None,
            "captured_amount": None,
            "failed_at": None,
            "failure_reason": None,
            "voided_at": None,
            "refunded_at": None,
            "refunded_amount": ZERO,
        }
        # A payment can be recorded directly in its outcome state.
        row.update(self._payment_status_changes(status, p.amount, None, e.at))
        self.t(PAYMENTS).insert(row, e.at)

    def on_payment_status_changed(self, e: PaymentStatusChanged) -> None:
        status = _check(e.status, PAYMENT_STATUSES, "payment status")
        changes = {"status": status}
        changes.update(
            self._payment_status_changes(status, e.amount, getattr(e, "reason", None), e.at)
        )
        self.t(PAYMENTS).update(keys.shop_payment_id(self.seed, e.payment_id), changes, e.at)

    @staticmethod
    def _payment_status_changes(
        status: str, amount: float | None, reason: str | None, at: Any
    ) -> dict[str, Any]:
        """Timestamp and amount columns implied by entering ``status``.

        For ``captured`` the amount is the captured amount; for ``refunded`` /
        ``partially_refunded`` it is the cumulative refunded amount. Amount
        columns are left unchanged when the event carries no amount (``None``).
        Recorded payments pass their full amount, which is the captured amount
        for a payment recorded as captured.
        """
        ch: dict[str, Any] = {}
        col = PAYMENT_STATUS_TIMESTAMPS.get(status)
        if col is not None:
            ch[col] = at
        if status == "failed":
            ch["failure_reason"] = reason or DEFAULT_FAILURE_REASON
        if amount is not None:
            if status == "captured":
                ch["captured_amount"] = money(amount)
            elif status in ("refunded", "partially_refunded"):
                ch["refunded_amount"] = money(amount)
        return ch

    def on_refund_issued(self, e: RefundIssued) -> None:
        r = e.refund
        self.t(REFUNDS).insert(
            {
                "id": keys.shop_refund_id(self.seed, r.refund_id),
                "order_id": keys.shop_order_id(self.seed, r.order_id),
                "payment_id": (
                    keys.shop_payment_id(self.seed, r.payment_id) if r.payment_id is not None else None
                ),
                "amount": money(r.amount),
                "currency": r.currency,
                "reason": r.reason,
                "status": _check(r.status, REFUND_STATUSES, "refund status"),
                "issued_at": r.created_at,
                "completed_at": r.created_at if r.status == "completed" else None,
            },
            e.at,
        )

    def on_refund_changed(self, e: RefundChanged) -> None:
        status = _check(e.status, REFUND_STATUSES, "refund status")
        changes: dict[str, Any] = {"status": status}
        if status == "completed":
            changes["completed_at"] = e.at
        self.t(REFUNDS).update(keys.shop_refund_id(self.seed, e.refund_id), changes, e.at)

    # -- subscriptions -------------------------------------------------------
    def on_subscription_created(self, e: SubscriptionCreated) -> None:
        s = e.subscription
        status = _check(s.status, SUBSCRIPTION_STATUSES, "subscription status")
        self.t(SUBSCRIPTIONS).insert(
            {
                "id": keys.shop_subscription_id(self.seed, s.subscription_id),
                "customer_id": keys.shop_customer_id(self.seed, s.customer_id),
                "status": status,
                "interval_weeks": s.interval_weeks,
                "skus": self._sku_codes(s.sku_ids),
                "quantities": list(s.qtys),
                "started_at": s.created_at,
                "next_order_at": s.next_order_at,
                "paused_at": e.at if status == "paused" else None,
                "cancelled_at": e.at if status == "cancelled" else None,
            },
            e.at,
        )

    def on_subscription_changed(self, e: SubscriptionChanged) -> None:
        status = _check(e.status, SUBSCRIPTION_STATUSES, "subscription status")
        changes: dict[str, Any] = {"status": status, "next_order_at": e.next_order_at}
        if status == "paused":
            changes["paused_at"] = e.at
        elif status == "cancelled":
            changes["cancelled_at"] = e.at
            changes["next_order_at"] = None
        self.t(SUBSCRIPTIONS).update(
            keys.shop_subscription_id(self.seed, e.subscription_id), changes, e.at
        )
