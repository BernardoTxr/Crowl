from __future__ import annotations

from datetime import UTC, date, datetime

from app.routers.fares import _ObservedCell, _current_state
from crowl_core.enums import Availability, ExecutionCoverage, TransportMode
from crowl_core.models import (
    CrawlRun,
    FareCellObservation,
    FareSnapshotRevision,
    TravelWindow,
)


def _row(
    *,
    mode: TransportMode,
    price: int,
    availability: Availability,
    coverage: ExecutionCoverage,
    diagnostic: str,
    identifier: int,
) -> _ObservedCell:
    observed_at = datetime(2027, 1, 2, 2, identifier, tzinfo=UTC)
    revision = FareSnapshotRevision(
        id=identifier,
        mode_bucket=mode,
        availability=Availability.AVAILABLE,
        total_price_eur_cents=price,
        observed_at=observed_at,
    )
    observation = FareCellObservation(
        id=identifier,
        mode_bucket=mode,
        availability=availability,
        execution_coverage=coverage,
        source_scope_ratio=1.0,
        observed_at=observed_at,
        diagnostic=diagnostic,
    )
    run = CrawlRun(
        id=identifier,
        scheduled_for=date(2027, 1, 2),
        created_at=observed_at,
    )
    return _ObservedCell(
        observation=observation,
        revision=revision,
        window=TravelWindow(friday_date=date(2027, 1, 8)),
        crawl_run=run,
    )


def test_all_mode_state_stays_bound_to_the_winning_revision_mode() -> None:
    failed_cheaper_flight = _row(
        mode=TransportMode.FLIGHT,
        price=5_000,
        availability=Availability.UNKNOWN_DUE_FAILURE,
        coverage=ExecutionCoverage.FAILED,
        diagnostic="FLIGHT_UPSTREAM_FAILURE",
        identifier=1,
    )
    available_train = _row(
        mode=TransportMode.TRAIN,
        price=10_000,
        availability=Availability.AVAILABLE,
        coverage=ExecutionCoverage.COMPLETE,
        diagnostic="TRAIN_OK",
        identifier=2,
    )

    state = _current_state([failed_cheaper_flight, available_train])

    assert state.revision is failed_cheaper_flight.revision
    assert state.availability is Availability.UNKNOWN_DUE_FAILURE
    assert state.execution_coverage is ExecutionCoverage.FAILED
    assert state.diagnostic == "FLIGHT_UPSTREAM_FAILURE"
    assert state.crawl_run_id == 1
