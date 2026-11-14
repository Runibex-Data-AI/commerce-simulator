"""Basket composition and free-shipping bunching."""

from __future__ import annotations

import itertools

import pytest

from cpgsim.engine.rng import Rng
from cpgsim.models.baskets import draw_basket, threshold_topup

SKUS = list(range(100, 280))
WEIGHTS = [1.0 / (i + 1) ** 0.9 for i in range(len(SKUS))]  # long tail
CUM = list(itertools.accumulate(WEIGHTS))
AFFINITY = {100: [(101, 1.0), (150, 0.5)], 101: [(100, 1.0)], 200: [(201, 1.0)]}


def test_basket_basic_properties() -> None:
    sizes, qtys = [], []
    for i in range(20000):
        lines = draw_basket(Rng(1, "basket", i), SKUS, CUM, AFFINITY, 1.4, 0.12)
        ids = [s for s, _ in lines]
        assert len(ids) == len(set(ids)) >= 1
        assert all(s in SKUS for s in ids)
        assert len(lines) <= 8
        sizes.append(len(lines))
        qtys.extend(q for _, q in lines)
    assert sum(sizes) / len(sizes) == pytest.approx(2.4, abs=0.06)
    share2 = qtys.count(2) / len(qtys)
    share3 = qtys.count(3) / len(qtys)
    assert share2 == pytest.approx(0.12, abs=0.01)
    assert 0.005 < share3 < 0.03
    assert set(qtys) <= {1, 2, 3}


def test_basket_deterministic() -> None:
    a = draw_basket(Rng(5, "b"), SKUS, CUM, AFFINITY, 1.4, 0.12)
    b = draw_basket(Rng(5, "b"), SKUS, CUM, AFFINITY, 1.4, 0.12)
    assert a == b


def test_affinity_raises_pair_rate() -> None:
    def pair_rate(aff) -> float:
        with_100 = pairs = 0
        for i in range(20000):
            ids = {s for s, _ in draw_basket(Rng(2, "aff", i), SKUS, CUM, aff, 1.4, 0.12)}
            if 100 in ids and len(ids) > 1:
                with_100 += 1
                pairs += 101 in ids
        return pairs / with_100

    assert pair_rate(AFFINITY) > pair_rate({}) + 0.1


def test_affinity_partner_must_be_available() -> None:
    aff = {100: [(999, 5.0)]}  # partner not in the SKU list
    for i in range(2000):
        ids = [s for s, _ in draw_basket(Rng(3, "x", i), SKUS, CUM, aff, 2.0, 0.1)]
        assert 999 not in ids


def test_small_catalogue_caps_lines() -> None:
    lines = draw_basket(Rng(1), [1, 2], [0.5, 1.0], {}, 10.0, 0.1)
    assert len(lines) <= 2 and len({s for s, _ in lines}) == len(lines)
    assert draw_basket(Rng(1), [], [], {}, 1.0, 0.1) == []


CHEAP = [(1, 2.95), (2, 4.49), (3, 6.99), (4, 9.95), (5, 12.50)]


def test_topup_band_and_choice() -> None:
    always = 1.0
    assert threshold_topup(Rng(1), 18.0, 29.0, always, CHEAP) is None  # below band
    assert threshold_topup(Rng(1), 29.0, 29.0, always, CHEAP) is None  # already free
    assert threshold_topup(Rng(1), 27.0, 29.0, always, CHEAP) == (1, 1)  # 2.95 covers 2.00
    assert threshold_topup(Rng(1), 25.0, 29.0, always, CHEAP) == (2, 1)  # 4.49 < 2 x 2.95
    assert threshold_topup(Rng(1), 23.5, 29.0, always, CHEAP) == (1, 2)  # 5.90 < 6.99
    assert threshold_topup(Rng(1), 19.0, 29.0, always, CHEAP) == (5, 1)  # gap 10: 12.50 < 3 x 4.49
    assert threshold_topup(Rng(1), 19.0, 29.0, 0.0, CHEAP) is None
    assert threshold_topup(Rng(1), 26.0, 29.0, always, []) is None
    # Exact-cent landing counts as reaching the threshold.
    assert threshold_topup(Rng(1), 26.05, 29.0, always, CHEAP) == (1, 1)


def test_threshold_bunching() -> None:
    """Basket values bunch just above the threshold."""
    threshold = 29.0
    finals = []
    for i in range(20000):
        r = Rng(7, "val", i)
        gross = round(r.uniform(10.0, 60.0), 2)
        add = threshold_topup(r, gross, threshold, 0.55, CHEAP)
        if add:
            price = dict(CHEAP)[add[0]]
            gross = round(gross + price * add[1], 2)
            assert gross >= threshold
        finals.append(gross)
    below = sum(1 for g in finals if threshold - 5 <= g < threshold)
    above = sum(1 for g in finals if threshold <= g < threshold + 5)
    assert above > 1.8 * below
