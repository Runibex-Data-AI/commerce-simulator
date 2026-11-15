-- Retailer and marketplace data feeds: flat, file-like rows as delivered, in the
-- sender's own codes (retailer article numbers map to materials via erp.knmt.kdmat;
-- about 95% are mapped). One load per delivered file (feed_loads). Every table is
-- insert-only: a restated period arrives as a new load whose rows replace the
-- earlier load's rows for the same period; take the latest load per period
-- (max delivered_at, then load_id). No is_deleted/deleted_at (insert-only).
CREATE SCHEMA IF NOT EXISTS feeds;

CREATE TABLE IF NOT EXISTS feeds.feed_loads (
    load_id           varchar(40)  NOT NULL PRIMARY KEY,  -- e.g. KRZ_SO_20260302_01
    source            varchar(12)  NOT NULL,              -- KRANZ | MYLO | SHOPNOVA | BELLE
    feed_type         varchar(24)  NOT NULL,              -- sellout_weekly | sellout_monthly | mp_sales_daily | mp_inventory_daily
    period_start      date         NOT NULL,
    period_end        date         NOT NULL,              -- inclusive
    file_name         varchar(80)  NOT NULL,
    delivered_at      timestamptz  NOT NULL,              -- when the file arrived (business time)
    row_count         integer      NOT NULL,
    is_restatement    boolean      NOT NULL DEFAULT false,
    restates_load_id  varchar(40),                        -- load whose rows this load replaces
    created_at        timestamptz  NOT NULL,
    updated_at        timestamptz  NOT NULL
);
CREATE INDEX IF NOT EXISTS feed_loads_updated_at_idx ON feeds.feed_loads (updated_at);
CREATE INDEX IF NOT EXISTS feed_loads_period_idx ON feeds.feed_loads (source, feed_type, period_start);
COMMENT ON TABLE  feeds.feed_loads IS 'One row per delivered feed file (insert-only). A restated period arrives as a new load (is_restatement, restates_load_id) whose rows replace the earlier load''s rows.';
COMMENT ON COLUMN feeds.feed_loads.load_id IS 'Sender prefix, feed type, period start and run number, e.g. KRZ_SO_20260302_01 (run 02 and later are restatements).';
COMMENT ON COLUMN feeds.feed_loads.source IS 'Sender: KRANZ, MYLO (drugstore chains), SHOPNOVA (marketplace), BELLE (perfumery).';
COMMENT ON COLUMN feeds.feed_loads.feed_type IS 'sellout_weekly (ISO weeks), sellout_monthly (4-4-5 retail months), mp_sales_daily, mp_inventory_daily.';
COMMENT ON COLUMN feeds.feed_loads.period_end IS 'Last day of the reported period, inclusive.';
COMMENT ON COLUMN feeds.feed_loads.file_name IS 'File name as delivered, in the sender''s own naming convention.';
COMMENT ON COLUMN feeds.feed_loads.delivered_at IS 'When the file arrived (business time); some retailer files arrive a day or two late.';
COMMENT ON COLUMN feeds.feed_loads.row_count IS 'Number of data rows in the file.';
COMMENT ON COLUMN feeds.feed_loads.is_restatement IS 'True for a re-sent period that replaces an earlier load.';
COMMENT ON COLUMN feeds.feed_loads.restates_load_id IS 'The load whose rows this restatement replaces; NULL for original loads.';

CREATE TABLE IF NOT EXISTS feeds.retailer_sellout (
    load_id        varchar(40)    NOT NULL,
    line_no        integer        NOT NULL,     -- row number in the file
    retailer       varchar(12)    NOT NULL,     -- sender code, as feed_loads.source
    period_type    char(1)        NOT NULL,     -- W week | M retail month (4-4-5)
    period_start   date           NOT NULL,     -- Monday
    period_end     date           NOT NULL,     -- Sunday, inclusive
    period_label   varchar(12)    NOT NULL,     -- the retailer's own label: 2026-W10, KW10/2026, 2026P03
    region_code    varchar(12)    NOT NULL,     -- the retailer's sales region code
    sales_channel  varchar(8)     NOT NULL,     -- the retailer's code for store vs online
    article_no     varchar(20)    NOT NULL,     -- the retailer's article number
    article_text   varchar(60),
    units_sold     integer        NOT NULL,
    stock_units    integer,                     -- region stock at period end (STORE rows; NULL on ONLINE rows)
    sales_value    numeric(14,2)  NOT NULL,
    value_basis    varchar(5)     NOT NULL,     -- NET | GROSS (incl. VAT)
    currency       char(3)        NOT NULL,     -- EUR
    created_at     timestamptz    NOT NULL,
    updated_at     timestamptz    NOT NULL,
    PRIMARY KEY (load_id, line_no)
);
CREATE INDEX IF NOT EXISTS retailer_sellout_updated_at_idx ON feeds.retailer_sellout (updated_at);
CREATE INDEX IF NOT EXISTS retailer_sellout_period_idx ON feeds.retailer_sellout (retailer, period_start);
CREATE INDEX IF NOT EXISTS retailer_sellout_article_idx ON feeds.retailer_sellout (article_no);
COMMENT ON TABLE  feeds.retailer_sellout IS 'Retailer sell-out as delivered (insert-only): one row per period, sales region, sales channel and article, in the retailer''s own codes and layout.';
COMMENT ON COLUMN feeds.retailer_sellout.line_no IS 'Row number in the file (ordered by region, sales channel, article number).';
COMMENT ON COLUMN feeds.retailer_sellout.period_type IS 'W: ISO week (Monday-Sunday). M: retail month of a 4-4-5 calendar of ISO weeks (P12 takes week 53).';
COMMENT ON COLUMN feeds.retailer_sellout.period_label IS 'The retailer''s own period label, e.g. 2026-W10 (Kranz), KW10/2026 (Mylo), 2026P03 (Belle).';
COMMENT ON COLUMN feeds.retailer_sellout.region_code IS 'The retailer''s sales region: the region served by one of its distribution centres, in the retailer''s own coding.';
COMMENT ON COLUMN feeds.retailer_sellout.sales_channel IS 'The retailer''s code for stores versus its online shop: STORE/ONLINE (Kranz), F/O (Mylo), FIL/ECOM (Belle).';
COMMENT ON COLUMN feeds.retailer_sellout.article_no IS 'The retailer''s article number; map to the material through erp.knmt.kdmat (about 95% are mapped).';
COMMENT ON COLUMN feeds.retailer_sellout.article_text IS 'Article description in the retailer''s own style.';
COMMENT ON COLUMN feeds.retailer_sellout.units_sold IS 'Units sold to consumers in the period (store rows are present for every listed article, also with 0 units).';
COMMENT ON COLUMN feeds.retailer_sellout.stock_units IS 'Region stock at period end on store rows (0 = out of stock); NULL on online rows.';
COMMENT ON COLUMN feeds.retailer_sellout.sales_value IS 'Consumer sales value in value_basis (net or gross of VAT, by retailer).';
COMMENT ON COLUMN feeds.retailer_sellout.value_basis IS 'NET (excl. VAT) or GROSS (incl. VAT), by retailer.';

CREATE TABLE IF NOT EXISTS feeds.marketplace_sales_daily (
    report_date      date           NOT NULL,
    product_code     varchar(20)    NOT NULL,   -- marketplace product code (SNV + 8 digits)
    load_id          varchar(40)    NOT NULL,
    product_title    varchar(80),
    units_ordered    integer        NOT NULL,
    ordered_revenue  numeric(14,2)  NOT NULL,   -- gross, at the marketplace's consumer price
    currency         char(3)        NOT NULL,
    glance_views     integer        NOT NULL,   -- product detail page views (traffic)
    created_at       timestamptz    NOT NULL,
    updated_at       timestamptz    NOT NULL,
    PRIMARY KEY (report_date, product_code, load_id)
);
CREATE INDEX IF NOT EXISTS marketplace_sales_daily_updated_at_idx ON feeds.marketplace_sales_daily (updated_at);
CREATE INDEX IF NOT EXISTS marketplace_sales_daily_load_idx ON feeds.marketplace_sales_daily (load_id);
COMMENT ON TABLE  feeds.marketplace_sales_daily IS 'Marketplace vendor sales and traffic report, one row per day and product with units or views (insert-only).';
COMMENT ON COLUMN feeds.marketplace_sales_daily.product_code IS 'Marketplace product code (SNV + 8 digits), never an EAN; map through erp.knmt.kdmat.';
COMMENT ON COLUMN feeds.marketplace_sales_daily.units_ordered IS 'Units ordered by consumers on the marketplace that day.';
COMMENT ON COLUMN feeds.marketplace_sales_daily.ordered_revenue IS 'Gross revenue at the marketplace''s consumer price (incl. VAT and marketplace discounts).';
COMMENT ON COLUMN feeds.marketplace_sales_daily.glance_views IS 'Product detail page views (traffic); views without units point to out-of-stock days.';

CREATE TABLE IF NOT EXISTS feeds.marketplace_inventory_daily (
    report_date        date         NOT NULL,   -- end-of-day position
    product_code       varchar(20)  NOT NULL,
    load_id            varchar(40)  NOT NULL,
    sellable_on_hand   integer      NOT NULL,   -- all fulfilment centres
    inbound_quantity   integer      NOT NULL,   -- confirmed purchase-order units not yet received
    created_at         timestamptz  NOT NULL,
    updated_at         timestamptz  NOT NULL,
    PRIMARY KEY (report_date, product_code, load_id)
);
CREATE INDEX IF NOT EXISTS marketplace_inventory_daily_updated_at_idx ON feeds.marketplace_inventory_daily (updated_at);
CREATE INDEX IF NOT EXISTS marketplace_inventory_daily_load_idx ON feeds.marketplace_inventory_daily (load_id);
COMMENT ON TABLE  feeds.marketplace_inventory_daily IS 'Marketplace vendor inventory report: end-of-day position per listed product with stock or inbound units (insert-only).';
COMMENT ON COLUMN feeds.marketplace_inventory_daily.report_date IS 'Date of the end-of-day stock position.';
COMMENT ON COLUMN feeds.marketplace_inventory_daily.sellable_on_hand IS 'Sellable units on hand across all marketplace fulfilment centres.';
COMMENT ON COLUMN feeds.marketplace_inventory_daily.inbound_quantity IS 'Confirmed purchase-order units not yet received at the fulfilment centres.';
