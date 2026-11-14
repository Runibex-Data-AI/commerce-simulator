"""Repurchase intents: when a product runs out and whether the customer reorders."""

from __future__ import annotations

from ..engine.rng import Rng

MIN_INTENT_DAYS = 3.0


def intent_delay_days(rng: Rng, usage_days: float, qty: int, shape: float) -> float:
    """Days until a purchased product runs out.

    Gamma-distributed with mean ``usage_days * qty`` (a pack lasts
    ``usage_days``; two packs last twice as long) and shape ``shape``
    (higher = less spread). Never below 3 days. One gamma draw from ``rng``.
    """
    mean = float(usage_days) * max(1, int(qty))
    return max(MIN_INTENT_DAYS, rng.gamma_mean(mean, shape))


def conversion_probability(base: float, decay: float, missed: int, multiplier: float = 1.0) -> float:
    """Probability that a repurchase intent converts into an order.

    ``base * decay ** missed * multiplier``, clamped to [0, 1]: each missed
    intent lowers the chance, so customers lapse naturally. ``multiplier``
    carries scenario effects (e.g. a churning cohort).
    """
    p = base * (decay ** max(0, missed)) * multiplier
    return min(1.0, max(0.0, p))
