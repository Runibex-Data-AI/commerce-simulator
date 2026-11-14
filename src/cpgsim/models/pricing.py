"""Price elasticity, post-promotion dip and VAT split."""

from __future__ import annotations

import math

MIN_DIP_FACTOR = 0.3


def elasticity_lift(discount_pct: float, elasticity: float) -> float:
    """Demand multiplier for a price cut: ``(1 - discount) ** elasticity``.

    ``discount_pct`` is a fraction (0.25 = 25% off); ``elasticity`` is
    negative. 25% off at -2.0 gives 1.78.
    """
    if not 0.0 <= discount_pct < 1.0:
        raise ValueError(f"discount must be in [0, 1): {discount_pct}")
    return (1.0 - discount_pct) ** elasticity


def post_promo_dip(
    lift: float,
    pull_forward_share: float,
    days_since_end: float,
    dip_days: float,
    promo_days: float,
) -> float:
    """Demand multiplier (<= 1) in the days after a promotion ended.

    A promotion with lift ``L`` over ``P`` days sells ``(L - 1) * P`` extra
    baseline-days of demand; a share ``s`` of that was pulled forward and is
    missing afterwards: ``A = s * (L - 1) * P``.

    The dip decays linearly over ``D = dip_days``::

        f(t) = 1 - k * (1 - t / D)    for 0 <= t < D, else 1

    with ``t = days_since_end`` (0 = first day after the promotion). ``k`` is
    chosen so the dip summed over the daily steps t = 0, 1, ..., D-1 equals
    ``A``: sum k * (1 - t/D) = k * (D + 1) / 2, hence ``k = 2A / (D + 1)``.
    ``k`` is capped at 0.7 so the factor never falls below 0.3 (the cap then
    leaves part of ``A`` unrecovered, which only matters for extreme lifts).
    """
    if lift <= 1.0 or dip_days <= 0 or not 0 <= days_since_end < dip_days:
        return 1.0
    area = pull_forward_share * (lift - 1.0) * promo_days
    k = 2.0 * area / (dip_days + 1.0)
    k = min(max(k, 0.0), 1.0 - MIN_DIP_FACTOR)
    return 1.0 - k * (1.0 - days_since_end / dip_days)


def split_gross(gross: float, vat: float) -> tuple[float, float]:
    """Split a VAT-inclusive amount into ``(net, tax)``, both rounded to cents.

    Computed in integer cents (net rounded half-up), so ``net + tax`` equals
    ``gross`` to the cent: ``round(net + tax, 2) == round(gross, 2)``, and
    ``Decimal(str(net)) + Decimal(str(tax)) == Decimal(str(round(gross, 2)))``.
    Negative amounts (refunds) are split symmetrically.
    """
    cents = int(round(gross * 100))
    sign = -1 if cents < 0 else 1
    cents = abs(cents)
    net_c = int(math.floor(cents / (1.0 + vat) + 0.5))
    tax_c = cents - net_c
    return sign * net_c / 100, sign * tax_c / 100
