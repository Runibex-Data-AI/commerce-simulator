"""Non-homogeneous Poisson arrivals by thinning, partition-invariant.

Time is cut into canonical UTC hour buckets. For each bucket a homogeneous
Poisson process with rate ``lam_max_per_hour`` is drawn from an Rng keyed on
``(seed, stream, bucket start)``, giving candidate times and a uniform ``u``
per candidate. A candidate at time ``t`` is accepted iff
``u < rate_per_hour(t) / lam_max_per_hour`` (Lewis-Shedler thinning).

Because candidates depend only on the seed, the stream and the bucket, and the
accept decision only on the candidate and ``rate_per_hour(t)``, the arrivals
in ``(a, c]`` are exactly the union of those in ``(a, b]`` and ``(b, c]``:
tick length, commit batching and restarts do not change the result.

Requirements for that guarantee:

* ``lam_max_per_hour`` must not depend on how time is sliced (use a config
  constant, or a pure function of the bucket hour);
* ``rate_per_hour(t)`` must be a pure function of ``t`` (seed, config,
  scenario windows), never of entity state.

If ``rate_per_hour(t) > lam_max_per_hour`` the candidate is accepted (rate is
clipped to the bound) and the event is counted in module-level statistics,
see ``clip_stats()``. Clipping loses intensity, so callers should size
``lam_max`` with headroom over the largest product of factors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Callable

from ..engine.rng import Rng

UTC = timezone.utc
_HOUR = timedelta(hours=1)
_US_PER_HOUR = 3_600_000_000

# Clipping diagnostics (never affect results). Each candidate is evaluated in
# exactly one slice, so counts do not depend on how time is partitioned.
_clip = {"count": 0, "max_ratio": 0.0}


def clip_stats() -> dict[str, float]:
    """Candidates whose rate exceeded ``lam_max`` since the last reset.

    ``count``: number of clipped candidates; ``max_ratio``: largest observed
    ``rate / lam_max``.
    """
    return dict(_clip)


def reset_clip_stats() -> None:
    _clip["count"] = 0
    _clip["max_ratio"] = 0.0


def bucket_key(hour_start: datetime) -> str:
    """Canonical key of a UTC hour bucket, e.g. ``'2025-03-01T13'``."""
    return hour_start.astimezone(UTC).strftime("%Y-%m-%dT%H")


@lru_cache(maxsize=512)
def _candidates(
    seed: int, stream: str, hour_start: datetime, lam_max_per_hour: float
) -> tuple[tuple[datetime, float, int], ...]:
    rng = Rng(seed, "arrivals", stream, hour_start.isoformat())
    n = rng.poisson(lam_max_per_hour)
    if n == 0:
        return ()
    rnd = rng.random
    span = _US_PER_HOUR - 1
    # Offsets in [1, 3_599_999_999] microseconds: strictly inside the hour.
    raw = sorted((int(rnd() * span) + 1, rnd()) for _ in range(n))
    return tuple(
        (hour_start + timedelta(microseconds=off), u, i) for i, (off, u) in enumerate(raw)
    )


def _check_hour(hour_start: datetime) -> datetime:
    if hour_start.tzinfo is None:
        raise ValueError("hour_start must be timezone-aware")
    h = hour_start.astimezone(UTC)
    if h.minute or h.second or h.microsecond:
        raise ValueError(f"hour_start is not on a UTC hour boundary: {hour_start}")
    return h


def hourly_candidates(
    seed: int, stream: str, hour_start: datetime, lam_max_per_hour: float
) -> list[tuple[datetime, float, int]]:
    """Homogeneous Poisson candidates inside ``(hour_start, hour_start + 1h)``.

    Returns ``(t, u, index)`` sorted by ``t``: aware UTC time with microsecond
    resolution, never on the hour boundary; ``u`` uniform in [0, 1) for
    thinning; ``index`` the position in the sorted list. Deterministic per
    ``(seed, stream, hour_start as UTC ISO string, lam_max_per_hour)``.
    """
    if lam_max_per_hour <= 0:
        return []
    return list(_candidates(int(seed), stream, _check_hour(hour_start), float(lam_max_per_hour)))


def thinned_arrivals(
    seed: int,
    stream: str,
    start: datetime,
    end: datetime,
    rate_per_hour: Callable[[datetime], float],
    lam_max_per_hour: float,
) -> list[tuple[datetime, int, str]]:
    """NHPP arrivals with ``start < t <= end``, by thinning hourly candidates.

    ``rate_per_hour(t)`` receives an aware UTC datetime and returns the
    intensity in arrivals per hour. Returns ``(t, index, bucket_key)`` in time
    order; ``(bucket_key, index)`` is unique per stream and suitable for
    content-derived event keys. Identical however ``(start, end]`` is
    partitioned (see module docstring).
    """
    if lam_max_per_hour <= 0 or end <= start:
        return []
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    seed = int(seed)
    lam = float(lam_max_per_hour)
    h = start.replace(minute=0, second=0, microsecond=0)
    out: list[tuple[datetime, int, str]] = []
    while h < end:
        cands = _candidates(seed, stream, h, lam)
        if cands:
            key = h.strftime("%Y-%m-%dT%H")
            for t, u, i in cands:
                if t <= start:
                    continue
                if t > end:
                    break
                r = rate_per_hour(t)
                if u * lam < r:
                    out.append((t, i, key))
                if r > lam:
                    _clip["count"] += 1
                    ratio = r / lam
                    if ratio > _clip["max_ratio"]:
                        _clip["max_ratio"] = ratio
        h += _HOUR
    return out
