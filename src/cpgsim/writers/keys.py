"""Key formatting shared by writers (and truth), one function per system key.

Neutral ids from the domain are formatted here into each system's key style.
All functions are pure, so any writer can reference another system's key
(e.g. the ERP writer puts the shop order UUID into ``vbak.bstnk``, the WMS
writer stores the ERP delivery number) without shared state.
"""

from __future__ import annotations

from ..engine.rng import stable_uuid

# -- shop: UUID keys -----------------------------------------------------------


def shop_customer_id(seed: int, customer_id: int) -> str:
    return stable_uuid(seed, "shop.customer", customer_id)


def shop_address_id(seed: int, customer_id: int, n: int = 1) -> str:
    return stable_uuid(seed, "shop.address", customer_id, n)


def shop_order_id(seed: int, order_id: int) -> str:
    return stable_uuid(seed, "shop.order", order_id)


def shop_order_number(order_id: int) -> str:
    """Human-facing order number shown to customers, e.g. ``NV-10004711``."""
    return f"NV-{10_000_000 + order_id}"


def shop_order_line_id(seed: int, order_id: int, line_no: int) -> str:
    return stable_uuid(seed, "shop.order_line", order_id, line_no)


def shop_payment_id(seed: int, payment_id: int) -> str:
    return stable_uuid(seed, "shop.payment", payment_id)


def shop_refund_id(seed: int, refund_id: int) -> str:
    return stable_uuid(seed, "shop.refund", refund_id)


def shop_promotion_id(seed: int, promo_id: int) -> str:
    return stable_uuid(seed, "shop.promotion", promo_id)


def shop_subscription_id(seed: int, subscription_id: int) -> str:
    return stable_uuid(seed, "shop.subscription", subscription_id)


# -- erp: SAP-style ten-digit document numbers, 18-char material numbers -------

SALES_DOC_BASE = 10_000_000  # 0010000001 ...
DELIVERY_BASE = 80_000_000  # 0080000001 ...
BILLING_BASE = 90_000_000  # 0090000001 ... (invoices and credit memos share the range)


def vbeln_sales(sales_doc_id: int) -> str:
    return f"{SALES_DOC_BASE + sales_doc_id:010d}"


def vbeln_delivery(delivery_id: int) -> str:
    return f"{DELIVERY_BASE + delivery_id:010d}"


def vbeln_billing(billing_id: int) -> str:
    return f"{BILLING_BASE + billing_id:010d}"


def knumv_sales(sales_doc_id: int) -> str:
    """Condition record number of a sales document (KONV key)."""
    return f"{1_000_000_000 + sales_doc_id:010d}"


def matnr(sku_id: int) -> str:
    """18-character, zero-padded numeric material number (classic SAP)."""
    return f"{100_000 + sku_id:018d}"


def item_posnr(item_no: int) -> str:
    """Six-digit item number, e.g. ``000010``."""
    return f"{item_no:06d}"


def kunnr(account_id: str, account_numbers: dict[str, int]) -> str:
    """Ten-digit customer number for a B2B account (numbers assigned by catalogue order)."""
    return f"{account_numbers[account_id]:010d}"


# -- wms: integer keys -----------------------------------------------------------


def wms_shipment_id(delivery_id: int) -> int:
    return 500_000 + delivery_id


def wms_return_id(return_id: int) -> int:
    return 700_000 + return_id


def tracking_number(carrier: str, delivery_id: int) -> str:
    prefix = {"Paketblitz": "PB", "Nordpost": "NP", "Swiftbox": "SX"}.get(carrier, "FR")
    return f"{prefix}{3_000_000_000 + delivery_id * 7 + 3}"
