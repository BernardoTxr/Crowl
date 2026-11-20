from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from crowl_core.travel_windows import (
    TravelWindowSpec,
    generate_travel_windows,
    lead_days,
    next_eligible_friday,
)


PARIS = ZoneInfo("Europe/Paris")


def test_window_uses_paris_wall_clock_across_dst_start() -> None:
    window = TravelWindowSpec.for_friday(date(2026, 3, 27))

    assert window.local_start.isoformat() == "2026-03-27T19:00:00+01:00"
    assert window.local_end.isoformat() == "2026-03-30T07:00:00+02:00"
    assert window.starts_at.isoformat() == "2026-03-27T18:00:00+00:00"
    assert window.ends_at.isoformat() == "2026-03-30T05:00:00+00:00"


def test_window_uses_paris_wall_clock_across_dst_end() -> None:
    window = TravelWindowSpec.for_friday(date(2026, 10, 23))

    assert window.local_start.isoformat() == "2026-10-23T19:00:00+02:00"
    assert window.local_end.isoformat() == "2026-10-26T07:00:00+01:00"


def test_friday_at_1900_is_still_eligible_but_one_second_later_is_next_week() -> None:
    exact = datetime(2026, 1, 9, 19, 0, tzinfo=PARIS)
    after = datetime(2026, 1, 9, 19, 0, 1, tzinfo=PARIS)

    assert next_eligible_friday(exact) == date(2026, 1, 9)
    assert next_eligible_friday(after) == date(2026, 1, 16)


def test_six_month_horizon_uses_calendar_arithmetic() -> None:
    windows = generate_travel_windows(date(2026, 1, 31))

    assert windows[0].friday == date(2026, 2, 6)
    assert windows[-1].friday <= date(2026, 7, 31)
    assert windows[-1].friday + timedelta(days=7) > date(2026, 7, 31)


def test_lead_days_is_paris_calendar_difference_not_24_hour_division() -> None:
    departure = datetime(2026, 3, 29, 22, 30, tzinfo=timezone.utc)

    assert lead_days(date(2026, 3, 28), departure) == 2


def test_non_friday_and_naive_inputs_fail_loudly() -> None:
    with pytest.raises(ValueError, match="Friday"):
        TravelWindowSpec.for_friday(date(2026, 1, 8))
    with pytest.raises(ValueError, match="timezone-aware"):
        generate_travel_windows(datetime(2026, 1, 8, 12, 0))
