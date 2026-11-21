"""Weekend window generation and lead-day calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta


PARIS = ZoneInfo("Europe/Paris")


@dataclass(frozen=True, slots=True)
class TravelWindowSpec:
    """A Friday-to-Monday weekend window, persisted as UTC instants."""

    friday: date
    starts_at: datetime
    ends_at: datetime
    timezone_name: str = "Europe/Paris"
    minimum_stay_minutes: int = 12 * 60
    maximum_direction_minutes: int = 12 * 60

    @classmethod
    def for_friday(cls, friday: date) -> "TravelWindowSpec":
        if friday.weekday() != 4:
            raise ValueError("travel window identity must be a Friday")
        local_start = datetime.combine(friday, time(19, 0), PARIS)
        local_end = datetime.combine(friday + timedelta(days=3), time(7, 0), PARIS)
        return cls(
            friday=friday,
            starts_at=local_start.astimezone(timezone.utc),
            ends_at=local_end.astimezone(timezone.utc),
        )

    @property
    def local_start(self) -> datetime:
        return self.starts_at.astimezone(ZoneInfo(self.timezone_name))

    @property
    def local_end(self) -> datetime:
        return self.ends_at.astimezone(ZoneInfo(self.timezone_name))

    @property
    def label(self) -> str:
        monday = self.friday + timedelta(days=3)
        return (
            f"Fri {self.friday.day} {self.friday:%b} after 19:00 → "
            f"Mon {monday.day} {monday:%b} by 07:00"
        )


def _coerce_local_as_of(as_of: date | datetime | None) -> datetime:
    if as_of is None:
        return datetime.now(timezone.utc).astimezone(PARIS)
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of datetime must be timezone-aware")
        return as_of.astimezone(PARIS)
    return datetime.combine(as_of, time.min, PARIS)


def next_eligible_friday(as_of: date | datetime | None = None) -> date:
    """Return the next Friday whose 19:00 departure boundary has not passed."""

    local = _coerce_local_as_of(as_of)
    days_until_friday = (4 - local.weekday()) % 7
    friday = local.date() + timedelta(days=days_until_friday)
    if days_until_friday == 0 and local.time() > time(19, 0):
        friday += timedelta(days=7)
    return friday


def generate_travel_windows(
    as_of: date | datetime | None = None,
    *,
    months_ahead: int = 6,
) -> list[TravelWindowSpec]:
    """Generate eligible weekends through the six-calendar-month horizon.

    The final Friday is included when it falls exactly on the horizon date.
    Calendar-month arithmetic avoids treating six months as a fixed 180 days.
    """

    if months_ahead < 0:
        raise ValueError("months_ahead must be non-negative")
    local = _coerce_local_as_of(as_of)
    horizon = local.date() + relativedelta(months=months_ahead)
    friday = next_eligible_friday(local)
    windows: list[TravelWindowSpec] = []
    while friday <= horizon:
        windows.append(TravelWindowSpec.for_friday(friday))
        friday += timedelta(days=7)
    return windows


def lead_days(observed_on: date, outbound_departure_at: datetime) -> int:
    """Calendar days to departure in Paris, intentionally not 24-hour periods."""

    if outbound_departure_at.tzinfo is None or outbound_departure_at.utcoffset() is None:
        raise ValueError("outbound departure must be timezone-aware")
    return (outbound_departure_at.astimezone(PARIS).date() - observed_on).days
