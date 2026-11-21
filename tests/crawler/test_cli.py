from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import select

from crowl_core import Base, create_database_engine, create_session_factory
from crowl_core.catalog import seed_development_data
from crowl_core.models import (
    City,
    CrawlRun,
    ExtractorRun,
    FareSnapshot,
    HealthEvent,
    SnapshotBooking,
    Source,
)
from crowl_crawler.adapters.base import SourceAdapter
from crowl_crawler.adapters.registry import AdapterRegistry, build_default_registry
from crowl_crawler.cli import _production_run, main
from crowl_crawler.models import (
    AdapterMetadata,
    BookingComponent,
    ConnectorResult,
    CoverageDiscovery,
    ExtractionStrategy,
    NormalizedOffer,
    PriceCompleteness,
    PricingSemantics,
    SearchOutcome,
    SearchRequest,
    Segment,
    TransportMode,
)


ROOT = Path(__file__).resolve().parents[2]
PILOT_DATE = date(2027, 1, 1)
PILOT_WEEKEND = date(2027, 1, 8)
PILOT_ORIGIN = "par-bercy-seine-bus"
PILOT_DESTINATION_CITY = "amsterdam-nl"
PILOT_DESTINATION_TERMINAL = "ams-sloterdijk-bus"


class _PilotFlixBusAdapter(SourceAdapter):
    """Deterministic real-source-shaped adapter for the publication boundary."""

    def __init__(self, *, with_offer: bool) -> None:
        self.with_offer = with_offer
        self.requests: list[SearchRequest] = []
        self.closed = False

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            source_slug="flixbus",
            display_name="FlixBus",
            modes=(TransportMode.BUS,),
            strategy=ExtractionStrategy.PLAYWRIGHT,
            intended_strategy=ExtractionStrategy.PLAYWRIGHT,
            connector_version="activation-pilot-test-v1",
            parser_version="activation-pilot-test-v1",
            enabled=True,
            disabled_reason=None,
            access_basis="deterministic activation-pilot CLI test adapter",
        )

    async def discover_coverage(self) -> CoverageDiscovery:
        raise AssertionError("the activation pilot must not discover broad coverage")

    async def canary(
        self, requests: tuple[SearchRequest, ...]
    ) -> tuple[ConnectorResult, ...]:
        raise AssertionError("the activation pilot task must not be a canary")

    async def search(self, request: SearchRequest) -> ConnectorResult:
        self.requests.append(request)
        observed_at = datetime(2027, 1, 1, 12, tzinfo=UTC)
        if not self.with_offer:
            return ConnectorResult(
                outcome=SearchOutcome.SUCCESS_EMPTY,
                retryable=False,
                diagnostic_code="FLIXBUS_EMPTY_OK",
                expected_count=1,
                completed_count=1,
                offers=(),
                parser_version="activation-pilot-test-v1",
                observed_at=observed_at,
                response_fingerprint="a" * 64,
                schema_fingerprint="b" * 64,
            )

        assert request.return_window is not None
        outbound_departure = request.outbound_window.starts_at + timedelta(hours=1)
        outbound_arrival = outbound_departure + timedelta(hours=8)
        return_arrival = request.return_window.ends_at - timedelta(minutes=30)
        return_departure = return_arrival - timedelta(hours=8)
        outbound = Segment(
            sequence=1,
            mode=TransportMode.BUS,
            operator="FlixBus",
            service_number="FB100",
            departure_terminal_id=PILOT_ORIGIN,
            departure_terminal_name="Paris Bercy Seine Bus Station",
            arrival_terminal_id=PILOT_DESTINATION_TERMINAL,
            arrival_terminal_name="Amsterdam Sloterdijk (Piarcoplein)",
            departs_at=outbound_departure,
            arrives_at=outbound_arrival,
        )
        inbound = Segment(
            sequence=1,
            mode=TransportMode.BUS,
            operator="FlixBus",
            service_number="FB101",
            departure_terminal_id=PILOT_DESTINATION_TERMINAL,
            departure_terminal_name="Amsterdam Sloterdijk (Piarcoplein)",
            arrival_terminal_id=PILOT_ORIGIN,
            arrival_terminal_name="Paris Bercy Seine Bus Station",
            departs_at=return_departure,
            arrives_at=return_arrival,
        )
        booking = BookingComponent(
            seller="FlixBus",
            source_slug="flixbus",
            amount_minor=8_900,
            currency="EUR",
            booking_url=None,
            fare_name="Displayed fare",
            price_completeness=PriceCompleteness.ESTIMATED,
            mandatory_fees_included=False,
            passenger_age=22,
            passenger_student=True,
            passenger_eligibility_confirmed=True,
            bookable=True,
            is_teaser=False,
        )
        offer = NormalizedOffer(
            provider_offer_id="activation-pilot-offer",
            source_slug="flixbus",
            destination_city_id=PILOT_DESTINATION_CITY,
            pricing_semantics=PricingSemantics.ROUND_TRIP_ONLY,
            price_completeness=PriceCompleteness.ESTIMATED,
            total_amount_minor=8_900,
            currency="EUR",
            mandatory_fees_included=False,
            passenger_age=22,
            passenger_student=True,
            passenger_eligibility_confirmed=True,
            bookable=True,
            is_teaser=False,
            observed_at=observed_at,
            outbound_segments=(outbound,),
            return_segments=(inbound,),
            booking_components=(booking,),
        )
        return ConnectorResult(
            outcome=SearchOutcome.SUCCESS_RESULTS,
            retryable=False,
            diagnostic_code="FLIXBUS_RESULTS_OK",
            expected_count=1,
            completed_count=1,
            offers=(offer,),
            parser_version="activation-pilot-test-v1",
            observed_at=observed_at,
            response_fingerprint="a" * 64,
            schema_fingerprint="b" * 64,
        )

    async def close(self) -> None:
        self.closed = True


class CliTests(unittest.TestCase):
    def test_sources_json_lists_exactly_the_requested_sources(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(("sources", "--json"))
        rows = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(rows), 14)
        self.assertEqual(
            {row["source_slug"] for row in rows if row["enabled"] is True},
            {"flixbus"},
        )

    def test_fixture_batch_command_prints_success_report(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(("fixture-batch", "--date", "2027-01-01"))
        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["offer_count"], 4)
        self.assertGreater(report["health_event_count"], 0)

    def test_fixture_publish_requires_database_url(self) -> None:
        errors = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(errors):
            exit_code = main(("fixture-publish", "--date", "2027-01-01"))
        self.assertEqual(exit_code, 2)
        self.assertIn("requires DATABASE_URL", errors.getvalue())

    def test_activation_pilot_requires_daily_slot_acknowledgement(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
            main(_pilot_args(acknowledge=False))

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--acknowledge-daily-slot", errors.getvalue())

    def test_activation_pilot_refuses_disabled_source_without_creating_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = self._seed_pilot_database(directory, enable_flixbus=False)
            output = io.StringIO()
            errors = io.StringIO()
            with (
                patch.dict(os.environ, {"DATABASE_URL": database_url}, clear=True),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                exit_code = main(_pilot_args())

            self.assertEqual(exit_code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("FlixBus is not enabled", errors.getvalue())

            engine = create_database_engine(database_url)
            sessions = create_session_factory(engine)
            with sessions() as session:
                run = session.scalar(
                    select(CrawlRun).where(CrawlRun.is_synthetic.is_(False))
                )
                self.assertIsNone(run)
            engine.dispose()

    def test_activation_pilot_refuses_when_another_source_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = self._seed_pilot_database(directory, enable_flixbus=True)
            registry = _pilot_registry(_PilotFlixBusAdapter(with_offer=True))
            easyjet = registry.get("easyjet")
            enabled_easyjet = Mock(spec=SourceAdapter)
            enabled_easyjet.metadata.return_value = replace(
                easyjet.metadata(),
                strategy=easyjet.metadata().intended_strategy,
                enabled=True,
                disabled_reason=None,
            )
            registry.register(enabled_easyjet, replace=True)
            output = io.StringIO()
            errors = io.StringIO()
            with (
                patch.dict(os.environ, {"DATABASE_URL": database_url}, clear=True),
                patch(
                    "crowl_crawler.cli.build_default_registry",
                    return_value=registry,
                ),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                exit_code = main(_pilot_args())

            self.assertEqual(exit_code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("registry must enable exactly FlixBus", errors.getvalue())

            engine = create_database_engine(database_url)
            sessions = create_session_factory(engine)
            with sessions() as session:
                run = session.scalar(
                    select(CrawlRun).where(CrawlRun.is_synthetic.is_(False))
                )
                self.assertIsNone(run)
            engine.dispose()

    def test_activation_pilot_cancellation_abandons_prepared_run_and_reraises(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = self._seed_pilot_database(directory, enable_flixbus=True)
            registry = _pilot_registry(_PilotFlixBusAdapter(with_offer=True))
            with (
                patch.dict(os.environ, {"DATABASE_URL": database_url}, clear=True),
                patch(
                    "crowl_crawler.cli.build_default_registry",
                    return_value=registry,
                ),
                patch(
                    "crowl_crawler.cli.BatchCoordinator.run",
                    new=AsyncMock(side_effect=asyncio.CancelledError()),
                ),
                self.assertRaises(asyncio.CancelledError),
            ):
                main(_pilot_args())

            engine = create_database_engine(database_url)
            sessions = create_session_factory(engine)
            with sessions() as session:
                run = session.scalar(
                    select(CrawlRun).where(CrawlRun.is_synthetic.is_(False))
                )
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run.status.value, "abandoned")
                self.assertIsNone(run.published_at)
                self.assertEqual(run.diagnostic, "PILOT_PROCESS_INTERRUPTED")
                self.assertEqual(
                    session.scalars(
                        select(FareSnapshot).where(FareSnapshot.is_synthetic.is_(False))
                    ).all(),
                    [],
                )
            engine.dispose()

    def test_activation_pilot_no_offer_does_not_publish_and_consumes_slot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = self._seed_pilot_database(directory, enable_flixbus=True)
            adapter = _PilotFlixBusAdapter(with_offer=False)
            registry = _pilot_registry(adapter)
            output = io.StringIO()
            errors = io.StringIO()
            with (
                patch.dict(os.environ, {"DATABASE_URL": database_url}, clear=True),
                patch(
                    "crowl_crawler.cli.build_default_registry",
                    return_value=registry,
                ),
                patch("crowl_crawler.cli.DatabasePublisher.publish") as publish,
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                exit_code = main(_pilot_args())

            self.assertEqual(exit_code, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("nothing was published", errors.getvalue())
            publish.assert_not_called()
            self.assertTrue(adapter.closed)
            self.assertEqual(len(adapter.requests), 1)

            engine = create_database_engine(database_url)
            sessions = create_session_factory(engine)
            with sessions() as session:
                run = session.scalar(
                    select(CrawlRun).where(CrawlRun.is_synthetic.is_(False))
                )
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run.status.value, "failed")
                self.assertIsNone(run.published_at)
                self.assertEqual(run.diagnostic, "PILOT_NO_PUBLISHABLE_OFFER")
                extractors = {
                    row.source_id: row
                    for row in session.scalars(
                        select(ExtractorRun).where(ExtractorRun.crawl_run_id == run.id)
                    ).all()
                }
                self.assertEqual(len(extractors), 14)
                self.assertEqual(extractors["flixbus"].status.value, "succeeded")
                self.assertEqual(extractors["flixbus"].diagnostic, "SOURCE_OK")
                self.assertEqual(extractors["flixbus"].expected_searches, 1)
                self.assertEqual(extractors["flixbus"].attempted_searches, 1)
                self.assertEqual(extractors["flixbus"].successful_searches, 1)
                self.assertEqual(extractors["flixbus"].empty_searches, 1)
                self.assertEqual(extractors["flixbus"].result_searches, 0)
                self.assertEqual(
                    {
                        row.status.value
                        for slug, row in extractors.items()
                        if slug != "flixbus"
                    },
                    {"disabled"},
                )
                self.assertEqual(
                    {
                        row.diagnostic
                        for slug, row in extractors.items()
                        if slug != "flixbus"
                    },
                    {"SOURCE_DISABLED"},
                )
                events = session.scalars(
                    select(HealthEvent).where(HealthEvent.crawl_run_id == run.id)
                ).all()
                event_types = [event.event_type for event in events]
                self.assertEqual(event_types.count("batch_started"), 1)
                self.assertEqual(event_types.count("batch_finished"), 1)
                self.assertEqual(event_types.count("extractor_started"), 14)
                self.assertEqual(event_types.count("extractor_finished"), 14)
                self.assertEqual(event_types.count("batch_heartbeat"), 14)
                self.assertEqual(event_types.count("search_finished"), 1)
                self.assertIn(
                    "outcome=success_empty",
                    " ".join(event.diagnostic for event in events),
                )
                self.assertEqual(
                    session.scalars(
                        select(FareSnapshot).where(FareSnapshot.is_synthetic.is_(False))
                    ).all(),
                    [],
                )
            engine.dispose()

            second_errors = io.StringIO()
            with (
                patch.dict(os.environ, {"DATABASE_URL": database_url}, clear=True),
                patch(
                    "crowl_crawler.cli.build_default_registry",
                    return_value=registry,
                ),
                patch("crowl_crawler.cli.DatabasePublisher.publish") as publish,
                redirect_stderr(second_errors),
            ):
                second_exit_code = main(_pilot_args())

            self.assertEqual(second_exit_code, 2)
            self.assertIn("already exists", second_errors.getvalue())
            publish.assert_not_called()

    def test_activation_pilot_publishes_one_atomic_round_trip_cell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = self._seed_pilot_database(directory, enable_flixbus=True)
            adapter = _PilotFlixBusAdapter(with_offer=True)
            registry = _pilot_registry(adapter)
            output = io.StringIO()
            errors = io.StringIO()
            with (
                patch.dict(os.environ, {"DATABASE_URL": database_url}, clear=True),
                patch(
                    "crowl_crawler.cli.build_default_registry",
                    return_value=registry,
                ),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                exit_code = main(_pilot_args(pretty=True))

            self.assertEqual(exit_code, 0, errors.getvalue())
            self.assertEqual(errors.getvalue(), "")
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["kind"], "activation_pilot_publish")
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(payload["offer_count"], 1)
            self.assertEqual(payload["scope"]["source"], "flixbus")
            self.assertEqual(payload["scope"]["search_kind"], "round_trip")
            self.assertEqual(payload["publication"]["snapshots_created"], 1)
            self.assertEqual(len(payload["sources"]), 14)
            flixbus = next(
                item for item in payload["sources"] if item["source_slug"] == "flixbus"
            )
            self.assertEqual(flixbus["expected_tasks"], 1)
            self.assertEqual(flixbus["result_bearing_tasks"], 1)
            self.assertTrue(
                all(
                    item["expected_tasks"] == 0
                    for item in payload["sources"]
                    if item["source_slug"] != "flixbus"
                )
            )
            self.assertTrue(adapter.closed)
            self.assertEqual(len(adapter.requests), 1)
            request = adapter.requests[0]
            self.assertEqual(request.origin_terminal_ids, (PILOT_ORIGIN,))
            self.assertEqual(
                request.destination_terminal_ids,
                (PILOT_DESTINATION_TERMINAL,),
            )
            self.assertEqual(request.weekend_start, PILOT_WEEKEND)

            engine = create_database_engine(database_url)
            sessions = create_session_factory(engine)
            with sessions() as session:
                run = session.scalar(
                    select(CrawlRun).where(CrawlRun.is_synthetic.is_(False))
                )
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run.status.value, "succeeded")
                self.assertIsNotNone(run.published_at)
                # The immutable manifest has one round-trip cell/task. The SQL
                # coverage ledger deliberately expands it into two directions.
                self.assertEqual(run.expected_cells, 2)
                self.assertEqual(run.completed_cells, 2)
                snapshots = session.scalars(
                    select(FareSnapshot).where(FareSnapshot.is_synthetic.is_(False))
                ).all()
                self.assertEqual(len(snapshots), 1)
                self.assertEqual(
                    snapshots[0].destination_city_id,
                    PILOT_DESTINATION_CITY,
                )
                booking = session.scalar(select(SnapshotBooking))
                self.assertIsNotNone(booking)
                assert booking is not None
                self.assertEqual(booking.source_id, "flixbus")
                self.assertEqual(booking.price_completeness.value, "estimated")
                self.assertFalse(booking.mandatory_fees_included)
                self.assertIsNone(booking.booking_url)
            engine.dispose()

    def test_production_run_requires_database_url(self) -> None:
        errors = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(errors):
            exit_code = main(("run", "--date", "2027-01-01"))
        self.assertEqual(exit_code, 2)
        self.assertIn("run requires DATABASE_URL", errors.getvalue())

    def test_production_run_publishes_disabled_state_without_fixture_fares(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "crowl.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            engine = create_database_engine(database_url)
            Base.metadata.create_all(engine)
            sessions = create_session_factory(engine)
            with sessions() as session:
                seed_development_data(
                    session,
                    data_dir=ROOT / "data",
                    as_of=date(2027, 1, 1),
                    include_fixture_fares=True,
                )
                for city in session.scalars(select(City)).all():
                    city.active = city.id == "lisbon-pt"
                session.commit()
            engine.dispose()

            default_registry = build_default_registry(include_fixture=False)
            registry = AdapterRegistry((default_registry.get("ryanair"),))
            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "DATABASE_URL": database_url,
                        "CROWL_BATCH_DEADLINE_HOURS": "1",
                    },
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                exit_code = asyncio.run(
                    _production_run(
                        scheduled_for=date(2027, 1, 1),
                        pretty=False,
                        registry=registry,
                        fx_syncer=None,
                    )
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["sources"][0]["state"], "disabled")
            self.assertEqual(payload["sources"][0]["expected_tasks"], 0)
            self.assertEqual(payload["publication"]["snapshots_created"], 0)
            self.assertEqual(payload["fx_sync"]["status"], "skipped")
            self.assertNotIn("executions", payload["sources"][0])

            engine = create_database_engine(database_url)
            sessions = create_session_factory(engine)
            with sessions() as session:
                extractor = session.scalar(
                    select(ExtractorRun)
                    .join(CrawlRun, ExtractorRun.crawl_run_id == CrawlRun.id)
                    .where(
                        ExtractorRun.source_id == "ryanair",
                        CrawlRun.is_synthetic.is_(False),
                    )
                )
                self.assertIsNotNone(extractor)
                assert extractor is not None
                self.assertEqual(extractor.status.value, "disabled")
                self.assertEqual(extractor.attempted_searches, 0)
                self.assertEqual(
                    session.get(Source, "ryanair").strategy,
                    "playwright",
                )
                same_day_runs = session.scalars(
                    select(CrawlRun).where(CrawlRun.scheduled_for == date(2027, 1, 1))
                ).all()
                self.assertEqual(len(same_day_runs), 2)
                self.assertEqual(
                    {item.is_synthetic for item in same_day_runs},
                    {False, True},
                )
            engine.dispose()

    def test_unmapped_editorial_cities_do_not_abort_mapped_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "crowl.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            engine = create_database_engine(database_url)
            Base.metadata.create_all(engine)
            sessions = create_session_factory(engine)
            with sessions() as session:
                seed_development_data(
                    session,
                    data_dir=ROOT / "data",
                    as_of=date(2027, 1, 1),
                    include_fixture_fares=False,
                )
                for city in session.scalars(select(City)).all():
                    city.active = city.id in {"lisbon-pt", "athens-gr"}
                session.commit()
            engine.dispose()

            default_registry = build_default_registry(include_fixture=False)
            registry = AdapterRegistry((default_registry.get("ryanair"),))
            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "DATABASE_URL": database_url,
                        "CROWL_BATCH_DEADLINE_HOURS": "1",
                    },
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                exit_code = asyncio.run(
                    _production_run(
                        scheduled_for=date(2027, 1, 1),
                        pretty=False,
                        registry=registry,
                        fx_syncer=None,
                    )
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(payload["publication"]["snapshots_created"], 0)

    @staticmethod
    def _seed_pilot_database(directory: str, *, enable_flixbus: bool) -> str:
        database_path = Path(directory) / "crowl.sqlite3"
        database_url = f"sqlite+pysqlite:///{database_path}"
        engine = create_database_engine(database_url)
        Base.metadata.create_all(engine)
        sessions = create_session_factory(engine)
        with sessions() as session:
            seed_development_data(
                session,
                data_dir=ROOT / "data",
                as_of=PILOT_DATE,
                include_fixture_fares=False,
            )
            source = session.get(Source, "flixbus")
            assert source is not None
            source.enabled = enable_flixbus
            source.disabled_reason = None if enable_flixbus else source.disabled_reason
            session.commit()
        engine.dispose()
        return database_url


def _pilot_registry(adapter: SourceAdapter) -> AdapterRegistry:
    registry = build_default_registry(include_fixture=False)
    registry.register(adapter, replace=True)
    return registry


def _pilot_args(*, acknowledge: bool = True, pretty: bool = False) -> tuple[str, ...]:
    args = [
        "activation-pilot-publish",
        "--source",
        "flixbus",
        "--origin-terminal",
        PILOT_ORIGIN,
        "--destination-city",
        PILOT_DESTINATION_CITY,
        "--destination-terminal",
        PILOT_DESTINATION_TERMINAL,
        "--weekend",
        PILOT_WEEKEND.isoformat(),
        "--date",
        PILOT_DATE.isoformat(),
    ]
    if acknowledge:
        args.append("--acknowledge-daily-slot")
    if pretty:
        args.append("--pretty")
    return tuple(args)


if __name__ == "__main__":
    unittest.main()
