from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from crowl_core import Base, create_database_engine, create_session_factory
from crowl_core.catalog import seed_development_data
from crowl_core.enums import Direction as DatabaseDirection
from crowl_core.enums import TransportMode as DatabaseTransportMode
from crowl_core.models import (
    FareSnapshot,
    FareSnapshotRevision,
    SnapshotBooking,
    SnapshotLeg,
    TerminalTransfer,
)
from crowl_crawler.adapters.base import SourceAdapter
from crowl_crawler.adapters.registry import AdapterRegistry
from crowl_crawler.batch import BatchCoordinator
from crowl_crawler.health import InMemoryHealthSink
from crowl_crawler.manifest import DailyManifestBuilder, FIXTURE_DESTINATIONS
from crowl_crawler.models import (
    AdapterMetadata,
    BookingComponent,
    ConnectorResult,
    CoverageDiscovery,
    Direction,
    ExtractionStrategy,
    NormalizedOffer,
    PriceCompleteness,
    PricingSemantics,
    SearchKind,
    SearchOutcome,
    SearchRequest,
    Segment,
    TransportMode,
)
from crowl_crawler.persistence import DatabasePublisher


ROOT = Path(__file__).resolve().parents[2]
RUN_DATE = date(2027, 1, 1)
WEEKEND = date(2027, 1, 8)
OBSERVED_AT = datetime(2027, 1, 1, 2, tzinfo=UTC)
PARIS = ZoneInfo("Europe/Paris")
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
_BOOKING_HOSTS = {
    "transavia": "www.transavia.com",
    "sncf_connect": "www.sncf-connect.com",
}


class ScenarioAdapter(SourceAdapter):
    def __init__(
        self,
        *,
        source_slug: str,
        mode: TransportMode,
        one_way_direction: Direction | None = None,
        one_way_price: int | None = None,
        atomic_round_trip_price: int | None = None,
        included_transfer_cost_eur_cents: int = 0,
    ) -> None:
        self.source_slug = source_slug
        self.mode = mode
        self.one_way_direction = one_way_direction
        self.one_way_price = one_way_price
        self.atomic_round_trip_price = atomic_round_trip_price
        self.included_transfer_cost_eur_cents = included_transfer_cost_eur_cents
        self._metadata = AdapterMetadata(
            source_slug=source_slug,
            display_name=f"{source_slug} scenario feed",
            modes=(mode,),
            strategy=ExtractionStrategy.HTTP_STRUCTURED,
            intended_strategy=ExtractionStrategy.HTTP_STRUCTURED,
            connector_version="scenario-v1",
            parser_version="scenario-v1",
            enabled=True,
            disabled_reason=None,
            access_basis="test-only adapter contract",
        )

    def metadata(self) -> AdapterMetadata:
        return self._metadata

    async def discover_coverage(self) -> CoverageDiscovery:
        return CoverageDiscovery(
            source_slug=self.source_slug,
            supported_terminal_pairs=(),
            sellable_through=None,
            observed_at=OBSERVED_AT,
            diagnostic_code="SCENARIO_COVERAGE",
        )

    async def canary(
        self, requests: tuple[SearchRequest, ...]
    ) -> tuple[ConnectorResult, ...]:
        return tuple([await self.search(request) for request in requests])

    async def search(self, request: SearchRequest) -> ConnectorResult:
        if (
            request.search_kind is SearchKind.ROUND_TRIP
            and self.atomic_round_trip_price is not None
        ):
            return self._atomic_result(request, self.atomic_round_trip_price)
        if (
            request.search_kind is SearchKind.ONE_WAY
            and request.direction is self.one_way_direction
            and self.one_way_price is not None
        ):
            return self._directional_result(request, self.one_way_price)
        return ConnectorResult(
            outcome=SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE,
            retryable=False,
            diagnostic_code="SCENARIO_UNSUPPORTED",
            expected_count=1,
            completed_count=1,
            offers=(),
            parser_version="scenario-v1",
            observed_at=OBSERVED_AT,
        )

    def _directional_result(
        self, request: SearchRequest, price: int
    ) -> ConnectorResult:
        assert request.direction is not None
        segment = _directional_segment(request.direction, self.mode, self.source_slug)
        segments = (segment,)
        if (
            request.direction is Direction.OUTBOUND
            and self.included_transfer_cost_eur_cents
        ):
            segments = (
                replace(
                    segment,
                    arrival_terminal_id="par-bva",
                    arrival_terminal_name="Paris Beauvais Airport",
                    arrives_at=segment.departs_at + timedelta(minutes=30),
                ),
                replace(
                    segment,
                    sequence=2,
                    departure_terminal_id="par-cdg",
                    departure_terminal_name="Paris Charles de Gaulle Airport",
                    departs_at=segment.departs_at + timedelta(hours=2),
                    arrives_at=segment.arrives_at + timedelta(hours=2),
                    protected_connection=False,
                ),
            )
        offer = NormalizedOffer(
            provider_offer_id=f"{self.source_slug}-{request.direction.value}",
            source_slug=self.source_slug,
            destination_city_id=request.destination_city_id,
            pricing_semantics=PricingSemantics.ONE_WAY,
            price_completeness=PriceCompleteness.COMPLETE,
            total_amount_minor=price,
            currency="EUR",
            mandatory_fees_included=True,
            passenger_age=request.passenger.age,
            passenger_student=request.passenger.student,
            passenger_eligibility_confirmed=True,
            bookable=True,
            is_teaser=False,
            observed_at=OBSERVED_AT,
            outbound_segments=segments,
            booking_components=(
                BookingComponent(
                    seller=f"{self.source_slug} direct",
                    source_slug=self.source_slug,
                    amount_minor=price,
                    currency="EUR",
                    booking_url=(
                        f"https://{_BOOKING_HOSTS[self.source_slug]}/"
                        f"{request.direction.value}"
                    ),
                    fare_name="Scenario one way",
                    mandatory_fees_included=True,
                    passenger_age=request.passenger.age,
                    passenger_student=request.passenger.student,
                    passenger_eligibility_confirmed=True,
                    bookable=True,
                    is_teaser=False,
                ),
            ),
            included_transfer_cost_eur_cents=(
                self.included_transfer_cost_eur_cents
            ),
            expires_at=OBSERVED_AT + timedelta(minutes=30),
        )
        return _result(offer)

    def _atomic_result(
        self, request: SearchRequest, price: int
    ) -> ConnectorResult:
        offer = NormalizedOffer(
            provider_offer_id=f"{self.source_slug}-atomic",
            source_slug=self.source_slug,
            destination_city_id=request.destination_city_id,
            pricing_semantics=PricingSemantics.ROUND_TRIP_ONLY,
            price_completeness=PriceCompleteness.COMPLETE,
            total_amount_minor=price,
            currency="EUR",
            mandatory_fees_included=True,
            passenger_age=request.passenger.age,
            passenger_student=request.passenger.student,
            passenger_eligibility_confirmed=True,
            bookable=True,
            is_teaser=False,
            observed_at=OBSERVED_AT,
            outbound_segments=(
                _directional_segment(Direction.OUTBOUND, self.mode, self.source_slug),
            ),
            return_segments=(
                _directional_segment(Direction.RETURN, self.mode, self.source_slug),
            ),
            booking_components=(
                BookingComponent(
                    seller=f"{self.source_slug} direct",
                    source_slug=self.source_slug,
                    amount_minor=price,
                    currency="EUR",
                    booking_url=(
                        f"https://{_BOOKING_HOSTS[self.source_slug]}/round-trip"
                    ),
                    fare_name="Scenario round trip",
                    mandatory_fees_included=True,
                    passenger_age=request.passenger.age,
                    passenger_student=request.passenger.student,
                    passenger_eligibility_confirmed=True,
                    bookable=True,
                    is_teaser=False,
                ),
            ),
            expires_at=OBSERVED_AT + timedelta(minutes=30),
        )
        return _result(offer)

    async def close(self) -> None:
        return None


def _directional_segment(
    direction: Direction,
    mode: TransportMode,
    operator: str,
) -> Segment:
    if direction is Direction.OUTBOUND:
        departs_at = datetime(2027, 1, 8, 20, 0, tzinfo=PARIS)
        arrives_at = datetime(2027, 1, 8, 21, 30, tzinfo=AMSTERDAM)
        origin = "par-ory" if mode is TransportMode.FLIGHT else "massy-tgv"
        destination = (
            "ams-schiphol" if mode is TransportMode.FLIGHT else "ams-centraal"
        )
    else:
        departs_at = datetime(2027, 1, 10, 18, 0, tzinfo=AMSTERDAM)
        arrives_at = datetime(2027, 1, 10, 21, 30, tzinfo=PARIS)
        origin = "ams-schiphol" if mode is TransportMode.FLIGHT else "ams-centraal"
        destination = "par-ory" if mode is TransportMode.FLIGHT else "massy-tgv"
    return Segment(
        sequence=1,
        mode=mode,
        operator=operator,
        service_number=f"{operator}-{direction.value}",
        departure_terminal_id=origin,
        departure_terminal_name=origin,
        arrival_terminal_id=destination,
        arrival_terminal_name=destination,
        departs_at=departs_at,
        arrives_at=arrives_at,
    )


def _result(offer: NormalizedOffer) -> ConnectorResult:
    return ConnectorResult(
        outcome=SearchOutcome.SUCCESS_RESULTS,
        retryable=False,
        diagnostic_code="SCENARIO_RESULTS",
        expected_count=1,
        completed_count=1,
        offers=(offer,),
        parser_version="scenario-v1",
        observed_at=OBSERVED_AT,
    )


class TestComposedPublication:
    def setup_method(self) -> None:
        self.engine = create_database_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = create_session_factory(self.engine)
        with self.sessions() as session:
            seed_development_data(
                session,
                data_dir=ROOT / "data",
                as_of=RUN_DATE,
                include_fixture_fares=False,
            )

    def teardown_method(self) -> None:
        self.engine.dispose()

    def _publish(self, adapters: tuple[SourceAdapter, ...]):
        registry = AdapterRegistry(adapters)
        manifest = DailyManifestBuilder(
            origin_terminal_ids=("par-ory", "massy-tgv"),
            config_version="composition-test-v1",
        ).build(
            scheduled_for=RUN_DATE,
            source_slugs=tuple(item.metadata().source_slug for item in adapters),
            destinations=(FIXTURE_DESTINATIONS["amsterdam-nl"],),
            weekend_fridays=(WEEKEND,),
            include_one_way_tasks=True,
            created_at=OBSERVED_AT,
        )
        sink = InMemoryHealthSink()
        report = asyncio.run(
            BatchCoordinator(
                registry,
                health_sink=sink,
                clock=lambda: OBSERVED_AT,
            ).run(manifest)
        )
        result = DatabasePublisher(self.sessions).publish(
            manifest,
            report,
            health_events=sink.events,
            source_metadata=registry.metadata(include_fixture=False),
        )
        return result

    def test_flight_out_train_return_publishes_mixed_trip_and_two_bookings(
        self,
    ) -> None:
        result = self._publish(
            (
                ScenarioAdapter(
                    source_slug="transavia",
                    mode=TransportMode.FLIGHT,
                    one_way_direction=Direction.OUTBOUND,
                    one_way_price=4_000,
                ),
                ScenarioAdapter(
                    source_slug="sncf_connect",
                    mode=TransportMode.TRAIN,
                    one_way_direction=Direction.RETURN,
                    one_way_price=3_500,
                ),
            )
        )

        assert result.snapshots_created == 1
        assert result.coverage_cells_written == 4
        with self.sessions() as session:
            snapshot = session.scalar(select(FareSnapshot))
            revision = session.scalar(select(FareSnapshotRevision))
            bookings = session.scalars(
                select(SnapshotBooking).order_by(SnapshotBooking.component_order)
            ).all()
            legs = session.scalars(
                select(SnapshotLeg).order_by(SnapshotLeg.direction)
            ).all()
        assert snapshot is not None and revision is not None
        assert snapshot.mode_bucket is DatabaseTransportMode.MIXED
        assert revision.total_price_eur_cents == 7_500
        assert [item.source_id for item in bookings] == [
            "transavia",
            "sncf_connect",
        ]
        assert all(item.pricing_semantics.value == "composable" for item in bookings)
        assert len({item.booking_id for item in legs}) == 2
        assert {(item.direction, item.mode) for item in legs} == {
            (DatabaseDirection.OUTBOUND, DatabaseTransportMode.FLIGHT),
            (DatabaseDirection.RETURN, DatabaseTransportMode.TRAIN),
        }

    def test_atomic_round_trip_is_not_split_for_directional_composition(self) -> None:
        self._publish(
            (
                ScenarioAdapter(
                    source_slug="transavia",
                    mode=TransportMode.FLIGHT,
                    atomic_round_trip_price=8_500,
                ),
                ScenarioAdapter(
                    source_slug="sncf_connect",
                    mode=TransportMode.TRAIN,
                    one_way_direction=Direction.RETURN,
                    one_way_price=1_000,
                ),
            )
        )

        with self.sessions() as session:
            snapshots = session.scalars(select(FareSnapshot)).all()
            bookings = session.scalars(select(SnapshotBooking)).all()
            legs = session.scalars(select(SnapshotLeg)).all()
        assert len(snapshots) == 1
        assert snapshots[0].mode_bucket is DatabaseTransportMode.FLIGHT
        assert len(bookings) == 1
        assert bookings[0].source_id == "transavia"
        assert bookings[0].pricing_semantics.value == "round_trip_only"
        assert len(legs) == 2
        assert {item.booking_id for item in legs} == {bookings[0].id}

    def test_directional_transfer_cost_flows_through_round_trip_composition(
        self,
    ) -> None:
        with self.sessions() as session, session.begin():
            session.add(
                TerminalTransfer(
                    from_terminal_id="par-bva",
                    to_terminal_id="par-cdg",
                    minimum_minutes=75,
                    cost_eur_cents=1_800,
                    cost_is_complete=True,
                    evidence_url="https://example.invalid/verified-transfer-rule",
                )
            )

        result = self._publish(
            (
                ScenarioAdapter(
                    source_slug="transavia",
                    mode=TransportMode.FLIGHT,
                    one_way_direction=Direction.OUTBOUND,
                    one_way_price=5_800,
                    included_transfer_cost_eur_cents=1_800,
                ),
                ScenarioAdapter(
                    source_slug="sncf_connect",
                    mode=TransportMode.TRAIN,
                    one_way_direction=Direction.RETURN,
                    one_way_price=3_500,
                ),
            )
        )

        assert result.revisions_created == 1
        with self.sessions() as session:
            revision = session.scalar(select(FareSnapshotRevision))
            legs = session.scalars(
                select(SnapshotLeg).order_by(
                    SnapshotLeg.direction, SnapshotLeg.segment_order
                )
            ).all()
        assert revision is not None
        assert revision.total_price_eur_cents == 9_300
        assert len(legs) == 3
        assert legs[1].protection_state.value == "unprotected_transfer"
