"""Repeat-customer response to events and promotions, deal seekers and seasonal subscriptions.

Covers demandplan.repeat_lift / demand_windows (real promotion calendar) and
the customers / subscriptions / scenario pieces that build on them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from cpgsim.config import Config
from cpgsim.domain import customers, demandplan, promotions
from cpgsim.models import pricing
from cpgsim.scenarios.base import ScenarioParamError
from cpgsim.scenarios.promo_margin_trap import PromoMarginTrap

from test_demand_promotions import StubScenarios, make_world, margin_trap_spec, no_web_promos


def test_event_windows_black_friday_and_christmas():
    w, _ = make_world(no_web_promos())
    ws = demandplan.demand_windows(w, [], date(2025, 11, 1), date(2025, 12, 31))
    assert [x.kind for x in ws] == ["black_friday", "christmas"]
    bf, xmas = ws
    # 2025: Black Friday 28 Nov, week Mon 24 Nov .. Cyber Monday 1 Dec
    assert (bf.first, bf.last) == (date(2025, 11, 24), date(2025, 12, 1))
    assert 2.3 < bf.lift < 2.6
    assert (xmas.first, xmas.last) == (date(2025, 12, 2), date(2025, 12, 20))
    assert xmas.lift == pytest.approx(1.6)
    assert demandplan.demand_windows(w, [], date(2025, 11, 25), date(2025, 11, 30)) == []


def test_promotion_window_only_for_covered_skus():
    w, _ = make_world(no_web_promos())
    w.scenarios.specs = [margin_trap_spec(w)]
    cat = w.ref.catalogue
    vel = next(s for s in cat.active(date(2026, 3, 10)) if s.brand == "Velmora")
    cut = next(s for s in cat.active(date(2026, 3, 10)) if s.brand == "Cutara")
    (pw,) = demandplan.demand_windows(w, [vel.sku_id], date(2026, 2, 1), date(2026, 3, 31))
    lift = pricing.elasticity_lift(0.25, -2.0) * promotions.FREE_SHIPPING_LIFT
    assert (pw.kind, pw.first, pw.last) == ("promotion", date(2026, 3, 2), date(2026, 3, 29))
    assert pw.lift == pytest.approx(lift)
    assert demandplan.demand_windows(w, [cut.sku_id], date(2026, 2, 1), date(2026, 3, 31)) == []


def test_black_week_code_promo_is_not_a_window_of_its_own():
    w, _ = make_world(Config())
    ws = demandplan.demand_windows(w, [s.sku_id for s in w.ref.catalogue.active(date(2025, 11, 24))][:5],
                                   date(2025, 11, 24), date(2025, 11, 24))
    assert any(p.name.startswith("Black Week") for p in promotions.promotions_between(
        w, date(2025, 11, 24), date(2025, 11, 24), "web"))
    assert ws[0].kind == "black_friday"
    assert not any(x.kind == "promotion" and (x.first, x.last) == (ws[0].first, ws[0].last) for x in ws)


def test_repeat_lift_event_times_promo_only_lifts():
    w, _ = make_world(no_web_promos())
    w.scenarios.specs = [margin_trap_spec(w)]
    cat = w.ref.catalogue
    vel = next(s for s in cat.active(date(2026, 3, 10)) if s.brand == "Velmora")
    cut = next(s for s in cat.active(date(2026, 3, 10)) if s.brand == "Cutara")
    lift = pricing.elasticity_lift(0.25, -2.0) * promotions.FREE_SHIPPING_LIFT
    assert demandplan.repeat_lift(w, [vel.sku_id], date(2026, 3, 10)) == pytest.approx(lift)
    assert demandplan.repeat_lift(w, [vel.sku_id, cut.sku_id], date(2026, 3, 10)) == pytest.approx((lift + 1) / 2)
    # post-promo dip and the Christmas Eve lull do not lower it
    assert demandplan.repeat_lift(w, [vel.sku_id], date(2026, 4, 1)) == 1.0
    assert demandplan.repeat_lift(w, [cut.sku_id], date(2025, 12, 24)) == 1.0
    assert demandplan.repeat_lift(w, [cut.sku_id], date(2025, 11, 28)) == pytest.approx(
        demandplan.web_event_factor(w, date(2025, 11, 28))
    )


def test_event_conversion_multiplier_capped():
    w, _ = make_world(no_web_promos())
    w.now = w.at_local(date(2025, 11, 28), 12)
    m = customers.event_conversion_multiplier(w, [])
    assert m == customers.EVENT_CONVERSION_CAP
    w.now = w.at_local(date(2025, 12, 10), 12)
    assert customers.event_conversion_multiplier(w, []) == pytest.approx(1 + customers.EVENT_CONVERSION_ELASTICITY * 0.6)
    w.now = w.at_local(date(2025, 10, 10), 12)
    assert customers.event_conversion_multiplier(w, []) == 1.0


# ---------------------------------------------------------------------------
# promo_margin_trap parameters
# ---------------------------------------------------------------------------


def test_margin_trap_params_and_spec():
    impl = PromoMarginTrap()
    cfg = Config()
    p = impl.normalize({}, cfg)
    assert p["deal_seeker_share"] > 0 and 0 < p["deal_seeker_return_rate"] < 1 and 0 <= p["cherry_pick_share"] <= 1
    with pytest.raises(ScenarioParamError):
        impl.normalize({"deal_seeker_share": -1}, cfg)
    with pytest.raises(ScenarioParamError):
        impl.normalize({"cherry_pick_share": 1.5}, cfg)
    with pytest.raises(ScenarioParamError):
        impl.normalize({"deal_seeker_return_rate": 2}, cfg)

    class W:
        params = p
        start = datetime(2026, 3, 1, 23)
        end = datetime(2026, 3, 29, 22)

    (spec,) = impl.promotions(W())
    for k in ("deal_seeker_share", "deal_seeker_return_rate", "cherry_pick_share"):
        assert spec[k] == p[k]


def test_default_configs_carry_the_margin_trap_defaults():
    import yaml

    from cpgsim.config import DEFAULT_CONFIG, load_config

    cfg = load_config()
    (entry,) = [s for s in cfg.scenarios if s.id == "promo_margin_trap"]
    gen = yaml.safe_load((DEFAULT_CONFIG.parent / "scenarios" / "promo_margin_trap.yaml").read_text())
    for k, v in PromoMarginTrap.defaults.items():
        assert entry.params()[k] == v
        assert gen["params"][k] == v


# ---------------------------------------------------------------------------
# Deal seekers (customers module, real promotion calendar)
# ---------------------------------------------------------------------------


def _deal_world(share=1.0):
    w, _ = make_world(no_web_promos())
    spec = margin_trap_spec(w)
    spec.update(deal_seeker_share=share, deal_seeker_return_rate=0.15, cherry_pick_share=1.0)
    w.scenarios.specs = [spec]
    return w, spec


def test_deal_seeker_arrivals_only_in_window_and_partition_invariant():
    w, spec = _deal_world()
    a = w.at_local(date(2026, 2, 27), 0)
    b = w.at_local(date(2026, 4, 2), 0)
    whole = customers.deal_seeker_arrivals(w, a, b)
    parts = []
    t = a
    while t < b:
        nxt = min(t + timedelta(hours=7), b)
        parts.extend(customers.deal_seeker_arrivals(w, t, nxt))
        t = nxt
    assert [e.key for e in whole] == [e.key for e in parts]
    assert all(spec["start"] <= e.due < spec["end"] for e in whole)
    per_day = len(whole) / 28
    base = w.cfg.channels.web.base_orders_per_day
    assert 0.85 * base < per_day < 1.15 * base
    w0, _ = _deal_world(share=0.0)
    assert customers.deal_seeker_arrivals(w0, a, b) == []


def test_deal_basket_cheap_single_brand_items():
    w, _ = _deal_world()
    w.now = w.at_local(date(2026, 3, 10), 12)
    cat = w.ref.catalogue
    baskets = [customers.deal_basket(w, w.rng("t.deal", i), "Velmora") for i in range(400)]
    lines = [ln for b in baskets for ln in b]
    assert all(cat.get(s).brand == "Velmora" for s, _ in lines)
    assert sum(len(b) == 1 for b in baskets) / len(baskets) > 0.75
    assert sum(q for _, q in lines) / len(lines) < 1.2
    mean_price = sum(cat.get(s).rrp_eur for s, _ in lines) / len(lines)
    brand = [s for s in cat.active(date(2026, 3, 10)) if s.brand == "Velmora"]
    pop_price = sum(s.rrp_eur * s.popularity for s in brand) / sum(s.popularity for s in brand)
    assert mean_price < pop_price


def test_cherry_pick_keeps_promoted_lines_only_in_window():
    w, _ = _deal_world()
    cat = w.ref.catalogue
    d = date(2026, 3, 10)
    vel = next(s for s in cat.active(d) if s.brand == "Velmora").sku_id
    cut = next(s for s in cat.active(d) if s.brand == "Cutara").sku_id
    w.now = w.at_local(d, 12)
    assert customers.cherry_pick(w, w.rng("t", 1), [[cut, 1], [vel, 2]]) == [[vel, 2]]
    assert customers.cherry_pick(w, w.rng("t", 2), [[cut, 1]]) == [[cut, 1]]
    w.now = w.at_local(date(2026, 4, 10), 12)
    rng = w.rng("t", 3)
    state = rng.getstate()
    assert customers.cherry_pick(w, rng, [[cut, 1], [vel, 2]]) == [[cut, 1], [vel, 2]]
    assert rng.getstate() == state  # no draw outside the window


def test_unannounced_promotions_are_not_windows_yet():
    w, _ = make_world(no_web_promos())
    w.scenarios.specs = [margin_trap_spec(w)]  # announced 3 days before 2 March
    vel = next(s for s in w.ref.catalogue.active(date(2026, 3, 10)) if s.brand == "Velmora")
    span = (date(2026, 2, 1), date(2026, 3, 31))
    assert demandplan.demand_windows(w, [vel.sku_id], *span, known_at=w.at_local(date(2026, 2, 20), 12)) == []
    (pw,) = demandplan.demand_windows(w, [vel.sku_id], *span, known_at=w.at_local(date(2026, 2, 28), 12))
    assert pw.kind == "promotion"
    # calendar events are always known
    assert demandplan.demand_windows(w, [], date(2025, 11, 1), date(2025, 11, 30), known_at=w.at_local(date(2025, 1, 1), 0))
