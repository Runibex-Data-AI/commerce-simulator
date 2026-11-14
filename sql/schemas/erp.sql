-- ERP (SAP-style). Modelled on SAP SD and material/customer master tables.
--
-- A plausible imitation of the structure, not a replica of SAP:
--   * SAP field names, ten-digit document numbers (sales 0010..., deliveries
--     0080..., billing 0090...), 18-character zero-padded material numbers,
--     six-digit item numbers.
--   * Character key fields keep their leading zeros (stored as text).
--   * Business dates/times (erdat/erzet, audat, fkdat, wadat_ist, podat...)
--     are Europe/Berlin local, stored as Postgres date / time.
--   * Status fields are S/4HANA style: '' (blank, not relevant) / A / B / C.
--   * Custom fields carry the customer-namespace prefix zz (as in a real
--     customer system) where SAP keeps the information elsewhere.
--   * Loading contract: every table has a primary key, created_at/updated_at
--     (timestamptz, set from the simulation clock, indexed) and
--     is_deleted/deleted_at. Nothing is ever deleted physically.
--
-- Fixed organisational values: sales org vkorg '1000', plant werks '1000',
-- storage location lgort '0001', shipping point vstel '1000', company code
-- bukrs '1000', division spart '00', distribution channel vtweg '10' (trade)
-- or '20' (e-commerce, one collective customer per country).
--
-- No foreign keys: rows are written table by table within one transaction and
-- the BI side should not rely on them.

CREATE SCHEMA IF NOT EXISTS erp;

-- ---------------------------------------------------------------------------
-- Material master
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS erp.mara (
    matnr       varchar(18)   NOT NULL PRIMARY KEY,
    mtart       varchar(4)    NOT NULL,              -- material type, FERT = finished product
    mbrsh       varchar(1)    NOT NULL,              -- industry sector
    matkl       varchar(9)    NOT NULL,              -- material group
    meins       varchar(3)    NOT NULL,              -- base unit of measure
    ean11       varchar(18),                         -- EAN/GTIN
    numtp       varchar(2),                          -- EAN category, HE = manufacturer EAN
    prdha       varchar(18)   NOT NULL,              -- product hierarchy
    brand_id    varchar(4)    NOT NULL,
    groes       varchar(32),                         -- size/dimensions text
    brgew       numeric(13,3) NOT NULL,
    ntgew       numeric(13,3) NOT NULL,
    gewei       varchar(3)    NOT NULL,
    ersda       date          NOT NULL,
    ernam       varchar(12)   NOT NULL,
    laeda       date          NOT NULL,
    mstae       varchar(2)    NOT NULL DEFAULT '',  -- cross-plant material status
    mstde       date,                                -- valid-from date of mstae
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz
);
CREATE INDEX IF NOT EXISTS mara_updated_at_idx ON erp.mara (updated_at);
COMMENT ON TABLE  erp.mara IS 'Material master, general data (SAP MARA). One row per SKU (mtart FERT) plus the service material SHIPPING (mtart DIEN, no stock, no mbew row) used by the freight item 000900 of web orders and invoices.';
COMMENT ON COLUMN erp.mara.matnr IS 'Material number, 18 characters, zero-padded numeric; service materials use a text key (SHIPPING).';
COMMENT ON COLUMN erp.mara.matkl IS 'Material group per product category (see ErpWriter MATKL); KP9910 freight and services.';
COMMENT ON COLUMN erp.mara.ersda IS 'Created on: the listing date (launch date - 60 days), also for materials already listed before the simulated history starts.';
COMMENT ON COLUMN erp.mara.laeda IS 'Date of last change (the replication into this system).';
COMMENT ON COLUMN erp.mara.prdha IS 'Product hierarchy, 18 characters: brand (5) + category (5) + line (8) codes.';
COMMENT ON COLUMN erp.mara.brand_id IS 'Brand code (S/4HANA BRAND_ID).';
COMMENT ON COLUMN erp.mara.mstae IS 'Material status: Z1 = to be delisted from mstde. Blank = active.';
COMMENT ON COLUMN erp.mara.mstde IS 'Date from which mstae applies (planned delisting date).';
COMMENT ON COLUMN erp.mara.ntgew IS 'Net weight; gross weight brgew includes an assumed 10% packaging share.';

CREATE TABLE IF NOT EXISTS erp.makt (
    matnr       varchar(18)   NOT NULL,
    spras       varchar(1)    NOT NULL,              -- language key: D German, E English
    maktx       varchar(40)   NOT NULL,              -- material description (max 40 chars)
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz,
    PRIMARY KEY (matnr, spras)
);
CREATE INDEX IF NOT EXISTS makt_updated_at_idx ON erp.makt (updated_at);
COMMENT ON TABLE  erp.makt IS 'Material descriptions per language (SAP MAKT). Truncated to 40 characters as in SAP.';

CREATE TABLE IF NOT EXISTS erp.mbew (
    matnr       varchar(18)   NOT NULL,
    bwkey       varchar(4)    NOT NULL,              -- valuation area (= plant 1000)
    vprsv       varchar(1)    NOT NULL,              -- price control, S = standard price
    stprs       numeric(11,2) NOT NULL,              -- standard price in EUR per peinh units
    peinh       integer       NOT NULL,              -- price unit
    bklas       varchar(4)    NOT NULL,              -- valuation class
    laepr       date          NOT NULL,              -- date of last price change
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz,
    PRIMARY KEY (matnr, bwkey)
);
CREATE INDEX IF NOT EXISTS mbew_updated_at_idx ON erp.mbew (updated_at);
COMMENT ON TABLE  erp.mbew IS 'Material valuation (SAP MBEW): current standard cost. Revisions update the row in place; laepr is the valid-from date of the current price.';
COMMENT ON COLUMN erp.mbew.stprs IS 'Standard price in company code currency (EUR) per peinh base units.';

-- ---------------------------------------------------------------------------
-- Customer master
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS erp.kna1 (
    kunnr       varchar(10)   NOT NULL PRIMARY KEY,
    ktokd       varchar(4)    NOT NULL,              -- account group
    name1       varchar(35)   NOT NULL,
    sortl       varchar(10)   NOT NULL,              -- search term
    konzs       varchar(10),                         -- group key (retail chain)
    land1       varchar(3)    NOT NULL,
    regio       varchar(3),
    pstlz       varchar(10),
    ort01       varchar(35),
    stras       varchar(60),
    spras       varchar(1)    NOT NULL,
    stceg       varchar(20),                         -- VAT registration number
    erdat       date          NOT NULL,
    ernam       varchar(12)   NOT NULL,
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz
);
CREATE INDEX IF NOT EXISTS kna1_updated_at_idx ON erp.kna1 (updated_at);
COMMENT ON TABLE  erp.kna1 IS 'Customer master, general data (SAP KNA1): retailer sold-to accounts, their distribution centres and one collective web customer per country.';
COMMENT ON COLUMN erp.kna1.ktokd IS 'Account group: Z001 retailer sold-to, Z002 retailer ship-to (DC), Z010 web collective customer.';
COMMENT ON COLUMN erp.kna1.sortl IS 'Search term: the account short name used across the business.';
COMMENT ON COLUMN erp.kna1.konzs IS 'Group key linking a retailer and its distribution centres.';
COMMENT ON COLUMN erp.kna1.regio IS 'Region code within land1 (ISO 3166-2 subdivision part, numeric codes zero-padded to two digits).';
COMMENT ON COLUMN erp.kna1.erdat IS 'Account opened on; accounts that existed before the simulated history starts carry their (earlier) opening date.';
COMMENT ON COLUMN erp.kna1.stceg IS 'Fictional VAT id with a deliberately invalid check digit, so it never matches a real registration.';

CREATE TABLE IF NOT EXISTS erp.knmt (
    vkorg       varchar(4)    NOT NULL,
    vtweg       varchar(2)    NOT NULL,
    kunnr       varchar(10)   NOT NULL,
    matnr       varchar(18)   NOT NULL,
    kdmat       varchar(35)   NOT NULL,              -- customer's own article number
    erdat       date          NOT NULL,
    ernam       varchar(12)   NOT NULL,
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz,
    PRIMARY KEY (vkorg, vtweg, kunnr, matnr)
);
CREATE INDEX IF NOT EXISTS knmt_updated_at_idx ON erp.knmt (updated_at);
COMMENT ON TABLE  erp.knmt IS 'Customer-material info records (SAP KNMT): the retailer''s article number per material. Not complete for every listed article.';

-- ---------------------------------------------------------------------------
-- Exchange rates
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS erp.tcurr (
    kurst       varchar(4)    NOT NULL,              -- exchange rate type, M = standard
    fcurr       varchar(5)    NOT NULL,
    tcurr       varchar(5)    NOT NULL,
    gdatu       char(8)       NOT NULL,              -- valid-from date YYYYMMDD
    ukurs       numeric(9,5)  NOT NULL,
    ffact       integer       NOT NULL,
    tfact       integer       NOT NULL,
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz,
    PRIMARY KEY (kurst, fcurr, tcurr, gdatu)
);
CREATE INDEX IF NOT EXISTS tcurr_updated_at_idx ON erp.tcurr (updated_at);
COMMENT ON TABLE  erp.tcurr IS 'Exchange rates (SAP TCURR), one monthly rate per currency pair: ffact units of fcurr = ukurs * tfact units of tcurr.';
COMMENT ON COLUMN erp.tcurr.gdatu IS 'Valid-from date as YYYYMMDD text. NOT inverted: real SAP stores 99999999 minus the date so the newest rate sorts first; this simulator keeps the plain date for readability.';
COMMENT ON COLUMN erp.tcurr.ukurs IS 'Units of tcurr per unit of fcurr (direct quotation).';

-- ---------------------------------------------------------------------------
-- Sales documents
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS erp.vbak (
    vbeln       varchar(10)   NOT NULL PRIMARY KEY,
    vbtyp       varchar(1)    NOT NULL,              -- C = order
    auart       varchar(4)    NOT NULL,              -- sales document type
    vkorg       varchar(4)    NOT NULL,
    vtweg       varchar(2)    NOT NULL,
    spart       varchar(2)    NOT NULL,
    kunnr       varchar(10)   NOT NULL,              -- sold-to party
    zzkunwe     varchar(10)   NOT NULL,              -- ship-to party
    erdat       date          NOT NULL,
    erzet       time          NOT NULL,
    ernam       varchar(12)   NOT NULL,
    audat       date          NOT NULL,              -- document date
    vdatu       date,                                -- requested delivery date
    bstnk       varchar(40),                         -- customer reference
    netwr       numeric(15,2) NOT NULL,
    waerk       varchar(5)    NOT NULL,
    kurrf       numeric(9,5)  NOT NULL,
    netwr_eur   numeric(15,2) NOT NULL,
    knumv       varchar(10)   NOT NULL,              -- pricing document (erp.konv)
    gbstk       char(1)       NOT NULL DEFAULT '',  -- overall status
    lfstk       char(1)       NOT NULL DEFAULT '',  -- delivery status
    fkstk       char(1)       NOT NULL DEFAULT '',  -- billing status
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz
);
CREATE INDEX IF NOT EXISTS vbak_updated_at_idx ON erp.vbak (updated_at);
CREATE INDEX IF NOT EXISTS vbak_bstnk_idx ON erp.vbak (bstnk);
COMMENT ON TABLE  erp.vbak IS 'Sales document header (SAP VBAK): web orders mirrored from the shop and retailer purchase orders received by EDI.';
COMMENT ON COLUMN erp.vbak.auart IS 'Sales document type: ZWEB web shop order, ZEDI retailer EDI order.';
COMMENT ON COLUMN erp.vbak.vtweg IS 'Distribution channel: 10 trade, 20 e-commerce.';
COMMENT ON COLUMN erp.vbak.zzkunwe IS 'Ship-to party (SAP keeps partners in VBPA, which is not modelled).';
COMMENT ON COLUMN erp.vbak.bstnk IS 'Customer reference: the shop order UUID for web orders, the retailer PO number for EDI orders. Free text, not a key.';
COMMENT ON COLUMN erp.vbak.netwr IS 'Net value in document currency waerk (excl. tax): sum of vbap.netwr, including the freight item 000900 of web orders. netwr + sum(vbap.mwsbp) equals the shop order total.';
COMMENT ON COLUMN erp.vbak.kurrf IS 'Exchange rate to EUR: EUR per one unit of waerk (the month''s rate).';
COMMENT ON COLUMN erp.vbak.netwr_eur IS 'Net value converted to EUR at kurrf.';
COMMENT ON COLUMN erp.vbak.gbstk IS 'Overall processing status: A open, B in process, C completed (also for fully rejected documents, see vbap.abgru).';
COMMENT ON COLUMN erp.vbak.lfstk IS 'Delivery status: A not delivered, B partially delivered, C fully delivered.';
COMMENT ON COLUMN erp.vbak.fkstk IS 'Billing status: A not billed, B partially billed, C fully billed.';

CREATE TABLE IF NOT EXISTS erp.vbap (
    vbeln       varchar(10)   NOT NULL,
    posnr       varchar(6)    NOT NULL,
    matnr       varchar(18)   NOT NULL,
    arktx       varchar(40)   NOT NULL,              -- item description
    pstyv       varchar(4)    NOT NULL,              -- item category
    kwmeng      numeric(15,3) NOT NULL,              -- ordered quantity
    kbmeng      numeric(15,3) NOT NULL,              -- confirmed quantity
    vrkme       varchar(3)    NOT NULL,
    meins       varchar(3)    NOT NULL,
    kdmat       varchar(35),                         -- customer article number
    netwr       numeric(15,2) NOT NULL,
    mwsbp       numeric(15,2) NOT NULL,              -- tax amount
    wavwr       numeric(15,2) NOT NULL,              -- cost
    waerk       varchar(5)    NOT NULL,
    werks       varchar(4)    NOT NULL,
    lgort       varchar(4)    NOT NULL,
    vstel       varchar(4)    NOT NULL,
    abgru       varchar(2)    NOT NULL DEFAULT '',  -- reason for rejection
    gbsta       char(1)       NOT NULL DEFAULT '',
    lfsta       char(1)       NOT NULL DEFAULT '',
    fksta       char(1)       NOT NULL DEFAULT '',
    zzlfimg     numeric(15,3) NOT NULL DEFAULT 0,    -- delivered quantity (cumulative)
    zzfkimg     numeric(15,3) NOT NULL DEFAULT 0,    -- billed quantity (cumulative)
    erdat       date          NOT NULL,
    erzet       time          NOT NULL,
    ernam       varchar(12)   NOT NULL,
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz,
    PRIMARY KEY (vbeln, posnr)
);
CREATE INDEX IF NOT EXISTS vbap_updated_at_idx ON erp.vbap (updated_at);
CREATE INDEX IF NOT EXISTS vbap_matnr_idx ON erp.vbap (matnr);
COMMENT ON TABLE  erp.vbap IS 'Sales document items (SAP VBAP). Web orders with a shipping charge carry it as freight item 000900 (matnr SHIPPING, pstyv TAD, unit LE): not delivery-relevant (lfsta blank, blank lgort/vstel), billed once on the first invoice.';
COMMENT ON COLUMN erp.vbap.pstyv IS 'Item category: TAN standard item, TAD service item (freight).';
COMMENT ON COLUMN erp.vbap.kbmeng IS 'Confirmed quantity after the stock check (retailer orders may be cut).';
COMMENT ON COLUMN erp.vbap.wavwr IS 'Cost (standard cost x quantity) in document currency.';
COMMENT ON COLUMN erp.vbap.abgru IS 'Reason for rejection: Z1 cancelled by customer, Z2 out of stock, Z3 payment failed, ZZ other. Blank = not rejected.';
COMMENT ON COLUMN erp.vbap.gbsta IS 'Item overall status: A open, B in process, C completed or rejected.';
COMMENT ON COLUMN erp.vbap.lfsta IS 'Item delivery status relative to kbmeng: A none, B partial, C full; blank when rejected or nothing to deliver.';
COMMENT ON COLUMN erp.vbap.fksta IS 'Item billing status relative to kbmeng: A none, B partial, C full; blank when rejected or nothing to bill.';
COMMENT ON COLUMN erp.vbap.zzlfimg IS 'Cumulative delivered quantity (SAP derives this from the document flow, which is not modelled).';
COMMENT ON COLUMN erp.vbap.zzfkimg IS 'Cumulative billed quantity (SAP derives this from the document flow, which is not modelled).';

CREATE TABLE IF NOT EXISTS erp.konv (
    knumv       varchar(10)   NOT NULL,
    kposn       varchar(6)    NOT NULL,              -- item (= vbap.posnr)
    stunr       varchar(3)    NOT NULL,              -- step number in the pricing procedure
    zaehk       varchar(2)    NOT NULL,              -- condition counter
    kappl       varchar(2)    NOT NULL,              -- application, V = sales
    kschl       varchar(4)    NOT NULL,              -- condition type
    krech       varchar(1)    NOT NULL,              -- calculation type
    kbetr       numeric(15,3) NOT NULL,              -- rate
    waers       varchar(5)    NOT NULL,
    kpein       integer,                             -- pricing unit
    kmein       varchar(3),                          -- condition unit
    kwert       numeric(15,2) NOT NULL,              -- condition value
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz,
    PRIMARY KEY (knumv, kposn, stunr, zaehk)
);
CREATE INDEX IF NOT EXISTS konv_updated_at_idx ON erp.konv (updated_at);
COMMENT ON TABLE  erp.konv IS 'Pricing conditions of sales documents (SAP KONV; PRCD_ELEMENTS in S/4HANA). Join on vbak.knumv.';
COMMENT ON COLUMN erp.konv.kschl IS 'Condition type: PR00 list price, K007 customer discount, ZPRO promotion discount, HD00 freight, MWST output tax.';
COMMENT ON COLUMN erp.konv.krech IS 'Calculation type: C quantity-based (kbetr per kpein kmein), A percentage (kbetr in %), B fixed amount.';
COMMENT ON COLUMN erp.konv.kbetr IS 'Rate: unit price for PR00, percent for discounts and tax (negative for discounts; ZPRO carries the promotion''s nominal rate, kwert the cent-rounded amount), amount for freight (HD00 on the freight item 000900).';
COMMENT ON COLUMN erp.konv.kwert IS 'Condition value for the item in document currency waers (discounts negative).';

-- ---------------------------------------------------------------------------
-- Outbound deliveries
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS erp.likp (
    vbeln       varchar(10)   NOT NULL PRIMARY KEY,
    vbtyp       varchar(1)    NOT NULL,              -- J = delivery
    lfart       varchar(4)    NOT NULL,              -- delivery type
    vkorg       varchar(4)    NOT NULL,
    vstel       varchar(4)    NOT NULL,
    vsbed       varchar(2)    NOT NULL,              -- shipping conditions
    kunnr       varchar(10)   NOT NULL,              -- ship-to party
    kunag       varchar(10)   NOT NULL,              -- sold-to party
    erdat       date          NOT NULL,
    erzet       time          NOT NULL,
    ernam       varchar(12)   NOT NULL,
    lfdat       date          NOT NULL,              -- delivery date
    wadat       date          NOT NULL,              -- planned goods issue date
    wadat_ist   date,                                -- actual goods issue date
    btgew       numeric(15,3) NOT NULL,
    ntgew       numeric(15,3) NOT NULL,
    gewei       varchar(3)    NOT NULL,
    zzcarrier   varchar(35)   NOT NULL,
    bolnr       varchar(35),                         -- tracking / bill of lading number
    zzfreight_eur numeric(15,2),
    kostk       char(1)       NOT NULL DEFAULT '',  -- picking status
    wbstk       char(1)       NOT NULL DEFAULT '',  -- goods movement status
    pdstk       char(1)       NOT NULL DEFAULT '',  -- proof of delivery status
    podat       date,
    potim       time,
    zzpod_signer varchar(40),
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz
);
CREATE INDEX IF NOT EXISTS likp_updated_at_idx ON erp.likp (updated_at);
COMMENT ON TABLE  erp.likp IS 'Outbound delivery header (SAP LIKP). A cancelled delivery is soft-deleted (is_deleted); its lips rows stay as they were, so filter items through the header.';
COMMENT ON COLUMN erp.likp.vsbed IS 'Shipping conditions: 01 parcel (web), 02 pallet freight (retail).';
COMMENT ON COLUMN erp.likp.lfdat IS 'Delivery date: the requested date of the sales document, or the planned goods issue date if later.';
COMMENT ON COLUMN erp.likp.zzcarrier IS 'Carrier (parcel service or pallet forwarder) name.';
COMMENT ON COLUMN erp.likp.bolnr IS 'Carrier tracking number.';
COMMENT ON COLUMN erp.likp.zzfreight_eur IS 'Freight cost charged by the carrier, EUR.';
COMMENT ON COLUMN erp.likp.kostk IS 'Picking status: A not picked, C picked.';
COMMENT ON COLUMN erp.likp.wbstk IS 'Goods movement status: A goods issue not posted, C posted (shipped).';
COMMENT ON COLUMN erp.likp.pdstk IS 'Proof of delivery status: A not confirmed, C confirmed (carrier delivery scan or signed retailer POD).';
COMMENT ON COLUMN erp.likp.zzpod_signer IS 'Name of the person who signed the proof of delivery (retail).';

CREATE TABLE IF NOT EXISTS erp.lips (
    vbeln       varchar(10)   NOT NULL,
    posnr       varchar(6)    NOT NULL,
    pstyv       varchar(4)    NOT NULL,
    matnr       varchar(18)   NOT NULL,
    arktx       varchar(40)   NOT NULL,
    lfimg       numeric(15,3) NOT NULL,              -- delivery quantity
    pikmg       numeric(15,3) NOT NULL DEFAULT 0,    -- picked quantity
    vrkme       varchar(3)    NOT NULL,
    meins       varchar(3)    NOT NULL,
    werks       varchar(4)    NOT NULL,
    lgort       varchar(4)    NOT NULL,
    brgew       numeric(15,3) NOT NULL,
    ntgew       numeric(15,3) NOT NULL,
    gewei       varchar(3)    NOT NULL,
    vgbel       varchar(10)   NOT NULL,              -- reference document (sales order)
    vgpos       varchar(6)    NOT NULL,              -- reference item
    vgtyp       varchar(1)    NOT NULL,
    erdat       date          NOT NULL,
    erzet       time          NOT NULL,
    ernam       varchar(12)   NOT NULL,
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz,
    PRIMARY KEY (vbeln, posnr)
);
CREATE INDEX IF NOT EXISTS lips_updated_at_idx ON erp.lips (updated_at);
CREATE INDEX IF NOT EXISTS lips_vgbel_idx ON erp.lips (vgbel, vgpos);
COMMENT ON TABLE  erp.lips IS 'Outbound delivery items (SAP LIPS). posnr is 10 x the delivery line number.';
COMMENT ON COLUMN erp.lips.vgbel IS 'Sales document number (erp.vbak.vbeln) this item delivers.';
COMMENT ON COLUMN erp.lips.vgpos IS 'Sales document item (erp.vbap.posnr) this item delivers.';

-- ---------------------------------------------------------------------------
-- Billing documents (invoices F2 and credit memos G2)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS erp.vbrk (
    vbeln       varchar(10)   NOT NULL PRIMARY KEY,
    fkart       varchar(4)    NOT NULL,              -- billing type
    vbtyp       varchar(1)    NOT NULL,              -- M invoice, O credit memo
    fktyp       varchar(1)    NOT NULL,              -- L delivery-related, A order-related
    vkorg       varchar(4)    NOT NULL,
    vtweg       varchar(2)    NOT NULL,
    spart       varchar(2)    NOT NULL,
    bukrs       varchar(4)    NOT NULL,
    fkdat       date          NOT NULL,              -- billing date
    kunrg       varchar(10)   NOT NULL,              -- payer
    kunag       varchar(10)   NOT NULL,              -- sold-to party
    zterm       varchar(4)    NOT NULL,              -- terms of payment
    zfbdt       date          NOT NULL,              -- baseline date for payment
    zzdue_date  date          NOT NULL,
    netwr       numeric(15,2) NOT NULL,
    mwsbk       numeric(15,2) NOT NULL,              -- tax amount
    waerk       varchar(5)    NOT NULL,
    kurrf       numeric(9,5)  NOT NULL,
    netwr_eur   numeric(15,2) NOT NULL,
    rfbsk       varchar(1)    NOT NULL,              -- posting status to accounting
    zzaugru     varchar(3),                          -- credit memo reason
    erdat       date          NOT NULL,
    erzet       time          NOT NULL,
    ernam       varchar(12)   NOT NULL,
    zzpay_status char(1)      NOT NULL DEFAULT 'A',
    zzaugdt     date,                                -- clearing date
    zzpaid_amount numeric(15,2),
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz
);
CREATE INDEX IF NOT EXISTS vbrk_updated_at_idx ON erp.vbrk (updated_at);
COMMENT ON TABLE  erp.vbrk IS 'Billing document header (SAP VBRK): invoices (fkart F2, vbtyp M) and credit memos (fkart G2, vbtyp O). Amounts are positive for both; the sign comes from vbtyp.';
COMMENT ON COLUMN erp.vbrk.zterm IS 'Terms of payment: Z000 immediate (web, prepaid), Znnn net nnn days.';
COMMENT ON COLUMN erp.vbrk.zzdue_date IS 'Net due date (SAP derives it from zfbdt and zterm).';
COMMENT ON COLUMN erp.vbrk.kurrf IS 'Exchange rate to EUR: EUR per one unit of waerk.';
COMMENT ON COLUMN erp.vbrk.rfbsk IS 'Posting status: C = accounting document created.';
COMMENT ON COLUMN erp.vbrk.zzaugru IS 'Credit memo reason: R01 return, R02 lost in transit, R03 returned to sender, R04 cancelled after capture, R99 other.';
COMMENT ON COLUMN erp.vbrk.zzpay_status IS 'Payment clearing status (SAP keeps clearing in FI, not modelled): A open, B partially cleared, C cleared.';
COMMENT ON COLUMN erp.vbrk.zzaugdt IS 'Clearing date (local) of the customer payment.';
COMMENT ON COLUMN erp.vbrk.zzpaid_amount IS 'Amount received (gross, document currency); for trade payers net of deductions.';

CREATE TABLE IF NOT EXISTS erp.vbrp (
    vbeln       varchar(10)   NOT NULL,
    posnr       varchar(6)    NOT NULL,
    matnr       varchar(18)   NOT NULL,
    arktx       varchar(40)   NOT NULL,
    fkimg       numeric(15,3) NOT NULL,              -- billed quantity
    vrkme       varchar(3)    NOT NULL,
    meins       varchar(3)    NOT NULL,
    netwr       numeric(15,2) NOT NULL,
    mwsbp       numeric(15,2) NOT NULL,
    wavwr       numeric(15,2) NOT NULL,              -- cost in document currency
    werks       varchar(4)    NOT NULL,
    aubel       varchar(10)   NOT NULL,              -- sales document
    aupos       varchar(6)    NOT NULL,              -- sales document item
    vgbel       varchar(10),                         -- reference document
    vgpos       varchar(6),                          -- reference item
    vgtyp       varchar(1),                          -- reference document category
    erdat       date          NOT NULL,
    erzet       time          NOT NULL,
    ernam       varchar(12)   NOT NULL,
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    is_deleted  boolean       NOT NULL DEFAULT false,
    deleted_at  timestamptz,
    PRIMARY KEY (vbeln, posnr)
);
CREATE INDEX IF NOT EXISTS vbrp_updated_at_idx ON erp.vbrp (updated_at);
CREATE INDEX IF NOT EXISTS vbrp_aubel_idx ON erp.vbrp (aubel, aupos);
COMMENT ON TABLE  erp.vbrp IS 'Billing document items (SAP VBRP).';
COMMENT ON COLUMN erp.vbrp.vgbel IS 'Reference document: the delivery (vgtyp J) for invoices, the original invoice (vgtyp M) for credit memos.';
COMMENT ON COLUMN erp.vbrp.vgpos IS 'Reference item. Not populated in phase 1: to find the delivery item of an invoice item join erp.lips on lips.vbeln = vbrp.vgbel AND lips.vgpos = vbrp.aupos.';
COMMENT ON COLUMN erp.vbrp.wavwr IS 'Cost (standard cost x quantity) in document currency.';
COMMENT ON COLUMN erp.vbrp.aupos IS 'Sales document item billed; 000900 (matnr SHIPPING) is the web shipping charge, billed on the first invoice of the order.';
