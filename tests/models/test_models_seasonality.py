"""Statistical sanity of seasonality, event factors and intraday shape."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cpgsim.config import SeasonalityCfg
from cpgsim.models.seasonality import (
    PROFILES,
    category_factor,
    dow_weights,
    event_factor,
    hour_weights,
    intraday_factor,
    marketplace_sale_week,
)


class StubCalendar:
    def black_friday(self, year: int) -> date:
        d = date(year, 11, 1)
        first_fri = d + timedelta(days=(4 - d.weekday()) % 7)
        return first_fri + timedelta(weeks=3)

    def mothers_day(self, year: int) -> date:
        d = date(year, 5, 1)
        first_sun = d + timedelta(days=(6 - d.weekday()) % 7)
        return first_sun + timedelta(weeks=1)


CAL = StubCalendar()
CFG = SeasonalityCfg()


def _days(year: int):
    d = date(year, 1, 1)
    while d.year == year:
        yield d
        d += timedelta(days=1)


def _monthly(profile: str, year: int = 2025) -> dict[int, float]:
    acc: dict[int, list[float]] = {}
    for d in _days(year):
        acc.setdefault(d.month, []).append(category_factor(profile, d))
    return {m: sum(v) / len(v) for m, v in acc.items()}


@pytest.mark.parametrize("profile", sorted(PROFILES))
@pytest.mark.parametrize("year", [2025, 2028])
def test_annual_mean_is_one(profile: str, year: int) -> None:
    vals = [category_factor(profile, d) for d in _days(year)]
    assert sum(vals) / len(vals) == pytest.approx(1.0, abs=0.01)
    assert min(vals) > 0


def test_smooth_day_to_day() -> None:
    for profile in PROFILES:
        prev = category_factor(profile, date(2024, 12, 31))
        for d in _days(2025):
            v = category_factor(profile, d)
            assert abs(v - prev) < 0.6, (profile, d)
            prev = v


def test_unknown_profile() -> None:
    with pytest.raises(ValueError):
        category_factor("nope", date(2025, 1, 1))


def test_winter_profile() -> None:
    m = _monthly("winter")
    for month in (11, 12, 1, 2):
        assert 1.4 <= m[month] <= 2.0
    for month in (6, 7, 8):
        assert 0.35 <= m[month] <= 0.6
    assert max(m, key=m.get) in (12, 1)


def test_sun_profile() -> None:
    m = _monthly("sun")
    assert max(m, key=m.get) == 7
    assert m[7] == pytest.approx(2.6, abs=0.3)
    assert m[12] < 0.25 and m[1] < 0.25
    assert all(m[x] > 1.0 for x in (5, 6, 7, 8))


def test_after_sun_later_and_sharper_than_sun() -> None:
    def centroid(profile: str) -> float:
        days = list(_days(2025))
        w = [category_factor(profile, d) for d in days]
        return sum(i * x for i, x in enumerate(w)) / sum(w)

    shift = centroid("after_sun") - centroid("sun")
    assert 10 <= shift <= 25
    sun_peak = max(category_factor("sun", d) for d in _days(2025))
    as_peak = max(category_factor("after_sun", d) for d in _days(2025))
    assert as_peak > sun_peak


def test_gift_profile() -> None:
    m = _monthly("gift")
    assert m[12] == pytest.approx(7.0, abs=1.0)
    assert m[11] == pytest.approx(3.0, abs=0.7)
    for month in range(2, 10):
        assert m[month] < 0.3


def test_face_body_mild_winter_lift() -> None:
    for p in ("face", "body"):
        m = _monthly(p)
        assert 1.03 < m[1] < 1.25
        assert 0.8 < m[7] < 0.97


# -- events --------------------------------------------------------------------


def test_black_friday_week_web() -> None:
    bf = CAL.black_friday(2025)
    assert bf == date(2025, 11, 28)
    week = [bf - timedelta(days=4) + timedelta(days=i) for i in range(7)]
    vals = [event_factor(CFG, d, "web", None, CAL) for d in week]
    assert sum(vals) / 7 == pytest.approx(CFG.black_friday_lift, rel=1e-9)
    assert max(vals) == vals[4]  # Friday highest
    # Day before the week and Cyber Monday.
    assert event_factor(CFG, week[0] - timedelta(days=1), "web", None, CAL) == 1.0
    cm = bf + timedelta(days=3)
    # 2025: Cyber Monday is 1 Dec, inside the Christmas run-up; factors multiply.
    assert event_factor(CFG, cm, "web", None, CAL) == pytest.approx(
        CFG.cyber_monday_lift * CFG.christmas_lift
    )
    cm24 = CAL.black_friday(2024) + timedelta(days=3)  # 25 Nov 2024
    assert cm24 == date(2024, 11, 25)
    assert event_factor(CFG, cm24, "web", None, CAL) == pytest.approx(CFG.cyber_monday_lift)
    assert event_factor(CFG, cm24 + timedelta(days=1), "web", None, CAL) == 1.0


def test_retail_black_friday_is_mild() -> None:
    bf = CAL.black_friday(2025)
    week = [bf - timedelta(days=4) + timedelta(days=i) for i in range(7)]
    vals = [event_factor(CFG, d, "drogerie_kranz", None, CAL) for d in week]
    assert sum(vals) / 7 == pytest.approx(1.2)
    assert event_factor(CFG, date(2023, 11, 27), "mylo", None, CAL) == 1.0  # no Cyber Monday


def test_christmas() -> None:
    for ch in ("web", "mylo"):
        assert event_factor(CFG, date(2025, 12, 10), ch, None, CAL) == pytest.approx(CFG.christmas_lift)
        assert event_factor(CFG, date(2025, 12, 25), ch, None, CAL) == pytest.approx(0.5)
        assert event_factor(CFG, date(2025, 12, 29), ch, None, CAL) == pytest.approx(0.9)
    # Online tapers before Christmas Eve; stores keep the lift.
    assert event_factor(CFG, date(2025, 12, 22), "web", None, CAL) < CFG.christmas_lift
    assert event_factor(CFG, date(2025, 12, 22), "mylo", None, CAL) == pytest.approx(CFG.christmas_lift)
    assert event_factor(CFG, date(2025, 1, 5), "web", None, CAL) == 1.0


def test_marketplace_sale_only_on_marketplace() -> None:
    lo, hi = marketplace_sale_week(2025)
    assert lo.weekday() == 0 and (hi - lo).days == 6 and lo <= date(2025, 7, 15) <= hi
    assert event_factor(CFG, date(2025, 7, 15), "marketplace", None, CAL) == CFG.marketplace_sale_lift
    assert event_factor(CFG, date(2025, 7, 15), "shopnova", None, CAL) == CFG.marketplace_sale_lift
    assert event_factor(CFG, date(2025, 7, 15), "web", None, CAL) == 1.0
    assert event_factor(CFG, lo - timedelta(days=1), "marketplace", None, CAL) == 1.0


def test_mothers_day_cutara() -> None:
    md = CAL.mothers_day(2025)
    assert md == date(2025, 5, 11)
    inside = [md - timedelta(days=i) for i in range(1, 11)]
    for d in inside:
        assert event_factor(CFG, d, "web", "Cutara", CAL) == CFG.mothers_day_lift
        assert event_factor(CFG, d, "web", "Velmora", CAL) == 1.0
    assert event_factor(CFG, md, "web", "Cutara", CAL) == 1.0
    assert event_factor(CFG, md - timedelta(days=11), "web", "Cutara", CAL) == 1.0
    # Window can start in April (2nd Sunday of May on 8 May 2022 -> from 28 Apr).
    assert event_factor(CFG, date(2022, 4, 28), "mylo", "Cutara", CAL) == CFG.mothers_day_lift


# -- intraday ------------------------------------------------------------------


def test_weight_vectors_mean_one() -> None:
    hw, dw = hour_weights(CFG), dow_weights(CFG)
    assert len(hw) == 24 and len(dw) == 7
    assert sum(hw) / 24 == pytest.approx(1.0)
    assert sum(dw) / 7 == pytest.approx(1.0)
    assert hw.index(max(hw)) == 20
    assert hw.index(min(hw)) == 4
    assert dw[6] == max(dw) and dw[0] == sorted(dw)[-2]  # Sunday, then Monday


def test_intraday_factor_shape() -> None:
    tz = ZoneInfo("Europe/Berlin")
    cfg = CFG
    # A full week sampled every 5 minutes has mean ~1.
    start = datetime(2025, 3, 3, tzinfo=tz)  # Monday
    vals = [intraday_factor(cfg, start + timedelta(minutes=5 * i)) for i in range(7 * 288)]
    assert sum(vals) / len(vals) == pytest.approx(1.0, abs=0.005)
    # Within a day the peak is near 20:00 and the trough near 04:00.
    day = [(intraday_factor(cfg, datetime(2025, 3, 5, h, m, tzinfo=tz)), h + m / 60) for h in range(24) for m in range(0, 60, 10)]
    peak_at = max(day)[1]
    trough_at = min(day)[1]
    assert 19.5 <= peak_at <= 21.0
    assert 3.5 <= trough_at <= 5.0
    # At an hour centre the factor equals hour weight * dow weight.
    hw, dw = hour_weights(cfg), dow_weights(cfg)
    assert intraday_factor(cfg, datetime(2025, 3, 5, 20, 30, tzinfo=tz)) == pytest.approx(hw[20] * dw[2])
