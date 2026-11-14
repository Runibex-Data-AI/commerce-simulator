"""Business calendar for Germany, Austria and Switzerland.

Public holidays come from the ``holidays`` package. Regions are passed as
ISO 3166-2 codes (``DE-BY``, ``AT-9``, ``CH-ZH``) and mapped to the package's
subdivision codes (``BY``, ``9``, ``ZH``). Without a region only the
nationwide holidays apply, which for Switzerland is a short list (New Year,
Ascension, National Day, Christmas): pass the canton where it matters.

Choices worth knowing:

* Germany: the ``public`` category everywhere, plus the ``catholic`` category
  in Bavaria, so Assumption Day (15 August) counts as a Bavarian holiday. It
  is a holiday in the majority of Bavarian municipalities; the few exceptions
  are ignored. Corpus Christi in parts of Saxony and Thuringia is ignored.
* Austria and Switzerland: the ``public`` category.
* Working days are Monday to Friday; delivery days are Monday to Saturday
  (parcel carriers deliver on Saturdays). Neither includes public holidays.

Holiday lookups are cached per (country, subdivision, year).
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import holidays

COUNTRIES = ("DE", "AT", "CH")


@lru_cache(maxsize=None)
def _holiday_map(country: str, subdiv: str | None, year: int) -> dict[date, str]:
    """Public holidays (date -> English name) for one country, subdivision and year."""
    if country not in COUNTRIES:
        raise ValueError(f"unsupported country {country!r}; expected one of {COUNTRIES}")
    categories: tuple[str, ...] = ("public",)
    if country == "DE" and subdiv == "BY":
        categories = ("public", "catholic")
    h = holidays.country_holidays(country, subdiv=subdiv, years=year, categories=categories)
    return {d: name for d, name in sorted(h.items())}


@lru_cache(maxsize=None)
def _subdiv(country: str, region: str | None) -> str | None:
    """Map an ISO 3166-2 region (``DE-BY``) to the holidays subdivision (``BY``)."""
    if region is None or region == "":
        return None
    cc, sep, sub = region.partition("-")
    if not sep or cc != country:
        raise ValueError(f"region {region!r} is not an ISO 3166-2 code of country {country!r}")
    if sub not in holidays.country_holidays(country).subdivisions:
        raise ValueError(f"unknown subdivision {region!r}")
    return sub


class Calendar:
    """Holidays, working days and retail event dates for DE, AT and CH.

    Stateless apart from the module-level caches; one instance is shared via
    ``world.ref.calendar``.
    """

    # -- holidays --------------------------------------------------------
    def holidays(self, year: int, country: str, region: str | None = None) -> dict[date, str]:
        """All public holidays of a year (date -> name), sorted by date."""
        return dict(_holiday_map(country, _subdiv(country, region), year))

    def holiday_name(self, d: date, country: str, region: str | None = None) -> str | None:
        return _holiday_map(country, _subdiv(country, region), d.year).get(d)

    def is_holiday(self, d: date, country: str, region: str | None = None) -> bool:
        return d in _holiday_map(country, _subdiv(country, region), d.year)

    # -- working and delivery days ----------------------------------------
    def is_working_day(self, d: date, country: str, region: str | None = None) -> bool:
        """Monday to Friday and not a public holiday."""
        return d.weekday() < 5 and not self.is_holiday(d, country, region)

    def is_delivery_day(self, d: date, country: str, region: str | None = None) -> bool:
        """Monday to Saturday and not a public holiday (parcels arrive on Saturdays)."""
        return d.weekday() < 6 and not self.is_holiday(d, country, region)

    def next_working_day(
        self, d: date, country: str, region: str | None = None, include_today: bool = False
    ) -> date:
        """First working day after ``d`` (or ``d`` itself if ``include_today`` and it is one)."""
        x = d if include_today else d + timedelta(days=1)
        while not self.is_working_day(x, country, region):
            x += timedelta(days=1)
        return x

    def next_delivery_day(
        self, d: date, country: str, region: str | None = None, include_today: bool = False
    ) -> date:
        """First delivery day after ``d`` (or ``d`` itself if ``include_today`` and it is one)."""
        x = d if include_today else d + timedelta(days=1)
        while not self.is_delivery_day(x, country, region):
            x += timedelta(days=1)
        return x

    def add_working_days(self, d: date, n: int, country: str, region: str | None = None) -> date:
        """Move ``n`` working days from ``d`` (backwards for negative ``n``; ``n == 0`` returns ``d``).

        ``d`` itself need not be a working day: Saturday + 1 working day is Monday.
        """
        step = timedelta(days=1 if n >= 0 else -1)
        x, left = d, abs(n)
        while left > 0:
            x += step
            if self.is_working_day(x, country, region):
                left -= 1
        return x

    def working_days_between(self, start: date, end: date, country: str, region: str | None = None) -> int:
        """Number of working days in the half-open interval ``(start, end]``."""
        if end <= start:
            return 0
        n, x = 0, start
        while x < end:
            x += timedelta(days=1)
            if self.is_working_day(x, country, region):
                n += 1
        return n

    # -- retail event dates -----------------------------------------------
    @staticmethod
    def black_friday(year: int) -> date:
        """The Friday after the fourth Thursday of November (US Thanksgiving).

        This is the fourth Friday of November except when November starts on
        a Friday or Saturday, when it is the fifth (e.g. 29 November 2024).
        """
        first = date(year, 11, 1)
        first_thursday = 1 + (3 - first.weekday()) % 7
        return date(year, 11, first_thursday + 21 + 1)

    @staticmethod
    def cyber_monday(year: int) -> date:
        """Monday after Black Friday."""
        return Calendar.black_friday(year) + timedelta(days=3)

    @staticmethod
    def mothers_day(year: int) -> date:
        """Second Sunday of May (the same date in DE, AT and CH)."""
        first = date(year, 5, 1)
        first_sunday = 1 + (6 - first.weekday()) % 7
        return date(year, 5, first_sunday + 7)
