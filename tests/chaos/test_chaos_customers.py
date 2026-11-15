"""Customer-record chaos: duplicate customers, malformed postcodes, GDPR erasure."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from cpgsim import chaos
from cpgsim.chaos import shop as cshop
from cpgsim.domain import customers
from cpgsim.domain.events import CustomerAnonymised, CustomerRegistered
from cpgsim.domain.model import Address, ChaosLedger, Customer, Subscription
from cpgsim.writers import keys

from .conftest import START, UTC, chaos_cfg


def _binomial_ok(k: int, n: int, p: float) -> bool:
    sd = math.sqrt(n * p * (1 - p))
    return abs(k - n * p) <= 3 * sd + 1


def _new(h, rng_name="t.new", i=0, **kw):
    return customers.new_customer(h.world, h.world.rng(rng_name, i), **kw)


ONLY_DUP = dict(duplicate_customer_rate=1.0, malformed_postcode_rate=0.0, gdpr_erasure_rate_monthly=0.0)


# -- duplicate customers: rate and scheduling ---------------------------------------------------------


def test_duplicate_rate_and_schedule_are_pure_in_the_id(harness):
    h = harness()
    n = 40_000
    for cid in range(1, n + 1):
        chaos.on_customer_registered(h.world, SimpleNamespace(customer_id=cid, country="DE"))
    hits = [cid for cid in range(1, n + 1) if h.queue.contains(f"chaos.dup_customer:{cid}")]
    assert _binomial_ok(len(hits), n, 0.02)
    days = [cshop.duplicate_delay_days(h.world, cid) for cid in hits]
    assert min(days) >= 14.0 and max(days) <= 240.0
    # log-uniform: the median sits near the geometric mean of 14 and 240
    days.sort()
    assert 45 < days[len(days) // 2] < 75
    assert days == sorted(cshop.duplicate_delay_days(h.world, cid) for cid in hits)  # pure
    # duplicates themselves never spawn duplicates
    chaos.on_customer_registered(h.world, SimpleNamespace(customer_id=keys.DUPLICATE_CUSTOMER_BASE + hits[0],
                                                          country="DE"))
    assert not h.queue.contains(f"chaos.dup_customer:{keys.DUPLICATE_CUSTOMER_BASE + hits[0]}")
    assert h.events == []  # registration itself records nothing


def test_original_person_rederives_the_shop_record(harness):
    h = harness(chaos_cfg(duplicate_customer_rate=0.0, malformed_postcode_rate=0.0))
    for i in range(30):
        c = _new(h, i=i)
        (reg,) = [e for e in h.of(CustomerRegistered) if e.customer.customer_id == c.customer_id]
        p = cshop.original_person(h.world, c.customer_id, c.country)
        assert (p["first_name"], p["last_name"], p["email"], p["phone"], p["birth_year"]) == (
            reg.first_name, reg.last_name, reg.email, reg.phone, reg.birth_year)
        assert p["address"] == c.address == reg.address


def test_duplicate_record_varies_name_email_and_street(harness):
    h = harness()
    seen = set()
    for cid in range(1, 400):
        rec = cshop.duplicate_record(h.world, cid, "DE")
        orig = cshop.original_person(h.world, cid, "DE")
        assert rec["customer_id"] == keys.DUPLICATE_CUSTOMER_BASE + cid
        assert rec == cshop.duplicate_record(h.world, cid, "DE")  # pure in the id
        assert rec["email"] != orig["email"] and "@" in rec["email"]
        assert rec["address"] == orig["address"]  # the person's true address (the entity ships there)
        sa = rec["shop_address"]
        assert (sa.postcode, sa.city, sa.region) == (orig["address"].postcode, orig["address"].city,
                                                      orig["address"].region)
        assert (sa.street, sa.house_number) != (orig["address"].street, orig["address"].house_number)
        assert (rec["first_name"], rec["last_name"]) != (orig["first_name"], orig["last_name"])
        kinds = [v.split(":")[0] for v in rec["variants"]]
        assert kinds == ["name", "email", "street"]
        seen.update(rec["variants"])
    assert {"name:typo", "name:initial", "name:case", "name:swapped", "email:domain", "email:pattern",
            "street:abbreviated", "street:number_in_street"} <= seen


def _register_dup(h, c):
    """Run until the duplicate registers, with the original awaiting a repurchase (it can act again).

    A customer who can never act again is evicted in a real run, so linking requires a pending intent
    (``customers.can_act_again``). The intent lies beyond the run and is consumed afterwards (as if it fired).
    """
    cid = c.customer_id
    if h.store.has("customer", cid):
        h.store.mut("customer", cid).intent_seq = 1
        h.world.schedule("cust.intent", c.created_at + timedelta(days=400), f"cust.intent:{cid}:1",
                         {"customer_id": cid, "seq": 1, "skus": [1], "qtys": [1]})
    due = h.queue.peek_due()
    assert h.queue.contains(f"chaos.dup_customer:{cid}")
    h.run_until(c.created_at + timedelta(days=241))
    if h.store.has("customer", cid):
        h.store.mut("customer", cid).intent_seq = 2  # the pending intent is now being handled
    return due


def test_duplicate_registration_links_an_active_original(harness):
    h = harness(chaos_cfg(**ONLY_DUP))
    c = _new(h, i=1)
    cid, dup = c.customer_id, keys.DUPLICATE_CUSTOMER_BASE + c.customer_id
    expected_due = START + timedelta(days=cshop.duplicate_delay_days(h.world, cid))
    assert h.queue.peek_due() == expected_due
    _register_dup(h, c)
    regs = [e for e in h.of(CustomerRegistered) if e.customer.customer_id == dup]
    assert len(regs) == 1 and regs[0].at == expected_due
    reg = regs[0]
    assert (reg.customer.acquisition, reg.customer.influencer_code) == (c.acquisition, c.influencer_code)
    assert h.store.get("customer", dup) is None  # no entity before the next repeat order
    assert h.store.get("customer", cid).meta["duplicate_id"] == dup
    # shop rows: customer and address (referential integrity)
    sid = keys.shop_customer_id(h.world.seed, dup)
    row = h.rows("shop.customers")[(sid,)]
    assert row["signup_source"] == c.acquisition and row["registered_at"] == expected_due
    addr_rows = [a for a in h.rows("shop.addresses").values() if a["customer_id"] == sid]
    assert len(addr_rows) == 1 and addr_rows[0]["street"] and addr_rows[0]["city"]
    (eff,) = h.effects("duplicate_customer")
    assert (eff.entity_type, eff.entity_id, eff.effect, eff.window_id) == (
        "customer", str(dup), "duplicate_record", "duplicate_customer:chaos")
    assert eff.detail["original_customer_id"] == cid and eff.detail["linked_to_active_original"] is True
    assert eff.detail["original_shop_id"] == keys.shop_customer_id(h.world.seed, cid)


@pytest.mark.parametrize("state", ["lapsed", "evicted", "subscribed", "anonymised"])
def test_duplicate_of_an_inactive_original_stays_orderless(harness, state):
    h = harness(chaos_cfg(**ONLY_DUP))
    c = _new(h, i=2)
    cid, dup = c.customer_id, keys.DUPLICATE_CUSTOMER_BASE + c.customer_id
    if state == "lapsed":
        h.store.mut("customer", cid).lapsed = True
    elif state == "evicted":
        h.store.remove("customer", cid)
    elif state == "subscribed":
        h.store.put("subscription", 7, Subscription(subscription_id=7, customer_id=cid, sku_ids=[1], qtys=[1],
                                                    interval_weeks=8, status="active", created_at=START,
                                                    next_order_at=START + timedelta(weeks=8)))
        h.store.mut("customer", cid).subscription_id = 7
    else:
        h.store.mut("customer", cid).meta["anonymised"] = True
    _register_dup(h, c)
    (reg,) = [e for e in h.of(CustomerRegistered) if e.customer.customer_id == dup]
    if state == "evicted":  # the acquisition travels in the event data, so eviction changes nothing
        assert (reg.customer.acquisition, reg.customer.influencer_code) == (c.acquisition, c.influencer_code)
    else:
        assert "duplicate_id" not in h.store.get("customer", cid).meta
    (eff,) = h.effects("duplicate_customer")
    assert eff.detail["linked_to_active_original"] is False


def test_repeat_customer_hands_the_person_over(harness):
    h = harness(chaos_cfg(**ONLY_DUP))
    c = _new(h, i=3)
    cid, dup_id = c.customer_id, keys.DUPLICATE_CUSTOMER_BASE + c.customer_id
    c = h.store.mut("customer", cid)
    c.acquisition, c.influencer_code = "influencer", "GLOW10"
    c.orders, c.missed_intents, c.last_skus = 3, 1, [5, 9]
    c.last_order_at = START + timedelta(days=20)
    c.meta["chain"] = True
    assert chaos.repeat_customer(h.world, c) is c  # not linked yet
    _register_dup(h, c)
    orig = h.store.get("customer", cid)
    buyer = chaos.repeat_customer(h.world, orig)
    assert buyer.customer_id == dup_id and h.store.get("customer", dup_id) is buyer
    assert (buyer.acquisition, buyer.influencer_code, buyer.orders, buyer.missed_intents, buyer.last_skus,
            buyer.last_order_at) == ("influencer", "GLOW10", 3, 1, [5, 9], START + timedelta(days=20))
    assert buyer.meta["acquired_at"] == START.isoformat() and buyer.meta["chain"] is True
    assert buyer.address == orig.address
    assert buyer.meta[cshop.ACCOUNT_ORDER_OFFSET] == 3  # the shop counts the new account's own orders
    assert h.store.get("customer", cid) is None  # no open orders, no intent: evicted at the hand-over
    assert chaos.repeat_customer(h.world, buyer) is buyer  # duplicates never hand over again


def test_duplicate_email_suffix_looks_like_an_ordinary_customer(harness):
    """The duplicate's address is made unique like anyone's; it never encodes the derivable duplicate id."""
    import re

    h = harness()
    for cid in (1, 777, 46_000, 120_000):
        rec = cshop.duplicate_record(h.world, cid, "DE")
        local = rec["email"].split("@")[0]
        sid = cshop.duplicate_email_suffix_id(h.world, cid)
        assert cid < sid <= cid + cshop.DUP_EMAIL_SUFFIX_SPAN
        assert local.endswith(customers.b36(sid))
        assert not local.endswith(customers.b36(keys.DUPLICATE_CUSTOMER_BASE + cid))
    # the review's one-line regex no longer separates duplicates (their suffix was b36(50,000,000 + id))
    hits = sum(bool(re.search(r"t[r-u][0-9a-z]{3}@", cshop.duplicate_record(h.world, cid, "DE")["email"]))
               for cid in range(50_000, 50_200))
    assert hits == 0


def test_first_order_on_a_duplicate_account_is_not_a_repeat(harness, monkeypatch):
    """The shop's repeat flag and mkt's new-customer flag count the account's own orders."""
    from cpgsim.domain import marketing, web

    h = harness(chaos_cfg(**ONLY_DUP))
    c = _new(h, i=6)
    c = h.store.mut("customer", c.customer_id)
    c.orders = 2
    _register_dup(h, c)
    buyer = chaos.repeat_customer(h.world, h.store.get("customer", c.customer_id))
    noted = []
    monkeypatch.setattr(marketing, "note_order", lambda w, o, cu, src, new_customer: noted.append(new_customer))
    monkeypatch.setattr(chaos, "on_order_placed", lambda w, o: None)
    sku = h.world.ref.catalogue.skus[0].sku_id
    first = web.place_order(h.world, buyer, [(sku, 1)], source="direct")
    second = web.place_order(h.world, h.store.get("customer", buyer.customer_id), [(sku, 1)], source="direct")
    assert (first.is_repeat, second.is_repeat) == (False, True)
    assert noted == []  # marketing attributes an order when its payment is authorised, not when it is placed
    assert h.store.get("customer", buyer.customer_id).orders == 4  # the person's history is kept


def test_duplicate_account_of_an_influencer_customer_gets_no_first_order_discount(harness, monkeypatch):
    """The influencer first-order discount is the person's: a duplicate account keeps the code, not the offer."""
    from cpgsim.domain import marketing, promotions, web

    h = harness(chaos_cfg(**ONLY_DUP))
    code = sorted(h.world.cfg.customers.influencer_codes)[0]
    assert marketing.influencer_promotion(h.world, code) is not None  # a fresh account would get it
    c = _new(h, i=7)
    c = h.store.mut("customer", c.customer_id)
    c.acquisition, c.influencer_code, c.orders = "influencer", code, 1
    _register_dup(h, c)
    buyer = chaos.repeat_customer(h.world, h.store.get("customer", c.customer_id))
    assert (buyer.acquisition, buyer.influencer_code) == ("influencer", code)
    monkeypatch.setattr(promotions, "web_promo_for_basket", lambda world, t, sku_ids, rng: None)
    monkeypatch.setattr(chaos, "on_order_placed", lambda w, o: None)
    sku = h.world.ref.catalogue.skus[0].sku_id
    first = web.place_order(h.world, buyer, [(sku, 2)], source="direct")
    assert first.is_repeat is False and first.meta.get("person_repeat") is True  # the account's first order
    assert first.promo_id is None and all(ln.discount == 0.0 for ln in first.lines)


def test_original_with_open_orders_survives_the_hand_over(harness):
    h = harness(chaos_cfg(**ONLY_DUP))
    c = _new(h, i=4)
    customers.order_opened(h.store.mut("customer", c.customer_id))
    _register_dup(h, c)
    buyer = chaos.repeat_customer(h.world, h.store.get("customer", c.customer_id))
    assert buyer.customer_id == keys.DUPLICATE_CUSTOMER_BASE + c.customer_id
    orig = h.store.get("customer", c.customer_id)
    assert orig is not None and "duplicate_id" not in orig.meta


def test_on_intent_places_the_repeat_order_on_the_duplicate(harness, monkeypatch):
    h = harness(chaos_cfg(**ONLY_DUP))
    c = _new(h, i=5)
    cid = c.customer_id
    _register_dup(h, c)
    placed = []

    def place_order(world, buyer, basket, source):
        customers.order_opened(world.store.mut("customer", buyer.customer_id))
        placed.append(buyer.customer_id)

    monkeypatch.setattr(customers, "web", SimpleNamespace(repeat_basket=lambda w, r, cc, lines: lines,
                                                          place_order=place_order))
    monkeypatch.setattr(customers, "demandplan", SimpleNamespace(repeat_lift=lambda w, s, d: 1.0))
    monkeypatch.setattr(customers.repurchase, "conversion_probability", lambda *a: 1.0)
    c = h.store.mut("customer", cid)
    c.intent_seq = 2  # the intent being handled (seq 1 of the helper stays queued but is stale)
    ev = SimpleNamespace(data={"customer_id": cid, "seq": 2, "skus": [1], "qtys": [1]})
    customers.on_intent(h.world, ev)
    assert placed == [keys.DUPLICATE_CUSTOMER_BASE + cid]
    assert h.store.get("customer", cid) is None


def test_erased_original_is_not_re_registered(harness):
    h = harness(chaos_cfg(**ONLY_DUP))
    c = _new(h, i=6)
    h.store.put("chaos_ledger", "gdpr", ChaosLedger(ids=[c.customer_id]))
    _register_dup(h, c)
    assert not [e for e in h.of(CustomerRegistered) if e.customer.customer_id > keys.DUPLICATE_CUSTOMER_BASE]
    assert h.effects("duplicate_customer") == []


# -- malformed postcodes ---------------------------------------------------------------------------


class Script:
    """Rng stand-in returning scripted values."""

    def __init__(self, *xs):
        self.xs = list(xs)

    def random(self):
        return self.xs.pop(0)


@pytest.mark.parametrize(
    "u, pc, country, expected",
    [
        (0.10, "80331", "DE", (None, "missing")),
        (0.40, "01067", "DE", ("1067", "leading_zero_dropped")),
        (0.40, "80331", "DE", ("D-80331", "country_prefix")),  # no leading zero: falls through
        (0.40, "1010", "AT", ("A-1010", "country_prefix")),
        (0.60, "8001", "CH", ("CH-8001", "country_prefix")),
        (0.75, "80331", "DE", ("08331", "transposed")),
        (0.95, "80331", "DE", ("8033", "truncated")),
    ],
)
def test_malformed_postcode_variants(u, pc, country, expected):
    assert cshop.malformed_postcode(Script(u, 0.0), pc, country) == expected


def test_shop_address_rate_entity_keeps_true_address(harness):
    h = harness()
    n, changed = 40_000, 0
    for cid in range(1, n + 1):
        a = Address("DE", "DE-SN", "01067", "Dresden", "Lindenweg", "4")
        out = chaos.shop_address(h.world, cid, a)
        assert out is a
        changed += a.postcode != "01067"
    assert _binomial_ok(changed, n, 0.005)
    assert len(h.effects("malformed_postcode")) == changed
    h2 = harness(chaos_cfg(malformed_postcode_rate=1.0, duplicate_customer_rate=0.0))
    c = _new(h2, i=7)
    (reg,) = h2.of(CustomerRegistered)
    (eff,) = h2.effects("malformed_postcode")
    assert c.address.postcode == eff.detail["true_postcode"] != reg.address.postcode
    assert reg.address.postcode == eff.detail["shop_postcode"]
    (row,) = h2.rows("shop.addresses").values()
    assert row["postcode"] == (reg.address.postcode or None)


def test_malformed_postcode_variant_shares(harness):
    h = harness(chaos_cfg(malformed_postcode_rate=1.0))
    from collections import Counter

    cnt = Counter()
    for cid in range(1, 20_001):
        a = Address("DE", "DE-SN", "01067", "Dresden")
        chaos.shop_address(h.world, cid, a)
    for e in h.effects("malformed_postcode"):
        cnt[e.detail["variant"]] += 1
    n = sum(cnt.values())
    for variant, share in cshop.POSTCODE_VARIANTS:
        assert _binomial_ok(cnt[variant], n, share), (variant, cnt)


# -- GDPR erasure ----------------------------------------------------------------------------------


def test_gdpr_arrivals_monthly_and_partition_invariant(harness):
    h = harness()
    end = datetime(2026, 3, 1, tzinfo=UTC)
    whole = cshop.gdpr_arrivals(h.world, START, end)
    keys_ = [e.key for e in whole]
    assert keys_[:3] == ["chaos.gdpr:202504", "chaos.gdpr:202505", "chaos.gdpr:202506"] and len(whole) == 11
    for e in whole:
        loc = e.due.astimezone(h.world.tz)
        assert (loc.day, loc.hour, loc.minute) == (1, 2, 0) and e.priority == 50
    for step in (timedelta(hours=1), timedelta(hours=7, minutes=13), timedelta(days=3)):
        parts, t = [], START
        while t < end:
            nxt = min(end, t + step)
            parts += cshop.gdpr_arrivals(h.world, t, nxt)
            t = nxt
        assert [(e.key, e.due, e.data) for e in parts] == [(e.key, e.due, e.data) for e in whole]
    off = harness(chaos_cfg(gdpr_erasure_rate_monthly=0.0))
    assert cshop.gdpr_arrivals(off.world, START, end) == []


def _month_event(month="2025-04"):
    return SimpleNamespace(data={"month": month}, key=f"chaos.gdpr:{month.replace('-', '')}")


def test_gdpr_selection_count_ledger_and_subscribers(harness):
    h = harness(chaos_cfg(gdpr_erasure_rate_monthly=0.01))
    h.store.mut("counters", "all").values["customer"] = 10_000
    # a subscribed customer in the store is never picked
    h.store.put("subscription", 1, Subscription(subscription_id=1, customer_id=17, sku_ids=[1], qtys=[1],
                                                interval_weeks=8, status="active", created_at=START,
                                                next_order_at=START + timedelta(weeks=8)))
    h.store.put("customer", 17, Customer(17, "DE", Address("DE", "DE-BY", "80331", "München"), START,
                                         "direct", subscription_id=1))
    t = datetime(2025, 4, 1, 0, 0, tzinfo=UTC)
    h.world.now = t
    cshop.on_gdpr_month(h.world, _month_event())
    queued = sorted(e for e in h.queue._pending if e.startswith("chaos.gdpr_erase:"))
    assert len(queued) == 100  # floor(10000 * 0.01 + u)
    ids = [int(k.rsplit(":", 1)[1]) for k in queued]
    assert 17 not in ids and all(1 <= i <= 10_000 for i in ids)
    month_end = h.world.at_local(datetime(2025, 5, 1).date(), 0)
    assert h.queue.peek_due() > t
    h.run_until(month_end)
    ledger = h.store.get("chaos_ledger", "gdpr")
    assert sorted(ledger.ids) == sorted(ids)
    assert all(e.at < month_end for e in h.of(CustomerAnonymised))
    assert len(h.effects("gdpr_erasure")) >= 100
    # next month never picks an erased id again
    h.world.now = month_end + timedelta(hours=2)
    cshop.on_gdpr_month(h.world, _month_event("2025-05"))
    nxt = [int(k.rsplit(":", 1)[1]) for k in h.queue._pending if k.startswith("chaos.gdpr_erase:")]
    assert nxt and not set(nxt) & set(ids)


def test_gdpr_expected_count_is_rate_times_customers(harness):
    h = harness()
    h.store.mut("counters", "all").values["customer"] = 100_000
    h.world.now = datetime(2025, 4, 1, 0, 0, tzinfo=UTC)
    cshop.on_gdpr_month(h.world, _month_event())
    assert len([k for k in h.queue._pending if k.startswith("chaos.gdpr_erase:")]) == 50


def test_gdpr_erase_in_store_customer(harness):
    h = harness(chaos_cfg(duplicate_customer_rate=0.0))
    c = _new(h, i=8)
    cid = c.customer_id
    customers.order_opened(h.store.mut("customer", cid))
    h.store.mut("customer", cid).intent_seq = 4
    ev = SimpleNamespace(data={"customer_id": cid, "month": "2025-03"})
    cshop.on_gdpr_erase(h.world, ev)
    c = h.store.get("customer", cid)
    assert c.lapsed and c.meta["anonymised"] and c.intent_seq == 5  # open order keeps it in the store
    (an,) = h.of(CustomerAnonymised)
    assert an.customer_id == cid
    row = h.rows("shop.customers")
    (r,) = row.values()
    assert r["status"] == "anonymised" and r["first_name"] == "anonymised"
    (eff,) = h.effects("gdpr_erasure")
    assert (eff.entity_type, eff.entity_id, eff.detail) == ("customer", str(cid), {"month": "2025-03"})
    cshop.on_gdpr_erase(h.world, ev)  # ledger: never twice
    assert len(h.of(CustomerAnonymised)) == 1 and len(h.effects("gdpr_erasure")) == 1
    customers.order_closed(h.world, cid)
    assert h.store.get("customer", cid) is None


def test_gdpr_erasing_an_original_anonymises_its_duplicate(harness):
    h = harness(chaos_cfg(**ONLY_DUP))
    c = _new(h, i=9)
    cid, dup = c.customer_id, keys.DUPLICATE_CUSTOMER_BASE + c.customer_id
    assert not cshop.duplicate_exists(h.world, cid)  # still queued
    _register_dup(h, c)
    assert cshop.duplicate_exists(h.world, cid)
    buyer = chaos.repeat_customer(h.world, h.store.get("customer", cid))  # duplicate entity in the store
    customers.order_opened(h.store.mut("customer", buyer.customer_id))
    cshop.on_gdpr_erase(h.world, SimpleNamespace(data={"customer_id": cid, "month": "2025-11"}))
    assert {e.customer_id for e in h.of(CustomerAnonymised)} == {cid, dup}
    d = h.store.get("customer", dup)
    assert d.lapsed and d.meta["anonymised"]
    effs = {(e.entity_id, tuple(sorted(e.detail))) for e in h.effects("gdpr_erasure")}
    assert effs == {(str(cid), ("month",)), (str(dup), ("month", "original_customer_id"))}


def test_gdpr_erasure_before_the_duplicate_registers(harness):
    h = harness(chaos_cfg(**ONLY_DUP))
    c = _new(h, i=10)
    cid = c.customer_id
    cshop.on_gdpr_erase(h.world, SimpleNamespace(data={"customer_id": cid, "month": "2025-03"}))
    assert [e.customer_id for e in h.of(CustomerAnonymised)] == [cid]
    h.run_until(START + timedelta(days=241))
    assert not [e for e in h.of(CustomerRegistered) if e.customer.customer_id > keys.DUPLICATE_CUSTOMER_BASE]


def test_gdpr_skips_customer_who_subscribed_since_selection(harness):
    h = harness(chaos_cfg(duplicate_customer_rate=0.0))
    c = _new(h, i=11)
    h.store.put("subscription", 3, Subscription(subscription_id=3, customer_id=c.customer_id, sku_ids=[1],
                                                qtys=[1], interval_weeks=8, status="active", created_at=START,
                                                next_order_at=START + timedelta(weeks=8)))
    h.store.mut("customer", c.customer_id).subscription_id = 3
    cshop.on_gdpr_erase(h.world, SimpleNamespace(data={"customer_id": c.customer_id, "month": "2025-03"}))
    assert h.of(CustomerAnonymised) == [] and h.store.get("chaos_ledger", "gdpr") is None


def test_rng_is_not_shared_with_domain_streams(harness):
    """Chaos selection consumes no domain stream: a customer's person data is the same with chaos on or off."""
    on = harness(chaos_cfg(duplicate_customer_rate=1.0, malformed_postcode_rate=1.0))
    off = harness(chaos_cfg(duplicate_customer_rate=0.0, malformed_postcode_rate=0.0))
    for h in (on, off):
        for i in range(20):
            _new(h, i=i)
    a = [(e.customer.customer_id, e.first_name, e.email) for e in on.of(CustomerRegistered)]
    b = [(e.customer.customer_id, e.first_name, e.email) for e in off.of(CustomerRegistered)]
    assert a == b
