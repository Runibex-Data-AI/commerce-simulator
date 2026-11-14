# Loading the simulator into Incorta

How to connect an Incorta cluster to the simulator's Postgres database and keep it current with
query-based incremental loads on `updated_at`. Written for Incorta **8.2.2** (release
v8.2.2-2026.8, a service pack of v8.2.0) and simulator **v0.1.0** (phase 1: `shop`, `wms`, `erp`).

Statements about Incorta behaviour are taken from the Incorta documentation and linked in
[Version notes](#version-notes). Anything the documentation does not confirm is marked
**Not confirmed** and should be checked on your cluster. All SQL below has been run against the
v0.1.0 DDL on PostgreSQL 16.

## Before you start

- The simulator is running and its backfill has finished: `docker compose exec generator sim
  status` shows the clock at wall time, and the generator log says it is running live. Make the
  first full load after that, so it reads the whole history at once.
- You have the value of `CPGSIM_READER_PASSWORD` from the simulator's `.env`.
- Your Incorta user has the Schema Manager role (needed for load plans) and can create data
  sources.
- Port 5432 of the simulator host is reachable from the Incorta cluster and from nowhere else.
  Postgres accepts `bi_reader` over SSL from any address (see
  [docker/postgres/initdb/10-cpgsim.sh](../../docker/postgres/initdb/10-cpgsim.sh)), so the
  firewall or security group in front of it is the access control: allow only the cluster's
  outbound IP addresses, which your Incorta administrator can give you. If the database must
  not be reachable from outside your network at all, Incorta's Data Agent (the **Use Data
  Agent** option on the data source) is the documented alternative; it is not covered here.

## What is loaded

| Postgres schema | Tables | Load |
| --- | --- | --- |
| `shop` | 9 | Incremental on `updated_at` |
| `wms` | 6 | Incremental on `updated_at` |
| `wms` | `stock_levels_daily` | Full load only, once a day |
| `erp` | 13 | Incremental on `updated_at` |
| `mkt`, `feeds`, `shelf` | none yet | Empty in v0.1.0; tables arrive in phases 2 and 3 |
| `sim`, `truth` | | Not granted to `bi_reader`; cannot be loaded |

The simulator guarantees one loading contract for every business table (see the
[README](../../README.md#loading-it-into-a-bi-tool) and
[SPEC](../../docs/SPEC.md#loading-contract-for-bi-tools)): a primary key, `created_at` and
`updated_at` on every row with `updated_at` indexed and moved on every change, no hard deletes,
and insert-only snapshot tables. That is what makes the plain query-based setup below complete.

## 1. Data source

Create it under **Data > + New > Add Data Source > Database > PostgreSQL**.

| Field | Value |
| --- | --- |
| Data Source Name | `cpgsim` |
| Username | `bi_reader` |
| Password | the value of `CPGSIM_READER_PASSWORD` (or a Password Identifier if you use **Use Secret Manager**) |
| Connection String | `jdbc:postgresql://<host>:5432/cpgsim?sslmode=require` |
| Connection Pool | `10` (default 30; see below) |
| Connection Properties | empty |
| Log-based Incremental Load | off |
| Extra Options | empty |

The PostgreSQL connector has no separate host, port or database fields: they go into the
Connection String. Use the port you published with `CPGSIM_PG_PORT` if it is not 5432, and the
database name from `POSTGRES_DB` if you changed it.

**SSL.** The server only accepts `bi_reader` over SSL with SCRAM-SHA-256 passwords and TLS 1.2
or later; plain TCP connections are rejected. On first start the Postgres container creates a
**self-signed** certificate (`CN=cpgsim-postgres`, 10 years). Set `sslmode=require` in the
Connection String, as above: the connection is encrypted, but the driver does not check the
certificate or the host name. Do not rely on the driver default: it depends on the driver
version the connector ships (see **Connector version** below; check the installed connector
version in the marketplace) and never enforces SSL.

Use `sslmode=verify-full` only if you have replaced `server.crt` and `server.key` in the
Postgres data directory with a certificate signed by a CA whose name matches the host in the
Connection String, and the CA's root certificate is available to the driver (`sslrootcert`, a
file path on the machine that runs the Incorta loader). With the self-signed certificate,
`verify-full` fails. Incorta has no PostgreSQL certificate upload field, so this needs file
access to the loader host.

Put driver options into the Connection String as URL parameters
(`?sslmode=require&sslResponseTimeout=10000`). The Connection Properties field takes
`key=value` pairs, but how to separate several of them is **not confirmed** in the docs.

**Connector version.** Earlier PostgreSQL connector versions use JDBC driver 42.2.1;
connector 2.2.6.0 and later ships driver 42.7.8, whose SSL handshake timeout defaults to 5
seconds. If connections fail intermittently with "The
connection attempt failed", add `sslResponseTimeout=10000` (milliseconds) to the Connection
String.

**Connection pool.** The default of 30 is more concurrent queries than the default simulator VM
(2 vCPU, 2 GB RAM, Postgres limited to 1.2 CPU) can serve well during a full load. Ten is our
suggestion, not an Incorta recommendation; raise it if the database has more room. Postgres
allows 100 connections by default, and the generator needs only a few.

## 2. Incorta schemas and tables

Create four Incorta schemas:

| Incorta schema | Postgres tables | Why separate |
| --- | --- | --- |
| `cpgsim_shop` | all of `shop` | |
| `cpgsim_wms` | all of `wms` except `stock_levels_daily` | |
| `cpgsim_erp` | all of `erp` | |
| `cpgsim_wms_snapshots` | `wms.stock_levels_daily` | Full load only, on its own schedule |

An incremental load job fully reloads every table in the schema that has incremental load
disabled. Keeping the snapshot table in `cpgsim_wms` would therefore re-extract it completely on
every 15-minute run. A separate schema keeps it in its own daily plan using only documented
load plan features. Table-level scheduling is the alternative (see
[Load plans](#3-load-plans)).

### Settings for every incremental table

| Field | Value |
| --- | --- |
| Type | SQL Database |
| Data Source | `cpgsim` |
| Incremental | on |
| Incremental Type | Query-based |
| Incremental Extract Using | Maximum Value of a Column |
| Incremental Column | `updated_at` |
| Incremental Field Type | Timestamp |
| Fetch Size | 5000 (the default) |
| Query | the full query for the table, below |
| Update Query | the same query with `WHERE updated_at > ?`, below |

Then open the table in the Table Editor and set **Function** to **key** on each primary key
column listed for the table. With keys, every incremental run upserts: new rows are appended
and changed rows replace the version already loaded. Without keys, each run appends, and the
table fills up with duplicates.

Leave **Enforce Primary Key Constraint** (on the table's Advanced Settings tab since 2024.7.x)
at its default, off. It affects full loads only: incremental loads of a table with key columns
deduplicate on the key regardless of the setting. The Postgres primary keys already guarantee
unique key values, so a full load with the setting off does not finish with the duplicate-key
errors the Table Editor documentation describes.

**Why Maximum Value of a Column.** The loader replaces `?` with the largest `updated_at` already
in Incorta for that table. When the simulator advances with `sim run` or `sim start`, including
the catch-up after the generator was down, the generator stamps `created_at` and `updated_at`
with the time of the commit that writes the row (one stamp per commit, never earlier than the
simulated time it writes up to), only one generator writes, and all rows of a commit become
visible together. So, on the database side, no row can become visible later with an
`updated_at` below a value already loaded. The default strategy, Last Successful Extract Time,
uses the time the previous extraction started; a commit that takes its time stamp just before
that moment but becomes visible just after it is skipped. Incorta's own documentation warns
about this and recommends Maximum Value of a Column.

Two conditions limit this:

- **Not confirmed:** how Incorta binds the stored maximum into `?` for a `timestamptz` column
  (as a JDBC timestamp, or as a literal in the loader's JVM or session time zone) is not
  documented. A wrong offset would silently skip or re-read hours of changes. Run the
  [freshness check](#row-counts-and-freshness) after the first two incremental runs and again
  after the first daylight saving change, and compare `max(updated_at)` and the row count of
  every table with Postgres. This step is required, not optional.
- Only `sim run` and `sim start` stamp with commit time. `sim backfill` on an existing
  simulation (for example with `--until`) keeps stamping rows with their simulated event time,
  so rows it writes after Incorta has loaded can carry an `updated_at` at or below the
  watermark, and `updated_at > ?` never reads them. Once Incorta loads the database, advance
  the simulator only with `sim run` or `sim start`. After any `sim backfill` on a loaded
  database, proceed as after a reset ([After `sim reset`](#after-sim-reset)).

**Why the queries are written the way they are.** The column lists are explicit, so a column
added in a later simulator version does not change the table silently (Incorta requires a full
load after any column change anyway). Four expressions differ from a plain column name:

- `date_trunc('milliseconds', updated_at) AS updated_at`: Incorta's timestamp-based incremental
  strategies work at millisecond precision, Postgres stores microseconds. Truncating in the
  query keeps the watermark at or below the true value, so no row is missed through rounding
  (provided `?` is bound as expected; see above). The `WHERE` clause compares the raw, indexed
  column against it, so each run reads the rows of the last commit it already has once more;
  the key deduplicates them.
- `id::text AS id` for every `uuid` column in `shop`: keys and joins as strings, in the same
  text form as `erp.vbak.bstnk`.
- `array_to_string(skus, ',') AS skus` and the same for `quantities` (`text[]` and `integer[]`
  in Postgres): comma-separated text.
- `erzet::text AS erzet` and `potim::text AS potim` (SAP times, `time` in Postgres): `HH:MM:SS`
  text.

How Incorta maps the Postgres `uuid`, array and `time` types is **not confirmed** in its
documentation; the casts give plain string columns whatever the mapping.

Both queries of a table must return the same columns, and the Incremental Column must be in
both; the pairs below follow that.

**Soft-deleted and cancelled rows.** Nothing is ever deleted at the source, so load every row,
including `is_deleted = true`, and filter `is_deleted = false` in the business layer
(business schema views or insights). Cancellations and rejections are states, not deletions:
`shop.orders.status`, `erp.vbap.abgru`, `wms.shipments.status_code = 'CX'`. A cancelled ERP
delivery is soft-deleted on `erp.likp` and on its `erp.lips` rows in the same commit, so both
arrive in the same incremental run; filtering delivery items through their header as well is
still a safe habit. A GDPR erasure is an update (`shop.customers.anonymised_at`)
and arrives like any other change.

### `shop`

| Table | Key columns | Load |
| --- | --- | --- |
| `customers` | `id` | Incremental |
| `addresses` | `id` | Incremental |
| `promotions` | `id` | Incremental |
| `subscriptions` | `id` | Incremental |
| `orders` | `id` | Incremental |
| `order_lines` | `id` | Incremental |
| `payments` | `id` | Incremental |
| `refunds` | `id` | Incremental |
| `promo_redemptions` | `promotion_id`, `order_id` | Incremental |

#### `shop.customers`

```sql
-- Query
SELECT
  id::text AS id, email, first_name, last_name, phone, birth_year, locale, country,
  marketing_opt_in, signup_source, influencer_code, status, registered_at, anonymised_at,
  created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.customers

-- Update Query
SELECT
  id::text AS id, email, first_name, last_name, phone, birth_year, locale, country,
  marketing_opt_in, signup_source, influencer_code, status, registered_at, anonymised_at,
  created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.customers
WHERE updated_at > ?
```

#### `shop.addresses`

```sql
-- Query
SELECT
  id::text AS id, customer_id::text AS customer_id, address_type, is_default, first_name,
  last_name, street, house_number, postcode, city, country, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.addresses

-- Update Query
SELECT
  id::text AS id, customer_id::text AS customer_id, address_type, is_default, first_name,
  last_name, street, house_number, postcode, city, country, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.addresses
WHERE updated_at > ?
```

#### `shop.promotions`

```sql
-- Query
SELECT
  id::text AS id, name, promo_type, code, discount_pct, free_shipping, array_to_string(skus,
  ',') AS skus, brand, starts_at, ends_at, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.promotions

-- Update Query
SELECT
  id::text AS id, name, promo_type, code, discount_pct, free_shipping, array_to_string(skus,
  ',') AS skus, brand, starts_at, ends_at, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.promotions
WHERE updated_at > ?
```

#### `shop.subscriptions`

```sql
-- Query
SELECT
  id::text AS id, customer_id::text AS customer_id, status, interval_weeks,
  array_to_string(skus, ',') AS skus, array_to_string(quantities, ',') AS quantities,
  started_at, next_order_at, paused_at, cancelled_at, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.subscriptions

-- Update Query
SELECT
  id::text AS id, customer_id::text AS customer_id, status, interval_weeks,
  array_to_string(skus, ',') AS skus, array_to_string(quantities, ',') AS quantities,
  started_at, next_order_at, paused_at, cancelled_at, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.subscriptions
WHERE updated_at > ?
```

#### `shop.orders`

```sql
-- Query
SELECT
  id::text AS id, order_number, customer_id::text AS customer_id, status, currency, country,
  placed_at, items_subtotal, discount_total, shipping_fee, tax_total, total_amount,
  promo_code, promotion_id::text AS promotion_id, source, payment_method,
  shipping_address_id::text AS shipping_address_id, billing_address_id::text AS
  billing_address_id, subscription_id::text AS subscription_id, is_repeat_customer,
  item_count, paid_at, payment_failed_at, released_at, cancelled_at, cancel_reason,
  partially_shipped_at, shipped_at, delivered_at, return_requested_at, returned_at,
  return_rejected_at, refunded_at, created_at, date_trunc('milliseconds', updated_at) AS
  updated_at, is_deleted, deleted_at
FROM shop.orders

-- Update Query
SELECT
  id::text AS id, order_number, customer_id::text AS customer_id, status, currency, country,
  placed_at, items_subtotal, discount_total, shipping_fee, tax_total, total_amount,
  promo_code, promotion_id::text AS promotion_id, source, payment_method,
  shipping_address_id::text AS shipping_address_id, billing_address_id::text AS
  billing_address_id, subscription_id::text AS subscription_id, is_repeat_customer,
  item_count, paid_at, payment_failed_at, released_at, cancelled_at, cancel_reason,
  partially_shipped_at, shipped_at, delivered_at, return_requested_at, returned_at,
  return_rejected_at, refunded_at, created_at, date_trunc('milliseconds', updated_at) AS
  updated_at, is_deleted, deleted_at
FROM shop.orders
WHERE updated_at > ?
```

#### `shop.order_lines`

```sql
-- Query
SELECT
  id::text AS id, order_id::text AS order_id, line_no, sku, ean, product_name, quantity,
  unit_price, discount_amount, tax_rate, tax_amount, line_total, qty_shipped, qty_cancelled,
  qty_returned, created_at, date_trunc('milliseconds', updated_at) AS updated_at,
  is_deleted, deleted_at
FROM shop.order_lines

-- Update Query
SELECT
  id::text AS id, order_id::text AS order_id, line_no, sku, ean, product_name, quantity,
  unit_price, discount_amount, tax_rate, tax_amount, line_total, qty_shipped, qty_cancelled,
  qty_returned, created_at, date_trunc('milliseconds', updated_at) AS updated_at,
  is_deleted, deleted_at
FROM shop.order_lines
WHERE updated_at > ?
```

#### `shop.payments`

```sql
-- Query
SELECT
  id::text AS id, order_id::text AS order_id, attempt_no, method, provider, status, amount,
  currency, provider_reference, authorised_at, captured_at, captured_amount, failed_at,
  failure_reason, voided_at, refunded_at, refunded_amount, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.payments

-- Update Query
SELECT
  id::text AS id, order_id::text AS order_id, attempt_no, method, provider, status, amount,
  currency, provider_reference, authorised_at, captured_at, captured_amount, failed_at,
  failure_reason, voided_at, refunded_at, refunded_amount, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.payments
WHERE updated_at > ?
```

#### `shop.refunds`

```sql
-- Query
SELECT
  id::text AS id, order_id::text AS order_id, payment_id::text AS payment_id, amount,
  currency, reason, status, issued_at, completed_at, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.refunds

-- Update Query
SELECT
  id::text AS id, order_id::text AS order_id, payment_id::text AS payment_id, amount,
  currency, reason, status, issued_at, completed_at, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.refunds
WHERE updated_at > ?
```

#### `shop.promo_redemptions`

```sql
-- Query
SELECT
  promotion_id::text AS promotion_id, order_id::text AS order_id, customer_id::text AS
  customer_id, code, discount_amount, currency, redeemed_at, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.promo_redemptions

-- Update Query
SELECT
  promotion_id::text AS promotion_id, order_id::text AS order_id, customer_id::text AS
  customer_id, code, discount_amount, currency, redeemed_at, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM shop.promo_redemptions
WHERE updated_at > ?
```

### `wms`

| Table | Key columns | Load |
| --- | --- | --- |
| `shipments` | `shipment_id` | Incremental |
| `shipment_lines` | `shipment_id`, `line_no` | Incremental |
| `carrier_events` | `event_id` | Incremental |
| `returns` | `return_id` | Incremental |
| `return_lines` | `return_id`, `line_no` | Incremental |
| `stock_movements` | `movement_id` | Incremental |
| `stock_levels_daily` | `snapshot_date`, `warehouse_code`, `item_code`, `load_id` | Full only, in `cpgsim_wms_snapshots` |

#### `wms.shipments`

```sql
-- Query
SELECT
  shipment_id, delivery_no, shipment_type, status_code, warehouse_code, carrier_code,
  tracking_no, consignee_no, consignee_name, dest_country, dest_region, dest_postcode,
  dest_city, planned_ship_date, handling_units, weight_kg, freight_cost, freight_currency,
  pod_signed_by, created_local, status_local, picked_local, shipped_local, delivered_local,
  cancelled_local, created_at, date_trunc('milliseconds', updated_at) AS updated_at,
  is_deleted, deleted_at
FROM wms.shipments

-- Update Query
SELECT
  shipment_id, delivery_no, shipment_type, status_code, warehouse_code, carrier_code,
  tracking_no, consignee_no, consignee_name, dest_country, dest_region, dest_postcode,
  dest_city, planned_ship_date, handling_units, weight_kg, freight_cost, freight_currency,
  pod_signed_by, created_local, status_local, picked_local, shipped_local, delivered_local,
  cancelled_local, created_at, date_trunc('milliseconds', updated_at) AS updated_at,
  is_deleted, deleted_at
FROM wms.shipments
WHERE updated_at > ?
```

#### `wms.shipment_lines`

```sql
-- Query
SELECT
  shipment_id, line_no, item_code, ean, qty_ordered, qty_picked, weight_kg, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM wms.shipment_lines

-- Update Query
SELECT
  shipment_id, line_no, item_code, ean, qty_ordered, qty_picked, weight_kg, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM wms.shipment_lines
WHERE updated_at > ?
```

#### `wms.carrier_events`

```sql
-- Query
SELECT
  event_id, shipment_id, scan_seq, carrier_code, tracking_no, event_code, event_local,
  location, detail, created_at, date_trunc('milliseconds', updated_at) AS updated_at,
  is_deleted, deleted_at
FROM wms.carrier_events

-- Update Query
SELECT
  event_id, shipment_id, scan_seq, carrier_code, tracking_no, event_code, event_local,
  location, detail, created_at, date_trunc('milliseconds', updated_at) AS updated_at,
  is_deleted, deleted_at
FROM wms.carrier_events
WHERE updated_at > ?
```

#### `wms.returns`

```sql
-- Query
SELECT
  return_id, rma_no, shipment_id, delivery_no, warehouse_code, status_code, reason_code,
  reason_text, requested_local, status_local, label_issued_local, received_local,
  inspected_local, refunded_local, rejected_local, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM wms.returns

-- Update Query
SELECT
  return_id, rma_no, shipment_id, delivery_no, warehouse_code, status_code, reason_code,
  reason_text, requested_local, status_local, label_issued_local, received_local,
  inspected_local, refunded_local, rejected_local, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM wms.returns
WHERE updated_at > ?
```

#### `wms.return_lines`

```sql
-- Query
SELECT
  return_id, line_no, item_code, ean, qty, condition_code, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM wms.return_lines

-- Update Query
SELECT
  return_id, line_no, item_code, ean, qty, condition_code, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM wms.return_lines
WHERE updated_at > ?
```

#### `wms.stock_movements`

```sql
-- Query
SELECT
  movement_id, warehouse_code, movement_code, item_code, ean, qty, movement_local,
  delivery_no, return_id, reference, created_at, date_trunc('milliseconds', updated_at) AS
  updated_at, is_deleted, deleted_at
FROM wms.stock_movements

-- Update Query
SELECT
  movement_id, warehouse_code, movement_code, item_code, ean, qty, movement_local,
  delivery_no, return_id, reference, created_at, date_trunc('milliseconds', updated_at) AS
  updated_at, is_deleted, deleted_at
FROM wms.stock_movements
WHERE updated_at > ?
```

### `erp`

| Table | Key columns | Load |
| --- | --- | --- |
| `mara` | `matnr` | Incremental |
| `makt` | `matnr`, `spras` | Incremental |
| `mbew` | `matnr`, `bwkey` | Incremental |
| `kna1` | `kunnr` | Incremental |
| `knmt` | `vkorg`, `vtweg`, `kunnr`, `matnr` | Incremental |
| `tcurr` | `kurst`, `fcurr`, `tcurr`, `gdatu` | Incremental |
| `vbak` | `vbeln` | Incremental |
| `vbap` | `vbeln`, `posnr` | Incremental |
| `konv` | `knumv`, `kposn`, `stunr`, `zaehk` | Incremental |
| `likp` | `vbeln` | Incremental |
| `lips` | `vbeln`, `posnr` | Incremental |
| `vbrk` | `vbeln` | Incremental |
| `vbrp` | `vbeln`, `posnr` | Incremental |

#### `erp.mara`

```sql
-- Query
SELECT
  matnr, mtart, mbrsh, matkl, meins, ean11, numtp, prdha, brand_id, groes, brgew, ntgew,
  gewei, ersda, ernam, laeda, mstae, mstde, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.mara

-- Update Query
SELECT
  matnr, mtart, mbrsh, matkl, meins, ean11, numtp, prdha, brand_id, groes, brgew, ntgew,
  gewei, ersda, ernam, laeda, mstae, mstde, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.mara
WHERE updated_at > ?
```

#### `erp.makt`

```sql
-- Query
SELECT
  matnr, spras, maktx, created_at, date_trunc('milliseconds', updated_at) AS updated_at,
  is_deleted, deleted_at
FROM erp.makt

-- Update Query
SELECT
  matnr, spras, maktx, created_at, date_trunc('milliseconds', updated_at) AS updated_at,
  is_deleted, deleted_at
FROM erp.makt
WHERE updated_at > ?
```

#### `erp.mbew`

```sql
-- Query
SELECT
  matnr, bwkey, vprsv, stprs, peinh, bklas, laepr, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.mbew

-- Update Query
SELECT
  matnr, bwkey, vprsv, stprs, peinh, bklas, laepr, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.mbew
WHERE updated_at > ?
```

#### `erp.kna1`

```sql
-- Query
SELECT
  kunnr, ktokd, name1, sortl, konzs, land1, regio, pstlz, ort01, stras, spras, stceg, erdat,
  ernam, created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted,
  deleted_at
FROM erp.kna1

-- Update Query
SELECT
  kunnr, ktokd, name1, sortl, konzs, land1, regio, pstlz, ort01, stras, spras, stceg, erdat,
  ernam, created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted,
  deleted_at
FROM erp.kna1
WHERE updated_at > ?
```

#### `erp.knmt`

```sql
-- Query
SELECT
  vkorg, vtweg, kunnr, matnr, kdmat, erdat, ernam, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.knmt

-- Update Query
SELECT
  vkorg, vtweg, kunnr, matnr, kdmat, erdat, ernam, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.knmt
WHERE updated_at > ?
```

#### `erp.tcurr`

```sql
-- Query
SELECT
  kurst, fcurr, tcurr, gdatu, ukurs, ffact, tfact, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.tcurr

-- Update Query
SELECT
  kurst, fcurr, tcurr, gdatu, ukurs, ffact, tfact, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.tcurr
WHERE updated_at > ?
```

#### `erp.vbak`

```sql
-- Query
SELECT
  vbeln, vbtyp, auart, vkorg, vtweg, spart, kunnr, zzkunwe, erdat, erzet::text AS erzet,
  ernam, audat, vdatu, bstnk, netwr, waerk, kurrf, netwr_eur, knumv, gbstk, lfstk, fkstk,
  created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.vbak

-- Update Query
SELECT
  vbeln, vbtyp, auart, vkorg, vtweg, spart, kunnr, zzkunwe, erdat, erzet::text AS erzet,
  ernam, audat, vdatu, bstnk, netwr, waerk, kurrf, netwr_eur, knumv, gbstk, lfstk, fkstk,
  created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.vbak
WHERE updated_at > ?
```

#### `erp.vbap`

```sql
-- Query
SELECT
  vbeln, posnr, matnr, arktx, pstyv, kwmeng, kbmeng, vrkme, meins, kdmat, netwr, mwsbp,
  wavwr, waerk, werks, lgort, vstel, abgru, gbsta, lfsta, fksta, zzlfimg, zzfkimg, erdat,
  erzet::text AS erzet, ernam, created_at, date_trunc('milliseconds', updated_at) AS
  updated_at, is_deleted, deleted_at
FROM erp.vbap

-- Update Query
SELECT
  vbeln, posnr, matnr, arktx, pstyv, kwmeng, kbmeng, vrkme, meins, kdmat, netwr, mwsbp,
  wavwr, waerk, werks, lgort, vstel, abgru, gbsta, lfsta, fksta, zzlfimg, zzfkimg, erdat,
  erzet::text AS erzet, ernam, created_at, date_trunc('milliseconds', updated_at) AS
  updated_at, is_deleted, deleted_at
FROM erp.vbap
WHERE updated_at > ?
```

#### `erp.konv`

```sql
-- Query
SELECT
  knumv, kposn, stunr, zaehk, kappl, kschl, krech, kbetr, waers, kpein, kmein, kwert,
  created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.konv

-- Update Query
SELECT
  knumv, kposn, stunr, zaehk, kappl, kschl, krech, kbetr, waers, kpein, kmein, kwert,
  created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.konv
WHERE updated_at > ?
```

#### `erp.likp`

```sql
-- Query
SELECT
  vbeln, vbtyp, lfart, vkorg, vstel, vsbed, kunnr, kunag, erdat, erzet::text AS erzet,
  ernam, lfdat, wadat, wadat_ist, btgew, ntgew, gewei, zzcarrier, bolnr, zzfreight_eur,
  kostk, wbstk, pdstk, podat, potim::text AS potim, zzpod_signer, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.likp

-- Update Query
SELECT
  vbeln, vbtyp, lfart, vkorg, vstel, vsbed, kunnr, kunag, erdat, erzet::text AS erzet,
  ernam, lfdat, wadat, wadat_ist, btgew, ntgew, gewei, zzcarrier, bolnr, zzfreight_eur,
  kostk, wbstk, pdstk, podat, potim::text AS potim, zzpod_signer, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.likp
WHERE updated_at > ?
```

#### `erp.lips`

```sql
-- Query
SELECT
  vbeln, posnr, pstyv, matnr, arktx, lfimg, pikmg, vrkme, meins, werks, lgort, brgew, ntgew,
  gewei, vgbel, vgpos, vgtyp, erdat, erzet::text AS erzet, ernam, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.lips

-- Update Query
SELECT
  vbeln, posnr, pstyv, matnr, arktx, lfimg, pikmg, vrkme, meins, werks, lgort, brgew, ntgew,
  gewei, vgbel, vgpos, vgtyp, erdat, erzet::text AS erzet, ernam, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.lips
WHERE updated_at > ?
```

#### `erp.vbrk`

```sql
-- Query
SELECT
  vbeln, fkart, vbtyp, fktyp, vkorg, vtweg, spart, bukrs, fkdat, kunrg, kunag, zterm, zfbdt,
  zzdue_date, netwr, mwsbk, waerk, kurrf, netwr_eur, rfbsk, zzaugru, erdat, erzet::text AS
  erzet, ernam, zzpay_status, zzaugdt, zzpaid_amount, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.vbrk

-- Update Query
SELECT
  vbeln, fkart, vbtyp, fktyp, vkorg, vtweg, spart, bukrs, fkdat, kunrg, kunag, zterm, zfbdt,
  zzdue_date, netwr, mwsbk, waerk, kurrf, netwr_eur, rfbsk, zzaugru, erdat, erzet::text AS
  erzet, ernam, zzpay_status, zzaugdt, zzpaid_amount, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.vbrk
WHERE updated_at > ?
```

#### `erp.vbrp`

```sql
-- Query
SELECT
  vbeln, posnr, matnr, arktx, fkimg, vrkme, meins, netwr, mwsbp, wavwr, werks, aubel, aupos,
  vgbel, vgpos, vgtyp, erdat, erzet::text AS erzet, ernam, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.vbrp

-- Update Query
SELECT
  vbeln, posnr, matnr, arktx, fkimg, vrkme, meins, netwr, mwsbp, wavwr, werks, aubel, aupos,
  vgbel, vgpos, vgtyp, erdat, erzet::text AS erzet, ernam, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.vbrp
WHERE updated_at > ?
```

### Snapshot table: `wms.stock_levels_daily`

`wms.stock_levels_daily` is insert-only: the end-of-day stock per item, taken at 23:55
Europe/Berlin. A restatement arrives as new rows with a higher `load_id`, never as an update.
In schema `cpgsim_wms_snapshots`, set **Incremental** off, leave Update Query empty, and set the
four key columns (`snapshot_date`, `warehouse_code`, `item_code`, `load_id`, the Postgres
primary key; Enforce Primary Key Constraint stays off here too). The table is small (one row per item a day, on the order of 130,000 rows after
a 24-month backfill), so a daily full load is cheap and picks up restatements without any
special handling.

```sql
-- Query
SELECT
  snapshot_date, warehouse_code, item_code, load_id, ean, qty_on_hand, qty_reserved,
  qty_available, qty_in_production, snapshot_local, created_at, updated_at
FROM wms.stock_levels_daily
```

For analysis, use the highest `load_id` per `snapshot_date`, `warehouse_code` and `item_code`.

Do not switch on **Disable Full Load** for this table. That setting is for incremental tables
that must never be fully reloaded; it does not make a table full-load-only. Leaving Incremental
off does.

### Fallback: Last Successful Extract Time

Use this only if you cannot use Maximum Value of a Column. Set **Incremental Extract Using** to
Last Successful Extract Time, keep the same Query, and widen the Update Query by an overlap so
that commits in flight during the previous extraction are read again (the key deduplicates
them). For example:

```sql
WHERE updated_at > CAST(? AS timestamptz) - INTERVAL '1 minute'
```

The cast is needed: Postgres cannot infer the parameter type from `? - INTERVAL ...` alone.
Incorta documents this value as the loader's own time stamp of starting the last successful
extraction, so any clock difference between the Incorta loader and the simulator host counts
directly against the overlap. The overlap must exceed that clock skew plus the longest commit
(seconds during a catch-up); one minute assumes both hosts are NTP-synchronised, so increase it
if they are not. The same **not confirmed** binding of `?` against `timestamptz` applies as for
Maximum Value of a Column (see
[Why Maximum Value of a Column](#settings-for-every-incremental-table)); check the result with
the freshness check below.

## 3. Load plans

Load plans are the scheduling mechanism in 8.2. Create them under **Scheduler > Load Plans >
+ New > Add Load Plan**: enter a name, choose the default load type, drag the schemas into
**Selected Schemas**, save, then set the schedule (or choose **Schedule later**).

### Suspending and running plans

A load plan's status is **Active**, **Suspended**, **Completed** or **Not Scheduled**. A
suspended plan does not run on its schedule. To suspend a plan, go to **Scheduler > Load
Plans**, hover over the plan and select **More Options** (vertical ellipsis) **> Suspend**, or
select the **Active** status link in the list, and confirm. **More Options > Activate** (or the
**Suspended** status link) resumes it. Several selected plans can be suspended or activated at
once, and the **Edit Load Plan** dialog has a **Suspend scheduler** toggle under **Scheduler**
that does the same. Only the plan owner or a system administrator can change the status.

**Execute Load Plan** (the play button in the list) starts a plan by hand, whatever its
status, including Suspended and Not Scheduled. A plan that is already running cannot be
started again, and a manual run does not change the next scheduled run. Suspending pauses a
plan's future runs; the documentation does not say it stops a run already in progress, so check
**Schema Manager > Load Jobs**, where running jobs are listed and can be stopped, before you
continue.

The procedures below suspend the plans before any full load of the incremental schemas and
before a schema change, instead of relying on timing.

### First load

Before you schedule anything, run a full load of each of the four schemas once (Schema Designer,
**Load > Full Load**). Maximum Value of a Column takes its watermark from the data already in
Incorta, so incremental runs start from there.

### Suggested plans

| Plan | Schemas | Default load type | Schedule |
| --- | --- | --- | --- |
| `cpgsim_incremental_15m` | `cpgsim_shop`, `cpgsim_wms`, `cpgsim_erp` | Incremental Load | **Recur Every** 15 minutes, **Does not end** |
| `cpgsim_snapshots_daily` | `cpgsim_wms_snapshots` | Full Load | **Recur Every** 1 day, **At** 12:30 AM, time zone Europe/Berlin |
| `cpgsim_full_weekly` (optional) | `cpgsim_shop`, `cpgsim_wms`, `cpgsim_erp` | Full Load | **Schedule later** (Not Scheduled); run by hand, see below |

**Incremental, every 15 minutes.** One load group is enough: schemas in one group load
concurrently in a single load job. In an Incremental Load, only tables with Incremental on load
incrementally and every other table loads fully, which is why the snapshot table lives in its
own schema. At the default volume the simulator writes 11,000 to 13,000 rows a day, about 115
to 135 rows per 15 minutes on average, so each run is short. The data an insight sees is at
most one 30-second generator tick plus 15 minutes plus the run time behind the simulator. To
limit loads to working hours, use **Recur Every** 1 day with **Between** a start and end time
and **Every** 15 minutes instead.

**Overlapping loads.** Documented: a running plan cannot be started a second time by hand
(**Execute Load Plan**, or the API with `INC_05290501`), and a schema load through the Public
API fails with HTTP 500 and `INC_05270502: A load job cannot be started as the following
schemas are currently running: ...` while any load of that schema runs; the load is rejected,
not queued. A load job can also show the status **In Queue** while it waits for a Loader
Service. **Not confirmed:** what a scheduled trigger does when it fires while the previous run
of the same plan, or another load of the same schema, is still running. An Incorta Community
FAQ says the run is skipped, but no official documentation page says so. Check the load job
history after the first full day.

**Snapshots, daily.** The snapshot for a day is written at 23:55 Europe/Berlin; 12:30 AM leaves
a margin. Incorta's scheduler documentation notes that a scheduled job should run at least once
before a daylight saving change, and that `Etc/GMT+x` time zones have the opposite sign of
`GMT+x:00`; use a named zone such as Europe/Berlin.

**Weekly full reload (optional).** The loading contract makes the incremental load complete,
so a weekly full reload is a safety net against problems on the Incorta side or manual changes
in Postgres that bypassed `updated_at`. It re-extracts the whole database (about 9.3 million
rows, 2.7 GB of business data after a 24-month backfill, growing by about 1.5 GB a year in live
mode) from a small VM, and it would compete with the 15-minute plan on the same schemas. A
cheaper safety net is to run the row count check below once a week and reload fully only when
counts drift. If you keep the weekly reload, create `cpgsim_full_weekly` with **Schedule
later** and run it by hand in the quietest hour:

1. Suspend `cpgsim_incremental_15m` and wait until no run of it is listed as running under
   **Schema Manager > Load Jobs**.
2. Select **Execute Load Plan** on `cpgsim_full_weekly` and wait for it to finish.
3. Activate `cpgsim_incremental_15m` again.

A scheduled weekly plan cannot suspend the 15-minute plan, so it would rely on the scheduler's
undocumented behaviour for overlapping triggers (see **Overlapping loads** above). If you
schedule it anyway (**Recur Every** 1 week, **At** 3:00 AM, Europe/Berlin), check the load job
history after its first run; the weekday of a weekly schedule presumably follows its start
date (**From**), which is **not confirmed**.

**Alternative: table-level scheduling.** The v8.0.0 release notes announce granular table
scheduling in load plans (individual tables of a schema instead of the whole schema) and the
same schema or table in several load groups within a single load plan. With that,
`wms.stock_levels_daily` could stay in `cpgsim_wms` and be scheduled on its own. Other pages of
the same documentation still contradict it: the Scheduler page says a schema can be added only
once per plan, **Concepts > Full Load** says object full load jobs are available only on
demand, and **Data Ingestion and Loading** says a load job for a physical schema object can only
be on-demand, full or from staging. Neither the UI controls nor how two plans that share a
schema behave are documented, so try the feature on your cluster before relying on it, and
keep the separate schema above as the recommended setup. The documented per-table route in 8.2
is the Public API: `"tables": [...]` in the body of the schema load endpoint.

### After `sim reset`

`sim reset` drops all simulated data, and the backfill that follows writes a new history whose
`updated_at` values lie mostly below the watermark Incorta holds. Incremental loads would then
pick up at most the newest slice of the new history and keep the old timeline's rows. The same
applies after any `sim backfill` on a database Incorta has already loaded (see
[Why Maximum Value of a Column](#settings-for-every-incremental-table)). To reset:

1. Suspend `cpgsim_incremental_15m`, `cpgsim_snapshots_daily` and, if it is scheduled,
   `cpgsim_full_weekly` (**Scheduler > Load Plans > More Options > Suspend**; see
   [Suspending and running plans](#suspending-and-running-plans)). Wait until none of their runs
   is listed as running under **Schema Manager > Load Jobs**.
2. Run `sim reset` and wait until the backfill has finished and the generator runs live
   (`sim status`). Incorta keeps showing the old data meanwhile.
3. Run a full load of all four schemas, from the Schema Designer (**Load > Full Load**), with
   **Execute Load Plan** on `cpgsim_full_weekly` plus a full load of `cpgsim_wms_snapshots`, or
   with the Public API (`POST /incorta/api/v2/{tenant}/schema/{schemaName}/load` with
   `"loadSource": "FULL"`, available since v8.1.0; see the endpoint documentation for
   authentication). The API call fails with HTTP 500 and `INC_05270502` while any load of that
   schema is running; wait and retry on that error.
4. Activate the plans you suspended in step 1.
5. Run the checks in the next section.

Do the same after a new simulator version changes a table's columns: in Incorta, adding or
removing columns, changing data types, the query or the key columns all require a full load.
Suspend the plans and wait for running loads to finish before you change the Incorta schema
(Incorta does not recommend model changes during loads), make the change, run the full load,
then activate the plans again. New tables (phase 2 adds `mkt` and `feeds`) need their own
Incorta tables, added to the plans.

## 4. Checks after the first load

### Row counts and freshness

Run this as `bi_reader` (for example with `psql "host=<host> port=5432 dbname=cpgsim
user=bi_reader sslmode=require"`) and compare `row_count` with the row count of each table in
Incorta. Incorta keeps soft-deleted rows too, so the counts compare directly.

```sql
SELECT tbl, row_count, max_updated_at, now() - max_updated_at AS age
FROM (
  SELECT 'shop.customers' AS tbl, count(*) AS row_count, max(updated_at) AS max_updated_at
    FROM shop.customers
  UNION ALL SELECT 'shop.addresses', count(*), max(updated_at) FROM shop.addresses
  UNION ALL SELECT 'shop.promotions', count(*), max(updated_at) FROM shop.promotions
  UNION ALL SELECT 'shop.subscriptions', count(*), max(updated_at) FROM shop.subscriptions
  UNION ALL SELECT 'shop.orders', count(*), max(updated_at) FROM shop.orders
  UNION ALL SELECT 'shop.order_lines', count(*), max(updated_at) FROM shop.order_lines
  UNION ALL SELECT 'shop.payments', count(*), max(updated_at) FROM shop.payments
  UNION ALL SELECT 'shop.refunds', count(*), max(updated_at) FROM shop.refunds
  UNION ALL SELECT 'shop.promo_redemptions', count(*), max(updated_at) FROM shop.promo_redemptions
  UNION ALL SELECT 'wms.shipments', count(*), max(updated_at) FROM wms.shipments
  UNION ALL SELECT 'wms.shipment_lines', count(*), max(updated_at) FROM wms.shipment_lines
  UNION ALL SELECT 'wms.carrier_events', count(*), max(updated_at) FROM wms.carrier_events
  UNION ALL SELECT 'wms.returns', count(*), max(updated_at) FROM wms.returns
  UNION ALL SELECT 'wms.return_lines', count(*), max(updated_at) FROM wms.return_lines
  UNION ALL SELECT 'wms.stock_movements', count(*), max(updated_at) FROM wms.stock_movements
  UNION ALL SELECT 'wms.stock_levels_daily', count(*), max(updated_at) FROM wms.stock_levels_daily
  UNION ALL SELECT 'erp.mara', count(*), max(updated_at) FROM erp.mara
  UNION ALL SELECT 'erp.makt', count(*), max(updated_at) FROM erp.makt
  UNION ALL SELECT 'erp.mbew', count(*), max(updated_at) FROM erp.mbew
  UNION ALL SELECT 'erp.kna1', count(*), max(updated_at) FROM erp.kna1
  UNION ALL SELECT 'erp.knmt', count(*), max(updated_at) FROM erp.knmt
  UNION ALL SELECT 'erp.tcurr', count(*), max(updated_at) FROM erp.tcurr
  UNION ALL SELECT 'erp.vbak', count(*), max(updated_at) FROM erp.vbak
  UNION ALL SELECT 'erp.vbap', count(*), max(updated_at) FROM erp.vbap
  UNION ALL SELECT 'erp.konv', count(*), max(updated_at) FROM erp.konv
  UNION ALL SELECT 'erp.likp', count(*), max(updated_at) FROM erp.likp
  UNION ALL SELECT 'erp.lips', count(*), max(updated_at) FROM erp.lips
  UNION ALL SELECT 'erp.vbrk', count(*), max(updated_at) FROM erp.vbrk
  UNION ALL SELECT 'erp.vbrp', count(*), max(updated_at) FROM erp.vbrp
) t
ORDER BY tbl;
```

The generator commits every 30 seconds, so counts taken a few minutes apart differ on the busy
tables. For an exact comparison, stop the generator (`docker compose stop generator`), run the
incremental plan once, compare, and start it again (`docker compose start generator`); it
catches up on its own.

For freshness, `age` in Postgres shows how recently each table changed. On busy tables
(`shop.orders`, `erp.vbak`, `wms.carrier_events`) it stays within minutes in live mode;
master data (`erp.mara`, `erp.makt`, `erp.mbew`, `erp.tcurr`, `shop.promotions`) changes
rarely, and a large age there is normal. Do this comparison after the first two incremental
runs and again after the first daylight saving change; it is the check for the unconfirmed
binding of `?`. In Incorta, `max(updated_at)` of each table should
trail the Postgres value by no more than one schedule interval plus the run time. If an
incremental table stays behind while Postgres moves, the watermark is not bound as expected:
run a full load of that schema and switch the table to the
[fallback](#fallback-last-successful-extract-time) until the cause is found.

### Join keys between systems

The systems do not share keys; these are the joins to model, taken from the DDL and its column
comments in [sql/schemas](../../sql/schemas).

| From | To | Notes |
| --- | --- | --- |
| `shop.orders.id` (as text) | `erp.vbak.bstnk` | Web orders only (`vbak.auart = 'ZWEB'`). For retailer orders (`ZEDI`) `bstnk` holds the retailer PO number. Free text, not a key. |
| `erp.vbap.vbeln` | `erp.vbak.vbeln` | Item to header |
| `erp.konv.knumv`, `kposn` | `erp.vbak.knumv`, `erp.vbap.posnr` | Pricing conditions per item |
| `erp.lips.vgbel`, `vgpos` | `erp.vbap.vbeln`, `posnr` | Delivery item to sales item |
| `erp.lips.vbeln` | `erp.likp.vbeln` | Item to header; a cancelled delivery has `is_deleted` set on both |
| `wms.shipments.delivery_no` | `erp.likp.vbeln` | One shipment per delivery |
| `wms.shipment_lines.shipment_id`, `line_no` | `erp.lips` via `delivery_no` | `lips.posnr` is 10 x `line_no`, six digits zero-padded (`000010`) |
| `wms.carrier_events.shipment_id` | `wms.shipments.shipment_id` | |
| `wms.returns.shipment_id`, `delivery_no` | `wms.shipments.shipment_id`, `erp.likp.vbeln` | |
| `wms.return_lines.return_id` | `wms.returns.return_id` | |
| `wms.stock_movements.delivery_no`, `return_id` | `erp.likp.vbeln`, `wms.returns.return_id` | `delivery_no` on goods issues, `return_id` on return and scrap postings |
| `erp.vbrp.aubel`, `aupos` | `erp.vbap.vbeln`, `posnr` | Billing item to sales item; `000900` is the web shipping charge |
| `erp.vbrp.vgbel` | `erp.likp.vbeln` (`vgtyp = 'J'`) or the original invoice `erp.vbrk.vbeln` (`vgtyp = 'M'`) | Invoices reference the delivery, credit memos the invoice |
| `erp.vbrp.vgbel`, `vgpos` | `erp.lips.vbeln`, `posnr` (`vgtyp = 'J'`) or `erp.vbrp.vbeln`, `posnr` of the original invoice (`vgtyp = 'M'`) | Billing item to delivery item, or credit memo item to invoice item. `vgpos` is NULL on items without a delivery line, such as the shipping charge `000900`. Alternative for invoices: `vbrp.vgbel`, `aupos` to `lips.vbeln`, `vgpos`. Taken from the writer code; check `SELECT count(*) FROM erp.vbrp WHERE vgpos IS NOT NULL` on a backfilled database before modelling it. |
| `wms.*.item_code` | `erp.mara.matnr` | `matnr = lpad(item_code, 18, '0')` |
| `shop.order_lines.ean`, `wms.*.ean` | `erp.mara.ean11` | |
| `erp.vbak.kunnr`, `zzkunwe`; `erp.likp.kunnr`; `erp.vbrk.kunrg`; `wms.shipments.consignee_no` | `erp.kna1.kunnr` | Web customers are one collective account per country |

A join on an expression such as the `lpad` above needs a derived column, for example
`lpad(item_code, 18, '0') AS matnr` added to both queries of the WMS tables, or a formula column
in Incorta.

## Version notes

Incorta 8.2.2 is release **v8.2.2-2026.8**, Service Pack 16 of v8.2.0, released on 16 October
2026. Service packs carry only bug and security fixes, so 8.2.2 has the features of v8.2.0.
Incorta publishes no separate documentation tree for 8.x; the `latest` documentation covers it.
Later 8.2 service packs (8.2.3 to 8.2.6, SP17 to SP20) also exist and share these pages, so a
page may describe a fix made after 8.2.2. The statements in this guide are confirmed in these
pages:

| Statement | Source |
| --- | --- |
| 8.2.2 is v8.2.2-2026.8, SP16, bug fixes only | [V8 releases](https://docs.incorta.com/latest/v8-releases), [Service Pack 16](https://docs.incorta.com/latest/service-pack-16), [Release model](https://docs.incorta.com/latest/release-model) |
| Data source path and fields (Connection String format and default port, Connection Pool default 30, Connection Properties, Extra Options, Use Data Agent); table fields (Incremental Type, Incremental Extract Using, Incremental Column, Incremental Field Type, Fetch Size default 5000, Query, Update Query); JDBC 42.2.1 before and 42.7.8 from connector 2.2.6.0, and the `sslResponseTimeout` note | [PostgreSQL connector](https://docs.incorta.com/latest/references-connectors-postgresql) |
| `sslmode` values; `require` does not validate the certificate; `verify-full` needs `sslrootcert`; URL parameters override properties | [pgJDBC: connecting](https://jdbc.postgresql.org/documentation/use/) |
| Query and Update Query return the same columns; `?` replacement; Last Successful Extract Time is the loader's start time of the last successful extraction, and its caveat; Maximum Value of a Column; millisecond precision; keys make incremental loads upsert | [Incremental load](https://docs.incorta.com/latest/concepts-incremental-load) |
| Function = key, Disable Full Load; Enforce Primary Key Constraint (off by default, on Advanced Settings since 2024.7.x, full loads only, duplicate keys with it off make a full load finish with errors) | [Table Editor](https://docs.incorta.com/latest/tools-table-editor) |
| Column, query, key and incremental strategy changes require a full load; no model changes during loads; object full load jobs only on demand | [Full load](https://docs.incorta.com/latest/concepts-full-load) |
| Load menu: Full Load, Incremental Load (non-incremental tables load fully) | [Schema Designer](https://docs.incorta.com/latest/tools-schema-designer) |
| Tables with incremental disabled are fully loaded in an incremental job; groups run sequentially, schemas in a group concurrently; incremental loads deduplicate on the key regardless of Enforce Primary Key Constraint; load job status In Queue; physical schema object load jobs only on-demand, full or from staging | [Data ingestion and loading](https://docs.incorta.com/latest/references-data-ingestion-and-loading) |
| Load plan creation and the Schema Manager role; schedule options Recur Every, At, Between, From, Does not end, No Recurrence; time zone and DST caveats; status Active, Suspended, Completed, Not Scheduled; More Options > Suspend / Activate, the status link and the Suspend scheduler toggle; Execute Load Plan works in any status but not on a running plan; Schema Manager > Load Jobs | [Scheduler](https://docs.incorta.com/latest/tools-scheduler) |
| Table-level scheduling and the same schema or table in several load groups within one load plan (v8.0.0) | [Release notes v8.0.0](https://docs.incorta.com/latest/release-notes-v8-0-0-2026-8) |
| Schema load endpoint with `loadSource` FULL (v8.1.0), optional `tables` list; HTTP 500 `INC_05270502` while a load of the same schema is running | [Release notes v8.1.0](https://docs.incorta.com/latest/release-notes-v8-1-0-2026-8), [Load schema tables endpoint](https://docs.incorta.com/latest/references-api-load-schema-tables-endpoint-v2) |
| A running load plan cannot be started again (`INC_05290501`) | [Execute load plan endpoint](https://docs.incorta.com/latest/references-api-execute-load-plan-endpoint-v2) |

Not confirmed in the documentation, and marked as such above:

- what a scheduled trigger does while the previous run of the same plan, or another load of the
  same schema, is still running (an Incorta Community FAQ says it is skipped; no official page
  confirms it);
- table-level scheduling in practice: the UI controls, and whether it works at all given the
  Scheduler, Full Load and Data Ingestion pages that contradict the v8.0.0 release notes;
- how several `key=value` pairs are separated in Connection Properties;
- how the `?` watermark is bound against `timestamptz` with regard to time zones (applies to
  both Maximum Value of a Column and Last Successful Extract Time);
- how Incorta maps the Postgres `uuid`, array and `time` types (the queries cast them to text);
- that the weekday of a weekly schedule follows its start date.

Our own suggestions rather than Incorta recommendations: the connection pool of 10, the four
schema names, the plan names and times, and the millisecond truncation of `updated_at` in the
query (Incorta recommends millisecond precision at the source; the truncation is how this guide
achieves it).
