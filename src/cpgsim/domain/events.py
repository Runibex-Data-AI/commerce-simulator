"""Domain events: the only interface between business logic and writers.

Business logic calls ``world.emit(SomeEvent(...))``. Dispatch is synchronous:
every writer that handles the event type turns it into rows *immediately*, so
entity objects referenced by an event may be mutated afterwards without
affecting what was written.

Every event carries ``at``: the simulation time at which the business fact
happened. Writers use it for ``created_at``/``updated_at`` and for any
business timestamp columns (converted to the system's own time convention).

Which writer consumes what:

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
WebOrderMilestoneReached       shop.orders (update of shipped_at / delivered_at only)
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
FeedDelivered                  feeds.feed_loads + feeds.retailer_sellout | feeds.marketplace_sales_daily
                               / feeds.marketplace_inventory_daily
TradeAgreementDefined          erp.kona, erp.zkona_item
DeductionClaimed               erp.zdeduct, erp.zdeduct_item
DeductionChanged               erp.zdeduct (update)
DeductionCleared               erp.zdeduct_clr + erp.zdeduct (update of zztaken / zzrepaid)
InfluencerCodeRegistered       mkt.influencer_codes
CampaignDefined                mkt.campaigns
CampaignStatusChanged          mkt.campaigns (update)
CampaignSpendReported          mkt.campaign_spend_daily
SessionsReported               mkt.sessions_daily
OrdersAttributed               mkt.attributions
StatusErrorSet                 shop.orders | wms.shipments (update: status set in error)
StatusErrorCorrected           shop.orders | wms.shipments (update: status corrected)
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
    Campaign,
    Customer,
    Deduction,
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
class WebOrderMilestoneReached(DomainEvent):
    """A fulfilment milestone ('shipped' / 'delivered') reached without a status change.

    Used when the order already carries a customer-return status (a return of
    an earlier parcel) by the time its remaining goods ship or are delivered:
    the status stays, only the milestone's timestamp column is set.
    """

    order_id: int
    milestone: str  # 'shipped' | 'delivered'
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


# -- feeds (phase 2) ---------------------------------------------------------


@dataclass(slots=True)
class SelloutRow:
    dc_account: str
    sales_channel: str  # store | online
    sku_id: int
    units: int
    value_net_eur: float
    value_gross_eur: float
    stock_units: int | None = None  # store rows: region DC on hand at period end


@dataclass(slots=True)
class MarketplaceSalesRow:
    sku_id: int
    units: int
    revenue_gross_eur: float
    glance_views: int


@dataclass(slots=True)
class MarketplaceInventoryRow:
    sku_id: int
    sellable_units: int
    inbound_units: int


@dataclass(slots=True)
class FeedDelivered(DomainEvent):
    """One delivered feed file (feeds.feed_loads + its child rows)."""

    channel: str
    feed_type: str  # sellout_weekly | sellout_monthly | mp_sales_daily | mp_inventory_daily
    period_start: date
    period_end: date
    run: int  # 1 original, 2.. restatement
    restates_run: int | None = None
    sellout: list[SelloutRow] = field(default_factory=list)
    mp_sales: list[MarketplaceSalesRow] = field(default_factory=list)
    mp_inventory: list[MarketplaceInventoryRow] = field(default_factory=list)


# -- deductions (phase 2) ----------------------------------------------------


@dataclass(slots=True)
class DeductionClaimed(DomainEvent):  # erp.zdeduct insert (+ zdeduct_item)
    deduction: Deduction


@dataclass(slots=True)
class DeductionChanged(DomainEvent):  # erp.zdeduct update
    deduction_id: int
    status: str | None = None  # DEDUCTION_STATUSES; None = only fields
    # optional neutral fields: matched_on, disputed_on, dispute_reason, resolved_on, accepted_amount, closed_on
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DeductionCleared(DomainEvent):  # erp.zdeduct_clr insert + zdeduct zztaken/zzrepaid update
    deduction_id: int
    seq: int  # 1-based clearing sequence of the deduction (buzei)
    kind: str  # taken | repaid
    billing_id: int | None  # invoice short-paid (taken); None for repaid
    amount: float  # EUR, positive
    on: date  # local clearing date
    taken_total: float  # after this item
    repaid_total: float


@dataclass(slots=True)
class TradeAgreementItem:
    sku_id: int
    customer_article: str
    scan_rate_net: float  # EUR per unit sold


@dataclass(slots=True)
class TradeAgreementDefined(DomainEvent):  # erp.kona + erp.zkona_item
    promo_id: int
    channel: str
    account_id: str  # retailer sold-to
    text: str  # the retailer's wording
    start: date
    end: date  # inclusive
    discount_pct: float
    off_invoice_share: float  # retail_funding_share
    scanback_share: float
    advertising_fee_eur: float | None
    items: list[TradeAgreementItem] = field(default_factory=list)


# -- marketing (phase 2) -----------------------------------------------------


@dataclass(slots=True)
class InfluencerCodeRegistered(DomainEvent):  # mkt.influencer_codes
    code: str
    handle: str
    audience_platform: str
    commission_pct: float
    fixed_fee_eur: float
    valid_from: date


@dataclass(slots=True)
class CampaignDefined(DomainEvent):  # mkt.campaigns insert
    campaign: Campaign
    status: str  # planned | active


@dataclass(slots=True)
class CampaignStatusChanged(DomainEvent):  # mkt.campaigns update
    campaign_id: str
    status: str
    end_date: date | None = None  # set on 'ended' (differs from the defined end after an early CLI disable)
    daily_budget_eur: float | None = None  # set when a scenario changes the budget (influencer_cohort_churn)


@dataclass(slots=True)
class CampaignSpendRow:
    campaign_id: str
    spend_date: date
    impressions: int
    clicks: int
    spend_eur: float
    platform_conversions: int


@dataclass(slots=True)
class CampaignSpendReported(DomainEvent):  # mkt.campaign_spend_daily
    rows: list[CampaignSpendRow]


@dataclass(slots=True)
class SessionsRow:
    channel: str
    country: str
    device: str
    sessions: int
    users: int
    new_users: int
    orders: int
    revenue_eur: float


@dataclass(slots=True)
class SessionsReported(DomainEvent):  # mkt.sessions_daily
    session_date: date
    rows: list[SessionsRow]


@dataclass(slots=True)
class AttributionRow:
    order_id: int
    customer_id: int
    attribution_date: date
    channel: str
    campaign_id: str | None
    influencer_code: str | None
    is_new_customer: bool
    revenue_eur: float


@dataclass(slots=True)
class OrdersAttributed(DomainEvent):  # mkt.attributions
    rows: list[AttributionRow]


# -- chaos (phase 2) ---------------------------------------------------------


@dataclass(slots=True)
class StatusErrorSet(DomainEvent):  # shop.orders.status | wms.shipments.status_code (update)
    system: str  # shop | wms
    entity_id: int  # web order id (shop) | delivery id (wms)
    wrong_status: str  # neutral: WEB_ORDER_STATUSES | DELIVERY_STATUSES ('cancelled' in both systems)
    error_at: datetime  # written to shop.orders.cancelled_at | wms.shipments.cancelled_local (kept after correction)


@dataclass(slots=True)
class StatusErrorCorrected(DomainEvent):
    system: str
    entity_id: int
    status: str  # neutral current status
