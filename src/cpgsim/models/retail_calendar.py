"""Retail calendar: ISO weeks grouped into retail quarters and 4-4-5 retail months.

* **Retail quarter** (AC1 metric): 13 ISO weeks. Q1 = ISO weeks 1-13, Q2 =
  14-26, Q3 = 27-39, Q4 = 40-52, or 40-53 in a year with 53 ISO weeks.
* **Retail month** (the perfumery's "monthly" sell-out): a 4-4-5 calendar of
  ISO weeks. Each quarter has periods of 4, 4 and 5 weeks; P12 also takes ISO
  week 53. Periods run Monday to Sunday, so period boundaries align with the
  weekly retail demand.

Pure functions of the date.
"""

from __future__ import annotations

from datetime import date, timedelta

#: Weeks per period within a quarter (4-4-5).
PERIOD_WEEKS = (4, 4, 5)


def iso_monday(iso_year: int, iso_week: int) -> date:
    """Monday of ISO week ``iso_week`` of ISO year ``iso_year``."""
    return date.fromisocalendar(iso_year, iso_week, 1)


def iso_weeks_in_year(iso_year: int) -> int:
    """52 or 53."""
    return date(iso_year, 12, 28).isocalendar()[1]


def retail_quarter(d: date) -> tuple[int, int]:
    """``(iso_year, q)`` of the ISO week containing ``d``: weeks 1-13 q1, 14-26 q2, 27-39 q3, 40+ q4."""
    iso_year, week, _ = d.isocalendar()
    return iso_year, min((week - 1) // 13 + 1, 4)


def quarter_weeks(iso_year: int, q: int) -> list[date]:
    """Mondays of the ISO weeks of retail quarter ``q`` (13, or 14 for a Q4 with week 53)."""
    if q not in (1, 2, 3, 4):
        raise ValueError(f"retail quarter must be 1..4, got {q}")
    first = 13 * (q - 1) + 1
    last = iso_weeks_in_year(iso_year) if q == 4 else first + 12
    return [iso_monday(iso_year, w) for w in range(first, last + 1)]


def _period_week_range(iso_year: int, p: int) -> tuple[int, int]:
    """First and last ISO week of retail period ``p`` (1..12)."""
    q, k = divmod(p - 1, 3)
    first = 13 * q + 1 + sum(PERIOD_WEEKS[:k])
    last = first + PERIOD_WEEKS[k] - 1
    if p == 12:
        last = iso_weeks_in_year(iso_year)
    return first, last


def retail_period(d: date) -> tuple[int, int, date, date]:
    """``(iso_year, p, first Monday, last Sunday)`` of the 4-4-5 retail period containing ``d``.

    ``p`` runs 1..12; P12 takes ISO week 53 in long years.
    """
    iso_year, week, _ = d.isocalendar()
    q = min((week - 1) // 13, 3)
    offset = week - 13 * q  # 1-based week within the quarter (14 only for week 53)
    k = 0 if offset <= 4 else (1 if offset <= 8 else 2)
    p = 3 * q + k + 1
    first, last = _period_week_range(iso_year, p)
    return iso_year, p, iso_monday(iso_year, first), iso_monday(iso_year, last) + timedelta(days=6)


def period_bounds(iso_year: int, p: int) -> tuple[date, date]:
    """First Monday and last Sunday of retail period ``p`` of ``iso_year``."""
    if not 1 <= p <= 12:
        raise ValueError(f"retail period must be 1..12, got {p}")
    first, last = _period_week_range(iso_year, p)
    return iso_monday(iso_year, first), iso_monday(iso_year, last) + timedelta(days=6)
