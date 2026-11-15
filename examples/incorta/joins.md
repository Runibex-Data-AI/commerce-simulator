# Joins within each system

This page lists every join between two tables of the same Postgres schema, for the business
schemas that `bi_reader` can read: `shop`, `wms`, `erp`, `mkt` and `feeds`. `shelf` has no
tables yet. The DDL declares no foreign keys, so each join below comes from the DDL in
[sql/schemas](../../sql/schemas), its column comments and the writer code in
[src/cpgsim/writers](../../src/cpgsim/writers). Joins between schemas are in
[Join keys between systems](README.md#join-keys-between-systems) in the loading guide.

## How to read the tables

- **From** is the child (many) side and **To** is the parent (one) side. Define each join in
  Incorta in this direction: the child is the left table and the parent the right table.
  Incorta runs a join as a left outer join, so child rows without a parent are kept with empty
  parent columns. If a child row matches several parent rows, Incorta uses one of them, so the
  To columns must be unique. All To columns on this page are unique (see the bullet on unique To
  columns below).
- **Complete** means every non-NULL From value finds a row in To. This holds only if
  soft-deleted rows are loaded as well, as the loading guide does (see **Soft-deleted and
  cancelled rows** under
  [Settings for every incremental table](README.md#settings-for-every-incremental-table)). Data
  quality noise never breaks referential integrity inside a single system
  ([SPEC](../../docs/SPEC.md#data-quality-noise)).
- **Optional** means the From column is NULL on some rows by design. The note says on which
  rows. A row with a NULL value has no parent; it is not a broken key.
- A join on two columns, for example `erp.lips.vgbel`, `vgpos`, is a single join that uses both
  column pairs.
- Some To columns are unique but are not the table's primary key: `erp.vbak.knumv` and
  `wms.shipments.delivery_no`. Every other To column is the primary key of its table.
- **Second roles and self-joins need alias tables.** An alias is Incorta's documented way to
  handle two foreign keys to the same parent. Create one alias per extra role and join the child
  to the alias: for example `erp.vbak.kunnr` to `erp.kna1` (sold-to) and `erp.vbak.zzkunwe` to
  an alias of `erp.kna1` (ship-to). An alias inherits the joins in which the original table is
  the child (its joins to its own parents), but not the joins in which it is the parent, so
  define each role's join to the alias yourself. Incorta's own [alias
  example](https://docs.incorta.com/5.1/concepts-alias) shows this: after creating the alias it
  re-creates the join from Orders to the alias by hand, because a join in which the original
  table is the parent is not carried over; the alias's joins as a child are. Here that means:
  create the `erp.kna1` alias, then add the join from `erp.vbak.zzkunwe` to it yourself (and the
  same for every other ship-to or payer column you model against the alias). The same applies to
  the self-joins on `erp.vbrp` (credit memo item to invoice item) and `feeds.feed_loads`
  (restatement to restated load): join the table to an alias of itself.
- **Avoid cyclic joins.** A self-join defined on the table itself is a cycle; use the alias
  above. Where this page lists a header join and an item join between the same pair of tables
  (for example `erp.vbrp.aubel` to `erp.vbak` and `erp.vbrp.aubel`, `aupos` to `erp.vbap`,
  which itself joins `erp.vbak`), define only the joins your analysis needs, so that no table
  is reached by two join paths that form a loop.

## `shop`

| From | To | Cardinality | Notes |
| --- | --- | --- | --- |
| `shop.addresses.customer_id` | `shop.customers.id` | many-to-one | Complete. The writer creates one address per customer (`address_type = 'shipping'`), so in practice the relationship is one-to-one. |
| `shop.subscriptions.customer_id` | `shop.customers.id` | many-to-one | Complete. |
| `shop.orders.customer_id` | `shop.customers.id` | many-to-one | Complete. A duplicate customer record (noise) has its own `id`, and the person's later orders can be placed on it. Anonymised customers keep their row. |
| `shop.orders.shipping_address_id` | `shop.addresses.id` | many-to-one | Complete. |
| `shop.orders.billing_address_id` | `shop.addresses.id` | many-to-one | Complete. Always the same value as `shipping_address_id`, so one join to `addresses` is enough. |
| `shop.orders.promotion_id` | `shop.promotions.id` | many-to-one | Optional: NULL when no promotion was applied. The parent can be soft-deleted, because a withdrawn promotion gets `is_deleted = true`. Use this column rather than `promo_code`, which is set for code promotions only. |
| `shop.orders.subscription_id` | `shop.subscriptions.id` | many-to-one | Optional: set only on orders placed by a subscription cycle. |
| `shop.order_lines.order_id` | `shop.orders.id` | many-to-one | Complete. `(order_id, line_no)` is unique. |
| `shop.payments.order_id` | `shop.orders.id` | many-to-one | Complete. One row per payment attempt (`attempt_no`). |
| `shop.refunds.order_id` | `shop.orders.id` | many-to-one | Complete. |
| `shop.refunds.payment_id` | `shop.payments.id` | many-to-one | Optional: NULL when the refund was paid outside the original payment. |
| `shop.promo_redemptions.order_id` | `shop.orders.id` | one-to-one | Complete. One row for each order that has `promotion_id` set. |
| `shop.promo_redemptions.promotion_id` | `shop.promotions.id` | many-to-one | Complete. The parent can be soft-deleted (a withdrawn promotion). |
| `shop.promo_redemptions.customer_id` | `shop.customers.id` | many-to-one | Complete. Same customer as the order. |

`shop` has no product table. `order_lines.sku`, `promotions.skus` and `subscriptions.skus` are
SKU codes, not keys. Product attributes come from `erp.mara` through `ean`, across systems.

## `wms`

| From | To | Cardinality | Notes |
| --- | --- | --- | --- |
| `wms.shipment_lines.shipment_id` | `wms.shipments.shipment_id` | many-to-one | Complete. |
| `wms.carrier_events.shipment_id` | `wms.shipments.shipment_id` | many-to-one | Complete. A duplicate scan (noise) is an extra row with its own `scan_seq`; its parent still exists. |
| `wms.returns.shipment_id` | `wms.shipments.shipment_id` | many-to-one | Complete. `returns.delivery_no` = `shipments.delivery_no` reaches the same row. |
| `wms.return_lines.return_id` | `wms.returns.return_id` | many-to-one | Complete. `line_no` is the return's own numbering, not the shipment line number. |
| `wms.stock_movements.return_id` | `wms.returns.return_id` | many-to-one | Optional: set on `RT` and `SC` postings that belong to a customer return (RMA). NULL on returned-to-sender parcels booked back to stock (`RT` with `delivery_no` only), on scrap postings that are not returns (write-offs of delisted items, recalls) and on all other movement codes. |
| `wms.stock_movements.delivery_no` | `wms.shipments.delivery_no` | many-to-one | Optional: set on goods issues (`GI`) and on return postings (`RT`, and `SC` when a returned item is scrapped), where it is the original delivery. NULL on goods receipts (`GR`), adjustments (`AD`) and scrap postings that are not returns. The To column has a unique index but is not the key. |

`wms.stock_levels_daily` has no parent in `wms`, because the schema has no item or warehouse
table. Its `item_code` joins `erp.mara` across systems.

## `erp`

| From | To | Cardinality | Notes |
| --- | --- | --- | --- |
| `erp.makt.matnr` | `erp.mara.matnr` | many-to-one | Complete. Two rows per material (`spras` `D` and `E`). Filter on one language to get a one-to-one description lookup. |
| `erp.mbew.matnr` | `erp.mara.matnr` | one-to-one | One row per material (`bwkey` `1000`). The service material `SHIPPING` has no `mbew` row, so a cost lookup for it finds nothing. |
| `erp.knmt.matnr` | `erp.mara.matnr` | many-to-one | Complete. |
| `erp.knmt.kunnr` | `erp.kna1.kunnr` | many-to-one | Complete. The retailer's sold-to account. Not every retailer article has a `knmt` row (about 95 % do); see [Retailer article numbers](#retailer-article-numbers). |
| `erp.vbak.kunnr` | `erp.kna1.kunnr` | many-to-one | Complete. Sold-to party: the retailer's sold-to account, or the country's collective web customer. |
| `erp.vbak.zzkunwe` | `erp.kna1.kunnr` | many-to-one | Complete. Ship-to party: a retailer distribution centre, or the collective web customer. A second role of `kna1`. |
| `erp.vbap.vbeln` | `erp.vbak.vbeln` | many-to-one | Complete. |
| `erp.vbap.matnr` | `erp.mara.matnr` | many-to-one | Complete, including the freight item `000900` (`matnr = 'SHIPPING'`). |
| `erp.konv.knumv` | `erp.vbak.knumv` | many-to-one | Complete. `knumv` is unique on `vbak` but is not its key. Only sales documents have pricing conditions; billing documents have none. |
| `erp.konv.knumv`, `kposn` | `erp.vbap.vbeln`, `posnr` (through `erp.vbak`) | many-to-one | Complete. `konv` has no `vbeln` column. Resolve `knumv` to `vbak.vbeln`, then match `kposn` = `vbap.posnr`. Each item has several rows, one per condition. `vbeln` has to come from `erp.vbak`: join `konv` to `vbak` on `knumv` and reach `vbap` through the header, or add `vbeln` to both `konv` queries with a SQL join to `erp.vbak` on `knumv`. |
| `erp.likp.kunnr` | `erp.kna1.kunnr` | many-to-one | Complete. Ship-to party. |
| `erp.likp.kunag` | `erp.kna1.kunnr` | many-to-one | Complete. Sold-to party. |
| `erp.lips.vbeln` | `erp.likp.vbeln` | many-to-one | Complete. A cancelled delivery is soft-deleted on the header and on its items in the same commit. |
| `erp.lips.vgbel` | `erp.vbak.vbeln` | many-to-one | Complete. Delivery item to sales document header. |
| `erp.lips.vgbel`, `vgpos` | `erp.vbap.vbeln`, `posnr` | many-to-one | Complete. A sales item has several delivery items when it is delivered in parts. Items of cancelled deliveries still point to their sales item, so filter on `likp.is_deleted`. |
| `erp.lips.matnr` | `erp.mara.matnr` | many-to-one | Complete. |
| `erp.vbrk.kunrg` | `erp.kna1.kunnr` | many-to-one | Complete. Payer. |
| `erp.vbrk.kunag` | `erp.kna1.kunnr` | many-to-one | Complete. Sold-to party. |
| `erp.vbrp.vbeln` | `erp.vbrk.vbeln` | many-to-one | Complete. |
| `erp.vbrp.aubel` | `erp.vbak.vbeln` | many-to-one | Complete for invoices and credit memos. Billing item to sales document header. |
| `erp.vbrp.aubel`, `aupos` | `erp.vbap.vbeln`, `posnr` | many-to-one | Complete for invoices and credit memos. `aupos = '000900'` is the web shipping charge. |
| `erp.vbrp.vgbel` | `erp.likp.vbeln` | many-to-one | Invoice items only (`vgtyp = 'J'`). NULL on invoice items billed without a delivery. The number ranges do not overlap (deliveries `0080...`, billing documents `0090...`), so a plain join matches only delivery references. |
| `erp.vbrp.vgbel`, `vgpos` | `erp.lips.vbeln`, `posnr` | many-to-one | Invoice item to delivery item. `vgpos` is NULL on items without a delivery line, such as the shipping charge `000900`. Alternative for invoices: `vbrp.vgbel`, `aupos` to `lips.vbeln`, `vgpos`. |
| `erp.vbrp.vgbel` | `erp.vbrk.vbeln` | many-to-one | Credit memo items only (`vgtyp = 'M'`): points to the original invoice. |
| `erp.vbrp.vgbel`, `vgpos` | `erp.vbrp.vbeln`, `posnr` | many-to-one | Credit memo item to the original invoice item (a self-join). Credit memos only. |
| `erp.vbrp.matnr` | `erp.mara.matnr` | many-to-one | Complete. |
| `erp.kona.kunnr` | `erp.kna1.kunnr` | many-to-one | Complete. The retailer's sold-to account. |
| `erp.zkona_item.knuma` | `erp.kona.knuma` | many-to-one | Complete. |
| `erp.zkona_item.matnr` | `erp.mara.matnr` | many-to-one | Complete. |
| `erp.zdeduct.kunnr` | `erp.kna1.kunnr` | many-to-one | Complete. Payer: the retailer's sold-to account. |
| `erp.zdeduct.knuma` | `erp.kona.knuma` | many-to-one | Optional: set on `ZP01` and `ZX99`, and on per-promotion `ZW01` for retailers that have promotion agreements. NULL otherwise. When set, the agreement always exists. |
| `erp.zdeduct.vbeln_vl` | `erp.likp.vbeln` | many-to-one | Optional: shortage claims (`ZS01`) only. |
| `erp.zdeduct.vbeln_va` | `erp.vbak.vbeln` | many-to-one | Optional: service penalties (`ZL01`) only. |
| `erp.zdeduct.vbeln_vf` | `erp.vbrk.vbeln` | many-to-one | Optional: co-op (`ZK01`) and invoice-based advertising (`ZW01`) only. |
| `erp.zdeduct_item.belnr` | `erp.zdeduct.belnr` | many-to-one | Complete. Only `ZP01`, `ZX99` and `ZS01` claims have items. |
| `erp.zdeduct_item.matnr` | `erp.mara.matnr` | many-to-one | Optional: NULL when the retailer article has no `knmt` row. |
| `erp.zdeduct_clr.belnr` | `erp.zdeduct.belnr` | many-to-one | Complete. |
| `erp.zdeduct_clr.vbeln_vf` | `erp.vbrk.vbeln` | many-to-one | Optional: set on short payments (`zzkind = 'T'`), NULL on repayments (`P`). |

**Exchange rates.** `erp.tcurr` is a lookup by currency and month, not a key join. Match
`fcurr` = `waerk`, `tcurr` = `'EUR'` and `gdatu` = the first day of the month as `YYYYMMDD`. Only
`CHF` has rates; EUR documents need none. `vbak.kurrf` and `vbrk.kurrf` already hold the rate
each document used: the month of the order date, or of the billing date. A credit memo keeps the
rate of its original invoice.

**Account groups.** `erp.kna1.konzs` groups a retailer's sold-to account with its distribution
centres. It holds a short retailer code, not a `kunnr`, so use it to group rows. It is not a
join to another `kna1` row.

### Retailer article numbers

`erp.vbap.kdmat`, `erp.zkona_item.kdmat` and `erp.zdeduct_item.kdmat` carry the retailer's
article number. `erp.knmt` maps `kunnr` and `matnr` to `kdmat`, but `kdmat` is not part of its
key and about 5 % of retailer articles have no row. Join `vbap` and `zkona_item` to `mara` on
`matnr`, which is always set. Their `kdmat` and `matnr` pairs also recover the missing `knmt`
rows. To look up `knmt` from one of these item tables, use the header's `kunnr` together with
`kdmat`: `vbak.kunnr`, `kona.kunnr` or `zdeduct.kunnr`. All three hold the retailer's sold-to
account, the same account as `knmt.kunnr`.

The `feeds` tables carry the same article numbers (`retailer_sellout.article_no`) and the
marketplace's product codes (`product_code`), but no `kunnr`. Their join to `erp.knmt` goes
through the sender's sold-to account and is listed in
[Join keys between systems](README.md#join-keys-between-systems). For an article without a
`knmt` row, use the `kdmat` and `matnr` pairs recovered above, for the same sold-to account.

## `mkt`

| From | To | Cardinality | Notes |
| --- | --- | --- | --- |
| `mkt.campaigns.influencer_code` | `mkt.influencer_codes.code` | many-to-one | Optional: set on influencer campaigns only. Complete when set. |
| `mkt.campaign_spend_daily.campaign_id` | `mkt.campaigns.campaign_id` | many-to-one | Complete. One row per campaign and day. |
| `mkt.attributions.campaign_id` | `mkt.campaigns.campaign_id` | many-to-one | Optional: NULL for organic, direct, app and marketplace spillover traffic, and when no campaign of the order's channel was running. |
| `mkt.attributions.influencer_code` | `mkt.influencer_codes.code` | many-to-one | Optional: set on influencer-sourced orders only. Complete when set. |

`mkt.sessions_daily` has no key to the other `mkt` tables. Compare it with them only as
aggregates by date and channel. In the same way, `campaign_spend_daily.platform_conversions`
compares with a count of `attributions` rows by `campaign_id` and `spend_date` =
`attribution_date`. This is an aggregate comparison, not a join.

## `feeds`

| From | To | Cardinality | Notes |
| --- | --- | --- | --- |
| `feeds.retailer_sellout.load_id` | `feeds.feed_loads.load_id` | many-to-one | Complete. A load and its rows are written together. |
| `feeds.marketplace_sales_daily.load_id` | `feeds.feed_loads.load_id` | many-to-one | Complete. |
| `feeds.marketplace_inventory_daily.load_id` | `feeds.feed_loads.load_id` | many-to-one | Complete. |
| `feeds.feed_loads.restates_load_id` | `feeds.feed_loads.load_id` | many-to-one | Optional: set on restatements only (a self-join). |

Every `feeds` table is insert-only, and a restated period arrives as a new load. Before
aggregating, keep the latest load per period on `feed_loads` (latest `delivered_at`, then
highest `load_id`, per `source`, `feed_type` and `period_start`; see
[Reading feed restatements](README.md#reading-feed-restatements)); otherwise a restated period
is counted twice. `retailer_sellout.retailer` holds the same value as `feed_loads.source`, so
no extra join is needed for the sender.

`marketplace_sales_daily` and `marketplace_inventory_daily` share `report_date` and
`product_code`, but neither is the parent of the other: the sales table has rows only for
products with units or views, the inventory table only for products with stock or inbound
units. Keep the latest load on each side first, then compare them per day and product as a
full outer comparison, not a key join.
