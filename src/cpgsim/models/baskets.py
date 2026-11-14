"""Web basket composition and free-shipping top-ups."""

from __future__ import annotations

import bisect
import math
from typing import Sequence

from ..engine.rng import Rng

AFFINITY_PROB = 0.35  # per line after the first: pick a partner of an existing line
QTY_THREE_SHARE = 0.12  # P(qty 3) = qty_two_prob * this
TOPUP_BAND = 10.0  # baskets within this amount below the threshold may be topped up
_MAX_TRIES = 12  # popularity redraws when hitting an SKU already in the basket


def draw_basket(
    rng: Rng,
    sku_ids: Sequence[int],
    cum_weights: Sequence[float],
    affinity: dict[int, list[tuple[int, float]]],
    extra_mean: float,
    qty_two_prob: float,
    max_lines: int = 8,
) -> list[tuple[int, int]]:
    """Draw basket lines ``[(sku_id, qty), ...]`` with distinct SKUs.

    * Number of lines: ``1 + Poisson(extra_mean)``, capped at ``max_lines`` and
      the number of SKUs.
    * First line by popularity (``cum_weights`` are cumulative weights aligned
      with ``sku_ids``).
    * Each further line: with probability 0.35 an affinity partner of a random
      existing line (``affinity[sku] = [(partner, weight), ...]``, partners not
      in ``sku_ids`` or already in the basket are ignored), otherwise by
      popularity. If no new SKU is found after a few redraws the line is
      dropped, so very concentrated weights give slightly smaller baskets.
    * Quantity per line: 2 with ``qty_two_prob``, 3 with
      ``qty_two_prob * 0.12``, else 1.

    The sequence of draws is fixed for given inputs (deterministic).
    """
    n_skus = len(sku_ids)
    if n_skus == 0:
        return []
    rnd = rng.random
    n_lines = min(1 + rng.poisson(extra_mean), max_lines, n_skus)
    total = cum_weights[-1]
    bisect_right = bisect.bisect_right
    p2 = qty_two_prob
    p3 = qty_two_prob * (1.0 + QTY_THREE_SHARE)

    chosen: list[int] = []
    in_basket: set[int] = set()
    available: set[int] | None = None  # built lazily, only if affinity is used

    for line in range(n_lines):
        sku = None
        if line and affinity and rnd() < AFFINITY_PROB:
            anchor = chosen[int(rnd() * len(chosen))]
            partners = affinity.get(anchor)
            if partners:
                if available is None:
                    available = set(sku_ids)
                cand = [(p, w) for p, w in partners if w > 0 and p not in in_basket and p in available]
                if cand:
                    x = rnd() * sum(w for _, w in cand)
                    acc = 0.0
                    for p, w in cand:
                        acc += w
                        if x < acc:
                            sku = p
                            break
                    else:
                        sku = cand[-1][0]
        if sku is None:
            for _ in range(_MAX_TRIES):
                i = bisect_right(cum_weights, rnd() * total)
                s = sku_ids[i if i < n_skus else n_skus - 1]
                if s not in in_basket:
                    sku = s
                    break
            if sku is None:
                continue
        chosen.append(sku)
        in_basket.add(sku)

    out: list[tuple[int, int]] = []
    for sku in chosen:
        r = rnd()
        out.append((sku, 2 if r < p2 else 3 if r < p3 else 1))
    return out


def threshold_topup(
    rng: Rng,
    basket_gross: float,
    threshold: float,
    prob: float,
    cheap_candidates: Sequence[tuple[int, float]],
) -> tuple[int, int] | None:
    """Maybe add an item that lifts a basket just over the free-shipping threshold.

    If ``threshold - 10 <= basket_gross < threshold``, then with probability
    ``prob`` return ``(sku_id, qty)`` of the cheapest addition that reaches the
    threshold, from ``cheap_candidates = [(sku_id, gross unit price), ...]``
    with ``qty`` 1..3 (ties broken by lower quantity, then lower sku id). Returns
    ``None`` otherwise, or when no candidate can reach the threshold.
    Outside the band no random draw is made; inside it exactly one.

    Always choosing the cheapest sufficient item lands baskets just above the
    threshold, which produces the familiar bunching of basket values.
    """
    if not (threshold - TOPUP_BAND <= basket_gross < threshold):
        return None
    if rng.random() >= prob:
        return None
    gap = threshold - basket_gross
    best: tuple[float, int, int] | None = None
    for sku_id, price in cheap_candidates:
        if price <= 0:
            continue
        qty = max(1, math.ceil(round(gap / price, 9)))
        if qty > 3:
            continue
        # Guard float round-off: the topped-up basket must reach the threshold in cents.
        while qty <= 3 and round(basket_gross + qty * price, 2) < round(threshold, 2):
            qty += 1
        if qty > 3:
            continue
        key = (round(qty * price, 2), qty, sku_id)
        if best is None or key < best:
            best = key
    if best is None:
        return None
    return best[2], best[1]
