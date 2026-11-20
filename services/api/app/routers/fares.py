from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from crowl_core.enums import Availability, ExecutionCoverage, ModeFilter, TransportMode
from crowl_core.models import (
    City,
    CrawlRun,
    FareCellObservation,
    FareSnapshot,
    FareSnapshotRevision,
    SnapshotBooking,
    SnapshotLeg,
    Terminal,
    TravelWindow,
)
from crowl_core.travel_windows import lead_days

from ..dependencies import get_session
from ..presenters import as_utc, city_summary, freshness, load_terminal, terminal_brief
from ..schemas import (
    Booking,
    Journey,
    JourneysResponse,
    Leg,
    MapPriceItem,
    MapPricesResponse,
    PriceHistoryCell,
    PriceHistoryResponse,
    PriceScale,
)


router = APIRouter(tags=["fares"])


AVAILABILITY_PRIORITY = {
    Availability.UNKNOWN_DUE_FAILURE: 5,
    Availability.SOLD_OUT: 4,
    Availability.NOT_ON_SALE: 3,
    Availability.NO_VALID_OPTION: 2,
    Availability.AVAILABLE: 1,
}


@dataclass(frozen=True, slots=True)
class _ObservedCell:
    observation: FareCellObservation
    revision: FareSnapshotRevision | None
    window: TravelWindow
    crawl_run: CrawlRun

    @property
    def rank(self) -> tuple[int, date, datetime, int]:
        return (
            0 if self.crawl_run.is_synthetic else 1,
            self.crawl_run.scheduled_for,
            as_utc(self.observation.observed_at),
            self.observation.id,
        )


@dataclass(frozen=True, slots=True)
class _CurrentState:
    availability: Availability
    execution_coverage: ExecutionCoverage
    source_scope_ratio: float
    observed_at: datetime | None
    crawl_run_id: int | None
    diagnostic: str | None
    revision: FareSnapshotRevision | None
    is_synthetic: bool


def _choose_revision(
    revisions: list[FareSnapshotRevision],
) -> FareSnapshotRevision | None:
    available = [
        revision
        for revision in revisions
        if revision.availability == Availability.AVAILABLE
        and revision.total_price_eur_cents is not None
    ]
    if available:
        return min(
            available,
            key=lambda revision: (
                revision.total_price_eur_cents,
                as_utc(revision.observed_at),
                revision.id,
            ),
        )
    if not revisions:
        return None
    return max(
        revisions,
        key=lambda revision: (
            AVAILABILITY_PRIORITY[revision.availability],
            as_utc(revision.observed_at),
            revision.id,
        ),
    )


def _mode_clause(mode: ModeFilter):
    if mode == ModeFilter.ALL:
        return None
    return FareCellObservation.mode_bucket == TransportMode(mode.value)


def _observation_rows(
    session: Session,
    *,
    city_id: str | None = None,
    window_id: int | None = None,
    mode: ModeFilter = ModeFilter.ALL,
):
    query = (
        select(FareCellObservation, FareSnapshotRevision, TravelWindow, CrawlRun)
        .outerjoin(
            FareSnapshotRevision,
            FareCellObservation.trusted_revision_id == FareSnapshotRevision.id,
        )
        .join(TravelWindow, FareCellObservation.travel_window_id == TravelWindow.id)
        .join(CrawlRun, FareCellObservation.crawl_run_id == CrawlRun.id)
    )
    if city_id is not None:
        query = query.where(FareCellObservation.destination_city_id == city_id)
    if window_id is not None:
        query = query.where(FareCellObservation.travel_window_id == window_id)
    mode_clause = _mode_clause(mode)
    if mode_clause is not None:
        query = query.where(mode_clause)
    return [
        _ObservedCell(observation, revision, window, crawl_run)
        for observation, revision, window, crawl_run in session.execute(query).all()
    ]


def _latest_observation_per_mode(rows: list[_ObservedCell]) -> list[_ObservedCell]:
    latest: dict[TransportMode, _ObservedCell] = {}
    for row in rows:
        current = latest.get(row.observation.mode_bucket)
        if current is None or row.rank > current.rank:
            latest[row.observation.mode_bucket] = row
    return list(latest.values())


def _prefer_operational_rows(rows: list[_ObservedCell]) -> list[_ObservedCell]:
    """Keep development fixtures from competing with operational observations."""

    if any(not row.crawl_run.is_synthetic for row in rows):
        return [row for row in rows if not row.crawl_run.is_synthetic]
    return rows


def _current_state(rows: list[_ObservedCell]) -> _CurrentState:
    rows = _prefer_operational_rows(rows)
    if not rows:
        return _CurrentState(
            availability=Availability.UNKNOWN_DUE_FAILURE,
            execution_coverage=ExecutionCoverage.FAILED,
            source_scope_ratio=0.0,
            observed_at=None,
            crawl_run_id=None,
            diagnostic="NEVER_OBSERVED",
            revision=None,
            is_synthetic=False,
        )

    revisions = [row.revision for row in rows if row.revision is not None]
    revision = _choose_revision(revisions)

    # `mode=all` compares the retained trusted revisions from each mode.  Once
    # a winning revision has been selected, its current availability and
    # coverage must come from that same mode.  Aggregating state across every
    # mode could otherwise label a retained failed flight price as "available"
    # merely because a more expensive train observation succeeded today.
    state_rows = rows
    if revision is not None:
        matching_mode = [
            row
            for row in rows
            if row.observation.mode_bucket == revision.mode_bucket
        ]
        if matching_mode:
            state_rows = matching_mode
    if any(
        row.observation.availability is Availability.AVAILABLE
        for row in state_rows
    ):
        availability = Availability.AVAILABLE
    else:
        availability = max(
            (row.observation.availability for row in state_rows),
            key=lambda value: AVAILABILITY_PRIORITY[value],
        )

    coverages = [row.observation.execution_coverage for row in state_rows]
    if all(item is ExecutionCoverage.COMPLETE for item in coverages):
        execution_coverage = ExecutionCoverage.COMPLETE
    elif all(item is ExecutionCoverage.FAILED for item in coverages):
        execution_coverage = ExecutionCoverage.FAILED
    else:
        execution_coverage = ExecutionCoverage.PARTIAL

    latest = max(state_rows, key=lambda row: row.rank)
    diagnostics = sorted(
        {
            row.observation.diagnostic
            for row in state_rows
            if row.observation.diagnostic
        }
    )
    return _CurrentState(
        availability=availability,
        execution_coverage=execution_coverage,
        source_scope_ratio=min(
            row.observation.source_scope_ratio for row in state_rows
        ),
        observed_at=max(
            as_utc(row.observation.observed_at) for row in state_rows
        ),
        crawl_run_id=latest.crawl_run.id,
        diagnostic=",".join(diagnostics)[:500] or None,
        revision=revision,
        is_synthetic=latest.crawl_run.is_synthetic,
    )


def _endpoints(
    session: Session, revision_id: int
) -> tuple[object | None, object | None, object | None, object | None]:
    legs = session.scalars(
        select(SnapshotLeg)
        .where(SnapshotLeg.revision_id == revision_id)
        .order_by(SnapshotLeg.direction, SnapshotLeg.segment_order)
    ).all()
    outbound = [leg for leg in legs if leg.direction.value == "outbound"]
    returning = [leg for leg in legs if leg.direction.value == "return"]
    return (
        load_terminal(session, outbound[0].origin_terminal_id) if outbound else None,
        load_terminal(session, outbound[-1].destination_terminal_id) if outbound else None,
        load_terminal(session, returning[0].origin_terminal_id) if returning else None,
        load_terminal(session, returning[-1].destination_terminal_id) if returning else None,
    )


@router.get("/map-prices", response_model=MapPricesResponse)
def map_prices(
    request: Request,
    weekend: date = Query(...),
    mode: ModeFilter = ModeFilter.ALL,
    session: Session = Depends(get_session),
) -> MapPricesResponse:
    window = session.scalar(select(TravelWindow).where(TravelWindow.friday_date == weekend))
    if window is None:
        raise HTTPException(status_code=404, detail="weekend_not_found")

    cities = session.scalars(
        select(City)
        .where(City.active.is_(True))
        .options(selectinload(City.photos))
        .order_by(City.name)
    ).all()
    grouped: dict[str, list[_ObservedCell]] = defaultdict(list)
    for row in _observation_rows(session, window_id=window.id, mode=mode):
        grouped[row.observation.destination_city_id].append(row)

    items: list[MapPriceItem] = []
    observed_values = []
    for city in cities:
        state = _current_state(
            _latest_observation_per_mode(grouped.get(city.id, []))
        )
        revision = state.revision
        if revision is None:
            items.append(
                MapPriceItem(
                    city=city_summary(city),
                    price_eur_cents=None,
                    winning_mode=None,
                    rule_version_id=None,
                    profile_id=None,
                    availability=state.availability,
                    execution_coverage=state.execution_coverage,
                    source_scope_ratio=state.source_scope_ratio,
                    is_synthetic=state.is_synthetic,
                    freshness="never_observed",
                    observed_at=None,
                    current_observed_at=state.observed_at,
                    current_crawl_run_id=state.crawl_run_id,
                    current_diagnostic=state.diagnostic,
                    origin_terminal=None,
                    destination_terminal=None,
                    return_origin_terminal=None,
                    return_destination_terminal=None,
                    revision_id=None,
                )
            )
            continue
        observed_at = as_utc(revision.observed_at)
        observed_values.append(observed_at)
        origin, destination, return_origin, return_destination = _endpoints(session, revision.id)
        items.append(
            MapPriceItem(
                city=city_summary(city),
                price_eur_cents=revision.total_price_eur_cents,
                winning_mode=revision.mode_bucket,
                rule_version_id=revision.rule_version_id,
                profile_id=revision.profile_id,
                availability=state.availability,
                execution_coverage=state.execution_coverage,
                source_scope_ratio=state.source_scope_ratio,
                is_synthetic=state.is_synthetic,
                freshness=freshness(
                    observed_at,
                    request.app.state.settings.freshness_hours,
                    now=request.app.state.clock(),
                ),
                observed_at=observed_at,
                current_observed_at=state.observed_at,
                current_crawl_run_id=state.crawl_run_id,
                current_diagnostic=state.diagnostic,
                origin_terminal=origin,
                destination_terminal=destination,
                return_origin_terminal=return_origin,
                return_destination_terminal=return_destination,
                revision_id=revision.id,
            )
        )
    items.sort(
        key=lambda item: (
            item.price_eur_cents is None,
            item.price_eur_cents or 0,
            item.city.name.casefold(),
        )
    )
    return MapPricesResponse(
        weekend=weekend,
        mode=mode,
        observed_at=max(observed_values) if observed_values else None,
        price_scale_breakpoints_eur_cents=[5_000, 10_000, 17_500, 25_000],
        items=items,
    )


def _get_city(session: Session, slug: str) -> City:
    city = session.scalar(
        select(City)
        .where(City.slug == slug, City.active.is_(True))
        .options(selectinload(City.photos))
    )
    if city is None:
        raise HTTPException(status_code=404, detail="city_not_found")
    return city


def _nearest_percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


@router.get("/cities/{slug}/price-history", response_model=PriceHistoryResponse)
def price_history(
    slug: str,
    request: Request,
    mode: ModeFilter = ModeFilter.ALL,
    session: Session = Depends(get_session),
) -> PriceHistoryResponse:
    city = _get_city(session, slug)
    grouped: dict[tuple[date, int], tuple[TravelWindow, list[_ObservedCell]]] = {}
    for row in _observation_rows(session, city_id=city.id, mode=mode):
        key = (row.crawl_run.scheduled_for, row.window.id)
        if key not in grouped:
            grouped[key] = (row.window, [])
        grouped[key][1].append(row)

    cells: list[PriceHistoryCell] = []
    prices: list[int] = []
    for (observed_on, _), (window, rows) in grouped.items():
        state = _current_state(rows)
        revision = state.revision
        if revision is not None and revision.total_price_eur_cents is not None:
            prices.append(revision.total_price_eur_cents)
        departure = as_utc(revision.outbound_departure_at) if revision else None
        observed_at = as_utc(revision.observed_at) if revision else None
        latest_observation = max(rows, key=lambda row: row.rank).observation
        cells.append(
            PriceHistoryCell(
                weekend=window.friday_date,
                observed_on=observed_on,
                lead_days=lead_days(observed_on, departure) if departure else None,
                price_eur_cents=(
                    revision.total_price_eur_cents if revision else None
                ),
                availability=state.availability,
                execution_coverage=state.execution_coverage,
                source_scope_ratio=state.source_scope_ratio,
                is_synthetic=state.is_synthetic,
                freshness=(
                    freshness(
                        observed_at,
                        request.app.state.settings.freshness_hours,
                        now=request.app.state.clock(),
                    )
                    if observed_at
                    else "never_observed"
                ),
                observed_at=observed_at,
                current_observed_at=state.observed_at,
                current_crawl_run_id=state.crawl_run_id,
                current_diagnostic=state.diagnostic,
                revision_id=revision.id if revision else None,
                rule_version_id=(
                    revision.rule_version_id
                    if revision
                    else latest_observation.rule_version_id
                ),
                profile_id=(
                    revision.profile_id
                    if revision
                    else latest_observation.profile_id
                ),
                winning_mode=revision.mode_bucket if revision else None,
            )
        )
    cells.sort(key=lambda cell: (cell.weekend, cell.observed_on))
    return PriceHistoryResponse(
        city=city_summary(city),
        mode=mode,
        scale=PriceScale(
            p10_eur_cents=_nearest_percentile(prices, 0.10),
            p90_eur_cents=_nearest_percentile(prices, 0.90),
        ),
        cells=cells,
    )


def _journey(
    session: Session,
    revision: FareSnapshotRevision,
    *,
    historical: bool,
    freshness_hours: int,
    now: datetime,
    is_synthetic: bool,
) -> Journey:
    booking_rows = session.scalars(
        select(SnapshotBooking)
        .where(SnapshotBooking.revision_id == revision.id)
        .order_by(SnapshotBooking.component_order)
    ).all()
    leg_rows = session.scalars(
        select(SnapshotLeg)
        .where(SnapshotLeg.revision_id == revision.id)
        .order_by(SnapshotLeg.direction, SnapshotLeg.segment_order)
    ).all()
    terminal_ids = {
        terminal_id
        for leg in leg_rows
        for terminal_id in (leg.origin_terminal_id, leg.destination_terminal_id)
    }
    terminals = {
        terminal.id: terminal
        for terminal in session.scalars(select(Terminal).where(Terminal.id.in_(terminal_ids))).all()
    }
    return Journey(
        revision_id=revision.id,
        rule_version_id=revision.rule_version_id,
        profile_id=revision.profile_id,
        total_price_eur_cents=revision.total_price_eur_cents,
        mode=revision.mode_bucket,
        observed_at=as_utc(revision.observed_at),
        published_at=as_utc(revision.published_at),
        historical=historical,
        availability=revision.availability,
        execution_coverage=revision.execution_coverage,
        source_scope_ratio=revision.source_scope_ratio,
        is_synthetic=is_synthetic,
        freshness=freshness(
            revision.observed_at,
            freshness_hours,
            now=now,
        ),
        outbound_departure_at=as_utc(revision.outbound_departure_at),
        outbound_arrival_at=as_utc(revision.outbound_arrival_at),
        return_departure_at=as_utc(revision.return_departure_at),
        return_arrival_at=as_utc(revision.return_arrival_at),
        outbound_duration_minutes=revision.outbound_duration_minutes,
        return_duration_minutes=revision.return_duration_minutes,
        stay_duration_minutes=revision.stay_duration_minutes,
        transfer_count=revision.transfer_count,
        bookings=[
            Booking(
                id=row.id,
                component_order=row.component_order,
                source=row.source_id,
                seller=row.seller,
                fare_name=row.fare_name,
                pricing_semantics=row.pricing_semantics.value,
                original_amount_minor=row.original_amount_minor,
                original_currency=row.original_currency,
                eur_amount_cents=row.eur_amount_cents,
                fx_rate=row.fx_rate,
                fx_rate_date=row.fx_rate_date,
                student_fare_used=row.student_fare_used,
                mandatory_fees_included=row.mandatory_fees_included,
                passenger_age=row.passenger_age,
                passenger_student=row.passenger_student,
                passenger_eligibility_confirmed=(
                    row.passenger_eligibility_confirmed
                ),
                bookable_at_observation=row.bookable_at_observation,
                is_teaser=row.is_teaser,
                booking_url=row.booking_url,
                expires_at=as_utc(row.expires_at),
            )
            for row in booking_rows
        ],
        legs=[
            Leg(
                id=row.id,
                booking_id=row.booking_id,
                direction=row.direction.value,
                segment_order=row.segment_order,
                mode=row.mode,
                operator=row.operator,
                service_number=row.service_number,
                origin_terminal=terminal_brief(terminals[row.origin_terminal_id]),
                destination_terminal=terminal_brief(terminals[row.destination_terminal_id]),
                departs_at=as_utc(row.departs_at),
                arrives_at=as_utc(row.arrives_at),
                origin_timezone=row.origin_timezone,
                destination_timezone=row.destination_timezone,
                protection_state=row.protection_state.value,
            )
            for row in leg_rows
            if row.origin_terminal_id in terminals and row.destination_terminal_id in terminals
        ],
    )


@router.get("/cities/{slug}/journeys", response_model=JourneysResponse)
def journeys(
    slug: str,
    request: Request,
    weekend: date = Query(...),
    mode: ModeFilter = ModeFilter.ALL,
    revision_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> JourneysResponse:
    city = _get_city(session, slug)
    window = session.scalar(select(TravelWindow).where(TravelWindow.friday_date == weekend))
    if window is None:
        raise HTTPException(status_code=404, detail="weekend_not_found")
    if revision_id is not None:
        historical_row = session.execute(
            select(FareSnapshot, FareSnapshotRevision)
            .join(
                FareSnapshotRevision,
                FareSnapshotRevision.fare_snapshot_id == FareSnapshot.id,
            )
            .where(
                FareSnapshotRevision.id == revision_id,
                FareSnapshot.destination_city_id == city.id,
                FareSnapshot.travel_window_id == window.id,
            )
        ).one_or_none()
        if historical_row is None:
            raise HTTPException(status_code=404, detail="revision_not_found")
        snapshot, historical_revision = historical_row
        historical_run = session.get(CrawlRun, historical_revision.crawl_run_id)
        if historical_run is None:
            raise HTTPException(status_code=404, detail="revision_run_not_found")
        if mode != ModeFilter.ALL and snapshot.mode_bucket != TransportMode(mode.value):
            raise HTTPException(status_code=404, detail="revision_not_found")
        revisions = [historical_revision]
        state = _CurrentState(
            availability=historical_revision.availability,
            execution_coverage=historical_revision.execution_coverage,
            source_scope_ratio=historical_revision.source_scope_ratio,
            observed_at=as_utc(historical_revision.observed_at),
            crawl_run_id=historical_revision.crawl_run_id,
            diagnostic="HISTORICAL_REVISION",
            revision=historical_revision,
            is_synthetic=historical_run.is_synthetic,
        )
    else:
        rows = _prefer_operational_rows(
            _latest_observation_per_mode(
                _observation_rows(
                    session, city_id=city.id, window_id=window.id, mode=mode
                )
            )
        )
        state = _current_state(rows)
        revisions_by_id = {
            row.revision.id: row.revision
            for row in rows
            if row.revision is not None
        }
        revisions = list(revisions_by_id.values())
    selected = state.revision
    if selected is None:
        return JourneysResponse(
            city=city_summary(city),
            weekend=weekend,
            mode=mode,
            availability=state.availability,
            execution_coverage=state.execution_coverage,
            source_scope_ratio=state.source_scope_ratio,
            freshness="never_observed",
            observed_at=None,
            current_observed_at=state.observed_at,
            current_crawl_run_id=state.crawl_run_id,
            current_diagnostic=state.diagnostic,
            items=[],
        )
    available = [
        revision
        for revision in revisions
        if revision.availability == Availability.AVAILABLE
        and revision.total_price_eur_cents is not None
    ]
    available.sort(key=lambda item: (item.total_price_eur_cents, item.id))
    request_now = request.app.state.clock()
    historical = revision_id is not None or weekend < request_now.date()
    return JourneysResponse(
        city=city_summary(city),
        weekend=weekend,
        mode=mode,
        availability=state.availability,
        execution_coverage=state.execution_coverage,
        source_scope_ratio=state.source_scope_ratio,
        freshness=freshness(
            selected.observed_at,
            request.app.state.settings.freshness_hours,
            now=request_now,
        ),
        observed_at=as_utc(selected.observed_at),
        current_observed_at=state.observed_at,
        current_crawl_run_id=state.crawl_run_id,
        current_diagnostic=state.diagnostic,
        items=[
            _journey(
                session,
                revision,
                historical=historical,
                freshness_hours=request.app.state.settings.freshness_hours,
                now=request_now,
                is_synthetic=state.is_synthetic,
            )
            for revision in available
        ],
    )
