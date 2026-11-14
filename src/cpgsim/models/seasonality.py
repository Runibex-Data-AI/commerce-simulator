"""Seasonality, calendar events and intraday shape.

Three independent multipliers make up the time shape of demand:

* ``category_factor(profile, d)``: annual seasonality of a product category,
  smooth over the year with an annual mean of 1.
* ``event_factor(cfg, d, channel, brand, cal)``: discrete calendar events
  (Black Friday week, Cyber Monday, Christmas, Mother's Day, marketplace sale).
* ``intraday_factor(cfg, local_dt)``: hour-of-day times day-of-week, mean 1
  over a week.

All functions are pure. Values are synthetic shapes chosen to look credible,
not market data.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Protocol, Sequence

from ..config import SeasonalityCfg


class EventCalendar(Protocol):
    """The part of ``models.calendar.Calendar`` that event factors need."""

    def black_friday(self, year: int) -> date: ...

    def mothers_day(self, year: int) -> date: ...


# -- annual category profiles --------------------------------------------------
#
# Each profile is a list of N anchor values spread evenly over the year: anchor
# i sits at the centre of the i-th of N equal slices (N = 12 gives roughly the
# middle of each month). Values between anchors are interpolated linearly and
# cyclically, so a piecewise-linear curve on a uniform cyclic grid has exactly
# the mean of its anchors. Dividing by that mean gives an annual mean of 1.
# The raw anchors below are relative; only their shape matters.

_RAW: dict[str, list[float]] = {
    "flat": [1.0] * 12,
    # Face and body care: mild winter lift (dry skin), slight summer dip.
    "face": [1.12, 1.08, 1.02, 0.98, 0.95, 0.92, 0.90, 0.92, 0.97, 1.03, 1.08, 1.14],
    "body": [1.10, 1.07, 1.01, 0.97, 0.95, 0.93, 0.92, 0.93, 0.97, 1.02, 1.07, 1.12],
    # Lip care and rich creams: peak Nov-Feb about 1.7x, trough Jun-Aug about 0.5x.
    "winter": [1.78, 1.72, 1.00, 0.68, 0.52, 0.48, 0.46, 0.48, 0.62, 0.95, 1.68, 1.80],
    # Sun care: peak May-Aug, about 2.6x in July, about 0.15x in Dec-Jan.
    "sun": [0.14, 0.20, 0.42, 0.82, 1.62, 2.30, 2.70, 2.06, 0.92, 0.36, 0.18, 0.14],
    # Gift sets: half-month anchors (N = 24) so the December peak falls off
    # before New Year. Nov about 3x, Dec about 7x, otherwise close to zero.
    "gift": [
        0.06, 0.10,  # Jan
        0.10, 0.10,  # Feb
        0.10, 0.12,  # Mar
        0.14, 0.12,  # Apr (Easter)
        0.18, 0.12,  # May (Mother's Day)
        0.10, 0.10,  # Jun
        0.10, 0.10,  # Jul
        0.10, 0.10,  # Aug
        0.12, 0.16,  # Sep
        0.30, 0.65,  # Oct
        1.90, 3.90,  # Nov
        10.5, 3.60,  # Dec
    ],
}

# After-sun follows sun care two to three weeks later and more sharply: shift
# the sun curve by ~18 days and raise it to a power before normalising.
_AFTER_SUN_SHIFT_DAYS = 18.0
_AFTER_SUN_POWER = 1.45


def _interp_cyclic(anchors: Sequence[float], frac: float) -> float:
    """Value at year fraction ``frac`` in [0, 1) of a cyclic anchor grid."""
    n = len(anchors)
    x = frac * n - 0.5
    i = math.floor(x)
    f = x - i
    a = anchors[i % n]
    b = anchors[(i + 1) % n]
    return a + (b - a) * f


def _after_sun_anchors() -> list[float]:
    sun = _RAW["sun"]
    n = 48  # finer grid so the shift and power keep their shape
    shift = _AFTER_SUN_SHIFT_DAYS / 365.0
    return [
        _interp_cyclic(sun, ((i + 0.5) / n - shift) % 1.0) ** _AFTER_SUN_POWER for i in range(n)
    ]


def _normalised(raw: list[float]) -> tuple[float, ...]:
    m = sum(raw) / len(raw)
    return tuple(v / m for v in raw)


PROFILES: dict[str, tuple[float, ...]] = {k: _normalised(v) for k, v in _RAW.items()}
PROFILES["after_sun"] = _normalised(_after_sun_anchors())


def _year_fraction(d: date) -> float:
    """Centre of day ``d`` as a fraction of its year (leap years handled)."""
    n_days = 366 if (d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)) else 365
    return (d.timetuple().tm_yday - 0.5) / n_days


@lru_cache(maxsize=16384)
def category_factor(profile: str, d: date) -> float:
    """Annual seasonality multiplier for a category profile on date ``d``.

    Profiles: ``flat``, ``face``, ``body``, ``winter``, ``sun``, ``after_sun``,
    ``gift``. Smooth over the year (linear between anchors), annual mean 1.
    """
    try:
        anchors = PROFILES[profile]
    except KeyError:
        raise ValueError(f"unknown seasonality profile {profile!r}") from None
    return _interp_cyclic(anchors, _year_fraction(d))


# -- calendar events -----------------------------------------------------------

WEB_CHANNELS = frozenset({"web"})
# Marketplace channel names: the generic name and the configured fictional one.
MARKETPLACE_CHANNELS = frozenset({"marketplace", "shopnova"})

# Shape of the Black Friday week, Mon..Sun, mean 1: the excess lift on each day
# is (lift - 1) * weight, so the week's mean factor equals the configured lift
# and the Friday itself is highest.
_BF_WEEK_WEIGHTS = (0.80, 0.80, 0.85, 0.95, 1.45, 1.15, 1.00)
RETAIL_BF_WEEK_LIFT = 1.2  # stores: a mild Black Friday echo, no web-style spike

CHRISTMAS_EVE_FACTOR = 0.5  # 24-26 Dec
BETWEEN_YEARS_FACTOR = 0.9  # 27-31 Dec
MOTHERS_DAY_BRAND = "Cutara"
MOTHERS_DAY_WINDOW_DAYS = 10


def marketplace_sale_week(year: int) -> tuple[date, date]:
    """The fixed mid-July marketplace sale: Mon..Sun of the week containing 15 July."""
    mid = date(year, 7, 15)
    mon = mid - timedelta(days=mid.weekday())
    return mon, mon + timedelta(days=6)


def _christmas(d: date, lift: float, online: bool) -> float:
    """Christmas factor for a December date.

    1-20 Dec: run-up lift. Online channels taper linearly from the lift on
    20 Dec to the Christmas Eve level on 24 Dec (parcels would not arrive in
    time); stores keep the lift up to 23 Dec (last-minute shopping).
    """
    day = d.day
    if day <= 20:
        return lift
    if day <= 23:
        if not online:
            return lift
        return lift + (CHRISTMAS_EVE_FACTOR - lift) * (day - 20) / 4.0
    if day <= 26:
        return CHRISTMAS_EVE_FACTOR
    return BETWEEN_YEARS_FACTOR


def event_factor(
    cfg: SeasonalityCfg,
    d: date,
    channel: str,
    brand: str | None,
    cal: EventCalendar,
) -> float:
    """Multiplier from calendar events for a channel and (optional) brand.

    * ``web``: Black Friday week (Mon..Sun containing ``cal.black_friday``)
      with week mean ``black_friday_lift`` and the Friday highest; Cyber Monday
      (the Monday after) ``cyber_monday_lift``; Christmas run-up 1-20 Dec
      ``christmas_lift``; 24-26 Dec 0.5; 27-31 Dec 0.9.
    * marketplace (``marketplace`` / ``shopnova``): as web, plus
      ``marketplace_sale_lift`` in the mid-July sale week (phase 2).
    * any other channel (retailers): Christmas as above (lift kept up to
      23 Dec), a mild 1.2 Black Friday week, no Cyber Monday.
    * brand ``Cutara``: ``mothers_day_lift`` on the 10 days before Mother's Day,
      in every channel.

    Factors multiply. Returns 1.0 outside all events.
    """
    m = d.month
    if m not in (4, 5, 7, 11, 12):
        return 1.0
    online = channel in WEB_CHANNELS or channel in MARKETPLACE_CHANNELS
    f = 1.0

    if m in (11, 12):
        bf = cal.black_friday(d.year)
        week_mon = bf - timedelta(days=bf.weekday())
        offset = (d - week_mon).days
        if 0 <= offset < 7:
            lift = cfg.black_friday_lift if online else RETAIL_BF_WEEK_LIFT
            f *= 1.0 + (lift - 1.0) * _BF_WEEK_WEIGHTS[offset]
        elif offset == 7 and online:
            f *= cfg.cyber_monday_lift
        if m == 12:
            f *= _christmas(d, cfg.christmas_lift, online)

    elif m == 7:
        if channel in MARKETPLACE_CHANNELS:
            lo, hi = marketplace_sale_week(d.year)
            if lo <= d <= hi:
                f *= cfg.marketplace_sale_lift

    else:  # April / May
        if brand == MOTHERS_DAY_BRAND:
            md = cal.mothers_day(d.year)
            if 1 <= (md - d).days <= MOTHERS_DAY_WINDOW_DAYS:
                f *= cfg.mothers_day_lift
    return f


# -- intraday shape ------------------------------------------------------------


@lru_cache(maxsize=64)
def _norm(values: tuple[float, ...], n: int) -> tuple[float, ...]:
    if len(values) != n:
        raise ValueError(f"expected {n} weights, got {len(values)}")
    m = sum(values) / n
    if m <= 0:
        raise ValueError("weights must have a positive mean")
    return tuple(v / m for v in values)


def hour_weights(cfg: SeasonalityCfg) -> list[float]:
    """24 hour-of-day weights (local time, hour 0 = 00:00-01:00), mean 1."""
    return list(_norm(tuple(cfg.hour_of_day), 24))


def dow_weights(cfg: SeasonalityCfg) -> list[float]:
    """7 day-of-week weights, Mon..Sun, mean 1."""
    return list(_norm(tuple(cfg.day_of_week), 7))


def intraday_factor(cfg: SeasonalityCfg, local_dt: datetime) -> float:
    """Hour-of-day weight times day-of-week weight at a local wall-clock time.

    Each hour weight sits at the centre of its hour (h:30); between centres
    the weight is interpolated linearly, wrapping from 23:30 to 00:30. The
    interpolated curve keeps a daily mean of 1. The day-of-week weight is that
    of ``local_dt``'s calendar day.
    """
    hw = _norm(tuple(cfg.hour_of_day), 24)
    dw = _norm(tuple(cfg.day_of_week), 7)
    x = (
        local_dt.hour
        + local_dt.minute / 60.0
        + (local_dt.second + local_dt.microsecond / 1e6) / 3600.0
        - 0.5
    )
    i = math.floor(x)
    f = x - i
    a = hw[i % 24]
    b = hw[(i + 1) % 24]
    return (a + (b - a) * f) * dw[local_dt.weekday()]
