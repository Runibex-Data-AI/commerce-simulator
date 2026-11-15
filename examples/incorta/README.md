# Loading the simulator into Incorta

How to connect an Incorta cluster to the simulator's Postgres database and keep it current with
query-based incremental loads on `updated_at`. Written for Incorta **8.2.2** (release
v8.2.2-2026.8, a service pack of v8.2.0) and simulator **v0.2.0** (phase 2: `shop`, `wms`, `erp`,
`mkt`, `feeds`). The joins inside each system are listed in [joins.md](joins.md).

Statements about Incorta behaviour are taken from the Incorta documentation and linked in
[Version notes](#version-notes). Anything the documentation does not confirm is marked
**Not confirmed** and should be checked on your cluster. All SQL below has been run against the
v0.2.0 DDL on PostgreSQL 16.

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
| `erp` | 18 | Incremental on `updated_at` |
| `mkt` | 5 | Incremental on `updated_at` |
| `feeds` | 4 | Full load only, once a day |
| `shelf` | none yet | Empty in v0.2.0; tables arrive in phase 3 |
| `sim`, `truth` | | Not granted to `bi_reader`; cannot be loaded |

The simulator guarantees one loading contract for every business table (see the
[README](../../README.md#loading-it-into-a-bi-tool) and
[SPEC](../../docs/SPEC.md#loading-contract-for-bi-tools)): a primary key, `created_at` and
`updated_at` on every row with `updated_at` indexed and moved on every change, no hard deletes,
and insert-only snapshot and feed tables. That is what makes the plain query-based setup below
complete.

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

Create six Incorta schemas:

| Incorta schema | Postgres tables | Why separate |
| --- | --- | --- |
| `cpgsim_shop` | all of `shop` | |
| `cpgsim_wms` | all of `wms` except `stock_levels_daily` | |
| `cpgsim_erp` | all of `erp` | |
| `cpgsim_mkt` | all of `mkt` | |
| `cpgsim_wms_snapshots` | `wms.stock_levels_daily` | Full load only, on its own schedule |
| `cpgsim_feeds` | all of `feeds` | Full load only, on its own schedule |

An incremental load job fully reloads every table in the schema that has incremental load
disabled. Keeping the snapshot table in `cpgsim_wms` would therefore re-extract it completely on
every 15-minute run, and the same would happen to the `feeds` tables in any incremental
schema. Separate schemas keep them in their own daily plan using only documented load plan
features. Table-level scheduling is the alternative (see [Load plans](#3-load-plans)).

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
`shop.orders.status`, `erp.vbap.abgru`, `wms.shipments.status_code = 'CX'`,
`erp.zdeduct.zzstatus = 'R'` (a reversed deduction). A cancelled ERP
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
| `kona` | `knuma` | Incremental |
| `zkona_item` | `knuma`, `posnr` | Incremental |
| `zdeduct` | `belnr` | Incremental |
| `zdeduct_item` | `belnr`, `posnr` | Incremental |
| `zdeduct_clr` | `belnr`, `buzei` | Incremental |

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

#### `erp.kona`

Trade promotion agreements: one row per retailer promotion the brand co-funds, written when the
promotion is agreed, about six weeks before it starts. Insert-only: rows are never updated or
soft-deleted.

```sql
-- Query
SELECT
  knuma, boart, kunnr, botext, datab, datbi, zzdisc_pct, zzofi_pct, zzscan_pct, zzwkz_eur,
  erdat, ernam, created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted,
  deleted_at
FROM erp.kona

-- Update Query
SELECT
  knuma, boart, kunnr, botext, datab, datbi, zzdisc_pct, zzofi_pct, zzscan_pct, zzwkz_eur,
  erdat, ernam, created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted,
  deleted_at
FROM erp.kona
WHERE updated_at > ?
```

#### `erp.zkona_item`

The promoted materials of an agreement, with the agreed scan-back amount per unit sold
(`zzscan_eur`, net of VAT). Written in the same commit as the `erp.kona` header. Insert-only.

```sql
-- Query
SELECT
  knuma, posnr, matnr, kdmat, zzscan_eur, created_at, date_trunc('milliseconds', updated_at)
  AS updated_at, is_deleted, deleted_at
FROM erp.zkona_item

-- Update Query
SELECT
  knuma, posnr, matnr, kdmat, zzscan_eur, created_at, date_trunc('milliseconds', updated_at)
  AS updated_at, is_deleted, deleted_at
FROM erp.zkona_item
WHERE updated_at > ?
```

#### `erp.zdeduct`

Retailer deductions: one row per claim that a trade customer raises and short-pays from its
invoices. The row is inserted with the status the case has when it is claimed. Two kinds of
change update it later and move `updated_at`:

- A status change: `zzstatus` (O claimed, M matched, D disputed, A accepted, R reversed),
  together with `zzmatch_date`, `zzdisp_date`, `zzdisp_reason`, `zzres_date` and `zzaccepted`
  as they apply. Closing the case sets `zzclose_date`.
- A clearing: each new `erp.zdeduct_clr` row updates the running totals `zztaken` and
  `zzrepaid` in the same commit, so the header and its clearing item arrive in the same
  incremental run.

No credit memo is booked for an accepted deduction. The accepted trade funding is
`zzaccepted`, and `erp.vbrk` revenue of trade customers is gross of it.

```sql
-- Query
SELECT
  belnr, bukrs, gjahr, kunnr, zztype, xblnr, bldat, budat, wrbtr, waers, dmbtr, knuma,
  zzpromo_text, zzperiod_from, zzperiod_to, vbeln_vl, vbeln_va, vbeln_vf, zzstatus,
  zzmatch_date, zzdisp_date, zzdisp_reason, zzres_date, zzaccepted, zztaken, zzrepaid,
  zzclose_date, erdat, erzet::text AS erzet, ernam, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.zdeduct

-- Update Query
SELECT
  belnr, bukrs, gjahr, kunnr, zztype, xblnr, bldat, budat, wrbtr, waers, dmbtr, knuma,
  zzpromo_text, zzperiod_from, zzperiod_to, vbeln_vl, vbeln_va, vbeln_vf, zzstatus,
  zzmatch_date, zzdisp_date, zzdisp_reason, zzres_date, zzaccepted, zztaken, zzrepaid,
  zzclose_date, erdat, erzet::text AS erzet, ernam, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.zdeduct
WHERE updated_at > ?
```

#### `erp.zdeduct_item`

The articles and units a retailer claims, at a net rate per unit (`zzrate`); `wrbtr` is the
gross item amount. Written in the same commit as the `erp.zdeduct` header. Insert-only. `matnr`
is NULL when the retailer's article (`kdmat`) has no `erp.knmt` mapping.

```sql
-- Query
SELECT
  belnr, posnr, kdmat, matnr, zzunits, zzrate, wrbtr, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.zdeduct_item

-- Update Query
SELECT
  belnr, posnr, kdmat, matnr, zzunits, zzrate, wrbtr, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.zdeduct_item
WHERE updated_at > ?
```

#### `erp.zdeduct_clr`

One row per amount cleared against a deduction. `zzkind` is T for an amount taken by
short-paying an invoice (`vbeln_vf` set) or P for an amount the customer repaid (`vbeln_vf`
NULL). `dmbtr` is in EUR and always positive. A claim larger than one invoice is split across
several T rows. Insert-only; each row also updates `erp.zdeduct` in the same commit (see
above).

```sql
-- Query
SELECT
  belnr, buzei, zzkind, vbeln_vf, augdt, dmbtr, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.zdeduct_clr

-- Update Query
SELECT
  belnr, buzei, zzkind, vbeln_vf, augdt, dmbtr, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM erp.zdeduct_clr
WHERE updated_at > ?
```

### `mkt`

| Table | Key columns | Load |
| --- | --- | --- |
| `influencer_codes` | `code` | Incremental |
| `campaigns` | `campaign_id` | Incremental |
| `campaign_spend_daily` | `campaign_id`, `spend_date` | Incremental |
| `sessions_daily` | `session_date`, `channel`, `country`, `device` | Incremental |
| `attributions` | `attribution_id` | Incremental |

The `mkt` tables have no `uuid`, array or `time` columns, so the queries need no casts.
`order_ref`, `customer_ref` and `promo_ref` already hold UUIDs as text, in the same form as the
`id::text` columns of `shop`. The simulator never soft-deletes an `mkt` row, but the queries
keep `is_deleted` and `deleted_at` like every other table.

**Which tables change after insert.** Only `mkt.campaigns`. A campaign row is inserted when
the campaign is set up. Regular campaigns are set up at 09:00 Europe/Berlin, 14 days before
they start. Promotion email campaigns (`EM-P…`) are set up when the web promotion is announced,
3 days before it starts, in the morning. Scenario campaigns (`EM-X…`, `AF-X…`) are set up 14
days before they start, or when the scenario window becomes visible if that is later.
Campaigns set up before the history start that are still running or upcoming are written at
the history start. A campaign's `status` moves from `planned` to `active` at 00:05 on the
start date (a campaign set up on or
after that moment is inserted as `active`), and to `ended` at 00:05 the day after the end date,
when `end_date` is written again. While the `influencer_cohort_churn` scenario runs,
`daily_budget_eur` of the affected influencer campaigns is raised, and set back after the
window if the campaign still runs, in the daily export commit (below). Incorta keeps only the
current value; the history of these budget changes is in `truth`, which `bi_reader` cannot
read. The other four tables are insert-only in practice: `mkt.influencer_codes` is written
once at the history start (its `status` stays `active` and `valid_to` stays NULL), and the
daily tables are never rewritten. They still load incrementally on `updated_at`; with no
updates, each run only appends.

**Daily export timing.** `mkt.campaign_spend_daily`, `mkt.sessions_daily` and
`mkt.attributions` are written by one daily export per local day. The export for day D runs
once, on D+1 at a random minute between 04:00 and 05:00 Europe/Berlin, and writes all spend,
session and attribution rows of D (plus any budget updates on `mkt.campaigns`) in one commit.
A day is never exported twice and its rows are never updated afterwards. For the 15-minute
plan this means:

- The first incremental run after the export commit picks up the whole previous day, so it
  arrives in Incorta between about 04:00 and 05:15 plus the run time. All other runs of the
  day read nothing from these three tables; the `updated_at` index keeps that cheap.
- Today never has rows in these three tables, and yesterday has none until the export has run.
  Shop orders of those days are already loaded, so restrict any comparison of marketing facts
  with `shop` (attributed orders against orders, spend against new customers) to days up to
  the latest `session_date` in `mkt.sessions_daily`.
- `mkt.attributions` holds one row per tracked web order, dated by `attribution_date` (the
  local date the payment was authorised), not by `shop.orders.placed_at`. Orders whose payment
  fails never appear; an order cancelled later stays attributed.
- In the [freshness check](#row-counts-and-freshness), an `age` of up to about 25 hours is
  normal for these three tables. Their `max(updated_at)` in Incorta should still equal the
  Postgres value once the first run after the export has finished.

#### `mkt.influencer_codes`

```sql
-- Query
SELECT
  code, influencer_handle, audience_platform, commission_pct, fixed_fee_eur, valid_from,
  valid_to, status, created_at, date_trunc('milliseconds', updated_at) AS updated_at,
  is_deleted, deleted_at
FROM mkt.influencer_codes

-- Update Query
SELECT
  code, influencer_handle, audience_platform, commission_pct, fixed_fee_eur, valid_from,
  valid_to, status, created_at, date_trunc('milliseconds', updated_at) AS updated_at,
  is_deleted, deleted_at
FROM mkt.influencer_codes
WHERE updated_at > ?
```

#### `mkt.campaigns`

```sql
-- Query
SELECT
  campaign_id, campaign_name, channel, objective, influencer_code, promo_ref, start_date,
  end_date, daily_budget_eur, status, created_date, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM mkt.campaigns

-- Update Query
SELECT
  campaign_id, campaign_name, channel, objective, influencer_code, promo_ref, start_date,
  end_date, daily_budget_eur, status, created_date, created_at, date_trunc('milliseconds',
  updated_at) AS updated_at, is_deleted, deleted_at
FROM mkt.campaigns
WHERE updated_at > ?
```

#### `mkt.campaign_spend_daily`

`platform_conversions` is what the ad platform reports; it over-attributes compared with
`mkt.attributions`.

```sql
-- Query
SELECT
  campaign_id, spend_date, impressions, clicks, spend_eur, platform_conversions, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM mkt.campaign_spend_daily

-- Update Query
SELECT
  campaign_id, spend_date, impressions, clicks, spend_eur, platform_conversions, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM mkt.campaign_spend_daily
WHERE updated_at > ?
```

#### `mkt.sessions_daily`

About 92 % of shop orders are tracked (consent), so `orders` and `revenue_eur` stay below the
shop totals. `channel` uses the shop's source words, except that the shop's `deal_site` source
is `affiliate`, as in `mkt.campaigns`.

```sql
-- Query
SELECT
  session_date, channel, country, device, sessions, users, new_users, orders, revenue_eur,
  created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM mkt.sessions_daily

-- Update Query
SELECT
  session_date, channel, country, device, sessions, users, new_users, orders, revenue_eur,
  created_at, date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM mkt.sessions_daily
WHERE updated_at > ?
```

#### `mkt.attributions`

```sql
-- Query
SELECT
  attribution_id, order_ref, customer_ref, attribution_date, channel, campaign_id,
  influencer_code, model, is_new_customer, attributed_revenue_eur, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM mkt.attributions

-- Update Query
SELECT
  attribution_id, order_ref, customer_ref, attribution_date, channel, campaign_id,
  influencer_code, model, is_new_customer, attributed_revenue_eur, created_at,
  date_trunc('milliseconds', updated_at) AS updated_at, is_deleted, deleted_at
FROM mkt.attributions
WHERE updated_at > ?
```

### Insert-only tables (full load only)

`wms.stock_levels_daily` and the four `feeds` tables are insert-only: rows are never updated or
deleted, and a restatement arrives as new rows with a new `load_id`. They have no `is_deleted`
or `deleted_at`.

| Table | Key columns | Load |
| --- | --- | --- |
| `wms.stock_levels_daily` | `snapshot_date`, `warehouse_code`, `item_code`, `load_id` | Full only, in `cpgsim_wms_snapshots` |
| `feeds.feed_loads` | `load_id` | Full only, in `cpgsim_feeds` |
| `feeds.retailer_sellout` | `load_id`, `line_no` | Full only, in `cpgsim_feeds` |
| `feeds.marketplace_sales_daily` | `report_date`, `product_code`, `load_id` | Full only, in `cpgsim_feeds` |
| `feeds.marketplace_inventory_daily` | `report_date`, `product_code`, `load_id` | Full only, in `cpgsim_feeds` |

For each of these tables, set **Incremental** off, leave Update Query empty, set **Function**
to **key** on the key columns above (the Postgres primary key), and leave Enforce Primary Key
Constraint off here too.

Do not switch on **Disable Full Load** for these tables. That setting is for incremental tables
that must never be fully reloaded; it does not make a table full-load-only. Leaving Incremental
off does.

**Why no incremental.** The simulator's loading contract for these tables is "load them in
full, or append by `load_id`". Restatements come in as new rows, so a daily full load picks
them up with no special handling and no watermark to check. The tables are small: about
130,000 rows in `wms.stock_levels_daily` (one row per item a day) and about 0.3 million rows in
`feeds` after a 24-month backfill. Because every load stays in Incorta, row counts match
Postgres directly, and you choose the current load in the business layer.

**When feed files arrive.** All times are Europe/Berlin:

- Kranz: Monday, 06:00 to 09:00.
- Mylo: Monday, 10:00 to 14:00.
- Belle: the Wednesday after each retail month, 08:00 to 12:00.
- Shopnova: daily reports for the previous day, 07:00 to 09:00.

3 % of retailer files arrive one or two days late; `feeds.feed_loads.delivered_at` shows when
they arrived. With the 12:30 AM plan, a file reaches Incorta the night after it arrives.

#### `wms.stock_levels_daily`

The end-of-day stock per item, taken at 23:55 Europe/Berlin. A restatement arrives as new rows
with a higher `load_id`.

```sql
-- Query
SELECT
  snapshot_date, warehouse_code, item_code, load_id, ean, qty_on_hand, qty_reserved,
  qty_available, qty_in_production, snapshot_local, created_at, updated_at
FROM wms.stock_levels_daily
```

For analysis, use the highest `load_id` per `snapshot_date`, `warehouse_code` and `item_code`.

#### `feeds.feed_loads`

One row per delivered file: the sender (`source`: `KRANZ`, `MYLO`, `SHOPNOVA`, `BELLE`), the
feed type (`sellout_weekly`, `sellout_monthly`, `mp_sales_daily`, `mp_inventory_daily`), the
reported period (`period_end` is inclusive), the file name in the sender's convention,
`delivered_at` and `row_count`. A `load_id` is made of the sender prefix, the feed type, the
period start and a run number, for example `KRZ_SO_20260302_01`. Run `01` is the original file
and run `02` a restatement, which has `is_restatement = true` and names the replaced load in
`restates_load_id`. This is the table that tells you which load of a period is current.

```sql
-- Query
SELECT
  load_id, source, feed_type, period_start, period_end, file_name, delivered_at, row_count,
  is_restatement, restates_load_id, created_at, updated_at
FROM feeds.feed_loads
```

#### `feeds.retailer_sellout`

The retailers' sell-out: one row per period, sales region (the region one retailer DC serves),
sales channel and article. `line_no` is the row number within the file. Store rows are present
for every listed article, including weeks with 0 units sold, and `stock_units` holds the
region's stock at period end; on online rows it is NULL. `sales_value` is net or gross of VAT
depending on the retailer (`value_basis`), so convert it before you add values across
retailers. Kranz and Mylo report ISO weeks (`period_type = 'W'`); Belle reports retail months
of a 4-4-5 calendar of ISO weeks (`period_type = 'M'`).

| `retailer` | `period_label` | `region_code` | `sales_channel` (store / online) | `value_basis` | `article_no` |
| --- | --- | --- | --- | --- | --- |
| `KRANZ` | `2026-W10` | `VZ` + first three letters of the DC city (`VZERF`) | `STORE` / `ONLINE` | `NET` | six digits |
| `MYLO` | `KW10/2026` | region name per DC (`OST`, `SUED`, `WEST`, `NORDOST`, `SUEDWEST`, `NORD`) | `F` / `O` | `GROSS` | `MY` + seven digits |
| `BELLE` | `2026P03` | `BL-` + federal state of the DC (`BL-HE`) | `FIL` / `ECOM` | `GROSS` | `BL` + six digits |

`article_no` maps to `matnr` through `erp.knmt.kdmat` for the sender's sold-to account; about
95 % of articles have a record. See
[Join keys between systems](#join-keys-between-systems) for the join and
[joins.md](joins.md#retailer-article-numbers) for recovering the missing 5 %.

```sql
-- Query
SELECT
  load_id, line_no, retailer, period_type, period_start, period_end, period_label,
  region_code, sales_channel, article_no, article_text, units_sold, stock_units, sales_value,
  value_basis, currency, created_at, updated_at
FROM feeds.retailer_sellout
```

Restatements: by default 10 % of retailer periods are restated. In those periods, run `01`
under-reports the store rows of one region; run `02` comes with the retailer's next regular
delivery and contains the whole period again. Use only the latest load of each period (see
[Reading feed restatements](#reading-feed-restatements)). Adding run `01` and run `02` together
counts the period twice.

#### `feeds.marketplace_sales_daily`

Shopnova's vendor sales and traffic report: one row per day and product, only for products
with units ordered or page views that day. `ordered_revenue` is gross, at the marketplace's
consumer price. `glance_views` counts product detail page views; views with 0 units point to
out-of-stock days. Each report day is one load (`SNV_SALES_<yyyymmdd>_01`). `product_code` is
the marketplace's own code (`SNV` + eight digits), never an EAN; it maps to `matnr` through
`erp.knmt.kdmat` in the same way as retailer articles.

```sql
-- Query
SELECT
  report_date, product_code, load_id, product_title, units_ordered, ordered_revenue,
  currency, glance_views, created_at, updated_at
FROM feeds.marketplace_sales_daily
```

Restatements: the simulator currently restates retailer periods only, so each report day has
one load. Apply the latest-load rule anyway, with the report day as the period
(`feed_loads.period_start = report_date`), so the model stays correct if a day is ever re-sent.

#### `feeds.marketplace_inventory_daily`

Shopnova's vendor inventory report: the end-of-day position per listed product that has stock
or inbound units. `sellable_on_hand` is the total across all fulfilment centres;
`inbound_quantity` is confirmed purchase-order units not yet received. Each report day is one
load (`SNV_INV_<yyyymmdd>_01`), separate from the sales load of the same day. Codes, mapping
and restatements are as in `feeds.marketplace_sales_daily`.

```sql
-- Query
SELECT
  report_date, product_code, load_id, sellable_on_hand, inbound_quantity, created_at,
  updated_at
FROM feeds.marketplace_inventory_daily
```

#### Reading feed restatements

Load every row and pick the current load in the business layer (business schema views or
insights), the same way `is_deleted` is filtered on the incremental tables. The current load of
a period is the `feed_loads` row with the latest `delivered_at`, then the highest `load_id`,
among the rows with the same `source`, `feed_type` and `period_start`. Join a data table to
that `load_id` and leave out its other loads. Because both loads stay in Incorta, you can still
put the original next to the restated file.

```sql
SELECT s.retailer, s.period_label, s.region_code, s.sales_channel, s.article_no,
       s.units_sold, s.stock_units, s.sales_value, s.value_basis
FROM feeds.retailer_sellout s
JOIN (
  SELECT
    load_id,
    row_number() OVER (
      PARTITION BY source, feed_type, period_start
      ORDER BY delivered_at DESC, load_id DESC
    ) AS rn
  FROM feeds.feed_loads
) l ON l.load_id = s.load_id AND l.rn = 1
```

A simpler rule gives the same result in the current simulator, because each restatement names
the load it replaces: drop every load whose `load_id` appears in another row's
`restates_load_id`. Until a restatement arrives, the original load is the current one.

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

Before you schedule anything, run a full load of each of the six schemas once (Schema Designer,
**Load > Full Load**). Maximum Value of a Column takes its watermark from the data already in
Incorta, so incremental runs start from there.

### Suggested plans

| Plan | Schemas | Default load type | Schedule |
| --- | --- | --- | --- |
| `cpgsim_incremental_15m` | `cpgsim_shop`, `cpgsim_wms`, `cpgsim_erp`, `cpgsim_mkt` | Incremental Load | **Recur Every** 15 minutes, **Does not end** |
| `cpgsim_snapshots_daily` | `cpgsim_wms_snapshots`, `cpgsim_feeds` | Full Load | **Recur Every** 1 day, **At** 12:30 AM, time zone Europe/Berlin |
| `cpgsim_full_weekly` (optional) | all six schemas | Full Load | **Schedule later** (Not Scheduled); run by hand, see below |

**Incremental, every 15 minutes.** One load group is enough: schemas in one group load
concurrently in a single load job. In an Incremental Load, only tables with Incremental on load
incrementally and every other table loads fully, which is why the insert-only tables live in
their own schemas. At the default volume the simulator writes about 13,800 rows a day in an
ordinary month and 15,300 a day averaged over a year, about 145 to 160 rows per 15 minutes on
average, so each run is short. The data an insight sees is at most one 30-second generator
tick plus 15 minutes plus the run time behind the simulator. The exception is the three daily
`mkt` tables: the previous day's spend, sessions and attributions arrive in one commit between
04:00 and 05:00 (see [`mkt`](#mkt)). To limit loads to working hours, use **Recur Every** 1
day with **Between** a start and end time and **Every** 15 minutes instead.

**Overlapping loads.** Documented: a running plan cannot be started a second time by hand
(**Execute Load Plan**, or the API with `INC_05290501`), and a schema load through the Public
API fails with HTTP 500 and `INC_05270502: A load job cannot be started as the following
schemas are currently running: ...` while any load of that schema runs; the load is rejected,
not queued. A load job can also show the status **In Queue** while it waits for a Loader
Service. **Not confirmed:** what a scheduled trigger does when it fires while the previous run
of the same plan, or another load of the same schema, is still running. An Incorta Community
FAQ says the run is skipped, but no official documentation page says so. Check the load job
history after the first full day.

**Snapshots and feeds, daily.** The snapshot for a day is written at 23:55 Europe/Berlin; 12:30
AM leaves a margin. Feed files arrive in the morning (see
[Insert-only tables](#insert-only-tables-full-load-only)), so each file is loaded the night
after it arrives. Incorta's scheduler documentation notes that a scheduled job should run at
least once before a daylight saving change, and that `Etc/GMT+x` time zones have the opposite
sign of `GMT+x:00`; use a named zone such as Europe/Berlin.

**Weekly full reload (optional).** The loading contract makes the incremental load complete,
so a weekly full reload is a safety net against problems on the Incorta side or manual changes
in Postgres that bypassed `updated_at`. It re-extracts the whole database (about 10.9 million
rows; the business schemas take 3.1 GB including indexes after a 24-month backfill, and the
database grows by about 1.6 GB a year in live mode) from a small VM, and it would compete with
the 15-minute plan on the same schemas. A cheaper safety net is to run the row count check
below once a week and reload fully only when counts drift. If you keep the weekly reload,
create `cpgsim_full_weekly` with **Schedule later** and run it by hand in the quietest hour:

1. Suspend `cpgsim_incremental_15m` and `cpgsim_snapshots_daily` and wait until no run of
   them is listed as running under **Schema Manager > Load Jobs**.
2. Select **Execute Load Plan** on `cpgsim_full_weekly` and wait for it to finish.
3. Activate `cpgsim_incremental_15m` and `cpgsim_snapshots_daily` again.

A scheduled weekly plan cannot suspend the other plans, so it would rely on the scheduler's
undocumented behaviour for overlapping triggers (see **Overlapping loads** above). If you
schedule it anyway (**Recur Every** 1 week, **At** 3:00 AM, Europe/Berlin), check the load job
history after its first run; the weekday of a weekly schedule presumably follows its start
date (**From**), which is **not confirmed**.

**Alternative: table-level scheduling.** The v8.0.0 release notes announce granular table
scheduling in load plans (individual tables of a schema instead of the whole schema) and the
same schema or table in several load groups within a single load plan. With that,
`wms.stock_levels_daily` could stay in `cpgsim_wms` and be scheduled on its own, and the same
goes for the `feeds` tables. Other pages of
the same documentation still contradict it: the Scheduler page says a schema can be added only
once per plan, **Concepts > Full Load** says object full load jobs are available only on
demand, and **Data Ingestion and Loading** says a load job for a physical schema object can only
be on-demand, full or from staging. Neither the UI controls nor how two plans that share a
schema behave are documented, so try the feature on your cluster before relying on it, and
keep the separate schemas above as the recommended setup. The documented per-table route in 8.2
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
3. Run a full load of all six schemas, from the Schema Designer (**Load > Full Load**), with
   **Execute Load Plan** on `cpgsim_full_weekly`, or with the Public API
   (`POST /incorta/api/v2/{tenant}/schema/{schemaName}/load` with `"loadSource": "FULL"`,
   available since v8.1.0; see the endpoint documentation for authentication). The API call
   fails with HTTP 500 and `INC_05270502` while any load of that schema is running; wait and
   retry on that error.
4. Activate the plans you suspended in step 1.
5. Run the checks in the next section.

Do the same after a new simulator version changes a table's columns: in Incorta, adding or
removing columns, changing data types, the query or the key columns all require a full load.
Suspend the plans and wait for running loads to finish before you change the Incorta schema
(Incorta does not recommend model changes during loads), make the change, run the full load,
then activate the plans again. New tables need their own Incorta tables, added to the plans.
Coming from a v0.1.0 setup: v0.2.0 leaves the columns of every v0.1.0 table unchanged and adds
the five `erp` tables, `mkt` and `feeds`. Add the five tables to `cpgsim_erp`, create
`cpgsim_mkt` and `cpgsim_feeds`, run a full load of the three schemas and add them to the plans
as above. Phase 3 adds `shelf`.

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
  UNION ALL SELECT 'erp.kona', count(*), max(updated_at) FROM erp.kona
  UNION ALL SELECT 'erp.zkona_item', count(*), max(updated_at) FROM erp.zkona_item
  UNION ALL SELECT 'erp.zdeduct', count(*), max(updated_at) FROM erp.zdeduct
  UNION ALL SELECT 'erp.zdeduct_item', count(*), max(updated_at) FROM erp.zdeduct_item
  UNION ALL SELECT 'erp.zdeduct_clr', count(*), max(updated_at) FROM erp.zdeduct_clr
  UNION ALL SELECT 'mkt.influencer_codes', count(*), max(updated_at) FROM mkt.influencer_codes
  UNION ALL SELECT 'mkt.campaigns', count(*), max(updated_at) FROM mkt.campaigns
  UNION ALL SELECT 'mkt.campaign_spend_daily', count(*), max(updated_at) FROM mkt.campaign_spend_daily
  UNION ALL SELECT 'mkt.sessions_daily', count(*), max(updated_at) FROM mkt.sessions_daily
  UNION ALL SELECT 'mkt.attributions', count(*), max(updated_at) FROM mkt.attributions
  UNION ALL SELECT 'feeds.feed_loads', count(*), max(updated_at) FROM feeds.feed_loads
  UNION ALL SELECT 'feeds.retailer_sellout', count(*), max(updated_at) FROM feeds.retailer_sellout
  UNION ALL SELECT 'feeds.marketplace_sales_daily', count(*), max(updated_at) FROM feeds.marketplace_sales_daily
  UNION ALL SELECT 'feeds.marketplace_inventory_daily', count(*), max(updated_at) FROM feeds.marketplace_inventory_daily
) t
ORDER BY tbl;
```

The generator commits every 30 seconds, so counts taken a few minutes apart differ on the busy
tables. For an exact comparison, stop the generator (`docker compose stop generator`), run the
incremental plan once, compare, and start it again (`docker compose start generator`); it
catches up on its own.

For freshness, `age` in Postgres shows how recently each table changed. On busy tables
(`shop.orders`, `erp.vbak`, `wms.carrier_events`) it stays within minutes in live mode;
master data (`erp.mara`, `erp.makt`, `erp.mbew`, `erp.tcurr`, `shop.promotions`,
`mkt.influencer_codes`) changes rarely, and a large age there is normal. The three daily `mkt`
tables reach an age of up to about 25 hours before each export (see [`mkt`](#mkt)). The
insert-only tables change only when a snapshot is taken or a file arrives:
`wms.stock_levels_daily`, `feeds.feed_loads` and the two marketplace tables once a day, and
`feeds.retailer_sellout` once or twice a week, so an age of up to about a week is normal there;
compare their counts with Incorta after the daily plan has run.

Do this comparison after the first two incremental runs and again after the first daylight
saving change; it is the check for the unconfirmed binding of `?`. In Incorta, `max(updated_at)`
of each incremental table should trail the Postgres value by no more than one schedule interval
plus the run time. If an
incremental table stays behind while Postgres moves, the watermark is not bound as expected:
run a full load of that schema and switch the table to the
[fallback](#fallback-last-successful-extract-time) until the cause is found.

### Join keys between systems

The systems do not share keys; these are the joins to model, taken from the DDL and its column
comments in [sql/schemas](../../sql/schemas). Every join between two tables of the same system,
with cardinality and the rows where a key is NULL by design, is listed in
[joins.md](joins.md).

| From | To | Notes |
| --- | --- | --- |
| `shop.orders.id` (as text) | `erp.vbak.bstnk` | Web orders only (`vbak.auart = 'ZWEB'`). For retailer orders (`ZEDI`) `bstnk` holds the retailer PO number. Free text, not a key. About 1 % of web sales documents have `bstnk` NULL or cut to its first 18 characters (data quality noise), so those orders do not match on the full UUID; a truncated reference can still be matched as a prefix of `shop.orders.id`. |
| `erp.vbap.vbeln` | `erp.vbak.vbeln` | Item to header |
| `erp.konv.knumv`, `kposn` | `erp.vbak.knumv`, `erp.vbap.posnr` | Pricing conditions per item |
| `erp.lips.vgbel`, `vgpos` | `erp.vbap.vbeln`, `posnr` | Delivery item to sales item |
| `erp.lips.vbeln` | `erp.likp.vbeln` | Item to header; a cancelled delivery has `is_deleted` set on both |
| `wms.shipments.delivery_no` | `erp.likp.vbeln` | One shipment per delivery |
| `wms.shipment_lines.shipment_id`, `line_no` | `erp.lips` via `delivery_no` | `lips.posnr` is 10 x `line_no`, six digits zero-padded (`000010`) |
| `wms.carrier_events.shipment_id` | `wms.shipments.shipment_id` | |
| `wms.returns.shipment_id`, `delivery_no` | `wms.shipments.shipment_id`, `erp.likp.vbeln` | |
| `wms.return_lines.return_id` | `wms.returns.return_id` | |
| `wms.stock_movements.delivery_no`, `return_id` | `erp.likp.vbeln`, `wms.returns.return_id` | `delivery_no` on goods issues (`GI`) and on return postings (`RT`, and `SC` for scrapped returns), where it is the original delivery; `return_id` on `RT` and `SC` postings of a customer return (RMA), NULL on returned-to-sender parcels. See [joins.md](joins.md#wms) |
| `erp.vbrp.aubel`, `aupos` | `erp.vbap.vbeln`, `posnr` | Billing item to sales item; `000900` is the web shipping charge |
| `erp.vbrp.vgbel` | `erp.likp.vbeln` (`vgtyp = 'J'`) or the original invoice `erp.vbrk.vbeln` (`vgtyp = 'M'`) | Invoices reference the delivery, credit memos the invoice |
| `erp.vbrp.vgbel`, `vgpos` | `erp.lips.vbeln`, `posnr` (`vgtyp = 'J'`) or `erp.vbrp.vbeln`, `posnr` of the original invoice (`vgtyp = 'M'`) | Billing item to delivery item, or credit memo item to invoice item. `vgpos` is NULL on items without a delivery line, such as the shipping charge `000900`. Alternative for invoices: `vbrp.vgbel`, `aupos` to `lips.vbeln`, `vgpos`. |
| `wms.*.item_code` | `erp.mara.matnr` | `matnr = lpad(item_code, 18, '0')`: a formula column on the WMS (child) side joins plain `matnr`, see below |
| `shop.order_lines.ean`, `wms.*.ean` | `erp.mara.ean11` | |
| `erp.vbak.kunnr`, `zzkunwe`; `erp.likp.kunnr`; `erp.vbrk.kunrg`; `erp.kona.kunnr`; `erp.zdeduct.kunnr`; `wms.shipments.consignee_no` | `erp.kna1.kunnr` | Web customers are one collective account per country. `kona` and `zdeduct` hold the retailer's sold-to account. |
| `erp.zkona_item.knuma`; `erp.zdeduct.knuma` | `erp.kona.knuma` | Agreement items to header; on `zdeduct`, set for promotion (`ZP01`), unexplained (`ZX99`) and per-promotion advertising (`ZW01`) claims |
| `erp.zdeduct_item.belnr`; `erp.zdeduct_clr.belnr` | `erp.zdeduct.belnr` | Claim items and clearing items to the deduction |
| `erp.zdeduct.vbeln_vf`; `erp.zdeduct_clr.vbeln_vf` | `erp.vbrk.vbeln` | On `zdeduct`: the invoice a co-op (`ZK01`) or invoice-based advertising (`ZW01`) claim refers to. On `zdeduct_clr`: the invoice short-paid (`zzkind = 'T'`), NULL for repayments (`P`) |
| `erp.zdeduct.vbeln_vl`; `erp.zdeduct.vbeln_va` | `erp.likp.vbeln`; `erp.vbak.vbeln` | Shortage claims (`ZS01`) name the delivery, which reaches `wms.shipments.delivery_no`; service penalties (`ZL01`) name the sales order |
| `erp.zkona_item.kdmat`; `erp.zdeduct_item.kdmat` | `erp.knmt.kdmat`, with `erp.knmt.kunnr` = `kona.kunnr` or `zdeduct.kunnr` | Retailer article numbers. `zkona_item.matnr` is always set; `zdeduct_item.matnr` is NULL when the article has no `knmt` row |
| `mkt.attributions.order_ref` | `shop.orders.id` (as text) | One row per tracked web order (about 92 %); orders whose payment failed have none |
| `mkt.attributions.customer_ref` | `shop.customers.id` (as text) | |
| `mkt.campaigns.promo_ref` | `shop.promotions.id` (as text) | Promotion campaigns only |
| `shop.customers.influencer_code` | `mkt.influencer_codes.code` | Customers who signed up with an influencer code; `mkt.campaigns.influencer_code` and `mkt.attributions.influencer_code` hold the same codes |
| `feeds.feed_loads.source`, `feeds.retailer_sellout.retailer` | `erp.kna1.sortl` (with `kna1.ktokd = 'Z001'`) | The sender's sold-to account: `sortl` is `KRANZ`, `MYLO`, `SHOPNOVA` or `BELLE` on the sold-to rows (`Z001`). `kna1.konzs` does not match (`DROGERIE_K` for Kranz) |
| `feeds.retailer_sellout.article_no`; `feeds.marketplace_sales_daily.product_code`; `feeds.marketplace_inventory_daily.product_code` | `erp.knmt.kdmat`, with `erp.knmt.kunnr` = the sender's sold-to `kunnr` (row above) | Then `knmt.matnr` to `erp.mara.matnr`. About 95 % of articles have a `knmt` row; see [joins.md](joins.md#retailer-article-numbers) for the rest. Marketplace codes are never EANs |

A join on an expression such as the `lpad` above needs the expression as a column on the child
side. Incorta advises against joining on a formula column in another schema, so never add the
formula to `erp.mara`: add a formula column with `lpad(item_code, 18, '0')` to
`wms.shipment_lines` (and to the other WMS tables you join), and join it to the plain
`erp.mara.matnr`. Adding `lpad(item_code, 18, '0') AS matnr` to both queries of the WMS table
gives the same join key as a physical column. In the same way, the `feeds` article mapping needs
the sender's `kunnr` on the feed rows (or a small lookup of the four senders) before it can join
`erp.knmt` on two columns; `kunnr` and `kdmat` together are unique in `erp.knmt`, because each
retailer has one article number per material.

Every parent in these joins must be unique on its join columns, because Incorta uses one parent
row when a child row matches several (see [How to read the tables](joins.md#how-to-read-the-tables)
for join direction, alias tables and cycles).

Some phase 2 facts have no key between them and compare only as aggregates: `mkt.sessions_daily`
against `shop.orders` by date and `source`/`channel` (the shop's `deal_site` source is the
`affiliate` channel), and `feeds.retailer_sellout` region codes, which name the region a
retailer DC serves but are not `kna1` accounts.

## Version notes

The guide follows simulator v0.2.0. Compared with v0.1.0, v0.2.0 adds the `mkt` and `feeds`
schemas and five `erp` tables (`kona`, `zkona_item`, `zdeduct`, `zdeduct_item`,
`zdeduct_clr`); the columns of the v0.1.0 tables are unchanged (see
[After `sim reset`](#after-sim-reset) for adding the new tables to an existing setup).

Incorta 8.2.2 is release **v8.2.2-2026.8**, Service Pack 16 of v8.2.0, released on 16 October
2026. Service packs carry only bug and security fixes, so 8.2.2 has the features of v8.2.0.
Incorta publishes no separate documentation tree for 8.x; the `latest` documentation covers it.
Later 8.2 service packs (8.2.3 to 8.2.6, SP17 to SP20) also exist and share these pages, so a
page may describe a fix made after 8.2.2. The three statements on joins, aliases and join
conditions come from the 5.1 and 5.2 documentation pages and an Incorta Community article. The
statements in this guide are confirmed in these pages:

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
| Joins run from the child (left) to the parent (right) as left outer joins; with several parent matches Incorta uses one row, so parent join columns must be unique | [Joins (5.2 documentation)](https://docs.incorta.com/5.2/concepts-join); Incorta Community: [Data Engineering & Enrichment](https://community.incorta.com/t5/Data-Schemas-Knowledgebase/Data-Engineering-amp-Enrichment/ta-p/338) |
| Aliases are the way to handle two foreign keys to the same parent; an alias inherits the original table's joins as a child but not its joins as a parent (the documented example re-creates the Orders-to-alias join by hand); avoid cyclic joins | [Alias (5.1 documentation)](https://docs.incorta.com/5.1/concepts-alias) |
| Do not join on a formula column in another schema | [Join condition (5.1 documentation)](https://docs.incorta.com/5.1/concepts-join-condition) |

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
- that the weekday of a weekly schedule follows its start date;

Our own suggestions rather than Incorta recommendations: the connection pool of 10, the six
schema names, the plan names and times, and the millisecond truncation of `updated_at` in the
query (Incorta recommends millisecond precision at the source; the truncation is how this guide
achieves it).
