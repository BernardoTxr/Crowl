from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from crowl_core.models import City, Terminal

from .schemas import CitySummary, CityStatus, Coordinates, Country, Photo, TerminalBrief


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def freshness(
    value: datetime | None,
    freshness_hours: int,
    *,
    now: datetime | None = None,
) -> str:
    observed = as_utc(value)
    if observed is None:
        return "never_observed"
    current = as_utc(now) if now is not None else datetime.now(timezone.utc)
    assert current is not None
    return (
        "fresh"
        if current - observed <= timedelta(hours=freshness_hours)
        else "stale"
    )


def city_summary(city: City) -> CitySummary:
    primary = next((photo for photo in city.photos if photo.is_primary), None)
    if primary is None and city.photos:
        primary = city.photos[0]
    photo = None
    if primary is not None:
        photo = Photo(
            asset=primary.asset_path,
            alt=primary.alt_text,
            creator=primary.creator,
            source_url=primary.source_url,
            license=primary.license,
            attribution=primary.attribution,
            curation_status=primary.curation_status,
            object_position=primary.object_position,
        )
    return CitySummary(
        id=city.id,
        slug=city.slug,
        name=city.name,
        country=Country(code=city.country_code, name=city.country_name),
        timezone=city.timezone,
        coordinates=Coordinates(lat=city.latitude, lon=city.longitude),
        status=CityStatus(visited=city.visited, wishlist=city.wishlist),
        categories=list(city.categories),
        attributes=dict(city.attributes),
        summary=city.summary,
        photo=photo,
    )


def terminal_brief(terminal: Terminal) -> TerminalBrief:
    return TerminalBrief(
        id=terminal.id,
        name=terminal.name,
        kind=terminal.kind.value,
        timezone=terminal.timezone,
    )


def load_terminal(session: Session, terminal_id: str | None) -> TerminalBrief | None:
    if terminal_id is None:
        return None
    terminal = session.get(Terminal, terminal_id)
    return terminal_brief(terminal) if terminal else None
