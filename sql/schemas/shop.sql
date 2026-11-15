-- Web shop platform (velmora-shop).
-- Conventions: UUID keys, English snake_case, timestamptz in UTC, statuses as lower-case words,
-- money as numeric(12,2) gross (incl. VAT) with an ISO 4217 currency column (EUR, or CHF for Swiss orders).
-- Loading contract: every table has a primary key, created_at / updated_at (indexed) and
-- is_deleted / deleted_at. Rows are never hard-deleted.
-- No foreign keys: integrity is guaranteed by the generator, and the loader stays free of ordering constraints.
CREATE SCHEMA IF NOT EXISTS shop;

-- -- customers ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS shop.customers (
    id                uuid        PRIMARY KEY,
    email             text        NOT NULL,
    first_name        text        NOT NULL,
    last_name         text        NOT NULL,
    phone             text,
    birth_year        smallint,
    locale            text        NOT NULL,            -- de_DE | de_AT | de_CH
    country           char(2)     NOT NULL,            -- DE | AT | CH
    marketing_opt_in  boolean     NOT NULL,
    signup_source     text        NOT NULL,            -- organic_search | paid_search | social | influencer | ...
    influencer_code   text,
    status            text        NOT NULL,            -- active | anonymised
    registered_at     timestamptz NOT NULL,
    anonymised_at     timestamptz,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    is_deleted        boolean     NOT NULL DEFAULT false,
    deleted_at        timestamptz
);
CREATE INDEX IF NOT EXISTS customers_updated_at_idx ON shop.customers (updated_at);
CREATE INDEX IF NOT EXISTS customers_email_idx ON shop.customers (email);

COMMENT ON TABLE shop.customers IS 'Registered web shop customers. A GDPR erasure anonymises personal data in place (status anonymised); the row and its orders are kept.';
COMMENT ON COLUMN shop.customers.signup_source IS 'Acquisition channel at registration: organic_search, paid_search, social, influencer, marketplace_spillover, direct, or deal_site (deal-site shoppers during a promo_margin_trap promotion).';
COMMENT ON COLUMN shop.customers.influencer_code IS 'Influencer code used at sign-up, if any (signup_source = influencer). The customer stays attributed to it for good: derive influencer customers from this column (or mkt.attributions.influencer_code), not from shop.orders.promo_code, which is NULL or another promotion''s code when another promotion took the first order.';
COMMENT ON COLUMN shop.customers.status IS 'active or anonymised (personal data erased on request).';
COMMENT ON COLUMN shop.customers.anonymised_at IS 'When personal data was erased; NULL for active customers.';

CREATE TABLE IF NOT EXISTS shop.addresses (
    id            uuid        PRIMARY KEY,
    customer_id   uuid        NOT NULL,
    address_type  text        NOT NULL,                -- shipping | billing
    is_default    boolean     NOT NULL,
    first_name    text        NOT NULL,
    last_name     text        NOT NULL,
    street        text        NOT NULL,
    house_number  text,
    postcode      text,
    city          text        NOT NULL,
    country       char(2)     NOT NULL,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL,
    is_deleted    boolean     NOT NULL DEFAULT false,
    deleted_at    timestamptz
);
CREATE INDEX IF NOT EXISTS addresses_updated_at_idx ON shop.addresses (updated_at);
CREATE INDEX IF NOT EXISTS addresses_customer_idx ON shop.addresses (customer_id);

COMMENT ON TABLE shop.addresses IS 'Customer address book. The default shipping address created at registration also serves as billing address. No region/state column, as is usual for DACH shops.';
COMMENT ON COLUMN shop.addresses.postcode IS 'Postcode as entered by the customer; may be missing or malformed.';

-- -- promotions -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS shop.promotions (
    id             uuid         PRIMARY KEY,
    name           text         NOT NULL,
    promo_type     text         NOT NULL,              -- code | automatic
    code           text,
    discount_pct   numeric(5,4) NOT NULL,
    free_shipping  boolean      NOT NULL,
    skus           text[]       NOT NULL DEFAULT '{}',
    brand          text,
    starts_at      timestamptz  NOT NULL,
    ends_at        timestamptz  NOT NULL,
    created_at     timestamptz  NOT NULL,
    updated_at     timestamptz  NOT NULL,
    is_deleted     boolean      NOT NULL DEFAULT false,
    deleted_at     timestamptz
);
CREATE INDEX IF NOT EXISTS promotions_updated_at_idx ON shop.promotions (updated_at);
CREATE INDEX IF NOT EXISTS promotions_code_idx ON shop.promotions (code);

COMMENT ON TABLE shop.promotions IS 'Web shop promotions (retailer promotions are not in this system). Created when announced, before starts_at. Also one code promotion per influencer code: its first-order discount (code = the influencer code, valid for a customer''s first order only, open ended). "First order" is the person''s first order: the code is not granted again on the first order of a duplicate account of the same person. Never combined with another promotion: when one applies to that first order it applies alone and the code''s discount is dropped, not carried over.';
COMMENT ON COLUMN shop.promotions.promo_type IS 'code (customer enters a voucher code) or automatic (applied to qualifying baskets).';
COMMENT ON COLUMN shop.promotions.code IS 'Voucher code for promo_type = code; NULL for automatic promotions.';
COMMENT ON COLUMN shop.promotions.discount_pct IS 'Discount on qualifying items as a fraction, e.g. 0.2500 = 25 % off.';
COMMENT ON COLUMN shop.promotions.skus IS 'Shop SKU codes the promotion applies to. Empty: every SKU of brand (or the whole range if brand is NULL).';
COMMENT ON COLUMN shop.promotions.brand IS 'Brand the promotion is restricted to, if any.';

-- -- subscriptions ----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS shop.subscriptions (
    id              uuid        PRIMARY KEY,
    customer_id     uuid        NOT NULL,
    status          text        NOT NULL,              -- active | paused | cancelled
    interval_weeks  smallint    NOT NULL,              -- 4 | 8 | 12
    skus            text[]      NOT NULL,
    quantities      integer[]   NOT NULL,
    started_at      timestamptz NOT NULL,
    next_order_at   timestamptz,
    paused_at       timestamptz,
    cancelled_at    timestamptz,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    is_deleted      boolean     NOT NULL DEFAULT false,
    deleted_at      timestamptz
);
CREATE INDEX IF NOT EXISTS subscriptions_updated_at_idx ON shop.subscriptions (updated_at);
CREATE INDEX IF NOT EXISTS subscriptions_customer_idx ON shop.subscriptions (customer_id);

COMMENT ON TABLE shop.subscriptions IS 'Repeat-delivery subscriptions; each cycle places a regular order carrying subscription_id.';
COMMENT ON COLUMN shop.subscriptions.skus IS 'Shop SKU codes in the subscription, parallel to quantities.';
COMMENT ON COLUMN shop.subscriptions.quantities IS 'Units per delivery for each entry of skus (same position).';
COMMENT ON COLUMN shop.subscriptions.next_order_at IS 'When the next subscription order is due; for a paused subscription the date it resumes; NULL when cancelled.';
COMMENT ON COLUMN shop.subscriptions.paused_at IS 'Most recent pause.';

-- -- orders -----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS shop.orders (
    id                    uuid          PRIMARY KEY,
    order_number          text          NOT NULL,
    customer_id           uuid          NOT NULL,
    status                text          NOT NULL,
    currency              char(3)       NOT NULL,      -- EUR | CHF
    country               char(2)       NOT NULL,
    placed_at             timestamptz   NOT NULL,
    items_subtotal        numeric(12,2) NOT NULL,
    discount_total        numeric(12,2) NOT NULL,
    shipping_fee          numeric(12,2) NOT NULL,
    tax_total             numeric(12,2) NOT NULL,
    total_amount          numeric(12,2) NOT NULL,
    promo_code            text,
    promotion_id          uuid,
    source                text          NOT NULL,
    payment_method        text          NOT NULL,      -- paypal | invoice_bnpl | card | direct_debit | other
    shipping_address_id   uuid          NOT NULL,
    billing_address_id    uuid          NOT NULL,
    subscription_id       uuid,
    is_repeat_customer    boolean       NOT NULL,
    item_count            integer       NOT NULL,
    paid_at               timestamptz,
    payment_failed_at     timestamptz,
    released_at           timestamptz,
    cancelled_at          timestamptz,
    cancel_reason         text,
    partially_shipped_at  timestamptz,
    shipped_at            timestamptz,
    delivered_at          timestamptz,
    return_requested_at   timestamptz,
    returned_at           timestamptz,
    return_rejected_at    timestamptz,
    refunded_at           timestamptz,
    created_at            timestamptz   NOT NULL,
    updated_at            timestamptz   NOT NULL,
    is_deleted            boolean       NOT NULL DEFAULT false,
    deleted_at            timestamptz
);
-- Columns added after the first release (CREATE TABLE IF NOT EXISTS does not add them).
ALTER TABLE shop.orders ADD COLUMN IF NOT EXISTS return_rejected_at timestamptz;
CREATE INDEX IF NOT EXISTS orders_updated_at_idx ON shop.orders (updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS orders_order_number_idx ON shop.orders (order_number);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON shop.orders (customer_id);
CREATE INDEX IF NOT EXISTS orders_placed_at_idx ON shop.orders (placed_at);

COMMENT ON TABLE shop.orders IS 'Web orders. Every lifecycle step updates this row (status, timestamp columns, updated_at).';
COMMENT ON COLUMN shop.orders.order_number IS 'Human-facing order number shown to the customer, e.g. NV-10004711.';
COMMENT ON COLUMN shop.orders.status IS 'created, payment_failed, authorised, released, cancelled, partially_shipped, shipped, delivered, delivery_exception, returned_to_sender, lost, return_requested, returned, return_rejected, partially_refunded or refunded. return_rejected: the returned goods failed inspection, nothing is refunded and the goods go back to the customer.';
COMMENT ON COLUMN shop.orders.items_subtotal IS 'Sum of quantity x unit_price over all lines, gross, before discounts.';
COMMENT ON COLUMN shop.orders.discount_total IS 'Sum of line discounts (gross).';
COMMENT ON COLUMN shop.orders.shipping_fee IS 'Shipping fee charged, gross; 0 above the free-shipping threshold or with a free-shipping promotion. The threshold applies to the goods after discount, except for an influencer code''s first-order discount, where it applies to the goods before it (the code never adds a fee).';
COMMENT ON COLUMN shop.orders.tax_total IS 'VAT contained in total_amount: line taxes plus VAT on the shipping fee at the order''s standard rate.';
COMMENT ON COLUMN shop.orders.total_amount IS 'Amount charged to the customer: items_subtotal - discount_total + shipping_fee (gross).';
COMMENT ON COLUMN shop.orders.promo_code IS 'Voucher code entered (code promotions only); an influencer code on the first order of a customer acquired with it, unless another promotion took that order. One code per order: never combined with another promotion, so an influencer customer''s first order may carry NULL (automatic promotion) or another promotion''s code here. Not an attribution field: the customer stays attributed to its code via shop.customers.influencer_code and mkt.attributions.influencer_code.';
COMMENT ON COLUMN shop.orders.promotion_id IS 'Promotion applied to the order (code or automatic), if any; at most one per order.';
COMMENT ON COLUMN shop.orders.source IS 'Traffic source of the order session (utm-like word, e.g. organic_search, paid_search, social, influencer, direct, subscription).';
COMMENT ON COLUMN shop.orders.subscription_id IS 'Set for orders placed by a subscription cycle.';
COMMENT ON COLUMN shop.orders.is_repeat_customer IS 'True if the customer had ordered before this order.';
COMMENT ON COLUMN shop.orders.item_count IS 'Total units ordered (sum of order_lines.quantity).';
COMMENT ON COLUMN shop.orders.paid_at IS 'Payment authorised.';
COMMENT ON COLUMN shop.orders.released_at IS 'Released to the warehouse.';
COMMENT ON COLUMN shop.orders.partially_shipped_at IS 'First shipment of an order that shipped in parts.';
COMMENT ON COLUMN shop.orders.shipped_at IS 'All shippable items have left the warehouse (for an order whose backordered remainder was cancelled: the last shipment).';
COMMENT ON COLUMN shop.orders.delivered_at IS 'The last parcel was delivered (carrier delivery time).';
COMMENT ON COLUMN shop.orders.return_rejected_at IS 'Returned goods failed inspection (no refund).';
COMMENT ON COLUMN shop.orders.refunded_at IS 'Most recent (partial or full) refund.';

CREATE TABLE IF NOT EXISTS shop.order_lines (
    id               uuid          PRIMARY KEY,
    order_id         uuid          NOT NULL,
    line_no          integer       NOT NULL,
    sku              text          NOT NULL,
    ean              char(13)      NOT NULL,
    product_name     text          NOT NULL,
    quantity         integer       NOT NULL,
    unit_price       numeric(12,2) NOT NULL,
    discount_amount  numeric(12,2) NOT NULL,
    tax_rate         numeric(5,4)  NOT NULL,
    tax_amount       numeric(12,2) NOT NULL,
    line_total       numeric(12,2) NOT NULL,
    qty_shipped      integer       NOT NULL DEFAULT 0,
    qty_cancelled    integer       NOT NULL DEFAULT 0,
    qty_returned     integer       NOT NULL DEFAULT 0,
    created_at       timestamptz   NOT NULL,
    updated_at       timestamptz   NOT NULL,
    is_deleted       boolean       NOT NULL DEFAULT false,
    deleted_at       timestamptz
);
CREATE INDEX IF NOT EXISTS order_lines_updated_at_idx ON shop.order_lines (updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS order_lines_order_line_idx ON shop.order_lines (order_id, line_no);

COMMENT ON COLUMN shop.order_lines.sku IS 'Shop SKU code, e.g. VEL-BOD-HYB-400.';
COMMENT ON COLUMN shop.order_lines.unit_price IS 'Gross list price per unit at order time.';
COMMENT ON COLUMN shop.order_lines.discount_amount IS 'Total gross discount on the line (not per unit).';
COMMENT ON COLUMN shop.order_lines.tax_rate IS 'VAT rate as a fraction: 0.1900 (DE), 0.2000 (AT), 0.0810 (CH).';
COMMENT ON COLUMN shop.order_lines.tax_amount IS 'VAT contained in line_total: line_total minus the net amount line_total / (1 + tax_rate) rounded half-up to cents (the same split as the ERP item netwr / mwsbp).';
COMMENT ON COLUMN shop.order_lines.qty_returned IS 'Units returned and accepted; units of a rejected return are deducted again (they go back to the customer).';
COMMENT ON COLUMN shop.order_lines.line_total IS 'quantity x unit_price - discount_amount, gross.';

-- -- payments and refunds ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS shop.payments (
    id                  uuid          PRIMARY KEY,
    order_id            uuid          NOT NULL,
    attempt_no          smallint      NOT NULL,
    method              text          NOT NULL,
    provider            text          NOT NULL,
    status              text          NOT NULL,
    amount              numeric(12,2) NOT NULL,
    currency            char(3)       NOT NULL,
    provider_reference  text,
    authorised_at       timestamptz,
    captured_at         timestamptz,
    captured_amount     numeric(12,2),
    failed_at           timestamptz,
    failure_reason      text,
    voided_at           timestamptz,
    refunded_at         timestamptz,
    refunded_amount     numeric(12,2) NOT NULL DEFAULT 0,
    created_at          timestamptz   NOT NULL,
    updated_at          timestamptz   NOT NULL,
    is_deleted          boolean       NOT NULL DEFAULT false,
    deleted_at          timestamptz
);
CREATE INDEX IF NOT EXISTS payments_updated_at_idx ON shop.payments (updated_at);
CREATE INDEX IF NOT EXISTS payments_order_idx ON shop.payments (order_id);

COMMENT ON TABLE shop.payments IS 'Payment attempts; a failed attempt may be followed by a retry with attempt_no + 1.';
COMMENT ON COLUMN shop.payments.status IS 'pending, authorised, failed, captured, voided, refunded or partially_refunded.';
COMMENT ON COLUMN shop.payments.provider IS 'Payment service provider (fictional) that processed the attempt.';
COMMENT ON COLUMN shop.payments.provider_reference IS 'Transaction reference at the payment service provider.';
COMMENT ON COLUMN shop.payments.captured_amount IS 'Amount captured (usually at shipment).';
COMMENT ON COLUMN shop.payments.refunded_amount IS 'Cumulative amount refunded on this payment.';
COMMENT ON COLUMN shop.payments.refunded_at IS 'Most recent refund on this payment.';

CREATE TABLE IF NOT EXISTS shop.refunds (
    id          uuid          PRIMARY KEY,
    order_id    uuid          NOT NULL,
    payment_id  uuid,
    amount      numeric(12,2) NOT NULL,
    currency    char(3)       NOT NULL,
    reason      text          NOT NULL,              -- return | lost | returned_to_sender | cancelled_after_capture
    status      text          NOT NULL,              -- issued | completed
    issued_at   timestamptz   NOT NULL,
    completed_at timestamptz,
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz
);
ALTER TABLE shop.refunds ADD COLUMN IF NOT EXISTS completed_at timestamptz;
CREATE INDEX IF NOT EXISTS refunds_updated_at_idx ON shop.refunds (updated_at);
CREATE INDEX IF NOT EXISTS refunds_order_idx ON shop.refunds (order_id);

COMMENT ON COLUMN shop.refunds.amount IS 'Gross amount refunded to the customer, order currency.';
COMMENT ON COLUMN shop.refunds.payment_id IS 'Payment the refund was paid back on; NULL if refunded outside the original payment.';
COMMENT ON COLUMN shop.refunds.status IS 'issued (refund instructed), then completed once the payment provider has paid it out (hours to a week later, by payment method).';
COMMENT ON COLUMN shop.refunds.completed_at IS 'Payment provider paid the refund out.';

CREATE TABLE IF NOT EXISTS shop.promo_redemptions (
    promotion_id     uuid          NOT NULL,
    order_id         uuid          NOT NULL,
    customer_id      uuid          NOT NULL,
    code             text,
    discount_amount  numeric(12,2) NOT NULL,
    currency         char(3)       NOT NULL,
    redeemed_at      timestamptz   NOT NULL,
    created_at       timestamptz   NOT NULL,
    updated_at       timestamptz   NOT NULL,
    is_deleted       boolean       NOT NULL DEFAULT false,
    deleted_at       timestamptz,
    PRIMARY KEY (promotion_id, order_id)
);
CREATE INDEX IF NOT EXISTS promo_redemptions_updated_at_idx ON shop.promo_redemptions (updated_at);
CREATE INDEX IF NOT EXISTS promo_redemptions_order_idx ON shop.promo_redemptions (order_id);

COMMENT ON TABLE shop.promo_redemptions IS 'One row per order on which a promotion (code or automatic) was actually applied.';
COMMENT ON COLUMN shop.promo_redemptions.code IS 'Voucher code entered (code promotions only).';
COMMENT ON COLUMN shop.promo_redemptions.discount_amount IS 'Gross item discount granted on the order (sum of line discounts); excludes waived shipping.';
