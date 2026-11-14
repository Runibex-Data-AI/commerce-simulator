"""Lifecycle delays: log-normal durations with working-day and delivery-day gaps.

All functions are pure. Calendar-dependent functions take a calendar object
(``models.calendar.Calendar`` in production, a stub in tests) and call only
``is_working_day(d, country, region)`` and ``is_delivery_day(d, country,
region)``, positionally. Working days are Mon-Fri without public holidays;
delivery days are Mon-Sat without public holidays, both at the given location.

Local datetimes are timezone-aware wall-clock times (Europe/Berlin and the
equivalent Vienna/Zurich zones); results keep the input's ``tzinfo``. Wall
times are built with ``datetime.combine(..., tzinfo=tz)`` so DST is handled
by zoneinfo. All business hours used here lie outside DST gaps.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Protocol

from ..engine.rng import Rng

_MAX_SCAN_DAYS = 60  # guard against a calendar that never returns a working day


class WorkCalendar(Protocol):
    def is_working_day(self, d: date, country: str = "DE", region: str | None = None) -> bool: ...

    def is_delivery_day(self, d: date, country: str = "DE", region: str | None = None) -> bool: ...


def lognormal_days(rng: Rng, median: float, sigma: float) -> float:
    """Log-normal duration in days with the given median and log-sd ``sigma``."""
    return rng.lognormal_median(median, sigma)


def _at(d: date, hour: float, tz) -> datetime:
    """Aware local datetime at a fractional ``hour`` on date ``d``."""
    secs = int(round(hour * 3600.0))
    secs = min(max(secs, 0), 86_399)
    return datetime.combine(d, time(secs // 3600, (secs // 60) % 60, secs % 60), tzinfo=tz)


def _next_day(cal: WorkCalendar, d: date, country: str, region: str | None, delivery: bool) -> date:
    """First working (or delivery) day strictly after ``d``."""
    check = cal.is_delivery_day if delivery else cal.is_working_day
    for _ in range(_MAX_SCAN_DAYS):
        d += timedelta(days=1)
        if check(d, country, region):
            return d
    raise RuntimeError(f"no {'delivery' if delivery else 'working'} day within {_MAX_SCAN_DAYS} days of {d}")


def next_working_day_after(cal: WorkCalendar, d: date, country: str = "DE", region: str | None = None) -> date:
    """First working day strictly after ``d``."""
    return _next_day(cal, d, country, region, delivery=False)


def next_working_moment(
    cal: WorkCalendar,
    local_dt: datetime,
    country: str,
    region: str | None,
    start_hour: int,
    end_hour: int,
) -> datetime:
    """Earliest moment at or after ``local_dt`` within working hours.

    Returns ``local_dt`` itself if it is on a working day with
    ``start_hour <= time < end_hour``; ``start_hour`` on the same day if it is
    a working day and ``local_dt`` is before ``start_hour``; otherwise
    ``start_hour`` on the next working day.
    """
    if local_dt.tzinfo is None:
        raise ValueError("local_dt must be timezone-aware")
    d = local_dt.date()
    if cal.is_working_day(d, country, region):
        if local_dt.hour < start_hour:
            return _at(d, start_hour, local_dt.tzinfo)
        if local_dt.hour < end_hour:
            return local_dt
    return _at(_next_day(cal, d, country, region, delivery=False), start_hour, local_dt.tzinfo)


# -- warehouse -----------------------------------------------------------------


def pick_moment(
    rng: Rng,
    cal: WorkCalendar,
    released_local: datetime,
    pick_start_hour: int,
    ship_cutoff_hour: int,
    country: str = "DE",
    region: str | None = None,
    same_day_before_hour: int | None = None,
) -> datetime:
    """When the warehouse picks an order released at ``released_local``.

    Picking happens on the next working day after release, between
    ``pick_start_hour`` and ``ship_cutoff_hour`` (skewed towards the morning,
    as waves are worked down). Orders released on a working day before
    ``same_day_before_hour`` (default: ``pick_start_hour``, i.e. released
    overnight) are picked that same day. ``country``/``region`` is the
    warehouse location. One draw from ``rng``.
    """
    if released_local.tzinfo is None:
        raise ValueError("released_local must be timezone-aware")
    if same_day_before_hour is None:
        same_day_before_hour = pick_start_hour
    d = released_local.date()
    if not (cal.is_working_day(d, country, region) and released_local.hour < same_day_before_hour):
        d = _next_day(cal, d, country, region, delivery=False)
    span = max(ship_cutoff_hour - pick_start_hour, 0)
    # u**1.5 puts more mass early in the shift; the pick is always before the
    # cutoff, so a picked order ships the same day.
    hour = pick_start_hour + span * (rng.random() ** 1.5) * 0.98
    pick = _at(d, hour, released_local.tzinfo)
    return max(pick, released_local)


def ship_moment(
    rng: Rng,
    cal: WorkCalendar,
    picked_local: datetime,
    ship_cutoff_hour: int,
    country: str = "DE",
    region: str | None = None,
    handover_minutes: float = 75.0,
) -> datetime:
    """Carrier handover (the "shipped" scan) for an order picked at ``picked_local``.

    Picked on a working day before ``ship_cutoff_hour``: shipped the same day
    when the carrier collects, between the cutoff and ``handover_minutes``
    after it. Picked later (or on a non-working day): the next working day in
    the same collection window. One draw from ``rng``.
    """
    if picked_local.tzinfo is None:
        raise ValueError("picked_local must be timezone-aware")
    d = picked_local.date()
    if not (cal.is_working_day(d, country, region) and picked_local.hour < ship_cutoff_hour):
        d = _next_day(cal, d, country, region, delivery=False)
    hour = ship_cutoff_hour + rng.random() * handover_minutes / 60.0
    return max(_at(d, hour, picked_local.tzinfo), picked_local)


# -- carriers ------------------------------------------------------------------

DELIVERY_FIRST_HOUR = 8.5  # 08:30
DELIVERY_LAST_HOUR = 19.5  # 19:30
DELIVERY_MODE_HOUR = 13.0
_DELIVERY_JITTER_HOURS = 0.4


def _triangular_ppf(u: float, lo: float, mode: float, hi: float) -> float:
    """Inverse CDF of the triangular distribution on [lo, hi] with ``mode``."""
    c = (mode - lo) / (hi - lo)
    if u < c:
        return lo + math.sqrt(u * (hi - lo) * (mode - lo))
    return hi - math.sqrt((1.0 - u) * (hi - lo) * (hi - mode))


def parcel_delivery_moment(
    rng: Rng,
    cal: WorkCalendar,
    shipped_local: datetime,
    transit_days: float,
    country: str,
    region: str | None,
) -> datetime:
    """Delivery moment of a parcel shipped at ``shipped_local``.

    ``transit_days`` (typically a ``lognormal_days`` draw) counts delivery days
    (Mon-Sat, without public holidays at the destination ``country``/``region``)
    after the ship date: the parcel arrives on the ``max(1, floor(transit))``-th
    delivery day after shipping. The fractional part sets the time of day
    through the inverse CDF of a triangular distribution on 08:30-19:30 with
    its mode at 13:00, so longer transit means later in the day and roughly
    uniform fractions give a mid-day peak. A small jitter (one ``rng`` draw)
    is added, clamped to the delivery window. Result in ``shipped_local``'s tz.
    """
    if shipped_local.tzinfo is None:
        raise ValueError("shipped_local must be timezone-aware")
    transit = max(float(transit_days), 0.0)
    whole = math.floor(transit)
    frac = transit - whole
    d = shipped_local.date()
    for _ in range(max(1, whole)):
        d = _next_day(cal, d, country, region, delivery=True)
    hour = _triangular_ppf(frac, DELIVERY_FIRST_HOUR, DELIVERY_MODE_HOUR, DELIVERY_LAST_HOUR)
    hour += (rng.random() * 2.0 - 1.0) * _DELIVERY_JITTER_HOURS
    hour = min(max(hour, DELIVERY_FIRST_HOUR), DELIVERY_LAST_HOUR)
    return _at(d, hour, shipped_local.tzinfo)


def pallet_delivery_date(
    cal: WorkCalendar,
    ship_date: date,
    lead_working_days: int,
    country: str = "DE",
    region: str | None = None,
) -> date:
    """Pallet delivery date: ``lead_working_days`` working days after ``ship_date``.

    Counts Mon-Fri working days at the destination. ``lead_working_days <= 0``
    returns ``ship_date``.
    """
    d = ship_date
    for _ in range(max(0, int(lead_working_days))):
        d = _next_day(cal, d, country, region, delivery=False)
    return d
