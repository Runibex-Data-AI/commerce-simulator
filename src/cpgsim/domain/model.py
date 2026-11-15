"""Neutral domain entities.

These are the business objects the simulation reasons about. They carry
*neutral* identifiers (plain integers from per-kind counters) and *neutral*
vocabularies (status words below). Writers translate them into each source
system's conventions (UUIDs, ten-digit SAP numbers, two-letter WMS codes...).

Mutable entities live in the :class:`~cpgsim.engine.entities.EntityStore`
under the kind given in ``ENTITY_KINDS``. Reference data (catalogue,
geography, B2B accounts) is rebuilt deterministically from seed + config at
start-up and is not persisted.

Money: floats rounded to 2 decimals at the point of creation. Gross means
incl. VAT. Web amounts are in the order currency (EUR, or CHF for Swiss
orders); trade amounts are in EUR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Status vocabularies (neutral). Writers map these to system codes.
# ---------------------------------------------------------------------------

WEB_ORDER_STATUSES = (
    "created",  # placed, payment pending
    "payment_failed",  # final payment failure (after optional retry)
    "authorised",  # payment authorised
    "released",  # released to warehouse / ERP sales order exists
    "cancelled",  # cancelled by customer or for payment failure / stock
    "partially_shipped",
    "shipped",
    "delivered",
    "delivery_exception",
    "returned_to_sender",
    "lost",
    "return_requested",
    "returned",
    "return_rejected",  # returned goods failed inspection: no refund, goods go back to the customer
    "partially_refunded",
    "refunded",
)

PAYMENT_STATUSES = (
    "pending", "authorised", "failed", "captured", "voided", "refunded", "partially_refunded",
)
PAYMENT_METHODS = ("paypal", "invoice_bnpl", "card", "direct_debit", "other")

DELIVERY_STATUSES = (
    "open",  # created, waiting for pick
    "picked",
    "shipped",  # goods issue posted, handed to carrier
    "in_transit",
    "out_for_delivery",
    "delivered",  # proof of delivery for retail
    "exception",  # failed attempt, will be redelivered or returned
    "returned_to_sender",
    "lost",
    "cancelled",
)

CARRIER_SCAN_CODES = (
    "label_created", "picked_up", "hub_scan", "out_for_delivery", "delivered",
    "delivery_failed", "redelivery_scheduled", "returned_to_sender", "pod_signed",
)

RETURN_STATUSES = ("requested", "label_issued", "received", "inspected", "refunded", "rejected")
REFUND_STATUSES = ("issued", "completed")  # completed: the provider has paid the money out
RETURN_REASONS = ("damaged_in_transit", "wrong_item", "did_not_like", "allergic_reaction")

RETAIL_PO_STATUSES = (
    "received", "confirmed", "delivery_planned", "shipped", "delivered", "invoiced", "paid",
    "cancelled",
)

SUBSCRIPTION_STATUSES = ("active", "paused", "cancelled")

STOCK_MOVEMENT_TYPES = (
    "goods_receipt",  # production receipt into central warehouse
    "goods_issue",  # outbound delivery shipped
    "customer_return",  # returned goods put back to stock
    "scrap",  # returned goods not resaleable
    "adjustment",  # inventory count difference
)

# Trade deductions (phase 2). Writers map kinds to SAP claim types (ZP01 ...)
# and statuses to one-letter codes (O, M, D, A, R).
DEDUCTION_STATUSES = ("claimed", "matched", "disputed", "accepted", "reversed")
DEDUCTION_KINDS = ("promotion", "advertising", "coop", "clearance", "shortage", "service_penalty", "unexplained")


# ---------------------------------------------------------------------------
# Reference data (rebuilt from seed + config, not persisted)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Sku:
    sku_id: int  # 1..N, stable for a seed
    code: str  # web shop SKU code, e.g. "VEL-BOD-HYB-400"
    ean: str  # 13 digits, valid check digit, GS1 restricted-circulation prefix (fictional)
    brand: str  # Velmora | Cutara | Sorell
    category: str  # see catalogue.CATEGORIES
    line: str  # product line, e.g. "Hydra Balance"
    name: str  # "Velmora Hydra Balance Body Lotion 400 ml"
    pack_size: float
    pack_unit: str  # ml | g | pcs
    usage_days: float  # mean days one unit lasts a consumer
    rrp_eur: float  # recommended consumer price incl. 19% VAT
    rrp_chf: float  # Swiss consumer price incl. 8.1% VAT
    list_price_eur: float  # trade list price per unit, net
    std_cost_eur: float  # standard cost per unit at history start
    weight_g: int
    units_per_case: int
    launch_date: date
    delist_date: date | None
    hero: bool
    popularity: float  # relative unit weight before seasonality and promotions
    season_profile: str  # key understood by models.seasonality


@dataclass(slots=True)
class Address:
    country: str  # DE | AT | CH
    region: str  # ISO 3166-2, e.g. DE-BY, AT-9, CH-ZH
    postcode: str
    city: str
    street: str = ""
    house_number: str = ""


@dataclass(slots=True)
class B2BAccount:
    """A trade customer account (sold-to or ship-to) as the ERP knows it."""

    account_id: str  # neutral, e.g. "KRANZ", "KRANZ-DC1", "WEB-DE"
    kind: str  # retailer | retailer_dc | web_collective
    name: str
    retailer: str | None  # channel key from config, e.g. "drogerie_kranz"
    address: Address
    currency: str = "EUR"
    payment_terms_days: int = 30


# ---------------------------------------------------------------------------
# Mutable entities (EntityStore kinds in ENTITY_KINDS)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Customer:
    customer_id: int
    country: str
    address: Address
    created_at: datetime
    acquisition: str  # organic_search | paid_search | social | influencer | marketplace_spillover | direct | deal_site
    influencer_code: str | None = None
    orders: int = 0
    missed_intents: int = 0
    intent_seq: int = 0  # increments per scheduled repurchase intent (event key part)
    last_order_at: datetime | None = None
    last_skus: list[int] = field(default_factory=list)
    subscription_id: int | None = None
    lapsed: bool = False
    scenario_tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)  # module scratch space, JSON values only


@dataclass(slots=True)
class OrderLine:
    line_no: int  # 1-based
    sku_id: int
    qty: int
    unit_price: float  # gross list price per unit, order currency
    discount: float  # total gross discount on the line, order currency
    tax_rate: float  # 0.19 / 0.20 / 0.081
    shipped_qty: int = 0
    cancelled_qty: int = 0
    returned_qty: int = 0
    discount_pct: float = 0.0  # nominal promotion rate behind ``discount`` (0.25 = 25 % off), 0 if none

    @property
    def gross(self) -> float:
        return round(self.unit_price * self.qty - self.discount, 2)


@dataclass(slots=True)
class WebOrder:
    order_id: int
    customer_id: int
    country: str
    currency: str  # EUR | CHF
    placed_at: datetime
    ship_to: Address
    lines: list[OrderLine]
    shipping_fee: float  # gross, order currency (0 above free-shipping threshold)
    payment_method: str
    status: str = "created"
    promo_id: int | None = None
    subscription_id: int | None = None
    is_repeat: bool = False
    payment_attempts: int = 0
    payment_ids: list[int] = field(default_factory=list)
    sales_doc_id: int | None = None
    delivery_ids: list[int] = field(default_factory=list)
    billing_ids: list[int] = field(default_factory=list)
    return_ids: list[int] = field(default_factory=list)
    refunded_total: float = 0.0
    captured_total: float = 0.0
    backorder: dict[int, int] = field(default_factory=dict)  # line_no -> qty still waiting for stock
    released_at: datetime | None = None
    delivered_at: datetime | None = None
    scenario_tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)  # module scratch space, JSON values only

    @property
    def goods_gross(self) -> float:
        return round(sum(l.gross for l in self.lines), 2)

    @property
    def total(self) -> float:
        return round(self.goods_gross + self.shipping_fee, 2)


@dataclass(slots=True)
class Payment:
    payment_id: int
    order_id: int
    attempt: int
    method: str
    amount: float
    currency: str
    status: str
    created_at: datetime
    provider_ref: str = ""


@dataclass(slots=True)
class Condition:
    """One pricing condition on a sales document item (feeds erp.konv)."""

    ctype: str  # list_price | customer_discount | promo_discount | influencer_discount | shipping | tax
    rate: float  # percentage as fraction for discounts/tax, unit price for list_price
    amount: float  # signed value for the item in document currency (discounts negative)


#: Item number of the freight (shipping charge) item on web sales documents and
#: their invoices / credit memos.
FREIGHT_ITEM_NO = 900


def goods_item_no(n: int) -> int:
    """Item number of the ``n``-th (1-based) goods item: 10, 20, ... 890, then 910, 920, ...

    900 is reserved for the freight item, so a document with 90 or more goods
    lines (retailer orders and invoices) never collides with it.
    """
    no = 10 * n
    return no + 10 if no >= FREIGHT_ITEM_NO else no


@dataclass(slots=True)
class SalesItem:
    item_no: int  # 10, 20, 30 ... (SAP style numbering is neutral enough); freight: FREIGHT_ITEM_NO
    sku_id: int
    qty: int  # ordered quantity
    confirmed_qty: int  # after stock check / cuts
    net_value: float  # document currency, after discounts, excl. tax
    tax: float
    cost: float  # standard cost * qty, EUR
    conditions: list[Condition] = field(default_factory=list)
    customer_article: str | None = None  # retailer's own article number (EDI)
    rejection_reason: str | None = None  # None | customer_cancelled | out_of_stock | payment_failed
    delivered_qty: int = 0
    billed_qty: int = 0
    # goods | freight. A freight item (sku_id 0, qty 1) carries the web shipping
    # charge: it is never reserved, picked or delivered, only billed (once, on
    # the first invoice of the order).
    item_type: str = "goods"

    @property
    def is_freight(self) -> bool:
        return self.item_type == "freight"


@dataclass(slots=True)
class SalesDoc:
    """An ERP sales document (web order mirror or retailer purchase order)."""

    sales_doc_id: int
    doc_type: str  # web | retail
    sold_to: str  # B2BAccount.account_id
    ship_to: str  # B2BAccount.account_id
    currency: str
    fx_rate_eur: float  # EUR per 1 unit of doc currency at the month's rate
    created_at: datetime
    requested_date: date
    external_ref: str | None  # web: shop order id (neutral int as str); retail: retailer PO number
    items: list[SalesItem]
    channel: str  # web | drogerie_kranz | mylo | ...
    order_id: int | None = None  # web order id or retail po id
    status: str = "open"  # open | in_process | completed | rejected
    delivery_status: str = "none"  # none | partial | full
    billing_status: str = "none"  # none | partial | full
    shipping_net: float = 0.0  # web: net shipping charge (doc currency) = the freight item's net_value
    shipping_tax: float = 0.0
    meta: dict = field(default_factory=dict)  # module scratch space, JSON values only

    @property
    def net_value(self) -> float:
        return round(sum(i.net_value for i in self.items), 2)


@dataclass(slots=True)
class DeliveryLine:
    line_no: int  # 1-based within the delivery
    sku_id: int
    qty: int
    sales_item_no: int  # reference to SalesItem.item_no
    picked_qty: int = 0


@dataclass(slots=True)
class Delivery:
    """One outbound delivery from the central warehouse (ERP delivery + WMS shipment)."""

    delivery_id: int
    kind: str  # web | retail
    sales_doc_id: int
    ship_to_account: str  # B2BAccount.account_id (web: collective account)
    ship_to: Address
    carrier: str  # Paketblitz | Nordpost | Swiftbox | pallet forwarder name
    lines: list[DeliveryLine]
    created_at: datetime
    planned_ship_date: date
    status: str = "open"
    order_id: int | None = None  # web order id or retail po id
    seq_in_order: int = 1  # 1 for first shipment, 2 for split remainder
    tracking_no: str | None = None
    freight_cost_eur: float = 0.0
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    scan_seq: int = 0
    attempts: int = 0
    outcome: str | None = None  # delivered | returned_to_sender | lost (final)
    billing_id: int | None = None  # invoice raised at shipment (web) or after POD (retail)
    scenario_tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)  # module scratch space, JSON values only


@dataclass(slots=True)
class BillingItem:
    item_no: int  # SAP-style 10, 20, 30 ... (shipping charge item: 900)
    sku_id: int  # 0 for the shipping charge item
    qty: int
    net_value: float  # doc currency; negative never, credit memos are positive with doc_kind
    tax: float
    cost: float  # EUR
    sales_item_no: int
    delivery_id: int | None = None
    delivery_line_no: int | None = None  # invoices: the delivery line billed
    reference_item_no: int | None = None  # credit memos: the original invoice item_no


@dataclass(slots=True)
class BillingDoc:
    billing_id: int
    doc_kind: str  # invoice | credit_memo
    sales_doc_id: int
    payer: str  # B2BAccount.account_id
    currency: str
    fx_rate_eur: float
    billing_date: date
    created_at: datetime
    due_date: date
    items: list[BillingItem]
    reference_billing_id: int | None = None  # credit memo -> original invoice
    reason: str | None = None  # credit memo reason
    paid_at: datetime | None = None
    paid_amount: float | None = None

    @property
    def net_value(self) -> float:
        return round(sum(i.net_value for i in self.items), 2)

    @property
    def tax(self) -> float:
        return round(sum(i.tax for i in self.items), 2)


@dataclass(slots=True)
class ReturnLine:
    line_no: int
    order_line_no: int
    sku_id: int
    qty: int
    condition: str | None = None  # resaleable | damaged (after inspection)


@dataclass(slots=True)
class WebReturn:
    return_id: int
    order_id: int
    delivery_id: int
    customer_id: int
    reason: str
    lines: list[ReturnLine]
    requested_at: datetime
    status: str = "requested"
    free_text_reason: str | None = None  # phase 2 chaos
    refund_id: int | None = None


@dataclass(slots=True)
class Refund:
    refund_id: int
    order_id: int
    payment_id: int | None
    amount: float  # gross, order currency
    currency: str
    reason: str  # return | lost | returned_to_sender | cancelled_after_capture
    created_at: datetime
    status: str = "issued"  # issued | completed
    return_id: int | None = None


@dataclass(slots=True)
class Subscription:
    subscription_id: int
    customer_id: int
    sku_ids: list[int]
    qtys: list[int]
    interval_weeks: int  # 4 | 8 | 12
    status: str
    created_at: datetime
    next_order_at: datetime | None = None
    orders_placed: int = 0
    paused_until: datetime | None = None


@dataclass(slots=True)
class RetailPO:
    po_id: int
    retailer: str  # channel key, e.g. "drogerie_kranz"
    dc_account: str  # B2BAccount.account_id of the DC
    po_number: str  # retailer's own PO number (formatted by domain, retailer's format)
    created_at: datetime
    requested_delivery: date
    lines: list[RetailPOLine]
    status: str = "received"
    sales_doc_id: int | None = None
    delivery_id: int | None = None
    billing_id: int | None = None
    promo_ids: list[int] = field(default_factory=list)
    meta: dict = field(default_factory=dict)  # module scratch space, JSON values only
    invoice_gross: float | None = None  # set at invoice creation (payment does not need the billing entity)


@dataclass(slots=True)
class RetailPOLine:
    line_no: int
    sku_id: int
    customer_article: str
    qty: int
    confirmed_qty: int = 0


@dataclass(slots=True)
class StockPosition:
    sku_id: int
    on_hand: int
    reserved: int = 0
    in_production: int = 0
    backorder_qty: int = 0
    avg_daily_out: float = 0.0  # smoothed outbound units per day (planning)
    issued_today: int = 0  # units issued since the last daily snapshot
    blocked: int = 0  # quality-blocked units (not on hand)
    held_receipts: list[dict] = field(default_factory=list)  # receipt event data held during a block


@dataclass(slots=True)
class Promotion:
    promo_id: int
    channel: str  # web | retailer channel key
    name: str
    kind: str  # code | automatic
    code: str | None
    discount_pct: float  # 0.25 = 25 % off
    free_shipping: bool
    sku_ids: list[int]
    brand: str | None
    start: datetime
    end: datetime
    announced_at: datetime  # when the promotion record is created in the source system
    scenario_id: str | None = None


@dataclass(slots=True)
class RetailerInventory:
    """Hidden retailer inventory and demand state for one DC (not visible in phase 1 data)."""

    dc_account: str
    retailer: str
    on_hand: dict[int, float] = field(default_factory=dict)  # sku_id -> units
    on_order: dict[int, float] = field(default_factory=dict)  # sku_id -> units in transit
    forecast: dict[int, float] = field(default_factory=dict)  # sku_id -> weekly units (smoothed)
    push_backlog: dict[int, float] = field(default_factory=dict)  # sku_id -> units cut during push mode


# ---------------------------------------------------------------------------
# Phase 2: marketing, deductions, feeds, chaos
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Campaign:
    """A marketing campaign of the pure campaign calendar (not stored)."""

    campaign_id: str
    name: str
    channel: str  # paid_search | social | influencer | email | affiliate
    objective: str  # acquisition | promotion | retention
    start: date
    end: date  # inclusive
    daily_budget_eur: float
    created_on: date  # local date of defined_at
    defined_at: datetime  # when the campaign record is created (CampaignDefined)
    influencer_code: str | None = None
    promo_id: int | None = None
    scenario_id: str | None = None
    window_id: str | None = None  # scenario campaigns


@dataclass(slots=True)
class DeductionItem:
    sku_id: int | None
    customer_article: str | None
    mapped: bool  # False -> zdeduct_item.matnr NULL
    units: int
    rate_net: float
    amount: float  # gross


@dataclass(slots=True)
class Deduction:
    """A deduction claimed by a trade customer (id = deduction_id; removed when closed)."""

    deduction_id: int
    kind: str  # DEDUCTION_KINDS
    channel: str
    payer: str  # retailer account id
    claimed_on: date
    claimed_at: datetime
    amount: float  # claimed, EUR gross
    validated_amount: float  # what the brand can verify (drives matching)
    status: str = "claimed"  # DEDUCTION_STATUSES
    items: list[DeductionItem] = field(default_factory=list)
    promo_id: int | None = None
    promo_text: str | None = None
    period_from: date | None = None
    period_to: date | None = None
    delivery_id: int | None = None  # shortage
    sales_doc_id: int | None = None  # service penalty
    billing_id: int | None = None  # contractual kinds: the invoice that triggered the claim
    accepted_amount: float | None = None
    dispute_reason: str | None = None
    taken: float = 0.0  # short-paid so far
    repaid: float = 0.0  # repaid so far
    clr_seq: int = 0  # last clearing sequence used
    window_id: str | None = None  # scenario window (clearance claims)
    meta: dict = field(default_factory=dict)  # module scratch space, JSON values only


@dataclass(slots=True)
class DeductionBook:
    """Open deductions of one payer (id = payer account id)."""

    payer: str
    open_ids: list[int] = field(default_factory=list)  # ascending


@dataclass(slots=True)
class PromoClaimBasis:
    """Units sold on promotion, per promotion week and brand (id = f"{promo_id}:{week_start:%Y%m%d}:{brand}")."""

    promo_id: int
    channel: str
    week_start: date
    brand: str
    units: dict[int, int] = field(default_factory=dict)  # sku_id -> promo units sold


@dataclass(slots=True)
class FeedAccumulator:
    """Sell-out of one feed period being collected (id = f"{channel}:{W|M|D}:{period_start iso}")."""

    channel: str
    period_type: str  # W | M | D
    period_start: date
    period_end: date
    # "dc|sku" -> [store_units, online_units, value_net, value_gross, on_hand]  (W/M; on_hand = latest week end)
    # "sku"    -> [sold, demand, on_hand, on_order]                            (D, summed over FCs)
    rows: dict[str, list] = field(default_factory=dict)
    dcs_reported: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)  # restate flag, run-1 load, region/missing share


@dataclass(slots=True)
class MarketingDay:
    """Tracked web orders of one local day, until the daily export (id = local date iso)."""

    day: date
    by_channel: dict[str, list] = field(default_factory=dict)  # "channel|country" -> [orders, revenue_eur, new_customers]
    attributions: list[list] = field(default_factory=list)  # [order_id, customer_id, channel, campaign_id, code, is_new, revenue_eur]


@dataclass(slots=True)
class ChaosLedger:
    """Ids a chaos defect has acted on (id = defect name, e.g. "gdpr")."""

    ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class Counters:
    values: dict[str, int] = field(default_factory=dict)


ENTITY_KINDS: dict[str, type] = {
    "counters": Counters,
    "customer": Customer,
    "web_order": WebOrder,
    "payment": Payment,
    "sales_doc": SalesDoc,
    "delivery": Delivery,
    "billing": BillingDoc,
    "web_return": WebReturn,
    "subscription": Subscription,
    "retail_po": RetailPO,
    "stock": StockPosition,
    "retailer_inventory": RetailerInventory,
    "promotion": Promotion,
    # phase 2
    "deduction": Deduction,
    "deduction_book": DeductionBook,
    "promo_claim": PromoClaimBasis,
    "feed_acc": FeedAccumulator,
    "mkt_day": MarketingDay,
    "chaos_ledger": ChaosLedger,
}
