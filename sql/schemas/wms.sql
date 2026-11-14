-- Warehouse and shipping system (WMS) of the central warehouse.
--
-- Conventions of this system (see docs/ARCHITECTURE.md, `wms`):
--   * integer keys (shipment 500000 + n, return 700000 + n);
--   * two-letter status / reason / movement codes;
--   * business times are Europe/Berlin local `timestamp without time zone`
--     in *_local columns (ambiguous / skipped hours around DST changes are a
--     deliberate quirk). Loading-contract columns created_at / updated_at /
--     deleted_at are timestamptz like every other schema;
--   * shipments reference the ERP delivery number (delivery_no, ten digits),
--     never the shop order;
--   * items are identified by item_code = SAP material number with leading
--     zeros stripped, plus the EAN.
--
-- No foreign keys: rows are flushed table by table and the WMS of a real
-- estate rarely enforces them either. Idempotent: safe to run repeatedly.
CREATE SCHEMA IF NOT EXISTS wms;

-- -- shipments -----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS wms.shipments (
    shipment_id        integer       PRIMARY KEY,
    delivery_no        char(10)      NOT NULL,
    shipment_type      char(2)       NOT NULL,
    status_code        char(2)       NOT NULL,
    warehouse_code     varchar(4)    NOT NULL,
    carrier_code       varchar(3)    NOT NULL,
    tracking_no        varchar(20),
    consignee_no       char(10),
    consignee_name     text,
    dest_country       char(2)       NOT NULL,
    dest_region        varchar(6)    NOT NULL,
    dest_postcode      varchar(10)   NOT NULL,
    dest_city          text          NOT NULL,
    planned_ship_date  date          NOT NULL,
    handling_units     integer       NOT NULL,
    weight_kg          numeric(10,3) NOT NULL,
    freight_cost       numeric(10,2),
    freight_currency   char(3)       NOT NULL DEFAULT 'EUR',
    pod_signed_by      text,
    created_local      timestamp     NOT NULL,
    status_local       timestamp     NOT NULL,
    picked_local       timestamp,
    shipped_local      timestamp,
    delivered_local    timestamp,
    cancelled_local    timestamp,
    created_at         timestamptz   NOT NULL,
    updated_at         timestamptz   NOT NULL,
    is_deleted         boolean       NOT NULL DEFAULT false,
    deleted_at         timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS shipments_delivery_no_idx ON wms.shipments (delivery_no);
CREATE INDEX IF NOT EXISTS shipments_updated_at_idx ON wms.shipments (updated_at);
CREATE INDEX IF NOT EXISTS shipments_tracking_no_idx ON wms.shipments (tracking_no);

COMMENT ON TABLE wms.shipments IS 'One outbound shipment per ERP delivery: parcels for web orders, pallets for retailer DCs.';
COMMENT ON COLUMN wms.shipments.delivery_no IS 'ERP delivery number (erp.likp.vbeln), ten digits with leading zeros.';
COMMENT ON COLUMN wms.shipments.shipment_type IS 'PA parcel (web order), PL pallet freight (retailer delivery).';
COMMENT ON COLUMN wms.shipments.status_code IS 'OP open, PK picked, SH shipped, IT in transit, OD out for delivery, DL delivered, EX exception, RS returned to sender, LS lost, CX cancelled.';
COMMENT ON COLUMN wms.shipments.warehouse_code IS 'Shipping warehouse; WH01 is the central warehouse.';
COMMENT ON COLUMN wms.shipments.carrier_code IS 'PBL Paketblitz, NPT Nordpost, SWB Swiftbox, FRW pallet forwarder.';
COMMENT ON COLUMN wms.shipments.tracking_no IS 'Carrier tracking or consignment number; NULL until the label is printed.';
COMMENT ON COLUMN wms.shipments.consignee_no IS 'ERP ship-to customer number (erp.kna1.kunnr); the collective web account for parcels.';
COMMENT ON COLUMN wms.shipments.consignee_name IS 'Ship-to name for retailer deliveries; NULL for consumer parcels (personal data stays in the shop).';
COMMENT ON COLUMN wms.shipments.dest_region IS 'Carrier routing zone: ISO 3166-2 subdivision code, e.g. DE-BY, AT-9, CH-ZH.';
COMMENT ON COLUMN wms.shipments.handling_units IS 'Parcels (always 1) or pallets for the shipment.';
COMMENT ON COLUMN wms.shipments.weight_kg IS 'Net goods weight from material master weights, without packaging.';
COMMENT ON COLUMN wms.shipments.freight_cost IS 'Carrier freight cost in freight_currency; NULL until known (at shipping).';
COMMENT ON COLUMN wms.shipments.created_local IS 'Europe/Berlin local time without time zone (applies to every *_local column).';
COMMENT ON COLUMN wms.shipments.status_local IS 'Local time of the latest status change.';

CREATE TABLE IF NOT EXISTS wms.shipment_lines (
    shipment_id   integer      NOT NULL,
    line_no       integer      NOT NULL,
    item_code     varchar(18)  NOT NULL,
    ean           char(13)     NOT NULL,
    qty_ordered   integer      NOT NULL,
    qty_picked    integer      NOT NULL DEFAULT 0,
    weight_kg     numeric(10,3) NOT NULL,
    created_at    timestamptz  NOT NULL,
    updated_at    timestamptz  NOT NULL,
    is_deleted    boolean      NOT NULL DEFAULT false,
    deleted_at    timestamptz,
    PRIMARY KEY (shipment_id, line_no)
);
CREATE INDEX IF NOT EXISTS shipment_lines_updated_at_idx ON wms.shipment_lines (updated_at);
CREATE INDEX IF NOT EXISTS shipment_lines_item_code_idx ON wms.shipment_lines (item_code);

COMMENT ON TABLE wms.shipment_lines IS 'Items of a shipment (one per ERP delivery item).';
COMMENT ON COLUMN wms.shipment_lines.item_code IS 'SAP material number with leading zeros stripped (erp.mara.matnr = lpad(item_code, 18, ''0'')).';
COMMENT ON COLUMN wms.shipment_lines.qty_ordered IS 'Units requested by the ERP delivery.';
COMMENT ON COLUMN wms.shipment_lines.qty_picked IS 'Units picked; 0 until picking is confirmed.';

-- -- carrier tracking ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS wms.carrier_events (
    event_id      bigint       PRIMARY KEY,
    shipment_id   integer      NOT NULL,
    scan_seq      integer      NOT NULL,
    carrier_code  varchar(3)   NOT NULL,
    tracking_no   varchar(20),
    event_code    char(2)      NOT NULL,
    event_local   timestamp    NOT NULL,
    location      text,
    detail        text,
    created_at    timestamptz  NOT NULL,
    updated_at    timestamptz  NOT NULL,
    is_deleted    boolean      NOT NULL DEFAULT false,
    deleted_at    timestamptz
);
CREATE INDEX IF NOT EXISTS carrier_events_shipment_idx ON wms.carrier_events (shipment_id, scan_seq);
CREATE INDEX IF NOT EXISTS carrier_events_updated_at_idx ON wms.carrier_events (updated_at);

COMMENT ON TABLE wms.carrier_events IS 'Tracking scans received from carriers, one row per scan.';
COMMENT ON COLUMN wms.carrier_events.event_id IS 'shipment_id * 1000 + scan_seq.';
COMMENT ON COLUMN wms.carrier_events.scan_seq IS 'Carrier scan sequence within the shipment, from 1.';
COMMENT ON COLUMN wms.carrier_events.event_code IS 'LC label created, PU picked up, HS hub scan, OD out for delivery, DL delivered, DF delivery failed, RD redelivery scheduled, RS returned to sender, PS proof of delivery signed.';
COMMENT ON COLUMN wms.carrier_events.location IS 'Hub or city reported by the carrier.';

-- -- returns -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS wms.returns (
    return_id            integer      PRIMARY KEY,
    rma_no               varchar(12)  NOT NULL,
    shipment_id          integer      NOT NULL,
    delivery_no          char(10)     NOT NULL,
    warehouse_code       varchar(4)   NOT NULL,
    status_code          char(2)      NOT NULL,
    reason_code          char(2)      NOT NULL,
    reason_text          text,
    requested_local      timestamp    NOT NULL,
    status_local         timestamp    NOT NULL,
    label_issued_local   timestamp,
    received_local       timestamp,
    inspected_local      timestamp,
    refunded_local       timestamp,
    rejected_local       timestamp,
    created_at           timestamptz  NOT NULL,
    updated_at           timestamptz  NOT NULL,
    is_deleted           boolean      NOT NULL DEFAULT false,
    deleted_at           timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS returns_rma_no_idx ON wms.returns (rma_no);
CREATE INDEX IF NOT EXISTS returns_shipment_idx ON wms.returns (shipment_id);
CREATE INDEX IF NOT EXISTS returns_updated_at_idx ON wms.returns (updated_at);

COMMENT ON TABLE wms.returns IS 'Customer returns (RMA) against an outbound shipment.';
COMMENT ON COLUMN wms.returns.rma_no IS 'Return merchandise authorisation number printed on the return label.';
COMMENT ON COLUMN wms.returns.shipment_id IS 'Original outbound shipment (wms.shipments.shipment_id).';
COMMENT ON COLUMN wms.returns.delivery_no IS 'ERP delivery number of the original shipment.';
COMMENT ON COLUMN wms.returns.status_code IS 'RQ requested, LI label issued, RC received, IN inspected, RF refunded, RJ rejected.';
COMMENT ON COLUMN wms.returns.reason_code IS 'DM damaged in transit, WI wrong item, NL did not like, AR allergic reaction.';
COMMENT ON COLUMN wms.returns.reason_text IS 'Free-text reason typed by the customer, when given (alongside the coded reason).';
COMMENT ON COLUMN wms.returns.status_local IS 'Local time of the latest status change.';

CREATE TABLE IF NOT EXISTS wms.return_lines (
    return_id       integer      NOT NULL,
    line_no         integer      NOT NULL,
    item_code       varchar(18)  NOT NULL,
    ean             char(13)     NOT NULL,
    qty             integer      NOT NULL,
    condition_code  char(2),
    created_at      timestamptz  NOT NULL,
    updated_at      timestamptz  NOT NULL,
    is_deleted      boolean      NOT NULL DEFAULT false,
    deleted_at      timestamptz,
    PRIMARY KEY (return_id, line_no)
);
CREATE INDEX IF NOT EXISTS return_lines_updated_at_idx ON wms.return_lines (updated_at);

COMMENT ON TABLE wms.return_lines IS 'Items announced on a return.';
COMMENT ON COLUMN wms.return_lines.item_code IS 'SAP material number with leading zeros stripped.';
COMMENT ON COLUMN wms.return_lines.condition_code IS 'Set at inspection: OK resaleable (back to stock), DM damaged (scrapped). NULL before inspection.';

-- -- stock ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS wms.stock_movements (
    movement_id     bigint       PRIMARY KEY,
    warehouse_code  varchar(4)   NOT NULL,
    movement_code   char(2)      NOT NULL,
    item_code       varchar(18)  NOT NULL,
    ean             char(13)     NOT NULL,
    qty             integer      NOT NULL,
    movement_local  timestamp    NOT NULL,
    delivery_no     char(10),
    return_id       integer,
    reference       text,
    created_at      timestamptz  NOT NULL,
    updated_at      timestamptz  NOT NULL,
    is_deleted      boolean      NOT NULL DEFAULT false,
    deleted_at      timestamptz
);
CREATE INDEX IF NOT EXISTS stock_movements_item_idx ON wms.stock_movements (item_code, movement_local);
CREATE INDEX IF NOT EXISTS stock_movements_updated_at_idx ON wms.stock_movements (updated_at);

COMMENT ON TABLE wms.stock_movements IS 'Stock postings in the central warehouse.';
COMMENT ON COLUMN wms.stock_movements.movement_code IS 'GR goods receipt (production), GI goods issue (shipment), RT customer return to stock, SC scrap, AD inventory adjustment.';
COMMENT ON COLUMN wms.stock_movements.qty IS 'Signed units: positive into stock, negative out of stock.';
COMMENT ON COLUMN wms.stock_movements.delivery_no IS 'ERP delivery number for goods issues; NULL otherwise.';
COMMENT ON COLUMN wms.stock_movements.return_id IS 'wms.returns.return_id for return and scrap postings; NULL otherwise.';

CREATE TABLE IF NOT EXISTS wms.stock_levels_daily (
    snapshot_date      date         NOT NULL,
    warehouse_code     varchar(4)   NOT NULL,
    item_code          varchar(18)  NOT NULL,
    load_id            bigint       NOT NULL,
    ean                char(13)     NOT NULL,
    qty_on_hand        integer      NOT NULL,
    qty_reserved       integer      NOT NULL,
    qty_available      integer      NOT NULL,
    qty_in_production  integer      NOT NULL,
    snapshot_local     timestamp    NOT NULL,
    created_at         timestamptz  NOT NULL,
    updated_at         timestamptz  NOT NULL,
    PRIMARY KEY (snapshot_date, warehouse_code, item_code, load_id)
);
CREATE INDEX IF NOT EXISTS stock_levels_daily_updated_at_idx ON wms.stock_levels_daily (updated_at);
CREATE INDEX IF NOT EXISTS stock_levels_daily_load_idx ON wms.stock_levels_daily (load_id);

COMMENT ON TABLE wms.stock_levels_daily IS 'End-of-day stock snapshot per item. Insert-only: a restatement arrives as new rows with a new load_id.';
COMMENT ON COLUMN wms.stock_levels_daily.snapshot_date IS 'Local business date the snapshot describes.';
COMMENT ON COLUMN wms.stock_levels_daily.load_id IS 'Snapshot run: YYYYMMDD of snapshot_date followed by a two-digit run number (01 = first run).';
COMMENT ON COLUMN wms.stock_levels_daily.qty_reserved IS 'Units reserved for open deliveries.';
COMMENT ON COLUMN wms.stock_levels_daily.qty_available IS 'qty_on_hand - qty_reserved (may be negative when reservations exceed stock).';
COMMENT ON COLUMN wms.stock_levels_daily.qty_in_production IS 'Units on open production orders, not yet received.';
COMMENT ON COLUMN wms.stock_levels_daily.snapshot_local IS 'Local time the snapshot was taken.';
