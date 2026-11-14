# Architecture

Developer notes for the generator: how the engine works, the module contracts
and the conventions each source system follows. The behaviour is specified in
[SPEC.md](SPEC.md). This document covers how the code implements it.

## Layers

```
config ──► engine (clock, queue, entity store, rng, tick loop)
              │  handlers(world, event)
              ▼
           domain (business logic: catalogue, customers, web, fulfilment, retail, ...)
              │  world.emit(DomainEvent)
              ▼
           writers (shop, wms, erp, truth) ──► WriteBatch ──► one Postgres transaction per commit
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
  `customer_article(channel, sku_id)`; `CATEGORIES`.
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
| `billing` | Invoices, credit memos, clearing (called by fulfilment, returns and retail) | `create_invoice(...)`, `create_credit_memo(...) -> BillingDoc \| None` (None when nothing is left to credit), `clear(world, billing_id, amount=None)` |
| `warehouse` | Central stock, production replenishment, daily snapshots | `available`, `reserve`, `unreserve`, `issue`, `receive_return` |
| `customers` | Web customer creation, repurchase intents (event/promotion conversion and pull-forward), deal seekers of `promo_margin_trap` | `new_customer(world, rng, acquisition=None) -> Customer`, `schedule_repurchase(world, customer, order)`, `cherry_pick(world, rng, lines)`, `deal_return_rate(world, order)` |
| `web` | Web arrivals, basket, pricing, payment, release, cancellation, backorders | `place_order(world, customer, lines, source, subscription_id=None) -> WebOrder` |
| `fulfilment` | Deliveries for web and retail: pick, ship, carrier scans, outcomes, web invoicing at shipment | `create_delivery(world, kind, sales_doc, lines, ship_to_account, ship_to, carrier, planned_ship_date, order_id, seq_in_order=1) -> Delivery`, `cancel_delivery(world, delivery_id, reason)` |
| `returns` | Web returns, refunds, credit memos | `maybe_schedule_return(world, order, delivery)`, `refund(world, order, amount, reason, return_id=None, credit_items=None, include_shipping=False, delivery_id=None) -> refund_id \| None` |
| `subscriptions` | Subscription lifecycle and recurring orders (never gift, sun or after-sun products) | `maybe_start_subscription(world, customer, order)` |
| `retail` | Hidden retailer demand and inventory, retailer POs, confirmation, delivery planning, invoicing after proof of delivery, payment | none |
| `scenarios/manager` | Scenario windows from config and CLI, truth recording | `world.scenarios.active(scenario_id, t) -> list[ScenarioWindow]`, `factor(name, t, **ctx) -> float`, `record_effect(...)` |

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
evicted from the store (`customers.maybe_evict`, on lapse and when their last
order is cleaned up; a one-off `on_load` pass counts open orders for states
written before). Their shop rows stay; ids are never reused. New code must not
look customers up in the store by id unless an open order, intent or
subscription guarantees they are there. Measured on a 24-month state: resident
memory after resume 880 MB before, 415 MB on the first resume (eviction pass),
250 MB afterwards.

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
  keeps its status.
* A rejected return reduces `order_lines.qty_returned` again and moves a
  `returned` order to `return_rejected` (no refund). Refunds are `issued`, then
  `completed` (`RefundChanged`) when the payment provider has paid out, by
  payment method hours to a week later (`returns.REFUND_COMPLETION_DAYS`).

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
| deductions | phase 2 |

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
rate in `kbetr`, the cent-rounded amount in `kwert`.

Master data seeded at history start is back-dated: `mara.ersda` is the
listing date (launch - 60 days, `SkuListed.listed_on`) so a delisting date
never precedes it, and `kna1.erdat` is a deterministic account-opening date
5-13 years earlier (`B2BAccountCreated.opened_on`), before any back-dated web
customer. An unknown catalogue category has no material group and fails
loudly (`MATKL`).

### `truth`

Ground truth for planted scenarios (phase 1: `carrier_decay`,
`promo_margin_trap`). Entity ids are recorded both neutrally and in the
system key formats (`system_refs` jsonb) so findings in BI can be checked
directly. Never granted to `bi_reader`.
