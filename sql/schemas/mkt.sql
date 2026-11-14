-- Marketing stack: text keys, business dates as date (contract columns stay timestamptz).
-- Daily tables are written once per day after the day closes (04:00-05:00 local).
CREATE SCHEMA IF NOT EXISTS mkt;

CREATE TABLE IF NOT EXISTS mkt.influencer_codes (
    code              varchar(20)   NOT NULL PRIMARY KEY,
    influencer_handle varchar(40)   NOT NULL,   -- fictional
    audience_platform varchar(20)   NOT NULL,   -- short_video | photo_feed | blog | podcast
    commission_pct    numeric(5,2)  NOT NULL,   -- on attributed net revenue
    fixed_fee_eur     numeric(10,2) NOT NULL,   -- per collaboration flight
    valid_from        date          NOT NULL,
    valid_to          date,
    status            varchar(10)   NOT NULL,   -- active | paused | ended
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
    is_deleted boolean NOT NULL DEFAULT false, deleted_at timestamptz
);
CREATE INDEX IF NOT EXISTS influencer_codes_updated_at_idx ON mkt.influencer_codes (updated_at);
COMMENT ON TABLE  mkt.influencer_codes IS 'Influencer discount codes and their commercial terms (fictional influencers).';
COMMENT ON COLUMN mkt.influencer_codes.code IS 'Discount code as entered at the shop checkout (shop customers carry it as their acquisition code).';
COMMENT ON COLUMN mkt.influencer_codes.commission_pct IS 'Commission in percent of attributed net revenue.';
COMMENT ON COLUMN mkt.influencer_codes.fixed_fee_eur IS 'Fixed fee per collaboration flight, EUR.';

CREATE TABLE IF NOT EXISTS mkt.campaigns (
    campaign_id       varchar(30)   NOT NULL PRIMARY KEY,   -- e.g. PS-2026Q1-01
    campaign_name     varchar(80)   NOT NULL,
    channel           varchar(30)   NOT NULL,   -- paid_search | social | influencer | email | affiliate
    objective         varchar(20)   NOT NULL,   -- acquisition | promotion | retention
    influencer_code   varchar(20),
    promo_ref         varchar(36),              -- shop.promotions.id (UUID text) for promotion campaigns
    start_date        date          NOT NULL,
    end_date          date          NOT NULL,   -- inclusive
    daily_budget_eur  numeric(12,2) NOT NULL,
    status            varchar(10)   NOT NULL,   -- planned | active | ended
    created_date      date          NOT NULL,
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
    is_deleted boolean NOT NULL DEFAULT false, deleted_at timestamptz
);
CREATE INDEX IF NOT EXISTS campaigns_updated_at_idx ON mkt.campaigns (updated_at);
COMMENT ON TABLE  mkt.campaigns IS 'Marketing campaigns; the status moves planned -> active -> ended (updates).';
COMMENT ON COLUMN mkt.campaigns.campaign_id IS 'Channel prefix, year, quarter and number, e.g. PS-2026Q1-01; EM-P<n> for promotion emails.';
COMMENT ON COLUMN mkt.campaigns.influencer_code IS 'mkt.influencer_codes.code for influencer campaigns.';
COMMENT ON COLUMN mkt.campaigns.promo_ref IS 'shop.promotions.id (UUID text) of the promoted web promotion, for promotion campaigns.';
COMMENT ON COLUMN mkt.campaigns.end_date IS 'Last campaign day, inclusive (moves earlier when a campaign is stopped early).';
COMMENT ON COLUMN mkt.campaigns.daily_budget_eur IS 'Planned spend per day, EUR; updated (with updated_at) while a budget boost runs.';

CREATE TABLE IF NOT EXISTS mkt.campaign_spend_daily (
    campaign_id       varchar(30)   NOT NULL,
    spend_date        date          NOT NULL,
    impressions       bigint        NOT NULL,
    clicks            integer       NOT NULL,
    spend_eur         numeric(12,2) NOT NULL,
    platform_conversions integer    NOT NULL,   -- as reported by the ad platform (over-attributes)
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
    is_deleted boolean NOT NULL DEFAULT false, deleted_at timestamptz,
    PRIMARY KEY (campaign_id, spend_date)
);
CREATE INDEX IF NOT EXISTS campaign_spend_daily_updated_at_idx ON mkt.campaign_spend_daily (updated_at);
CREATE INDEX IF NOT EXISTS campaign_spend_daily_spend_date_idx ON mkt.campaign_spend_daily (spend_date);
COMMENT ON TABLE  mkt.campaign_spend_daily IS 'Daily delivery and spend per campaign, exported once per day after the day closes.';
COMMENT ON COLUMN mkt.campaign_spend_daily.platform_conversions IS 'Conversions as reported by the ad platform; over-attributes compared with mkt.attributions.';

CREATE TABLE IF NOT EXISTS mkt.sessions_daily (
    session_date      date          NOT NULL,
    channel           varchar(30)   NOT NULL,   -- traffic source (acquisition / repeat source words)
    country           char(2)       NOT NULL,
    device            varchar(10)   NOT NULL,   -- mobile | desktop | tablet
    sessions          integer       NOT NULL,
    users             integer       NOT NULL,
    new_users         integer       NOT NULL,
    orders            integer       NOT NULL,   -- tracked shop orders (consent: about 92% of shop orders)
    revenue_eur       numeric(14,2) NOT NULL,   -- tracked gross order value in EUR
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
    is_deleted boolean NOT NULL DEFAULT false, deleted_at timestamptz,
    PRIMARY KEY (session_date, channel, country, device)
);
CREATE INDEX IF NOT EXISTS sessions_daily_updated_at_idx ON mkt.sessions_daily (updated_at);
CREATE INDEX IF NOT EXISTS sessions_daily_session_date_idx ON mkt.sessions_daily (session_date);
COMMENT ON TABLE  mkt.sessions_daily IS 'Web analytics: sessions, users and tracked orders per day, traffic source, country and device.';
COMMENT ON COLUMN mkt.sessions_daily.channel IS 'Traffic source, in the shop''s source words (organic_search, paid_search, social, influencer, email, ...); the shop''s deal_site source is the affiliate channel (coupon and deal sites), as in mkt.campaigns.';
COMMENT ON COLUMN mkt.sessions_daily.orders IS 'Tracked shop orders; about 92% of shop orders are tracked (consent).';
COMMENT ON COLUMN mkt.sessions_daily.revenue_eur IS 'Tracked gross order value, EUR.';

CREATE TABLE IF NOT EXISTS mkt.attributions (
    attribution_id    varchar(40)   NOT NULL PRIMARY KEY,   -- ATT-NV-10004711
    order_ref         varchar(36)   NOT NULL,   -- shop.orders.id (UUID text)
    customer_ref      varchar(36)   NOT NULL,   -- shop.customers.id (UUID text)
    attribution_date  date          NOT NULL,   -- local date of the order confirmation (payment authorised)
    channel           varchar(30)   NOT NULL,
    campaign_id       varchar(30),
    influencer_code   varchar(20),
    model             varchar(12)   NOT NULL,   -- last_click
    is_new_customer   boolean       NOT NULL,
    attributed_revenue_eur numeric(12,2) NOT NULL,
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
    is_deleted boolean NOT NULL DEFAULT false, deleted_at timestamptz
);
CREATE INDEX IF NOT EXISTS attributions_updated_at_idx ON mkt.attributions (updated_at);
CREATE INDEX IF NOT EXISTS attributions_order_ref_idx ON mkt.attributions (order_ref);
CREATE INDEX IF NOT EXISTS attributions_attribution_date_idx ON mkt.attributions (attribution_date);
COMMENT ON TABLE  mkt.attributions IS 'Single-touch last-click attribution, one row per tracked shop order.';
COMMENT ON COLUMN mkt.attributions.order_ref IS 'shop.orders.id (UUID text).';
COMMENT ON COLUMN mkt.attributions.customer_ref IS 'shop.customers.id (UUID text).';
COMMENT ON COLUMN mkt.attributions.campaign_id IS 'mkt.campaigns.campaign_id; NULL for organic, direct, app and marketplace spillover traffic.';
COMMENT ON COLUMN mkt.attributions.attributed_revenue_eur IS 'Gross order value attributed, EUR.';
