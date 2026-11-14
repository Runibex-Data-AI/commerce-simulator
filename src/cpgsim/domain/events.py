"""Domain events: the only interface between business logic and writers.

Business logic calls ``world.emit(SomeEvent(...))``. Dispatch is synchronous:
every writer that handles the event type turns it into rows *immediately*, so
entity objects referenced by an event may be mutated afterwards without
affecting what was written.

Every event carries ``at``: the simulation time at which the business fact
happened. Writers use it for ``created_at``/``updated_at`` and for any
business timestamp columns (converted to the system's own time convention).

Which writer consumes what (phase 1):

=============================  ===============================================
Event                          Rows
=============================  ===============================================
SkuListed                      erp.mara, erp.makt, erp.mbew
ServiceMaterialListed          erp.mara, erp.makt (non-stock service material)
StandardCostRevised            erp.mbew (update)
B2BAccountCreated              erp.kna1
CustomerMaterialListed         erp.knmt
FxRatePublished                erp.tcurr
CustomerRegistered             shop.customers, shop.addresses
CustomerAnonymised             shop.customers, shop.addresses (phase 2 chaos)
PromotionDefined               shop.promotions (web promos only)
PromotionChanged               shop.promotions (update of ends_at; soft delete if withdrawn)
WebOrderPlaced                 shop.orders, shop.order_lines, shop.promo_redemptions
WebOrderStatusChanged          shop.orders (update)
WebOrderLinesChanged           shop.order_lines (update)
PaymentRecorded                shop.payments
PaymentStatusChanged           shop.payments (update)
RefundIssued                   shop.refunds
RefundChanged                  shop.refunds (update)
SubscriptionCreated            shop.subscriptions
SubscriptionChanged            shop.subscriptions (update)
SalesDocCreated                erp.vbak, erp.vbap, erp.konv
SalesDocChanged                erp.vbak / erp.vbap (update)
DeliveryCreated                erp.likp, erp.lips, wms.shipments, wms.shipment_lines
DeliveryChanged                erp.likp / erp.lips, wms.shipments / wms.shipment_lines (update)
CarrierScan                    wms.carrier_events
BillingDocCreated              erp.vbrk, erp.vbrp
BillingDocChanged              erp.vbrk (update; payment clearing)
StockMoved                     wms.stock_movements
StockSnapshotTaken             wms.stock_levels_daily
ReturnCreated                  wms.returns, wms.return_lines
ReturnChanged                  wms.returns / wms.return_lines (update)
ScenarioWindowRecorded         truth.scenario_windows
ScenarioWindowClosed           truth.scenario_windows (update end_at)
PlantedEffect                  truth.planted_effects
=============================  ===============================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .model import (
    Address,
    B2BAccount,
    BillingDoc,
    Customer,
    Delivery,
    Payment,
    Promotion,
    Refund,
    SalesDoc,
    Sku,
    Subscription,
    WebOrder,
    WebReturn,
)


@dataclass(slots=True)
class DomainEvent:
    at: datetime


# -- master data -------------------------------------------------------------


@dataclass(slots=True)
class SkuListed(DomainEvent):
    sku: Sku
    # business creation date of the material record (listing date); can lie
    # before ``at`` for materials seeded at history start
    listed_on: date | None = None


@dataclass(slots=True)
class ServiceMaterialListed(DomainEvent):
    """A non-stock service material (never reserved, picked or delivered)."""

    service: str  # neutral service key: shipping
    listed_on: date | None = None  # business creation date (before ``at`` when seeded)


@dataclass(slots=True)
class StandardCostRevised(DomainEvent):
    sku_id: int
    std_cost_eur: float
    valid_from: date


@dataclass(slots=True)
class B2BAccountCreated(DomainEvent):
    account: B2BAccount
    # business account-opening date; before ``at`` for accounts seeded at history start
    opened_on: date | None = None


@dataclass(slots=True)
class CustomerMaterialListed(DomainEvent):
    account_id: str  # retailer sold-to account
    sku_id: int
    customer_article: str  # retailer's article number


@dataclass(slots=True)
class FxRatePublished(DomainEvent):
    from_currency: str  # e.g. CHF
    to_currency: str  # EUR
    valid_from: date  # first day of month
    rate: float  # units of to_currency per 1 from_currency


# -- web shop ----------------------------------------------------------------


@dataclass(slots=True)
class CustomerRegistered(DomainEvent):
    customer: Customer
    first_name: str
    last_name: str
    email: str
    phone: str | None
    birth_year: int | None
    marketing_opt_in: bool
    locale: str  # de_DE | de_AT | de_CH
    address: Address  # also the default shipping address


@dataclass(slots=True)
class CustomerAnonymised(DomainEvent):
    customer_id: int


@dataclass(slots=True)
class PromotionDefined(DomainEvent):
    promotion: Promotion


@dataclass(slots=True)
class PromotionChanged(DomainEvent):
    """An announced promotion changed (phase 1: a live scenario promotion ended early)."""

    promotion: Promotion  # with its new end
    withdrawn: bool = False  # ended at the instant it started: never ran


@dataclass(slots=True)
class WebOrderPlaced(DomainEvent):
    order: WebOrder
    customer: Customer
    source: str  # acquisition / traffic source of this order (utm-like word)
    promotion: Promotion | None = None  # the promotion actually applied (discount or free shipping)


@dataclass(slots=True)
class WebOrderStatusChanged(DomainEvent):
    order_id: int
    status: str  # WEB_ORDER_STATUSES
    reason: str | None = None
    # business time of the transition when it is recognised later than it
    # happened (e.g. 'delivered' at the carrier's scan, recognised when the
    # backordered remainder is cancelled). Writers use it for the status
    # timestamp column; ``at`` stays the change time (updated_at).
    effective_at: datetime | None = None


@dataclass(slots=True)
class WebOrderLinesChanged(DomainEvent):
    order_id: int
    # line_no -> changed neutral fields (shipped_qty, cancelled_qty, returned_qty)
    lines: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class PaymentRecorded(DomainEvent):
    payment: Payment


@dataclass(slots=True)
class PaymentStatusChanged(DomainEvent):
    payment_id: int
    status: str  # PAYMENT_STATUSES
    # captured: cumulative captured amount; refunded / partially_refunded: CUMULATIVE refunded total
    amount: float | None = None
    reason: str | None = None  # failure reason for failed (declined | insufficient_funds | authentication_failed)


@dataclass(slots=True)
class RefundIssued(DomainEvent):
    refund: Refund


@dataclass(slots=True)
class RefundChanged(DomainEvent):
    refund_id: int
    status: str  # REFUND_STATUSES (completed: paid out by the payment provider)


@dataclass(slots=True)
class SubscriptionCreated(DomainEvent):
    subscription: Subscription


@dataclass(slots=True)
class SubscriptionChanged(DomainEvent):
    subscription_id: int
    status: str
    next_order_at: datetime | None = None


# -- ERP sales, deliveries, billing ------------------------------------------


@dataclass(slots=True)
class SalesDocCreated(DomainEvent):
    doc: SalesDoc


@dataclass(slots=True)
class SalesDocChanged(DomainEvent):
    sales_doc_id: int
    # neutral header fields that changed: status, delivery_status, billing_status
    header: dict[str, Any] = field(default_factory=dict)
    # item_no -> changed neutral item fields: confirmed_qty, rejection_reason,
    # delivered_qty, billed_qty. Whenever delivered_qty or billed_qty is present, the
    # item's current confirmed_qty must be included too (writers derive SAP item status).
    items: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class DeliveryCreated(DomainEvent):
    delivery: Delivery
    sales_doc: SalesDoc  # for references (customer, items)


@dataclass(slots=True)
class DeliveryChanged(DomainEvent):
    delivery_id: int
    status: str  # DELIVERY_STATUSES
    # optional neutral fields: tracking_no, freight_cost_eur, shipped_at,
    # delivered_at, picked (dict line_no -> picked_qty), pod_signed_by,
    # kind (web | retail). For status "cancelled", fields["line_nos"] lists the
    # delivery's line numbers (writers soft-delete the item rows).
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CarrierScan(DomainEvent):
    delivery_id: int
    scan_seq: int  # 1-based per delivery (unique together with delivery_id)
    code: str  # CARRIER_SCAN_CODES
    carrier: str
    location: str  # hub or city name
    detail: str | None = None
    tracking_no: str | None = None


@dataclass(slots=True)
class BillingDocCreated(DomainEvent):
    doc: BillingDoc


@dataclass(slots=True)
class BillingDocChanged(DomainEvent):
    billing_id: int
    # neutral fields: paid_at, paid_amount, status (open | cleared)
    fields: dict[str, Any] = field(default_factory=dict)


# -- warehouse ---------------------------------------------------------------


@dataclass(slots=True)
class StockMoved(DomainEvent):
    movement_id: int
    sku_id: int
    movement_type: str  # STOCK_MOVEMENT_TYPES
    qty: int  # positive into stock, negative out of stock
    delivery_id: int | None = None
    return_id: int | None = None
    reference: str | None = None


@dataclass(slots=True)
class StockSnapshotRow:
    sku_id: int
    on_hand: int
    reserved: int
    in_production: int


@dataclass(slots=True)
class StockSnapshotTaken(DomainEvent):
    snapshot_date: date  # local business date of the snapshot
    rows: list[StockSnapshotRow]


@dataclass(slots=True)
class ReturnCreated(DomainEvent):
    ret: WebReturn


@dataclass(slots=True)
class ReturnChanged(DomainEvent):
    return_id: int
    status: str  # RETURN_STATUSES
    # optional: line conditions after inspection (line_no -> resaleable|damaged)
    line_conditions: dict[int, str] = field(default_factory=dict)


# -- ground truth ------------------------------------------------------------


@dataclass(slots=True)
class ScenarioWindowRecorded(DomainEvent):
    scenario_id: str
    window_id: str  # unique, e.g. "carrier_decay:config:0" or "carrier_decay:cli:3"
    params: dict[str, Any]
    start: datetime
    end: datetime | None
    source: str  # config | cli


@dataclass(slots=True)
class ScenarioWindowClosed(DomainEvent):
    """A window recorded earlier (possibly in an earlier commit) ends at ``end``."""

    window_id: str
    end: datetime


@dataclass(slots=True)
class PlantedEffect(DomainEvent):
    effect_id: str  # deterministic unique key
    scenario_id: str  # scenario id, or chaos defect id in phase 2
    window_id: str | None
    entity_type: str  # web_order | delivery | customer | sku | week ...
    entity_id: str  # neutral id as text (writers do not reformat truth ids)
    effect: str  # e.g. "delay_multiplier", "promo_discount"
    magnitude: float | None
    detail: dict[str, Any] = field(default_factory=dict)
