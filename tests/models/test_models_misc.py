"""Payments, repurchase and pricing models."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pytest

from cpgsim.config import PaymentsCfg
from cpgsim.engine.rng import Rng
from cpgsim.models.payments import draw_method
from cpgsim.models.pricing import elasticity_lift, post_promo_dip, split_gross
from cpgsim.models.repurchase import conversion_probability, intent_delay_days


def test_payment_mix_frequencies() -> None:
    mix = PaymentsCfg().mix["DE"]
    c = Counter(draw_method(Rng(1, "pay", i), mix) for i in range(40000))
    for k, w in mix.items():
        assert c[k] / 40000 == pytest.approx(w, abs=0.01)


def test_payment_order_independent() -> None:
    mix = {"card": 0.2, "paypal": 0.5, "invoice_bnpl": 0.3}
    rev = dict(reversed(list(mix.items())))
    for i in range(200):
        assert draw_method(Rng(2, i), mix) == draw_method(Rng(2, i), rev)
    with pytest.raises(ValueError):
        draw_method(Rng(1), {})
    assert draw_method(Rng(1), {"a": 0.0, "b": 1.0}) == "b"


def test_intent_delay() -> None:
    xs = [intent_delay_days(Rng(3, "rep", i), 60.0, 1, 4.0) for i in range(20000)]
    assert sum(xs) / len(xs) == pytest.approx(60.0, rel=0.03)
    ys = [intent_delay_days(Rng(3, "rep2", i), 60.0, 2, 4.0) for i in range(20000)]
    assert sum(ys) / len(ys) == pytest.approx(120.0, rel=0.03)
    zs = [intent_delay_days(Rng(3, "short", i), 2.0, 1, 4.0) for i in range(1000)]
    assert min(zs) >= 3.0


def test_conversion_probability() -> None:
    assert conversion_probability(0.62, 0.55, 0) == pytest.approx(0.62)
    assert conversion_probability(0.62, 0.55, 2) == pytest.approx(0.62 * 0.55**2)
    assert conversion_probability(0.62, 0.55, 0, 0.3) == pytest.approx(0.186)
    assert conversion_probability(0.9, 1.0, 0, 3.0) == 1.0
    assert conversion_probability(0.5, 0.5, 1, -1.0) == 0.0


def test_elasticity_lift() -> None:
    assert elasticity_lift(0.25, -2.0) == pytest.approx(1.7778, abs=1e-4)
    assert elasticity_lift(0.10, -2.0) == pytest.approx(1.2346, abs=1e-4)
    assert elasticity_lift(0.0, -2.0) == 1.0
    with pytest.raises(ValueError):
        elasticity_lift(1.0, -2.0)


def test_post_promo_dip_area_and_bounds() -> None:
    lift, share, promo_days, dip_days = elasticity_lift(0.25, -2.0), 0.30, 7, 21
    factors = [post_promo_dip(lift, share, t, dip_days, promo_days) for t in range(dip_days)]
    missing = sum(1.0 - f for f in factors)
    assert missing == pytest.approx(share * (lift - 1.0) * promo_days, rel=1e-9)
    assert all(0.3 <= f <= 1.0 for f in factors)
    assert factors == sorted(factors)  # recovers over time
    assert post_promo_dip(lift, share, dip_days, dip_days, promo_days) == 1.0
    assert post_promo_dip(lift, share, -1, dip_days, promo_days) == 1.0
    assert post_promo_dip(1.0, share, 0, dip_days, promo_days) == 1.0
    # Extreme lift: capped at 0.3.
    assert post_promo_dip(10.0, 1.0, 0, 7, 30) == pytest.approx(0.3)


@pytest.mark.parametrize("vat", [0.19, 0.20, 0.081, 0.07, 0.0])
def test_split_gross_exact(vat: float) -> None:
    r = Rng(5, "vat", vat)
    for _ in range(5000):
        gross = round(r.uniform(-200.0, 2000.0), 2)
        net, tax = split_gross(gross, vat)
        assert round(net, 2) == net and round(tax, 2) == tax
        assert Decimal(str(net)) + Decimal(str(tax)) == Decimal(str(gross))
        assert abs(net * (1 + vat) - gross) <= 0.006 * (1 + vat)


def test_split_gross_examples() -> None:
    assert split_gross(29.95, 0.19) == (25.17, 4.78)
    assert split_gross(0.0, 0.19) == (0.0, 0.0)
    assert split_gross(-11.90, 0.19) == (-10.0, -1.9)
