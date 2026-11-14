"""Payment method choice."""

from __future__ import annotations

from ..engine.rng import Rng


def draw_method(rng: Rng, mix: dict[str, float]) -> str:
    """Draw a payment method from a ``{method: weight}`` mix.

    Keys are iterated in sorted order so the result does not depend on dict
    insertion order. Weights need not sum to 1. One draw from ``rng``.
    """
    keys = sorted(mix)
    if not keys:
        raise ValueError("empty payment mix")
    total = 0.0
    for k in keys:
        w = mix[k]
        if w < 0:
            raise ValueError(f"negative weight for {k!r}")
        total += w
    if total <= 0:
        raise ValueError("payment mix has no positive weight")
    x = rng.random() * total
    acc = 0.0
    for k in keys:
        acc += mix[k]
        if x < acc:
            return k
    # Float round-off can leave x == total; fall back to the last positive key.
    return next(k for k in reversed(keys) if mix[k] > 0)
