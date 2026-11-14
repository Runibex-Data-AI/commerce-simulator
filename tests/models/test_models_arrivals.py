"""Poisson candidates and partition-invariant thinning."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cpgsim.models import arrivals
from cpgsim.models.arrivals import (
    bucket_key,
    clip_stats,
    hourly_candidates,
    reset_clip_stats,
    thinned_arrivals,
)

UTC = timezone.utc
H0 = datetime(2025, 3, 1, 13, tzinfo=UTC)


def test_candidates_deterministic_sorted_inside_hour() -> None:
    a = hourly_candidates(7, "web.orders", H0, 40.0)
    b = hourly_candidates(7, "web.orders", H0.astimezone(ZoneInfo("Europe/Berlin")), 40.0)
    assert a == b and len(a) > 0
    times = [t for t, _, _ in a]
    assert times == sorted(times)
    assert all(H0 < t < H0 + timedelta(hours=1) for t in times)
    assert [i for _, _, i in a] == list(range(len(a)))
    assert all(0.0 <= u < 1.0 for _, u, _ in a)
    assert hourly_candidates(8, "web.orders", H0, 40.0) != a
    assert hourly_candidates(7, "other", H0, 40.0) != a


def test_candidates_reject_unaligned_or_naive() -> None:
    with pytest.raises(ValueError):
        hourly_candidates(1, "s", H0 + timedelta(minutes=1), 5.0)
    with pytest.raises(ValueError):
        hourly_candidates(1, "s", datetime(2025, 3, 1, 13), 5.0)
    assert hourly_candidates(1, "s", H0, 0.0) == []


def test_candidate_count_is_poisson() -> None:
    lam = 25.0
    counts = [len(hourly_candidates(3, "s", H0 + timedelta(hours=i), lam)) for i in range(2000)]
    mean = sum(counts) / len(counts)
    var = sum((c - mean) ** 2 for c in counts) / (len(counts) - 1)
    assert mean == pytest.approx(lam, abs=4 * math.sqrt(lam / len(counts)))
    assert var == pytest.approx(lam, rel=0.12)


def test_bucket_key() -> None:
    assert bucket_key(H0) == "2025-03-01T13"


def _rate(t: datetime) -> float:
    # Smooth daily cycle between 2 and 18 per hour.
    return 10.0 + 8.0 * math.sin(2 * math.pi * (t.hour + t.minute / 60) / 24)


def test_thinning_count_matches_integral() -> None:
    start, end = H0, H0 + timedelta(days=30)
    arr = thinned_arrivals(11, "web.orders", start, end, _rate, 20.0)
    expected = 30 * 24 * 10.0  # the sine integrates to zero over whole days
    assert len(arr) == pytest.approx(expected, abs=4 * math.sqrt(expected))
    assert all(start < t <= end for t, _, _ in arr)
    assert [t for t, _, _ in arr] == sorted(t for t, _, _ in arr)
    keys = {(k, i) for _, i, k in arr}
    assert len(keys) == len(arr)
    for t, _, k in arr:
        assert k == t.strftime("%Y-%m-%dT%H")


def test_thinning_follows_rate_shape() -> None:
    arr = thinned_arrivals(5, "s", H0, H0 + timedelta(days=60), _rate, 20.0)
    by_hour = [0] * 24
    for t, _, _ in arr:
        by_hour[t.hour] += 1
    assert by_hour[6] > 2.5 * by_hour[18]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_partition_invariance(seed: int) -> None:
    import random

    start = datetime(2025, 3, 29, 22, 17, 3, 123, tzinfo=UTC)  # spans the DST change in Berlin
    end = start + timedelta(hours=53, minutes=41, seconds=7)
    whole = thinned_arrivals(seed, "web.orders", start, end, _rate, 20.0)

    # Random partitions, including cuts exactly on hour boundaries and on arrival times.
    pr = random.Random(seed)  # test-only partition generator
    for _ in range(5):
        cuts = sorted(
            {start + timedelta(seconds=pr.uniform(0, (end - start).total_seconds())) for _ in range(40)}
            | {start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=h) for h in (1, 2, 30)}
            | {whole[len(whole) // 2][0], whole[3][0]}
        )
        bounds = [start] + [c for c in cuts if start < c < end] + [end]
        pieces = []
        for a, b in zip(bounds, bounds[1:]):
            pieces.extend(thinned_arrivals(seed, "web.orders", a, b, _rate, 20.0))
        assert pieces == whole

    # Fixed tick partitions: 30 s and 1 h.
    for step in (timedelta(seconds=30), timedelta(hours=1)):
        pieces, a = [], start
        while a < end:
            b = min(a + step, end)
            pieces.extend(thinned_arrivals(seed, "web.orders", a, b, _rate, 20.0))
            a = b
        assert pieces == whole


def test_empty_and_naive_slices() -> None:
    assert thinned_arrivals(1, "s", H0, H0, _rate, 20.0) == []
    assert thinned_arrivals(1, "s", H0 + timedelta(hours=1), H0, _rate, 20.0) == []
    with pytest.raises(ValueError):
        thinned_arrivals(1, "s", datetime(2025, 1, 1), datetime(2025, 1, 2), _rate, 20.0)


def test_clipping_is_counted() -> None:
    reset_clip_stats()
    arr = thinned_arrivals(1, "s", H0, H0 + timedelta(hours=10), lambda t: 30.0, 10.0)
    st = clip_stats()
    assert st["count"] == len(arr) > 0  # every candidate accepted and counted
    assert st["max_ratio"] == pytest.approx(3.0)
    reset_clip_stats()
    thinned_arrivals(1, "s", H0, H0 + timedelta(hours=10), lambda t: 5.0, 10.0)
    assert clip_stats()["count"] == 0
    assert arrivals.clip_stats() is not arrivals._clip  # a copy
