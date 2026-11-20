from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from crowl_core import Base, create_database_engine, create_session_factory
from crowl_core.catalog import (
    ensure_default_profile_and_rules,
    seed_development_data,
    seed_fixture_history,
)
from crowl_core.enums import TransportMode as DatabaseTransportMode
from crowl_core.models import (
    ConnectorRequest,
    CrawlCoverageCell,
    CrawlRun,
    DurationHistogramDaily,
    ExtractorRun,
    FareCellObservation,
    FareSnapshot,
    FareSnapshotRevision,
    FxRate,
    HealthEvent,
    SnapshotBooking,
    SnapshotLeg,
    Source,
    TerminalTransfer,
    TravelWindow,
)

from crowl_crawler.adapters.fixture import FixtureAdapter
from crowl_crawler.adapters.registry import AdapterRegistry, build_default_registry
from crowl_crawler.batch import BatchCoordinator
from crowl_crawler.health import InMemoryHealthSink
from crowl_crawler.manifest import DailyManifestBuilder, FIXTURE_DESTINATIONS
from crowl_crawler.persistence import DatabasePublisher, PublicationError
from crowl_crawler.models import (
    AdapterMetadata,
    BatchStatus,
    ExtractionStrategy,
    ExtractorRunStatus,
    SearchOutcome,
    TransportMode,
)


ROOT = Path(__file__).resolve().parents[2]
RUN_DATE = date(2027, 1, 1)
WEEKEND = date(2027, 1, 8)
OBSERVED_AT = datetime(2027, 1, 1, 2, 0, tzinfo=UTC)


class AuthorizedProviderFixtureAdapter(FixtureAdapter):
    """Exercises the production contract without making a network request."""

    def __init__(self) -> None:
        super().__init__(clock=lambda: OBSERVED_AT)
        self._metadata = AdapterMetadata(
            source_slug="transavia",
            display_name="Transavia test feed",
            modes=(TransportMode.FLIGHT,),
            strategy=ExtractionStrategy.HTTP_STRUCTURED,
            intended_strategy=ExtractionStrategy.HTTP_STRUCTURED,
            connector_version="test-connector-v1",
            parser_version="test-parser-v1",
            enabled=True,
            disabled_reason=None,
            access_basis="test-only authorized feed contract",
        )

    async def search(self, request):  # type: ignore[no-untyped-def]
        result = await super().search(request)
        offers = []
        for offer in result.offers:
            components = tuple(
                replace(
                    component,
                    seller="Transavia test feed",
                    source_slug="transavia",
                    booking_url=(
                        "https://www.transavia.com/search?"
                        "utm_source=test&sessionToken=secret"
                    ),
                )
                for component in offer.booking_components
            )
            offers.append(
                replace(
                    offer,
                    provider_offer_id=offer.provider_offer_id.replace(
                        "fixture", "transavia"
                    ),
                    source_slug="transavia",
                    booking_components=components,
                )
            )
        return replace(result, offers=tuple(offers), parser_version="test-parser-v1")


class DatabasePublisherTests(unittest.TestCase):
    def setUp(self) -> None:
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

    def tearDown(self) -> None:
        self.engine.dispose()

    def _manifest(self, *, scheduled_for: date = RUN_DATE):  # type: ignore[no-untyped-def]
        return DailyManifestBuilder(
            origin_terminal_ids=("par-cdg", "par-ory", "par-gare-du-nord"),
            config_version="fixture-v1",
        ).build(
            scheduled_for=scheduled_for,
            source_slugs=("fixture",),
            destinations=(
                FIXTURE_DESTINATIONS["amsterdam-nl"],
                FIXTURE_DESTINATIONS["lisbon-pt"],
            ),
            weekend_fridays=(WEEKEND,),
            created_at=OBSERVED_AT,
        )

    def _collect(self, manifest):  # type: ignore[no-untyped-def]
        sink = InMemoryHealthSink()
        adapter = FixtureAdapter(clock=lambda: OBSERVED_AT)
        report = asyncio.run(
            BatchCoordinator(
                AdapterRegistry((adapter,)),
                health_sink=sink,
                clock=lambda: OBSERVED_AT,
            ).run(manifest)
        )
        return report, sink.events

    def _report_with_outcome(
        self,
        report,
        outcome: SearchOutcome,
        *,
        observed_at: datetime = OBSERVED_AT,
    ):  # type: ignore[no-untyped-def]
        successful = outcome in {
            SearchOutcome.SUCCESS_EMPTY,
            SearchOutcome.NOT_ON_SALE,
            SearchOutcome.NO_SERVICE,
            SearchOutcome.SOLD_OUT,
            SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE,
        }
        partial = outcome is SearchOutcome.PARTIAL_SUCCESS
        source_reports = []
        for source_report in report.source_reports:
            executions = tuple(
                replace(
                    execution,
                    result=replace(
                        execution.result,
                        outcome=outcome,
                        retryable=outcome is SearchOutcome.UPSTREAM_ERROR,
                        diagnostic_code=f"TEST_{outcome.value.upper()}",
                        completed_count=1 if successful or partial else 0,
                        offers=(),
                        observed_at=observed_at,
                    ),
                )
                for execution in source_report.executions
            )
            source_reports.append(
                replace(
                    source_report,
                    state=(
                        ExtractorRunStatus.SUCCEEDED
                        if successful
                        else ExtractorRunStatus.PARTIAL
                        if partial
                        else ExtractorRunStatus.FAILED
                    ),
                    completed_tasks=len(executions) if successful or partial else 0,
                    result_bearing_tasks=0,
                    outcome_counts={outcome.value: len(executions)},
                    diagnostic_code=(
                        "SOURCE_OK"
                        if successful
                        else "SOURCE_PARTIAL"
                        if partial
                        else "SOURCE_FAILED"
                    ),
                    executions=executions,
                )
            )
        return replace(
            report,
            status=(
                BatchStatus.SUCCEEDED
                if successful
                else BatchStatus.PARTIAL
                if partial
                else BatchStatus.FAILED
            ),
            started_at=observed_at,
            completed_at=observed_at,
            source_reports=tuple(source_reports),
        )

    def _provider_manifest(self):  # type: ignore[no-untyped-def]
        return DailyManifestBuilder(
            origin_terminal_ids=("par-cdg", "par-ory"),
            config_version="production-test-v1",
        ).build(
            scheduled_for=RUN_DATE,
            source_slugs=("transavia",),
            destinations=(FIXTURE_DESTINATIONS["lisbon-pt"],),
            weekend_fridays=(WEEKEND,),
            created_at=OBSERVED_AT,
        )

    def _collect_provider(self, manifest):  # type: ignore[no-untyped-def]
        sink = InMemoryHealthSink()
        adapter = AuthorizedProviderFixtureAdapter()
        report = asyncio.run(
            BatchCoordinator(
                AdapterRegistry((adapter,)),
                health_sink=sink,
                clock=lambda: OBSERVED_AT,
            ).run(manifest)
        )
        return adapter, report, sink.events

    def _count(self, model) -> int:  # type: ignore[no-untyped-def]
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def _with_unprotected_airport_transfer(
        self,
        report,
        *,
        included_transfer_cost_eur_cents: int,
    ):  # type: ignore[no-untyped-def]
        source_report = report.source_reports[0]
        execution = source_report.executions[0]
        offer = execution.result.offers[0]
        original = offer.outbound_segments[0]
        first = replace(
            original,
            sequence=1,
            arrival_terminal_id="par-bva",
            arrival_terminal_name="Paris Beauvais Airport",
            arrives_at=original.departs_at + timedelta(minutes=30),
        )
        second = replace(
            original,
            sequence=2,
            departure_terminal_id="par-cdg",
            departure_terminal_name="Paris Charles de Gaulle Airport",
            departs_at=original.departs_at + timedelta(hours=2),
            arrives_at=original.arrives_at + timedelta(hours=2),
            protected_connection=False,
        )
        increase = included_transfer_cost_eur_cents
        changed = replace(
            offer,
            provider_offer_id=f"{offer.provider_offer_id}-unprotected",
            total_amount_minor=offer.total_amount_minor + increase,
            outbound_segments=(first, second),
            booking_components=(
                replace(
                    offer.booking_components[0],
                    amount_minor=offer.booking_components[0].amount_minor + increase,
                ),
            ),
            included_transfer_cost_eur_cents=included_transfer_cost_eur_cents,
        )
        return replace(
            report,
            source_reports=(
                replace(
                    source_report,
                    executions=(
                        replace(
                            execution,
                            result=replace(execution.result, offers=(changed,)),
                        ),
                    ),
                ),
            ),
        )

    def test_publish_maps_complete_batch_into_shared_schema(self) -> None:
        manifest = self._manifest()
        report, events = self._collect(manifest)

        result = DatabasePublisher(self.sessions).publish(
            manifest, report, health_events=events
        )

        self.assertEqual(result.snapshots_created, 2)
        self.assertEqual(result.revisions_created, 2)
        self.assertEqual(result.snapshots_unchanged, 0)
        self.assertEqual(result.coverage_cells_written, 4)
        self.assertEqual(result.observations_written, 8)
        self.assertEqual(result.health_events_written, len(events))
        self.assertEqual(self._count(CrawlRun), 1)
        self.assertEqual(self._count(ExtractorRun), 1)
        self.assertEqual(self._count(CrawlCoverageCell), 4)
        self.assertEqual(self._count(ConnectorRequest), 2)
        self.assertEqual(self._count(FareSnapshot), 2)
        self.assertEqual(self._count(FareSnapshotRevision), 2)
        self.assertEqual(self._count(FareCellObservation), 8)
        self.assertEqual(self._count(SnapshotBooking), 2)
        self.assertEqual(self._count(SnapshotLeg), 4)
        self.assertEqual(self._count(HealthEvent), len(events))
        self.assertEqual(self._count(DurationHistogramDaily), 2)

        with self.sessions() as session:
            source = session.get(Source, "fixture")
            self.assertIsNotNone(source)
            assert source is not None
            self.assertFalse(source.is_requested)
            self.assertEqual(source.strategy, "fixture")
            self.assertNotIn("Ryanair", source.name)
            snapshots = session.scalars(select(FareSnapshot)).all()
            self.assertTrue(all(item.current_revision_id for item in snapshots))
            observations = session.scalars(select(FareCellObservation)).all()
            self.assertEqual(
                {
                    (item.destination_city_id, item.mode_bucket.value)
                    for item in observations
                },
                {
                    (city_id, mode)
                    for city_id in ("amsterdam-nl", "lisbon-pt")
                    for mode in ("flight", "train", "bus", "mixed")
                },
            )
            histograms = session.scalars(
                select(DurationHistogramDaily).order_by(
                    DurationHistogramDaily.city_id,
                    DurationHistogramDaily.mode,
                )
            ).all()
            self.assertEqual(
                [
                    (item.city_id, item.mode.value, item.sample_count, item.bins)
                    for item in histograms
                ],
                [
                    ("amsterdam-nl", "train", 2, {"210": 2}),
                    ("lisbon-pt", "flight", 2, {"150": 2}),
                ],
            )

    def test_same_day_rerun_reuses_cells_and_unchanged_revisions(self) -> None:
        manifest = self._manifest()
        first_report, first_events = self._collect(manifest)
        publisher = DatabasePublisher(self.sessions)
        publisher.publish(manifest, first_report, health_events=first_events)

        second_report, second_events = self._collect(manifest)
        result = publisher.publish(manifest, second_report, health_events=second_events)

        self.assertEqual(result.snapshots_created, 0)
        self.assertEqual(result.revisions_created, 0)
        self.assertEqual(result.snapshots_unchanged, 2)
        self.assertEqual(self._count(CrawlRun), 1)
        self.assertEqual(self._count(ExtractorRun), 1)
        self.assertEqual(self._count(CrawlCoverageCell), 4)
        self.assertEqual(self._count(ConnectorRequest), 2)
        self.assertEqual(self._count(FareSnapshot), 2)
        self.assertEqual(self._count(FareSnapshotRevision), 2)
        self.assertEqual(self._count(SnapshotBooking), 2)
        self.assertEqual(self._count(SnapshotLeg), 4)
        self.assertEqual(self._count(DurationHistogramDaily), 2)
        with self.sessions() as session:
            self.assertTrue(
                all(
                    item.sample_count == 2
                    for item in session.scalars(select(DurationHistogramDaily))
                )
            )

    def test_completed_empty_batches_publish_all_mode_availability_states(self) -> None:
        expected = {
            SearchOutcome.SUCCESS_EMPTY: "no_valid_option",
            SearchOutcome.NOT_ON_SALE: "not_on_sale",
            SearchOutcome.SOLD_OUT: "sold_out",
        }
        for outcome, availability in expected.items():
            with self.subTest(outcome=outcome.value):
                # Each subtest uses a fresh logical run in the same transaction
                # date, so clear only overlay/ledger data while preserving the
                # authoritative catalog.
                with self.sessions() as session, session.begin():
                    for model in (
                        FareCellObservation,
                        ConnectorRequest,
                        CrawlCoverageCell,
                        ExtractorRun,
                        HealthEvent,
                        CrawlRun,
                    ):
                        session.query(model).delete()

                manifest = self._manifest()
                report, events = self._collect(manifest)
                result = DatabasePublisher(self.sessions).publish(
                    manifest,
                    self._report_with_outcome(report, outcome),
                    health_events=events,
                )

                self.assertEqual(result.snapshots_created, 0)
                self.assertEqual(result.observations_written, 8)
                with self.sessions() as session:
                    observations = session.scalars(
                        select(FareCellObservation)
                    ).all()
                self.assertEqual(len(observations), 8)
                self.assertTrue(
                    all(item.availability.value == availability for item in observations)
                )
                self.assertTrue(
                    all(
                        item.execution_coverage.value == "complete"
                        for item in observations
                    )
                )
                self.assertTrue(
                    all(item.trusted_revision_id is None for item in observations)
                )

    def test_partial_search_result_is_not_misreported_as_a_definitive_empty(self) -> None:
        manifest = self._manifest()
        report, events = self._collect(manifest)
        result = DatabasePublisher(self.sessions).publish(
            manifest,
            self._report_with_outcome(report, SearchOutcome.PARTIAL_SUCCESS),
            health_events=events,
        )

        self.assertEqual(result.observations_written, 8)
        with self.sessions() as session:
            observations = session.scalars(select(FareCellObservation)).all()
        self.assertTrue(
            all(item.availability.value == "unknown_due_failure" for item in observations)
        )
        self.assertTrue(
            all(item.execution_coverage.value == "partial" for item in observations)
        )

    def test_failed_new_batch_retains_old_fare_without_refreshing_it(self) -> None:
        first_manifest = self._manifest()
        first_report, first_events = self._collect(first_manifest)
        publisher = DatabasePublisher(self.sessions)
        publisher.publish(first_manifest, first_report, health_events=first_events)

        with self.sessions() as session:
            old_snapshot = session.scalar(
                select(FareSnapshot).where(
                    FareSnapshot.destination_city_id == "amsterdam-nl",
                )
            )
            assert old_snapshot is not None and old_snapshot.current_revision_id
            old_revision = session.get(
                FareSnapshotRevision, old_snapshot.current_revision_id
            )
            assert old_revision is not None
            old_revision_id = old_revision.id
            old_price = old_revision.total_price_eur_cents
            old_observed_at = old_revision.observed_at

        second_observed_at = OBSERVED_AT + timedelta(days=1)
        second_manifest = self._manifest(
            scheduled_for=RUN_DATE + timedelta(days=1)
        )
        second_report, second_events = self._collect(second_manifest)
        failed_report = self._report_with_outcome(
            second_report,
            SearchOutcome.UPSTREAM_ERROR,
            observed_at=second_observed_at,
        )
        result = publisher.publish(
            second_manifest,
            failed_report,
            health_events=second_events,
        )

        self.assertEqual(result.revisions_created, 0)
        self.assertEqual(result.observations_written, 8)
        self.assertEqual(self._count(FareSnapshotRevision), 2)
        with self.sessions() as session:
            second_run = session.scalar(
                select(CrawlRun).where(
                    CrawlRun.scheduled_for == RUN_DATE + timedelta(days=1)
                )
            )
            assert second_run is not None
            observations = session.scalars(
                select(FareCellObservation).where(
                    FareCellObservation.crawl_run_id == second_run.id
                )
            ).all()
            retained = next(
                item
                for item in observations
                if item.destination_city_id == "amsterdam-nl"
                and item.mode_bucket.value == "train"
            )
            revision = session.get(
                FareSnapshotRevision, retained.trusted_revision_id
            )

        self.assertEqual(len(observations), 8)
        self.assertTrue(
            all(item.availability.value == "unknown_due_failure" for item in observations)
        )
        self.assertTrue(
            all(item.execution_coverage.value == "failed" for item in observations)
        )
        self.assertEqual(retained.trusted_revision_id, old_revision_id)
        assert revision is not None
        self.assertEqual(revision.total_price_eur_cents, old_price)
        self.assertEqual(revision.observed_at, old_observed_at)

    def test_extractor_failure_streak_uses_latest_earlier_daily_run(self) -> None:
        publisher = DatabasePublisher(self.sessions)
        for offset in (0, 1):
            observed_at = OBSERVED_AT + timedelta(days=offset)
            manifest = self._manifest(
                scheduled_for=RUN_DATE + timedelta(days=offset)
            )
            report, events = self._collect(manifest)
            publisher.publish(
                manifest,
                self._report_with_outcome(
                    report,
                    SearchOutcome.UPSTREAM_ERROR,
                    observed_at=observed_at,
                ),
                health_events=events,
            )

        success_manifest = self._manifest(
            scheduled_for=RUN_DATE + timedelta(days=2)
        )
        success_report, success_events = self._collect(success_manifest)
        publisher.publish(
            success_manifest,
            success_report,
            health_events=success_events,
        )

        with self.sessions() as session:
            rows = session.execute(
                select(CrawlRun.scheduled_for, ExtractorRun.consecutive_failures)
                .join(ExtractorRun, ExtractorRun.crawl_run_id == CrawlRun.id)
                .order_by(CrawlRun.scheduled_for)
            ).all()
        self.assertEqual(
            rows,
            [
                (RUN_DATE, 1),
                (RUN_DATE + timedelta(days=1), 2),
                (RUN_DATE + timedelta(days=2), 0),
            ],
        )

    def test_only_cheapest_valid_offer_is_published_for_a_daily_cell(self) -> None:
        manifest = self._manifest()
        report, events = self._collect(manifest)
        source_report = report.source_reports[0]
        execution = source_report.executions[0]
        cheapest = execution.result.offers[0]
        expensive_component = replace(
            cheapest.booking_components[0],
            amount_minor=cheapest.total_amount_minor + 5_000,
        )
        expensive = replace(
            cheapest,
            provider_offer_id=cheapest.provider_offer_id + "-expensive",
            total_amount_minor=cheapest.total_amount_minor + 5_000,
            booking_components=(expensive_component,),
        )
        result_with_alternative = replace(
            execution.result,
            offers=(expensive, cheapest),
        )
        execution_with_alternative = replace(
            execution,
            result=result_with_alternative,
        )
        source_with_alternative = replace(
            source_report,
            executions=(execution_with_alternative, *source_report.executions[1:]),
        )
        report_with_alternative = replace(
            report,
            source_reports=(source_with_alternative,),
        )

        DatabasePublisher(self.sessions).publish(
            manifest,
            report_with_alternative,
            health_events=events,
        )

        with self.sessions() as session:
            prices = session.scalars(
                select(FareSnapshotRevision.total_price_eur_cents)
            ).all()
        self.assertIn(cheapest.total_amount_minor, prices)
        self.assertNotIn(expensive.total_amount_minor, prices)
        self.assertEqual(len(prices), 2)

    def test_configured_non_fixture_round_trip_is_published_with_honest_scope(
        self,
    ) -> None:
        manifest = self._provider_manifest()
        adapter, report, events = self._collect_provider(manifest)

        result = DatabasePublisher(self.sessions).publish(
            manifest,
            report,
            health_events=events,
            source_metadata=(adapter.metadata(),),
        )

        self.assertEqual(result.snapshots_created, 1)
        self.assertEqual(result.revisions_created, 1)
        with self.sessions() as session:
            source = session.get(Source, "transavia")
            self.assertIsNotNone(source)
            assert source is not None
            self.assertTrue(source.enabled)
            self.assertEqual(source.connector_version, "test-connector-v1")
            revision = session.scalar(select(FareSnapshotRevision))
            self.assertIsNotNone(revision)
            assert revision is not None
            self.assertEqual(revision.source_scope_ratio, 1.0)
            self.assertEqual(revision.execution_coverage.value, "complete")
            booking = session.scalar(select(SnapshotBooking))
            self.assertIsNotNone(booking)
            assert booking is not None
            self.assertEqual(booking.source_id, "transavia")

    def test_complete_persisted_transfer_rule_and_cost_evidence_are_published(
        self,
    ) -> None:
        manifest = self._provider_manifest()
        adapter, report, events = self._collect_provider(manifest)
        transfer_cost = 1_800
        with self.sessions() as session, session.begin():
            session.add(
                TerminalTransfer(
                    from_terminal_id="par-bva",
                    to_terminal_id="par-cdg",
                    minimum_minutes=75,
                    cost_eur_cents=transfer_cost,
                    cost_is_complete=True,
                    evidence_url="https://example.invalid/verified-transfer-rule",
                )
            )
        changed_report = self._with_unprotected_airport_transfer(
            report,
            included_transfer_cost_eur_cents=transfer_cost,
        )

        result = DatabasePublisher(self.sessions).publish(
            manifest,
            changed_report,
            health_events=events,
            source_metadata=(adapter.metadata(),),
        )

        self.assertEqual(result.revisions_created, 1)
        with self.sessions() as session:
            revision = session.scalar(select(FareSnapshotRevision))
            legs = session.scalars(
                select(SnapshotLeg).order_by(
                    SnapshotLeg.direction, SnapshotLeg.segment_order
                )
            ).all()
        assert revision is not None
        original_offer = report.source_reports[0].executions[0].result.offers[0]
        self.assertEqual(
            revision.total_price_eur_cents,
            original_offer.total_amount_minor + transfer_cost,
        )
        self.assertEqual(len(legs), 3)
        self.assertEqual(legs[1].protection_state.value, "unprotected_transfer")

    def test_unprotected_transfer_without_cost_evidence_is_rejected(self) -> None:
        manifest = self._provider_manifest()
        adapter, report, events = self._collect_provider(manifest)
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
        changed_report = self._with_unprotected_airport_transfer(
            report,
            included_transfer_cost_eur_cents=0,
        )

        with self.assertRaisesRegex(PublicationError, "transfer_cost_not_included"):
            DatabasePublisher(self.sessions).publish(
                manifest,
                changed_report,
                health_events=events,
                source_metadata=(adapter.metadata(),),
            )

        self.assertEqual(self._count(FareSnapshotRevision), 0)

    def test_same_day_worse_provider_rerun_keeps_lowest_observed_fare(self) -> None:
        manifest = self._provider_manifest()
        first_adapter, first_report, first_events = self._collect_provider(manifest)
        publisher = DatabasePublisher(self.sessions)
        publisher.publish(
            manifest,
            first_report,
            health_events=first_events,
            source_metadata=(first_adapter.metadata(),),
        )
        first_offer = first_report.source_reports[0].executions[0].result.offers[0]

        second_adapter, second_report, second_events = self._collect_provider(manifest)
        source_report = second_report.source_reports[0]
        execution = source_report.executions[0]
        offer = execution.result.offers[0]
        increase = 5_000
        expensive = replace(
            offer,
            provider_offer_id=offer.provider_offer_id + "-worse",
            total_amount_minor=offer.total_amount_minor + increase,
            booking_components=(
                replace(
                    offer.booking_components[0],
                    amount_minor=offer.booking_components[0].amount_minor + increase,
                ),
            ),
        )
        worse_report = replace(
            second_report,
            source_reports=(
                replace(
                    source_report,
                    executions=(
                        replace(
                            execution,
                            result=replace(execution.result, offers=(expensive,)),
                        ),
                    ),
                ),
            ),
        )

        result = publisher.publish(
            manifest,
            worse_report,
            health_events=second_events,
            source_metadata=(second_adapter.metadata(),),
        )

        self.assertEqual(result.revisions_created, 0)
        self.assertEqual(result.snapshots_unchanged, 1)
        with self.sessions() as session:
            revisions = session.scalars(select(FareSnapshotRevision)).all()
            self.assertEqual(len(revisions), 1)
            self.assertEqual(
                revisions[0].total_price_eur_cents,
                first_offer.total_amount_minor,
            )

    def test_non_eur_offer_uses_persisted_rate_and_sanitized_booking_url(
        self,
    ) -> None:
        manifest = self._provider_manifest()
        adapter, report, events = self._collect_provider(manifest)
        source_report = report.source_reports[0]
        execution = source_report.executions[0]
        offer = execution.result.offers[0]
        component = replace(
            offer.booking_components[0],
            amount_minor=10_000,
            currency="GBP",
            booking_url=(
                "https://booking.transavia.com/search?from=ORY&"
                "utm_source=test&sessionToken=secret#checkout"
            ),
        )
        converted_offer = replace(
            offer,
            total_amount_minor=10_000,
            currency="GBP",
            booking_components=(component,),
        )
        converted_report = replace(
            report,
            source_reports=(
                replace(
                    source_report,
                    executions=(
                        replace(
                            execution,
                            result=replace(
                                execution.result,
                                offers=(converted_offer,),
                            ),
                        ),
                    ),
                ),
            ),
        )
        with self.sessions() as session, session.begin():
            session.add(
                FxRate(
                    rate_date=date(2026, 12, 31),
                    currency="GBP",
                    eur_per_unit=1.15,
                    published_at=datetime(2026, 12, 31, 15, tzinfo=UTC),
                )
            )

        result = DatabasePublisher(self.sessions).publish(
            manifest,
            converted_report,
            health_events=events,
            source_metadata=(adapter.metadata(),),
        )

        self.assertEqual(result.revisions_created, 1)
        with self.sessions() as session:
            revision = session.scalar(select(FareSnapshotRevision))
            booking = session.scalar(select(SnapshotBooking))
        assert revision is not None and booking is not None
        self.assertEqual(revision.total_price_eur_cents, 11_500)
        self.assertEqual(booking.original_amount_minor, 10_000)
        self.assertEqual(booking.original_currency, "GBP")
        self.assertEqual(booking.eur_amount_cents, 11_500)
        self.assertEqual(booking.fx_rate, 1.15)
        self.assertEqual(booking.fx_rate_date, date(2026, 12, 31))
        self.assertTrue(booking.mandatory_fees_included)
        self.assertEqual(booking.passenger_age, 22)
        self.assertTrue(booking.passenger_student)
        self.assertTrue(booking.passenger_eligibility_confirmed)
        self.assertTrue(booking.bookable_at_observation)
        self.assertFalse(booking.is_teaser)
        self.assertEqual(
            booking.booking_url,
            "https://booking.transavia.com/search?from=ORY",
        )

    def test_missing_fx_rate_marks_the_cell_unknown_instead_of_no_valid_option(
        self,
    ) -> None:
        manifest = self._provider_manifest()
        adapter, report, events = self._collect_provider(manifest)
        source_report = report.source_reports[0]
        execution = source_report.executions[0]
        offer = execution.result.offers[0]
        component = replace(
            offer.booking_components[0],
            amount_minor=10_000,
            currency="GBP",
        )
        non_eur_offer = replace(
            offer,
            total_amount_minor=10_000,
            currency="GBP",
            booking_components=(component,),
        )
        non_eur_report = replace(
            report,
            source_reports=(
                replace(
                    source_report,
                    executions=(
                        replace(
                            execution,
                            result=replace(
                                execution.result,
                                offers=(non_eur_offer,),
                            ),
                        ),
                    ),
                ),
            ),
        )

        result = DatabasePublisher(self.sessions).publish(
            manifest,
            non_eur_report,
            health_events=events,
            source_metadata=(adapter.metadata(),),
        )

        self.assertEqual(result.revisions_created, 0)
        with self.sessions() as session:
            observation = session.scalar(
                select(FareCellObservation).where(
                    FareCellObservation.destination_city_id == "lisbon-pt",
                    FareCellObservation.mode_bucket
                    == DatabaseTransportMode.FLIGHT,
                )
            )
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.availability.value, "unknown_due_failure")
        self.assertEqual(observation.execution_coverage.value, "partial")
        self.assertEqual(
            observation.diagnostic,
            "OFFER_TRUST_REJECTED:MISSING_FX_RATE",
        )

    def test_operational_fare_is_isolated_from_a_cheaper_seeded_fixture(
        self,
    ) -> None:
        with self.sessions() as session:
            profile, rule = ensure_default_profile_and_rules(session)
            created = seed_fixture_history(
                session,
                as_of=RUN_DATE,
                profile=profile,
                rule=rule,
            )
            session.commit()
        self.assertGreater(created, 0)

        manifest = self._provider_manifest()
        adapter, report, events = self._collect_provider(manifest)
        source_report = report.source_reports[0]
        execution = source_report.executions[0]
        offer = execution.result.offers[0]
        expensive_component = replace(
            offer.booking_components[0],
            amount_minor=99_900,
        )
        expensive_offer = replace(
            offer,
            total_amount_minor=99_900,
            booking_components=(expensive_component,),
        )
        expensive_report = replace(
            report,
            source_reports=(
                replace(
                    source_report,
                    executions=(
                        replace(
                            execution,
                            result=replace(
                                execution.result,
                                offers=(expensive_offer,),
                            ),
                        ),
                    ),
                ),
            ),
        )

        result = DatabasePublisher(self.sessions).publish(
            manifest,
            expensive_report,
            health_events=events,
            source_metadata=(adapter.metadata(),),
        )

        self.assertEqual(result.revisions_created, 1)
        with self.sessions() as session:
            window = session.scalar(
                select(TravelWindow).where(TravelWindow.friday_date == WEEKEND)
            )
            assert window is not None
            snapshots = session.scalars(
                select(FareSnapshot).where(
                    FareSnapshot.observed_on == RUN_DATE,
                    FareSnapshot.destination_city_id == "lisbon-pt",
                    FareSnapshot.travel_window_id == window.id,
                    FareSnapshot.mode_bucket == DatabaseTransportMode.FLIGHT,
                )
            ).all()
            self.assertEqual(len(snapshots), 2)
            by_provenance = {item.is_synthetic: item for item in snapshots}
            operational = session.get(
                FareSnapshotRevision,
                by_provenance[False].current_revision_id,
            )
            synthetic = session.get(
                FareSnapshotRevision,
                by_provenance[True].current_revision_id,
            )
        assert operational is not None and synthetic is not None
        self.assertEqual(operational.total_price_eur_cents, 99_900)
        self.assertLess(synthetic.total_price_eur_cents, 99_900)

    def test_teaser_offer_is_observed_but_never_published_as_a_fare(self) -> None:
        manifest = self._provider_manifest()
        adapter, report, events = self._collect_provider(manifest)
        source_report = report.source_reports[0]
        execution = source_report.executions[0]
        teaser = replace(execution.result.offers[0], is_teaser=True)
        teaser_report = replace(
            report,
            source_reports=(
                replace(
                    source_report,
                    executions=(
                        replace(
                            execution,
                            result=replace(execution.result, offers=(teaser,)),
                        ),
                    ),
                ),
            ),
        )

        result = DatabasePublisher(self.sessions).publish(
            manifest,
            teaser_report,
            health_events=events,
            source_metadata=(adapter.metadata(),),
        )

        self.assertEqual(result.revisions_created, 0)
        self.assertEqual(self._count(FareSnapshotRevision), 0)
        self.assertEqual(self._count(SnapshotBooking), 0)

    def test_disabled_source_is_persisted_without_claiming_live_success(self) -> None:
        registry = build_default_registry(include_fixture=False)
        manifest = DailyManifestBuilder(
            origin_terminal_ids=("par-cdg",),
            config_version="production-test-v1",
        ).build(
            scheduled_for=RUN_DATE,
            source_slugs=("ryanair",),
            destinations=(FIXTURE_DESTINATIONS["lisbon-pt"],),
            weekend_fridays=(WEEKEND,),
            created_at=OBSERVED_AT,
        )
        sink = InMemoryHealthSink()
        report = asyncio.run(
            BatchCoordinator(
                AdapterRegistry((registry.get("ryanair"),)),
                health_sink=sink,
                clock=lambda: OBSERVED_AT,
            ).run(manifest)
        )

        result = DatabasePublisher(self.sessions).publish(
            manifest,
            report,
            health_events=sink.events,
            source_metadata=(registry.get("ryanair").metadata(),),
        )

        self.assertEqual(result.snapshots_created, 0)
        with self.sessions() as session:
            extractor = session.scalar(select(ExtractorRun))
            crawl_run = session.scalar(select(CrawlRun))
            self.assertIsNotNone(extractor)
            self.assertIsNotNone(crawl_run)
            assert extractor is not None and crawl_run is not None
            self.assertEqual(extractor.status.value, "disabled")
            self.assertEqual(extractor.attempted_searches, 0)
            self.assertEqual(extractor.successful_searches, 0)
            self.assertEqual(crawl_run.successful_cells, 0)

    def test_invalid_offer_rolls_back_every_publication_record(self) -> None:
        manifest = self._manifest()
        report, events = self._collect(manifest)
        source_report = report.source_reports[0]
        execution = source_report.executions[0]
        offer = execution.result.offers[0]
        segment = offer.outbound_segments[0]
        invalid_segment = replace(
            segment,
            departs_at=segment.departs_at - timedelta(days=1),
            arrives_at=segment.arrives_at - timedelta(days=1),
        )
        invalid_offer = replace(offer, outbound_segments=(invalid_segment,))
        invalid_result = replace(execution.result, offers=(invalid_offer,))
        invalid_execution = replace(execution, result=invalid_result)
        invalid_source_report = replace(
            source_report,
            executions=(invalid_execution, *source_report.executions[1:]),
        )
        invalid_report = replace(report, source_reports=(invalid_source_report,))

        with self.assertRaises(PublicationError):
            DatabasePublisher(self.sessions).publish(
                manifest, invalid_report, health_events=events
            )

        self.assertEqual(self._count(CrawlRun), 0)
        self.assertEqual(self._count(ExtractorRun), 0)
        self.assertEqual(self._count(CrawlCoverageCell), 0)
        self.assertEqual(self._count(ConnectorRequest), 0)
        self.assertEqual(self._count(FareSnapshot), 0)
        self.assertEqual(self._count(FareSnapshotRevision), 0)
        self.assertEqual(self._count(HealthEvent), 0)
        with self.sessions() as session:
            self.assertIsNone(session.get(Source, "fixture"))

    def test_commit_failure_does_not_leave_partial_batch(self) -> None:
        manifest = self._manifest()
        report, events = self._collect(manifest)

        def reject_commit(_session: Session) -> None:
            raise RuntimeError("simulated commit failure")

        event.listen(self.sessions.class_, "before_commit", reject_commit)
        try:
            with self.assertRaisesRegex(RuntimeError, "simulated commit failure"):
                DatabasePublisher(self.sessions).publish(
                    manifest,
                    report,
                    health_events=events,
                )
        finally:
            event.remove(self.sessions.class_, "before_commit", reject_commit)

        self.assertEqual(self._count(CrawlRun), 0)
        self.assertEqual(self._count(ExtractorRun), 0)
        self.assertEqual(self._count(CrawlCoverageCell), 0)
        self.assertEqual(self._count(ConnectorRequest), 0)
        self.assertEqual(self._count(FareSnapshot), 0)
        self.assertEqual(self._count(FareSnapshotRevision), 0)
        self.assertEqual(self._count(HealthEvent), 0)


if __name__ == "__main__":
    unittest.main()
