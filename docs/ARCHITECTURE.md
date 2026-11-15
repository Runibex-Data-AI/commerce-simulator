# Architecture

Developer notes for the generator: how the engine works, the module contracts
and the conventions each source system follows. The behaviour is specified in
[SPEC.md](SPEC.md). This document covers how the code implements it.

## Layers

```
config ──► engine (clock, queue, entity store, rng, tick loop)
              │  handlers(world, event)
              ▼
           domain (business logic: catalogue, customers, web, fulfilment, retail,
              │     feeds, deductions, marketing, ...) and chaos (data quality noise)
              │  world.emit(DomainEvent)
              ▼
           writers (shop, wms, erp, mkt, feeds, truth) ──► WriteBatch ──► one Postgres transaction per commit
```

* **Business logic never writes SQL.** It mutates entities in `world.store`,
  schedules future work with `world.schedule(...)` and reports facts with
  `world.emit(SomeDomainEvent(at=world.now, ...))` (`domain/events.py`).
* **Writers never decide anything.** They translate domain events into each
  system's rows, keys and quirks, using `writers/keys.py` for every key.
* **One transaction per commit**: business rows, entity-state delta, queue
  delta, clock and run log. The spec allows one transaction per schema. A single
  transaction is stricter and makes restart safety exact.

## Determinism rules (read before writing a handler)

1. All randomness comes from `world.rng(*parts)`, where the parts name the
   decision (`world.rng("web.payment", order_id, attempt)`). Never use
   `random`, `numpy.random` global state, `uuid4`, `time.time()` or
   `datetime.now()` in domain code.
2. Event keys are unique and derived from content
   (`f"web.pick:{delivery_id}"`), never from a counter that depends on push order.
3. Neutral ids come from `world.next_id("web_order")` etc., called only in
   handlers, which run in a deterministic order.
4. Iterate dicts/sets of entities in sorted order (`store.items(kind)` is sorted).
5. Arrival sources (`@arrival_source`) are pure functions of seed, config,
   scenario windows and time. They must not read entity state.
6. Call `world.store.mut(kind, id)` before changing an entity.
7. Timestamps: `world.now` is the event's due time (aware UTC). Business
   time-of-day logic uses local time (`world.local()`, `world.at_local()`).
   Nothing may depend on the Postgres session time zone: `db.connect` pins
   `TimeZone=UTC`, and the engine normalises every `timestamptz` it reads
   (queue `due`, clock, overrides) and every entity datetime to UTC, so a
   restart on a session with a DST zone resumes with UTC arithmetic.
8. Floating-point money is rounded to cents at creation (`round(x, 2)`).

Consequence: the same seed, config and history start give the same rows,
whatever the tick length, commit batching or restarts.

## Engine

| Module | Role |
| --- | --- |
| `engine/rng.py` | `derive_seed`, `Rng` (random.Random + poisson, lognormal_median, gamma_mean, weighted_index), `stable_uuid` |
| `engine/queue.py` | `Event(due, kind, key, data, priority)`, heap ordered by `(due, priority, key)`, delta persistence to `sim.event_queue` |
| `engine/entities.py` | `EntityStore` with `get / mut / put / remove / items`, dirty tracking, persistence to `sim.entity_state` |
| `engine/codec.py` | JSON codec for dataclass entities |
| `engine/sink.py` | `TableSpec`, `TableBuffer.insert/update/soft_delete`, `WriteBatch.flush` (COPY + set-based UPDATE), loading-contract enforcement |
| `engine/world.py` | `World`: `now`, `rng`, `schedule`, `emit`, `next_id`, `store`, `ref`, `scenarios`, local-time helpers |
| `engine/registry.py` | `@handler(kind)`, `@arrival_source(name)`, `@bootstrap(order)`, `@on_load(order)` |
| `engine/runner.py` | `Simulation.open / backfill / run_live / commit` |

Event `data` must be JSON-serialisable (ints, floats, strings, lists, dicts).
Put ids in events and keep state in entities.

## Domain modules and their contracts

Reference data is rebuilt from seed and config at start-up (`world.ref`):

* `domain/reference.py`: `ReferenceData(catalogue, geo, accounts, calendar)`, `build_reference(cfg)`.
* `domain/catalogue.py`: `Catalogue` with `skus`, `get(sku_id)`, `by_ean(ean)`,
  `active(d)`, `siblings(sku_id)`, `affinity`, `retailer_assortment(channel, d)`,
  `customer_article(channel, sku_id)`, `lists(channel, sku_id)` (listing per channel; a channel with
  `brand_assortment`, the perfumery, lists by brand role); `CATEGORIES`.
* `models/retail_calendar.py`: 4-4-5 retail months of ISO weeks (`retail_period`, `retail_quarter`,
  `quarter_weeks`) for the perfumery's monthly sell-out and the AC1 quarters.
* `domain/geography.py`: `Geography.sample_address(rng, country) -> Address`
  (population-weighted real DACH postcodes with ISO 3166-2 region),
  `regions(country)`.
* `domain/people.py`: `person(seed_int, country) -> dict` (first/last name,
  email on reserved example domains, phone, birth year, street, house number).
* `domain/accounts.py`: `Accounts` with `all`, `numbers` (account id → kunnr
  number), `web_collective(country)`, `retailer(channel)`, `dcs(channel)`.
* `models/calendar.py`: `Calendar` with holidays for DE, AT and CH (`holidays` package),
  `is_working_day`, `next_working_day`, `add_working_days`, `black_friday`, `mothers_day`.

Pure models (no state, no I/O) live in `models/`: seasonality, arrivals
(NHPP thinning), delays (log-normal with working-day gaps), baskets, payments,
repurchase, pricing and elasticity.

Stateful business logic lives in `domain/`. Each module registers its handlers:

| Module | Owns | Public API used by other modules |
| --- | --- | --- |
| `masterdata` | Material/account master emission, monthly FX, annual cost revision | `fx_rate(world, currency, d) -> float` (EUR per unit), `std_cost(world, sku_id, d) -> float` |
| `demandplan` | Expected consumer demand per SKU, channel and day; web demand windows (events, promotions) for repeat customers | `sku_weights(world, channel, d) -> (sku_ids, cum_weights)`, `expected_daily_units(world, sku_id, d, channel=None)`, `repeat_lift(world, sku_ids, d)`, `demand_windows(world, sku_ids, d0, d1) -> list[DemandWindow]` |
| `promotions` | Web and retailer promotion calendar, lift and dip | `active_web_promos(world, t)`, `web_promo_for_basket(world, t, sku_ids, rng)`, `demand_factor(world, sku_id, channel, d)`, `retail_promos(world, channel, d)` |
| `billing` | Invoices, credit memos, clearing (called by fulfilment, returns and retail) | `create_invoice(...)`, `create_credit_memo(...) -> BillingDoc \| None` (None when nothing is left to credit), `clear(world, billing_id, amount=None, status=None)` |
| `warehouse` | Central stock, production replenishment, daily snapshots, the `hero_oos_substitution` quality block (`wh.quality_block`: QI-BLOCK, held receipts, QI-RECALL) | `available`, `reserve`, `unreserve`, `issue`, `receive_return` |
| `customers` | Web customer creation, repurchase intents (event/promotion conversion and pull-forward), deal seekers of `promo_margin_trap` | `new_customer(world, rng, acquisition=None) -> Customer`, `schedule_repurchase(world, customer, order)`, `cherry_pick(world, rng, lines)`, `deal_return_rate(world, order)` |
| `web` | Web arrivals, basket, pricing, payment, release, cancellation, backorders | `place_order(world, customer, lines, source, subscription_id=None) -> WebOrder` |
| `fulfilment` | Deliveries for web and retail: pick, ship, carrier scans, outcomes, web invoicing at shipment | `create_delivery(world, kind, sales_doc, lines, ship_to_account, ship_to, carrier, planned_ship_date, order_id, seq_in_order=1) -> Delivery`, `cancel_delivery(world, delivery_id, reason)` |
| `returns` | Web returns, refunds, credit memos | `maybe_schedule_return(world, order, delivery)`, `refund(world, order, amount, reason, return_id=None, credit_items=None, include_shipping=False, delivery_id=None) -> refund_id \| None` |
| `subscriptions` | Subscription lifecycle and recurring orders (never gift, sun or after-sun products) | `maybe_start_subscription(world, customer, order)` |
| `retail` | Hidden retailer demand and inventory per DC (weekly sell-out, daily for the marketplace), order weekdays, event-aware order-up-to targets, `retailer_overstock` push mode, hero substitution on lost units, retailer POs, confirmation, delivery planning, invoicing after proof of delivery, payment net of deductions | none (hands each period to `feeds` and `deductions`) |
| `feeds` | Feed accumulators per period (`feed_acc`), delivery at each sender's schedule (`feeds.delivery`), restatements as new loads | `record_sellout_week(world, channel, dc, week_start, sold, lost, demand, on_hand)`, `record_marketplace_day(world, channel, dc, day, sold, demand, on_hand, on_order)` |
| `deductions` | Trade promotion agreements, retailer claims by kind, matching, disputes, short payment with partial taking, repayment (`deduction`, `deduction_book`, `promo_claim`) | `record_sellout(...)`, `record_pod(...)`, `record_cut(...)`, `take_on_payment(world, channel, billing_id, gross) -> float` (amount short-paid) |
| `marketing` | Pure campaign calendar, spend-driven acquisition, attribution and the daily `mkt` export (`mkt_day`); influencer codes and their first-order promotion | `new_customer_multiplier(world, t)`, `acquisition_mix(world, t)`, `pick_influencer_code(world, rng, t)`, `note_order(world, order, customer, source, new_customer)`, `influencer_promotion(world, code)` |
| `scenarios/manager` | Scenario windows from config and CLI (self-limiting windows end at `start + duration`), truth recording | `world.scenarios.active(scenario_id, t) -> list[ScenarioWindow]`, `factor(name, t, **ctx) -> float` (lifetime factors such as `repurchase` keep applying after the window to entities acquired inside it), `transfers(name, t, world=world, **ctx)` (hero substitution of expected units), `visible_from(window)`, `record_effect(...)` |
| `chaos` (package) | Data quality defects (see Chaos below) | `hit(world, defect, *ids)`, `record(...)` and the seams called from customers, web, fulfilment, returns, masterdata and deductions |

Fulfilment hands lifecycle outcomes back through `world.schedule` events
handled by the owning module (`"web.delivery_outcome"`, `"retail.pod"`), so
fulfilment does not import `web` or `retail`. Their data: `web.shipped`
`{delivery_id, order_id, billing_id}`, `web.delivery_outcome`
`{delivery_id, order_id, outcome}`, `web.delivery_exception`
`{delivery_id, order_id}`, `retail.shipped` / `retail.pod`
`{delivery_id, order_id}`.

Scenario switches: the engine reads `sim.scenario_overrides` at a fresh start,
on resume, before every live tick and at the start of every backfill pass,
under a Postgres advisory lock (`db.SCENARIO_LOCK`) that `sim scenario
enable/disable` also takes while it picks the switch time and inserts the row.

* **One clock.** Both sides take the time from the database clock
  (`clock_timestamp()`) under the lock: the CLI uses
  `at = max(database clock, committed sim clock + 1 us)`, the engine advances
  to the database clock (capped at its own wall clock). A switch the engine did
  not see therefore lies after every slice it computed, whatever the clocks of
  the hosts running the CLI and the generator.
* **Causal visibility.** A row applies exactly from its `at`:
  `ScenarioManager.advance_to(slice start)` hides rows whose `at` has not been
  reached (from `factor()`, `active()`, `scenario_promotions()`, `version`
  and look-ahead consumers such as demand forecasts), and the runner splits
  slices at `next_switch()` so no slice straddles a switch. An engine that
  knew a switch hours early (down, then catching up) writes the same rows as
  one that saw it a second before (`tests/e2e/test_live.py`).
* **Late windows are reconciled.** A row first read with `at` at or behind the
  clock (manual SQL, a clock step) gets `at = clock + 1 us`, written back to
  `sim.scenario_overrides` in the locked transaction; a config window added to
  the config after the start (it is not in the `scenario_ledger` entity) gets
  `known_at = clock`, persisted in the ledger, and starts no earlier than
  `known_at + 1 us`, with its promotion announced no earlier than that. Window
  starts and announcements are emitted for `(slice start, slice end]` only, so
  without this they would be lost and redemptions would point at a promotion
  that was never written.
* **Early end.** A disable closes the window (`ScenarioWindowClosed`, truth)
  and, in the same `scen.close` handler, ends an announced scenario promotion:
  the stored `Promotion` gets the new end and `PromotionChanged` updates
  `shop.promotions.ends_at` (and `updated_at`; withdrawn at its start: soft
  deleted).

## Operations

**Single writer.** `Simulation.open` takes the session-level advisory lock
`db.WRITER_LOCK` and holds it for the life of the engine (it survives the
per-tick commits and goes with the connection). A second engine refuses to
start (`sim start` waits up to two minutes for a previous container's session
to go away). `sim reset` takes the same lock for its transaction and refuses
while a generator holds it; `--force` terminates the generator's session
first. As defence in depth every commit is fenced:
`UPDATE sim.clock ... WHERE seed = ? AND sim_time = <last committed>` must hit
one row, or the whole commit rolls back, so a writer whose database was reset
underneath it stops instead of writing into the new timeline. A fresh start
refuses leftover `sim.entity_state` / `sim.event_queue` rows without a clock.

**Config changes.** `Config.fingerprint()` hashes the business-relevant
config only (not `database`, `tick_seconds`, `history_months` or `backfill`).
`sim.clock.config_fingerprint` keeps the fingerprint the simulation started
with; resuming with a different one logs a warning and adds one row per new
fingerprint to `sim.config_history` (old, new, sim time), which `sim status`
reports. A fingerprint written by an older version (which also hashed the
tick and backfill settings) is recognised (`legacy_fingerprint`) and upgraded.

**Contract timestamps in live mode.** The backfill stamps `created_at` /
`updated_at` with the event time (deterministic, realistic history). In
`Simulation.run_live`, including the catch-up after downtime, the sink stamps
them with the commit time instead (`WriteBatch.flush(stamp=...)`, taken just
before the business rows are written, never earlier than the slice end);
business timestamp columns (`placed_at`, `shipped_at`, `*_local`, ...) keep the
event time. Guarantee: every row committed live has `updated_at` later than
the commit that preceded it and at most the commit duration (milliseconds for
a tick, up to seconds for a large catch-up commit) before it became visible.
A watermark on `max(updated_at)` of the loaded rows is therefore exact; a
watermark taken from the loader's clock (last load start) needs an overlap of a
few seconds plus clock skew, with an upsert on the primary key. `sim backfill`
on an existing simulation keeps event-time stamping: catch up with `sim run`
once a BI tool loads.

**Memory.** Resume streams `sim.entity_state` and `sim.event_queue` through
server-side cursors (JSON text decoded row by row). Customers who can never
act again (no open web order, no subscription, no live repurchase intent) are
evicted from the store (`customers.maybe_evict`, on lapse, on subscription
cancellation and when their last order is cleaned up; a one-off `on_load` pass
counts open orders for states written before). Their shop rows stay; ids are never reused. New code must not
look customers up in the store by id unless an open order, intent or
subscription guarantees they are there, and nothing written may depend on
whether an inactive customer is still in the store (`tests/e2e/test_live.py`
compares a run with eviction switched off). Phase-2 entity kinds are removed
when their lifecycle ends: `feed_acc` after delivery (at most two per channel
remain), `promo_claim` after the claim, `deduction` when the case closes (and
from its payer's `deduction_book`), `mkt_day` after the daily export;
`chaos_ledger` keeps the erased customer ids (small). Measured on a 24-month state: resident
memory after resume 880 MB before, 415 MB on the first resume (eviction pass),
250 MB afterwards.

**Housekeeping.** `sim.entity_state` and `sim.event_queue` are rewritten on
every commit (upserts and deletes, about 7,000 rows per simulated day in a
backfill), faster than autovacuum reclaims the dead rows: a 24-month backfill
left `sim.entity_state` at 480 to 890 MB for 62 MB of live state. The
engine therefore vacuums both tables between commits every
`runner.VACUUM_EVERY_STATE_ROWS` state rows (`SKIP_LOCKED`: a table
autovacuum is busy with is left to it, the engine never waits), `sim.sql` gives
`sim.entity_state` a fill factor of 70 (upserts stay on their page as HOT
updates) and both tables a 5 % autovacuum threshold, and `sim start` /
`sim backfill` finish with `Simulation.after_backfill`: `VACUUM (ANALYZE)` of
the state tables and `ANALYZE` of the business and truth schemas, so BI
queries plan on fresh statistics right away. Housekeeping changes no rows.
Measured on a 24-month backfill: `sim.entity_state` about 220 MB (92 % HOT
updates), `sim.event_queue` 31 MB, the `sim` schema 250 MB instead of 557 MB;
the 140 or so periodic vacuums did not measurably slow the backfill (168 s
with, 167 s without them on a 6-month A/B run), and the final pass took 5 to
10 seconds. Postgres logs a `skipping vacuum ... lock not available` warning
when a periodic vacuum meets a running autovacuum (up to about ten times per
backfill).

## Source system conventions

All business tables follow the loading contract (SPEC § Loading contract):
a primary key; `created_at timestamptz NOT NULL` and
`updated_at timestamptz NOT NULL` with an index on `updated_at`; transactional
tables also have `is_deleted boolean NOT NULL DEFAULT false` and
`deleted_at timestamptz`. Snapshot tables are insert-only and carry a
`load_id`. **Contract columns are `timestamptz` in every schema**, including
`wms`, so a watermark on `updated_at` is safe across DST changes. The
"local time without time zone" quirk applies to `wms` *business* timestamps
(`*_local` columns), not to the contract columns.

### `shop` (web shop platform)

UUID primary keys (`writers/keys.py`), English snake_case, `timestamptz` in
UTC, statuses as lower-case words (the neutral vocabulary), money as
`numeric(12,2)` with an ISO currency column.

* Net/tax split: one rule for every system, `models.pricing.split_gross`
  (net rounded half-up, tax the remainder). The shop writer's `included_tax`
  uses it, so shop line/order tax equals the ERP `vbap.mwsbp` to the cent.
* A status recognised later than it happened (the order becomes `shipped` /
  `delivered` when its backordered remainder is cancelled) carries
  `WebOrderStatusChanged.effective_at`: the status timestamp column gets the
  business time (last goods issue / delivery scan), `updated_at` the change time.
* A parcel returned to sender or lost (and its refund) does not end the order
  when a backordered remainder ships later: that shipment moves the order on to
  `shipped` and `delivered` (`web._only_parcels_failed`); a customer return
  keeps its status. A return of an earlier parcel can start before the rest
  of a split order ships or its backorder is cancelled: the return status
  stays and `WebOrderMilestoneReached` sets only `shipped_at` / `delivered_at`
  (at the last goods issue / delivery scan); a `refunded` order becomes
  `partially_refunded` when the remainder is captured.
* A rejected return reduces `order_lines.qty_returned` again and moves a
  `returned` order to `return_rejected` (no refund). Refunds are `issued`, then
  `completed` (`RefundChanged`) when the payment provider has paid out, by
  payment method hours to a week later (`returns.REFUND_COMPLETION_DAYS`).
* Promotions: one per order (`orders.promotion_id`, `promo_code` for code
  promotions, one `promo_redemptions` row). The best calendar promotion for the
  basket wins (`promotions.web_promo_for_basket`). A customer acquired with an
  influencer code (`customers.influencer_code`) gets
  `marketing.influencer_first_order_discount_pct` (15 %) off the goods, not
  shipping, of their first order, with the code as `promo_code`: one
  `promotions` row per code (code promotion, sitewide, valid from the code's
  `valid_from`, open ended to 2099-12-31, named "... off the first order"),
  created at the history start. Ids `800_000 +` the code's position in the
  sorted code list (`marketing.influencer_promotion`); the code promotions are
  not in the promotion calendar (no demand lift, never offered to anyone else).
  One code per order: when any other promotion takes effect on that first order
  (a regular web promotion or the `promo_margin_trap` promotion, automatic or a
  redeemed code), it applies alone and the influencer discount is dropped, not
  carried over to a later order (it is a first-order offer). So every planted
  scenario's redeemed orders and truth stay as they were. "First order" is the
  person's first order (`Customer.orders == 0`): a chaos duplicate account's
  first order does not get it, although the shop sees a new account with the
  code (`is_repeat_customer` false). The free-shipping threshold is tested on
  the goods before the code's discount (after it for every other promotion), so
  the code never adds a shipping fee to a basket that ships free at list
  prices. No rng draw is involved. The customer stays
  attributed to the code either way (`signup_source = 'influencer'`,
  `influencer_code` unchanged), even when `promo_code` / `promotion_id` carry
  the other promotion.

### `wms` (warehouse and shipping)

Integer keys, two-letter status codes, local Europe/Berlin
`timestamp without time zone` for business times. References the ERP
delivery number (`delivery_no`, ten digits), never the shop order.

| Neutral | Code | | Neutral | Code |
| --- | --- | --- | --- | --- |
| open | OP | | delivered | DL |
| picked | PK | | exception | EX |
| shipped | SH | | returned_to_sender | RS |
| in_transit | IT | | lost | LS |
| out_for_delivery | OD | | cancelled | CX |

Returns: requested RQ, label_issued LI, received RC, inspected IN, refunded RF, rejected RJ.
Stock movement types: goods_receipt GR, goods_issue GI, customer_return RT, scrap SC, adjustment AD.

### `erp` (SAP-style)

Modelled on SAP SD and master data. It is a plausible imitation of the
structure, not a replica. Ten-digit document numbers (sales `0010…`,
deliveries `0080…`, billing `0090…`), 18-character zero-padded material
numbers, six-digit item numbers, SAP field names. Header amounts in document
currency (`waerk`) plus EUR (`netwr_eur`, at the month's rate `kurrf`).

| Descriptive name (SPEC) | Table(s) |
| --- | --- |
| materials | `mara`, `makt` (descriptions) |
| customers | `kna1` |
| customer_materials | `knmt` |
| sales_doc_header / item | `vbak` / `vbap` |
| delivery_header / item | `likp` / `lips` |
| billing_header / item | `vbrk` / `vbrp` (invoices `fkart = 'F2'`) |
| credit_memos | `vbrk` / `vbrp` with `fkart = 'G2'`, `vbtyp = 'O'` (SAP keeps credit memos in the billing tables) |
| price_conditions | `konv` |
| standard_costs | `mbew` |
| exchange rates (extra) | `tcurr` |
| deductions | `zdeduct` (claim header), `zdeduct_item` (claimed articles and units), `zdeduct_clr` (clearing items: taken by short payment `T`, repaid `P`). SAP has no SD standard table for them; real systems keep them in FSCM dispute management or a customer table |
| trade promotion agreements (extra) | `kona` (header, `knuma`) / `zkona_item` (promoted materials, scan-back amount per unit) |

Status fields follow the S/4HANA style on the header and item tables
(`gbstk`, `lfstk`, `fkstk`, `wbstk`, `kostk`; values blank / A / B / C).
Every step that can complete a document (delivery, goods issue, billing,
cancelling a web order's backordered remainder) refreshes the header with
`billing.refresh_header` in the same `SalesDocChanged`, and an item cut after
delivery carries its delivered/billed quantities with the new confirmed
quantity, so header and item statuses never disagree.

The web shipping charge is a freight item of its own: item `000900`
(`SalesItem.item_type = "freight"`, `sku_id` 0) with the service material
`SHIPPING` (`mara.mtart = 'DIEN'`, unit `LE`, item category `TAD`, seeded by
`masterdata` through `ServiceMaterialListed`), its own `HD00` and `MWST`
conditions, and billed once as invoice item `000900` (`aupos` `000900`). It is
never reserved, picked or delivered and counts for the billing status only.
So `vbak.netwr + sum(vbap.mwsbp)` equals the shop order total, the non-tax
`konv` conditions of an item add up to `vbap.netwr`, and invoices minus
credit memos equal the shop total for a fully shipped, non-returned order
(`tests/e2e/test_reconciliation.py`). `ZPRO` carries the promotion's nominal
rate in `kbetr`, the cent-rounded amount in `kwert`. An influencer code's
first-order discount has a condition type of its own, `ZINF` (step 040, same
rate/amount rule), not `ZPRO`: the sales order has no promotion reference, so
the condition type is how finance tells an acquisition discount (paid for
alongside the influencer's commission) from a calendar promotion, and `ZPRO`
keeps meaning calendar promotions. One promotion per order, so an item carries
`ZPRO` or `ZINF`, never both.

Master data seeded at history start is back-dated: `mara.ersda` is the
listing date (launch - 60 days, `SkuListed.listed_on`) so a delisting date
never precedes it, and `kna1.erdat` is a deterministic account-opening date
5-13 years earlier (`B2BAccountCreated.opened_on`), before any back-dated web
customer. An unknown catalogue category has no material group and fails
loudly (`MATKL`).

**Trade deductions** (`domain/deductions.py`). Deduction kinds per channel
come from `deductions.types` (Kranz and Mylo: promotion, advertising fee per
promotion, shortage, service penalty; Shopnova: promotion, co-op percentage of
every invoice, shortage, service penalty; Belle: promotion, advertising
percentage of invoice value, shortage; every retailer can raise unexplained
claims). `zztype`: ZP01 promotion scan-back, ZW01 advertising, ZK01 co-op,
ZP05 clearance (`retailer_overstock`), ZS01 shortage, ZL01 service penalty,
ZX99 unexplained. Claims are matched or disputed, then accepted (in full,
partly or as goodwill) or reversed. A dispute over a claim with a verifiable
part (over agreement, D01; clearance) always settles at the verified amount
and the excess is repaid; only a claim with nothing verifiable (no promotion,
POD complete, duplicate) can be reversed. Payment of a retail invoice short-pays the
open deductions in ascending case order with partial taking, so a claim larger
than one invoice is split across invoices (`zdeduct_clr` rows with
`vbeln_vf`); the unaccepted part is repaid. **No credit memo is booked for an
accepted deduction in phase 2**: accepted trade funding is
`zdeduct.zzaccepted`, and trade revenue in `vbrk` is gross of it. Every case
reconciles to the cent: per paid retail invoice `zzpaid_amount + Σ taken =
netwr + mwsbk` (R1), per closed case `Σ T - Σ P = zzaccepted` (R2), per
promotion claim `wrbtr = Σ zdeduct_item.wrbtr` with an existing `kona` (R3).
A closed case leaves the store, so open deductions never accumulate.

### `feeds` (retailer and marketplace data feeds)

Flat, file-like rows as each sender delivers them (`writers/feeds.py`). One
`feed_loads` row per delivered file (`load_id` such as `KRZ_SO_20260302_01`,
file name, delivery time, row count) and its data rows in
`retailer_sellout`, `marketplace_sales_daily` or `marketplace_inventory_daily`.
**All four tables are insert-only.** A restated period arrives as a new load
(`is_restatement`, `restates_load_id`) whose rows replace the earlier load's
rows for that period, so a consumer takes the latest load per
(`source`, `feed_type`, `period_start`).

* Sell-out is per retailer, period, sales region (the region a DC serves),
  sales channel (store or online) and the retailer's own article number, with
  the period-end stock on store rows (also zero-sales rows of listed
  articles). Layouts differ by sender, not by schema: period label (`2026-W10`,
  `KW10/2026`, retail month `2026P03`), region codes, sales-channel codes, NET
  or GROSS value basis, article text style and file naming.
* Kranz and Mylo report weekly (Monday 06:00-09:00 and 10:00-14:00), Belle
  per retail month (4-4-5 calendar of ISO weeks, `models/retail_calendar.py`)
  on the Wednesday after the period, Shopnova daily the next morning. 3 % of
  retailer files arrive 1-2 days late (`feed_loads.delivered_at` only).
* The marketplace reports carry only the marketplace product code (`SNV` + 8
  digits), mapped through `erp.knmt` like retailer articles, and traffic as
  `glance_views` (views are reported on out-of-stock days with 0 units).

### `mkt` (marketing stack)

Text keys (`PS-2026Q1-01`, `ATT-NV-10004711`), dates rather than timestamps
for daily facts. `campaigns` are updated as their status moves (planned,
active, ended); `influencer_codes` is written once per code at the history
start and not updated afterwards; `campaign_spend_daily`,
`sessions_daily` and `attributions` are written once per day after the day
closes (04:00-05:00 local), like real marketing exports. Attribution is
single-touch last click, one row per tracked web order (92 %: consent loss)
noted when the order's payment is authorised (orders whose payment fails never
reach mkt; a later cancellation stays attributed), referencing the shop order
and customer UUIDs as text. Channel words are the shop's traffic sources, with
one exception: the shop's `deal_site` source is the `affiliate` channel, so
campaigns, attributions and sessions share one vocabulary. Coupon sites take
the last click of 15 % of the tracked promoted orders from organic search and
direct traffic (always-on coupon campaign, listing fee); the
`promo_margin_trap` deal-site campaign is paid by commission, so its clicks
come from the deal traffic it drives, not from spend. Affiliate sessions are
affiliate clicks (`clicks >= sessions >= orders`), and `platform_conversions`
never exceed clicks. `influencer_cohort_churn` raises the budget of every
influencer campaign of its code: each gets a truth `spend_boost` row and a
`daily_budget_eur` update on the first export it is boosted in, and the budget
goes back on the first export after the window if the campaign still runs
(a campaign that ends inside the window keeps the boosted budget). The campaign calendar
is a pure function of seed, config, year and the regular promotion calendar
(`domain/marketing.py`); ids never depend on scenario windows, and a campaign
row is written before any spend or attribution that references it, also for
scenario campaigns switched on live. Spend flights drive new-customer
acquisition through a multiplier normalised per calendar year (rate-weighted
mean 1), so web volume and the value-share calibration hold.

Influencer codes: a customer acquired with a code gets 15 % off the goods of
the first order (`marketing.influencer_first_order_discount_pct`, 0 switches it
off), with the code as the shop's `promo_code`, unless another promotion
applies to that order (one code per order; see `shop` above). Attribution
follows the customer's acquisition, never the order's promo code: such a first
order is still attributed to `influencer` with the customer's code (and the
code's campaign when one is usable, `is_new_customer` true), and the coupon-site
intercept never takes it. The commission
(`influencer_codes.commission_pct`) is paid on attributed net revenue, after
that discount, and is part of the influencer campaigns' `spend_eur`.

Design rule: the baseline business is healthy. Every paid channel pays back
within 12 months at baseline (a customer's 12-month contribution above the
channel's realised CAC), and unprofitable patterns come only from planted
scenarios recorded in truth (`promo_margin_trap`, `influencer_cohort_churn`).
Influencer as a channel stays profitable; the influencer story is the
`influencer_cohort_churn` code's window cohort, not the channel.
`test_paid_channels_pay_back_within_12_months` (phase-2 acceptance run,
`phase2_checks.paid_channel_payback`) enforces it, leaving out the customers a
scenario acquired.

Paid media defaults (`marketing.paid`) are calibrated against the measured
contribution per web order, about EUR 13.0 / 10.0 / 9.3 for a customer's orders
1 / 2 / 3 (cumulative 13.0 / 23.1 / 32.3), and the 12-month value per customer
(about EUR 23.9 paid search, 23.6 social, 19.8 influencer after the first-order
discount). Realised CAC runs about 2.5 % above `cac_eur`: paid search CAC EUR 22,
social EUR 20 (about EUR 20.5 realised), influencer EUR 14 (about EUR 16.4 with
the commission: the cheapest CAC, but a lower value); each pays back with the
customer's second order. CPM and CTR (search 16.0 / 3.5 %, social 4.2 / 1.5 %,
influencer 4.9 / 1.5 %) are set so that the clicks implied by spend match the
configured session conversion rates (`conversion_rates`, social 2.0 %): paid
sessions = clicks x U(0.80, 0.92) + 15 % of the expected sessions as organic.

### `truth`

Ground truth for planted scenarios and data quality defects. Entity ids are
recorded both neutrally and in the system key formats (`system_refs` jsonb)
so findings in BI can be checked directly. Never granted to `bi_reader`.

| entity_type | entity_id | system_refs |
| --- | --- | --- |
| web_order, delivery, customer (also duplicates), sku, sales_doc, billing | neutral id | shop / wms / erp keys |
| `carrier_scan` | `<delivery_id>:<scan_seq>` | `wms_event_id`, `wms_shipment_id`, `erp_delivery` |
| `customer_material` | `<retailer account>:<sku_id>` | `kunnr`, `matnr`, `kdmat`, `ean` |
| `feed_load` | load id | `feeds_load_id`, `feeds_restatement_load_id` |
| `retailer_dc_week` | `<dc account>:<YYYY-MM-DD>` (period start, or the PO order day for `post_overstock_cut`) | DC `kunnr`, `week_start` (always the Monday), `order_date` (when the id is an order day) |
| `deduction` | neutral id | `erp_deduction` (`belnr`) |
| `campaign` | campaign id | `mkt_campaign_id` |
| `web_return` | neutral id | `wms_return_id`, `rma_no` |
| `dst_transition` | local date | none |

Every truth effect is recorded exactly once by construction (unique handler
key, ledger or one call per entity), because the manager's in-memory
de-duplication does not survive a restart and a second record would break
the primary key.

## Chaos (data quality noise)

`src/cpgsim/chaos/` plants the defects of SPEC § Data quality noise at the
configured rates (`chaos:` in the config; a rate of 0 switches a defect off).
Domain code decides, writers only render: the seams in customers, web,
fulfilment, returns, masterdata and deductions call the package. Selection is
`hit(world, defect, *ids)`, a hash of `derive_seed(seed, "chaos", defect,
*ids)` against the rate: pure in the ids, it never consumes a domain random
stream. Every defect gets a truth window `<defect>:chaos` at history start
(`source = 'chaos'`, `params = {"rate": r}`) and one effect per planted
instance.

| Defect | Module | Footprint in the business data |
| --- | --- | --- |
| duplicate_customer | `shop` | a second `shop.customers` account of the same person (varied name, e-mail, street format), id `50_000_000 + original id`; when the original is still active the person's next repeat order is placed on it |
| malformed_postcode | `shop` | `shop.addresses.postcode` missing or malformed; the warehouse ships to the true one |
| status_error | `orders` | `shop.orders` set `cancelled` (60 %) or the first `wms.shipments` row set `CX` (40 %) in error and corrected 2-10 hours later; the correction keeps the error timestamp: `status <> 'cancelled' AND cancelled_at IS NOT NULL` (shop), `status_code <> 'CX' AND cancelled_local IS NOT NULL` (wms) |
| duplicate_carrier_event | `logistics` | a second `wms.carrier_events` row with the same time, code and location and its own `scan_seq` |
| dst_local_time | `dst` | one truth row per DST transition (`chaos.dst`); `wms` `*_local` times skip or repeat an hour |
| missing_order_ref | `orders` | web `vbak.bstnk` missing or truncated |
| unmapped_article | `master` | a retailer article without `erp.knmt` row (about 95 % coverage); EDI order lines (`vbap.kdmat` + `matnr`) and `zkona_item` keep the pair, so the mapping is recoverable from history; `zdeduct_item.matnr` is NULL |
| sellout_restatement | `domain/feeds.py` | a load under-reports one region; the next delivery restates the period as a new load |
| free_text_return_reason | `logistics` | `wms.returns.reason_text` (German phrases, 10 % contradict the code) |
| gdpr_erasure | `shop` | `shop.customers.status = 'anonymised'`, anonymised addresses and `influencer_code` cleared (`signup_source` and `mkt.attributions.influencer_code` keep the acquisition); `wms` keeps the consignee name (deliberate cross-system disagreement) |

Exact-reconciliation e2e tests run with chaos off (`CHAOS_OFF`); determinism
tests run with chaos on.
