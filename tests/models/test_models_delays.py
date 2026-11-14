"""Delays with working-day and delivery-day gaps."""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cpgsim.engine.rng import Rng
from cpgsim.models.delays import (
    lognormal_days,
    next_working_day_after,
    next_working_moment,
    pallet_delivery_date,
    parcel_delivery_moment,
    pick_moment,
    ship_moment,
)

TZ = ZoneInfo("Europe/Berlin")


class StubCalendar:
    """Weekends plus a few fixed holidays; DE-BY has an extra one (15 Aug)."""

    HOLIDAYS = {date(2025, 12, 25), date(2025, 12, 26), date(2026, 1, 1), date(2025, 10, 3),
                date(2025, 4, 18), date(2025, 4, 21)}
    REGIONAL = {("DE", "DE-BY"): {date(2025, 8, 15)}}

    def _holiday(self, d: date, country: str, region: str | None) -> bool:
        return d in self.HOLIDAYS or d in self.REGIONAL.get((country, region), set())

    def is_working_day(self, d: date, country: str = "DE", region: str | None = None) -> bool:
        return d.weekday() < 5 and not self._holiday(d, country, region)

    def is_delivery_day(self, d: date, country: str = "DE", region: str | None = None) -> bool:
        return d.weekday() < 6 and not self._holiday(d, country, region)


CAL = StubCalendar()


def test_lognormal_median() -> None:
    rng = Rng(1, "delays")
    xs = [lognormal_days(rng, 1.6, 0.5) for _ in range(20000)]
    assert statistics.median(xs) == pytest.approx(1.6, rel=0.03)
    assert min(xs) > 0


def test_next_working_moment() -> None:
    wed = datetime(2025, 3, 5, 10, 15, tzinfo=TZ)
    assert next_working_moment(CAL, wed, "DE", None, 6, 18) == wed
    early = datetime(2025, 3, 5, 4, 0, tzinfo=TZ)
    assert next_working_moment(CAL, early, "DE", None, 6, 18) == datetime(2025, 3, 5, 6, tzinfo=TZ)
    late_fri = datetime(2025, 3, 7, 19, 0, tzinfo=TZ)
    assert next_working_moment(CAL, late_fri, "DE", None, 6, 18) == datetime(2025, 3, 10, 6, tzinfo=TZ)
    xmas = datetime(2025, 12, 24, 20, 0, tzinfo=TZ)  # Wed evening -> Sat/Sun and 25/26 skipped
    assert next_working_moment(CAL, xmas, "DE", None, 6, 18) == datetime(2025, 12, 29, 6, tzinfo=TZ)
    # Regional holiday.
    aug = datetime(2025, 8, 15, 9, 0, tzinfo=TZ)
    assert next_working_moment(CAL, aug, "DE", "DE-BY", 6, 18) == datetime(2025, 8, 18, 6, tzinfo=TZ)
    assert next_working_moment(CAL, aug, "DE", "DE-HE", 6, 18) == aug
    with pytest.raises(ValueError):
        next_working_moment(CAL, datetime(2025, 3, 5, 10), "DE", None, 6, 18)


def test_next_working_day_after() -> None:
    assert next_working_day_after(CAL, date(2025, 4, 17)) == date(2025, 4, 22)  # Easter


def test_pick_and_ship() -> None:
    for i in range(500):
        rng = Rng(9, "pick", i)
        released = datetime(2025, 3, 3, tzinfo=TZ) + timedelta(minutes=37 * i)  # ~13 days of releases
        pick = pick_moment(rng, CAL, released, 6, 16, "DE", "DE-HE")
        assert pick > released or (pick == released)
        assert CAL.is_working_day(pick.date())
        assert 6 <= pick.hour < 16
        if CAL.is_working_day(released.date()) and released.hour < 6:
            assert pick.date() == released.date()
        else:
            assert pick.date() == next_working_day_after(CAL, released.date())
        ship = ship_moment(Rng(9, "ship", i), CAL, pick, 16, "DE", "DE-HE")
        assert ship.date() == pick.date()  # picked before cutoff -> same day
        assert 16 <= ship.hour <= 17 and ship > pick
    # Picked after cutoff -> next working day.
    late = datetime(2025, 3, 7, 16, 30, tzinfo=TZ)
    assert ship_moment(Rng(1), CAL, late, 16).date() == date(2025, 3, 10)


def test_parcel_delivery_never_sunday_or_holiday() -> None:
    shipped0 = datetime(2025, 12, 1, 16, 20, tzinfo=TZ)
    hours = []
    for i in range(3000):
        rng = Rng(4, "parcel", i)
        shipped = shipped0 + timedelta(days=i % 40)
        transit = lognormal_days(rng, 1.4, 0.45)
        t = parcel_delivery_moment(rng, CAL, shipped, transit, "DE", "DE-BY")
        assert t.tzinfo is not None
        assert t.weekday() != 6
        assert CAL.is_delivery_day(t.date(), "DE", "DE-BY")
        assert t.date() > shipped.date()
        assert (8, 30) <= (t.hour, t.minute) and (t.hour, t.minute) <= (19, 30)
        hours.append(t.hour + t.minute / 60)
    # Mid-day mode: more deliveries 11-15h than in the first and last two hours.
    mid = sum(1 for h in hours if 11 <= h < 15)
    edges = sum(1 for h in hours if h < 10.5 or h >= 17.5)
    assert mid > edges


def test_parcel_transit_counts_delivery_days() -> None:
    fri = datetime(2025, 3, 7, 17, 0, tzinfo=TZ)
    # 1.x days from Friday -> Saturday (delivery day); 2.x -> Monday (Sunday skipped).
    assert parcel_delivery_moment(Rng(1), CAL, fri, 1.5, "DE", None).date() == date(2025, 3, 8)
    assert parcel_delivery_moment(Rng(1), CAL, fri, 2.5, "DE", None).date() == date(2025, 3, 10)
    # Under one day still means next delivery day.
    assert parcel_delivery_moment(Rng(1), CAL, fri, 0.3, "DE", None).date() == date(2025, 3, 8)
    # Longer transit within a day -> later delivery (jitter aside).
    a = parcel_delivery_moment(Rng(2), CAL, fri, 1.05, "DE", None)
    b = parcel_delivery_moment(Rng(2), CAL, fri, 1.95, "DE", None)
    assert a < b


def test_pallet_delivery_date() -> None:
    assert pallet_delivery_date(CAL, date(2025, 3, 5), 2) == date(2025, 3, 7)
    assert pallet_delivery_date(CAL, date(2025, 3, 6), 2) == date(2025, 3, 10)
    assert pallet_delivery_date(CAL, date(2025, 12, 23), 3) == date(2025, 12, 30)
    assert pallet_delivery_date(CAL, date(2025, 3, 5), 0) == date(2025, 3, 5)
    assert pallet_delivery_date(CAL, date(2025, 8, 14), 1, "DE", "DE-BY") == date(2025, 8, 18)
