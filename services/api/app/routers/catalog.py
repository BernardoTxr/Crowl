from __future__ import annotations

from datetime import date, timedelta
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from crowl_core.catalog import CITY_CATALOG_FILENAME, CatalogValidationError, load_city_catalog
from crowl_core.enums import TransportMode
from crowl_core.models import City, DurationHistogramDaily, TravelWindow
from crowl_core.travel_windows import TravelWindowSpec, generate_travel_windows

from ..dependencies import get_session
from ..presenters import as_utc, city_summary
from ..schemas import (
    CitiesResponse,
    CityDetailResponse,
    MetaResponse,
    OriginMeta,
    Coordinates,
    TypicalDuration,
    Weekend,
    WeekendsResponse,
)


router = APIRouter(tags=["catalog"])

MIN_TYPICAL_DURATION_SAMPLES = 5
HOME_TIMEZONE = ZoneInfo("Europe/Paris")


def _catalog_payload(request: Request) -> dict:
    path = request.app.state.settings.data_dir / CITY_CATALOG_FILENAME
    try:
        return load_city_catalog(path)
    except CatalogValidationError as exc:
        raise HTTPException(status_code=503, detail="catalog_unavailable") from exc


@router.get("/meta", response_model=MetaResponse)
def meta(request: Request) -> MetaResponse:
    payload = _catalog_payload(request)
    origin = payload["origin"]
    return MetaResponse(
        catalog_variant="final",
        origin=OriginMeta(
            id=origin["id"],
            name=origin["name"],
            timezone=origin["timezone"],
            coordinates=Coordinates(**origin["coordinates"]),
        ),
        passenger={
            "count": 1,
            "age": 22,
            "student": True,
            "baggage": "smallest_included_personal_item",
            "paid_entitlements": False,
        },
        rules={
            "friday_departure_not_before": "19:00 Europe/Paris",
            "monday_return_not_after": "07:00 Europe/Paris",
            "minimum_stay_hours": 12,
            "maximum_direction_hours": 12,
            "strict_schengen_path": True,
            "transfer_to_terminal_excluded": True,
            "destination_transfer_excluded": True,
        },
        modes=["all", "flight", "train", "bus"],
        category_definitions=payload.get("categoryDefinitions", []),
        attribute_definitions=[
            {
                "id": item["id"],
                "label": item["label"],
                "min": item["min"],
                "max": item["max"],
                "low_label": item.get("lowLabel"),
                "high_label": item.get("highLabel"),
            }
            for item in payload.get("attributeDefinitions", [])
        ],
    )


@router.get("/weekends", response_model=WeekendsResponse)
def weekends(
    request: Request,
    session: Session = Depends(get_session),
) -> WeekendsResponse:
    now = request.app.state.clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise RuntimeError("API clock must return a timezone-aware datetime")
    eligible_fridays = [window.friday for window in generate_travel_windows(now)]
    rows = session.scalars(
        select(TravelWindow)
        .where(TravelWindow.friday_date.in_(eligible_fridays))
        .order_by(TravelWindow.friday_date)
    ).all()
    return WeekendsResponse(
        items=[
            Weekend(
                friday=row.friday_date,
                starts_at=as_utc(row.starts_at),
                ends_at=as_utc(row.ends_at),
                label=TravelWindowSpec(
                    friday=row.friday_date,
                    starts_at=row.starts_at,
                    ends_at=row.ends_at,
                    timezone_name=row.timezone,
                    minimum_stay_minutes=row.minimum_stay_minutes,
                    maximum_direction_minutes=row.maximum_direction_minutes,
                ).label,
                minimum_stay_hours=row.minimum_stay_minutes // 60,
                max_direction_hours=row.maximum_direction_minutes // 60,
            )
            for row in rows
        ]
    )


@router.get("/cities", response_model=CitiesResponse)
def cities(session: Session = Depends(get_session)) -> CitiesResponse:
    rows = session.scalars(
        select(City)
        .where(City.active.is_(True))
        .options(selectinload(City.photos))
        .order_by(City.name)
    ).all()
    items = [city_summary(city) for city in rows]
    return CitiesResponse(items=items, total=len(items))


def _weighted_percentile(bins: dict[int, int], percentile: float) -> int | None:
    count = sum(bins.values())
    if not count:
        return None
    # Nearest-rank percentile: deterministic for the small integer samples
    # represented by the daily histograms.
    target = max(1, ceil(percentile * count))
    cumulative = 0
    for minutes, frequency in sorted(bins.items()):
        cumulative += frequency
        if cumulative >= target:
            return minutes
    return max(bins)


def _validated_duration_bins(raw_bins: Any) -> dict[int, int]:
    """Keep canonical nonnegative 15-minute bins with nonnegative counts."""

    if not isinstance(raw_bins, dict):
        return {}
    valid: dict[int, int] = {}
    for raw_minutes, frequency in raw_bins.items():
        if isinstance(raw_minutes, bool):
            continue
        if isinstance(raw_minutes, int):
            minutes = raw_minutes
        elif isinstance(raw_minutes, str) and raw_minutes.isdecimal():
            minutes = int(raw_minutes)
        else:
            continue
        if minutes < 0 or minutes % 15 != 0:
            continue
        if type(frequency) is not int or frequency < 0:
            continue
        valid[minutes] = valid.get(minutes, 0) + frequency
    return valid


def _duration_observation_date(request: Request) -> date:
    instant = request.app.state.clock()
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise RuntimeError("API clock must return a timezone-aware datetime")
    return instant.astimezone(HOME_TIMEZONE).date()


@router.get("/cities/{slug}", response_model=CityDetailResponse)
def city_detail(
    slug: str,
    request: Request,
    session: Session = Depends(get_session),
) -> CityDetailResponse:
    city = session.scalar(
        select(City)
        .where(City.slug == slug, City.active.is_(True))
        .options(selectinload(City.photos))
    )
    if city is None:
        raise HTTPException(status_code=404, detail="city_not_found")

    observed_through = _duration_observation_date(request)
    cutoff = observed_through - timedelta(
        days=request.app.state.settings.duration_history_days
    )
    typical: list[TypicalDuration] = []
    for mode in (TransportMode.FLIGHT, TransportMode.TRAIN, TransportMode.BUS):
        histograms = session.scalars(
            select(DurationHistogramDaily).where(
                DurationHistogramDaily.city_id == city.id,
                DurationHistogramDaily.mode == mode,
                DurationHistogramDaily.observed_on >= cutoff,
                DurationHistogramDaily.observed_on <= observed_through,
            )
        ).all()
        operational = [item for item in histograms if not item.is_synthetic]
        using_synthetic = bool(histograms) and not operational
        if operational:
            histograms = operational
        merged: dict[int, int] = {}
        for histogram in histograms:
            for minutes, frequency in _validated_duration_bins(histogram.bins).items():
                merged[minutes] = merged.get(minutes, 0) + int(frequency)
        samples = sum(merged.values())
        percentiles = (
            _weighted_percentile(merged, 0.25),
            _weighted_percentile(merged, 0.50),
            _weighted_percentile(merged, 0.75),
        )
        available = samples >= MIN_TYPICAL_DURATION_SAMPLES and all(
            value is not None for value in percentiles
        )
        typical.append(
            TypicalDuration(
                mode=mode,
                state="available" if available else "not_enough_data",
                p25_minutes=percentiles[0] if available else None,
                median_minutes=percentiles[1] if available else None,
                p75_minutes=percentiles[2] if available else None,
                sample_count=samples,
                is_synthetic=using_synthetic,
            )
        )
    return CityDetailResponse(city=city_summary(city), typical_durations=typical)
