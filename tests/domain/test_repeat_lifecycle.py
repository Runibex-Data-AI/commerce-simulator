"""Repeat customers under events and promotions, seasonal subscriptions, promoted-order truth.

Uses the stubbed web harness of ``test_web_lifecycle`` (demand windows and the
repeat lift are stubbed there).
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from test_web_lifecycle import START, Harness, _happy_cfg, _sku

from cpgsim.domain import customers, subscriptions, web
from cpgsim.domain.demandplan import DemandWindow
from cpgsim.domain.events import SubscriptionCreated, WebOrderPlaced
from cpgsim.domain.model import Promotion


def _intents(h):
    return [ev for _, _, _, ev in h.world.queue._heap if ev.kind == "cust.intent"]


# ---------------------------------------------------------------------------
# Pull-forward and conversion
# ---------------------------------------------------------------------------


def test_pull_forward_moves_a_share_of_intents_into_the_window(monkeypatch):
    h = Harness(monkeypatch, with_writers=False)
    d0 = h.world.local_date()
    win = DemandWindow(d0 + timedelta(days=10), d0 + timedelta(days=16), 2.5, "black_friday")
    h.s.windows = [win]
    sku = _sku(h).sku_id
    n, moved, early = 3000, 0, []
    for i in range(n):
        c = h.customer()
        planned = d0 + timedelta(days=20)  # 10 days after the window starts
        customers._schedule_intent(h.world, c, h.world.at_local(planned, 12), [[sku, 1]], h.world.rng("t.pf", i))
    for ev in _intents(h):
        d = h.world.local_date(ev.due)
        if d < d0 + timedelta(days=20):
            moved += 1
            assert win.first <= d <= win.last
            early.append(ev.data["early_days"])
        else:
            assert "early_days" not in ev.data
    expected = customers.PULL_FORWARD_SHARE * (1 - 1 / 2.5)
    assert abs(moved / n - expected) < 0.03, moved / n
    assert min(early) >= 4 and max(early) <= 10


def test_pull_forward_ignores_windows_too_far_ahead_or_already_running(monkeypatch):
    h = Harness(monkeypatch, with_writers=False)
    d0 = h.world.local_date()
    sku = _sku(h).sku_id
    h.s.windows = [
        DemandWindow(d0, d0 + timedelta(days=5), 2.5, "black_friday"),  # already running (starts today)
        DemandWindow(d0 + timedelta(days=2), d0 + timedelta(days=5), 2.5, "promotion"),
    ]
    for i in range(200):
        planned = d0 + timedelta(days=2 + customers.PULL_FORWARD_DAYS + 5)  # beyond the horizon
        customers._schedule_intent(h.world, h.customer(), h.world.at_local(planned, 12), [[sku, 1]],
                                   h.world.rng("t.pf2", i))
    assert all(h.world.local_date(ev.due) == d0 + timedelta(days=42) for ev in _intents(h))


def test_early_purchase_pushes_next_intent_back(monkeypatch):
    h = Harness(monkeypatch, customers={"repurchase_prob": 1.0}, **_happy_cfg())
    c = h.customer()
    c.meta["chain"] = True
    sku = _sku(h).sku_id
    o = web.place_order(h.world, c, [(sku, 1)], source="direct")
    h.run_until(START + timedelta(days=6))
    (ev,) = _intents(h)
    planned = h.world.local_date(ev.due)
    # replay the same intent as if it had been pulled 12 days forward
    h.world.queue._heap.clear()
    ev.data["early_days"] = 12
    h.world.now = ev.due
    customers.on_intent(h.world, ev)
    assert h.world.store.get("customer", c.customer_id).meta["carry_days"] == 12
    o2 = [e.order for e in h.of(WebOrderPlaced) if e.order.order_id != o.order_id][0]
    h.run_until(ev.due + timedelta(days=6))
    nxt = [e for e in _intents(h)]
    assert len(nxt) == 1 and "carry_days" not in h.world.store.get("customer", c.customer_id).meta
    delivered = h.world.store.get("web_order", o2.order_id).delivered_at
    assert (nxt[0].due - delivered).days >= 12 + 3
    assert planned  # the original plan was a real date


def test_intent_conversion_rises_with_the_repeat_lift(monkeypatch):
    def converted(lift):
        h = Harness(monkeypatch, with_writers=False, customers={"repurchase_prob": 0.3}, **_happy_cfg())
        h.s.lift_fn = lambda d: lift
        sku = _sku(h).sku_id
        n = 0
        for i in range(600):
            c = h.customer()
            c.meta["chain"] = True
            customers._schedule_intent(h.world, c, START + timedelta(hours=1), [[sku, 1]], h.world.rng("t.cv", i))
        h.run_until(START + timedelta(days=2))
        n = sum(1 for e in h.of(WebOrderPlaced) if e.source != "subscription")
        return n / 600

    base, bf = converted(1.0), converted(2.5)
    assert abs(base - 0.3) < 0.06
    assert abs(bf - 0.3 * customers.EVENT_CONVERSION_CAP) < 0.07


# ---------------------------------------------------------------------------
# Subscriptions never hold strongly seasonal products
# ---------------------------------------------------------------------------


def _seasonal_sku(h, profile):
    d = h.world.local_date()
    return next(s for s in h.world.ref.catalogue.active(d) if s.season_profile == profile)


def test_seasonal_only_order_defers_the_subscription(monkeypatch):
    h = Harness(monkeypatch, with_writers=False, customers={"subscription_rate": 1.0}, **_happy_cfg())
    c = h.customer()
    gift = _seasonal_sku(h, "sun")
    o = web.place_order(h.world, c, [(gift.sku_id, 1)], source="direct")
    subscriptions.maybe_start_subscription(h.world, c, o)
    assert not h.of(SubscriptionCreated)
    assert not h.world.store.get("customer", c.customer_id).meta.get("sub_decided")
    plain = _sku(h)
    o2 = web.place_order(h.world, c, [(gift.sku_id, 1), (plain.sku_id, 1)], source="direct")
    subscriptions.maybe_start_subscription(h.world, c, o2)
    (created,) = h.of(SubscriptionCreated)
    assert created.subscription.sku_ids == [plain.sku_id]


def test_warm_start_subscriptions_skip_seasonal_products(monkeypatch):
    h = Harness(monkeypatch, with_writers=False, channels={"web": {"initial_customers": 500}})
    subscriptions.warm_start_subscriptions(h.world)
    cat = h.world.ref.catalogue
    created = [e.subscription for e in h.of(SubscriptionCreated)]
    profiles = Counter(cat.get(s).season_profile for sub in created for s in sub.sku_ids)
    assert created and not (set(profiles) & subscriptions.NO_SUBSCRIPTION_PROFILES), profiles
    # the stubbed weights are plain popularity, so seasonal SKUs would be drawn without the filter
    assert any(s.season_profile in subscriptions.NO_SUBSCRIPTION_PROFILES for s in cat.active(h.world.local_date()))


# ---------------------------------------------------------------------------
# Free shipping without minimum, promoted-order truth
# ---------------------------------------------------------------------------


def _fs_promo(brand, scenario_id=None):
    return Promotion(
        promo_id=900001, channel="web", name="FS", kind="automatic", code=None, discount_pct=0.25,
        free_shipping=True, sku_ids=[], brand=brand, start=START, end=START + timedelta(days=10),
        announced_at=START, scenario_id=scenario_id,
    )


def test_no_threshold_topup_when_basket_ships_free(monkeypatch):
    h = Harness(monkeypatch, with_writers=False, channels={"web": {"threshold_topup_prob": 1.0}})
    d = h.world.local_date()
    mass = _sku(h, "mass", max_price=8.0)
    threshold = h.cfg.channels.web.free_shipping_eur
    qty = int((threshold - 3.0) // mass.rrp_eur)  # just below the threshold, inside the top-up band
    ids, _ = h.s.sku_weights(h.world, "web", d)
    lines = [[mass.sku_id, qty]]
    web._topup(h.world, h.world.rng("t.tu", 1), d, "EUR", lines, ids)
    assert len(lines) == 2  # topped up normally
    h.s.active_promos = [_fs_promo(mass.brand)]
    lines = [[mass.sku_id, qty]]
    web._topup(h.world, h.world.rng("t.tu", 1), d, "EUR", lines, ids)
    assert lines == [[mass.sku_id, qty]]
    other = _sku(h, "derma")
    lines = [[other.sku_id, 1]]
    assert not web.free_shipping_promo_covers(h.world, [other.sku_id])


def test_promoted_order_truth_carries_contribution(monkeypatch):
    h = Harness(monkeypatch, **_happy_cfg())
    mass = _sku(h, "mass", max_price=6.0)
    promo = _fs_promo(mass.brand, scenario_id="promo_margin_trap")
    h.s.promo_fn = lambda world, t, sku_ids, rng: promo
    c = h.customer(country="DE")
    o = web.place_order(h.world, c, [(mass.sku_id, 1)], source="direct")
    (eff,) = h.s.effects
    det = eff[6]
    vat = h.cfg.channels.web.vat["DE"]
    gross = round(mass.rrp_eur - round(mass.rrp_eur * 0.25, 2), 2)
    assert det["net_revenue_eur"] == round(gross / (1 + vat), 2)
    assert det["cogs_eur"] == round(mass.std_cost_eur, 2)
    assert det["shipping_waived_eur"] == round(h.cfg.channels.web.shipping_fee_eur / (1 + vat), 2)
    assert 3.8 <= det["freight_eur"] <= 4.4
    assert det["contribution_eur"] == round(det["net_revenue_eur"] - det["cogs_eur"] - det["freight_eur"], 2)
    assert det["contribution_eur"] < 0 and det["deal_seeker"] is False
    assert o.shipping_fee == 0.0


def test_deal_seeker_order_flags_and_return_rate(monkeypatch):
    h = Harness(monkeypatch, with_writers=False, **_happy_cfg())
    mass = _sku(h, "mass")
    promo = _fs_promo(mass.brand, scenario_id="promo_margin_trap")
    h.s.promo_fn = lambda world, t, sku_ids, rng: promo
    spec = {"window_id": "promo_margin_trap:config:0", "scenario_id": "promo_margin_trap", "index": 0,
            "brand": mass.brand, "start": START, "end": START + timedelta(days=10), "deal_seeker_share": 1.0,
            "deal_seeker_return_rate": 0.4, "cherry_pick_share": 0.0}
    h.world.scenarios.scenario_promotions = lambda: [dict(spec)]
    evs = customers.deal_seeker_arrivals(h.world, START, START + timedelta(days=1))
    assert evs
    customers.on_deal_order(h.world, evs[0])
    (placed,) = h.of(WebOrderPlaced)
    o = h.world.store.get("web_order", placed.order.order_id)
    cust = h.world.store.get("customer", o.customer_id)
    assert placed.source == "deal_site" and cust.acquisition == "deal_site" and cust.meta["one_time"]
    assert o.meta["deal"] and customers.deal_return_rate(h.world, o) == 0.4
    assert h.s.effects[0][6]["deal_seeker"] is True
    plain = web.place_order(h.world, h.customer(), [(mass.sku_id, 1)], source="direct")
    assert customers.deal_return_rate(h.world, plain) is None
    assert date(2025, 1, 1) < h.world.local_date(o.placed_at)
