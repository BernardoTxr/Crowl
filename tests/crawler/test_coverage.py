from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select

from crowl_core import Base, create_database_engine, create_session_factory
from crowl_core.catalog import seed_development_data
from crowl_core.models import (
    CrawlCoverageCell,
    CrawlRun,
    ExtractorRun,
    FareCellObservation,
    HealthEvent,
    SourceRouteCoverage,
)
from crowl_crawler.adapters.base import SourceAdapter
from crowl_crawler.adapters.registry import AdapterRegistry, build_default_registry
from crowl_crawler.batch import BatchCoordinator
from crowl_crawler.coverage import (
    CoverageDiscoveryError,
    discover_and_persist_coverage,
    record_coverage_discovery_failure,
)
from crowl_crawler.lifecycle import DatabaseRunLifecycle
from crowl_crawler.manifest import DailyManifestBuilder, DestinationTarget
from crowl_crawler.models import (
    AdapterMetadata,
    ConnectorResult,
    CoverageDiscovery,
    ExtractionStrategy,
    SearchOutcome,
    SearchRequest,
    TransportMode,
)
from crowl_crawler.persistence import DatabasePublisher


ROOT = Path(__file__).resolve().parents[2]
RUN_DATE = date(2027, 1, 1)
OBSERVED_AT = datetime(2027, 1, 1, 2, tzinfo=UTC)
FIRST_WEEKEND = date(2027, 1, 8)
SECOND_WEEKEND = date(2027, 1, 15)


def _metadata(source_slug: str) -> AdapterMetadata:
    return AdapterMetadata(
        source_slug=source_slug,
        display_name=f"Test {source_slug}",
        modes=(TransportMode.FLIGHT,),
        strategy=ExtractionStrategy.HTTP_STRUCTURED,
        intended_strategy=ExtractionStrategy.HTTP_STRUCTURED,
        connector_version="coverage-test-v1",
        parser_version="coverage-parser-v1",
        enabled=True,
        disabled_reason=None,
        access_basis="test adapter; no external access",
        sale_horizon_days=30,
    )


class DiscoveryAdapter(SourceAdapter):
    def __init__(
        self,
        *,
        source_slug: str,
        discovery: CoverageDiscovery,
        session_factory=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._metadata = _metadata(source_slug)
        self._discovery = discovery
        self._session_factory = session_factory
        self.discovery_calls = 0
        self.search_calls = 0
        self.intent_was_durable = False

    def metadata(self) -> AdapterMetadata:
        return self._metadata

    async def discover_coverage(self) -> CoverageDiscovery:
        self.discovery_calls += 1
        if self._session_factory is not None:
            with self._session_factory() as session:
                self.intent_was_durable = bool(
                    session.scalar(
                        select(func.count(HealthEvent.id)).where(
                            HealthEvent.source_id == self._metadata.source_slug,
                            HealthEvent.event_type == "coverage_discovery_started",
                        )
                    )
                )
        return self._discovery

    async def canary(
        self, requests: tuple[SearchRequest, ...]
    ) -> tuple[ConnectorResult, ...]:
        self.search_calls += len(requests)
        raise AssertionError("coverage-only test unexpectedly searched fares")

    async def search(self, request: SearchRequest) -> ConnectorResult:
        self.search_calls += 1
        raise AssertionError("coverage-only test unexpectedly searched fares")

    async def close(self) -> None:
        return None


class CoveragePruningTests(unittest.TestCase):
    def test_route_and_sale_horizon_prune_without_cross_source_work(self) -> None:
        coverage = {
            "ryanair": CoverageDiscovery(
                source_slug="ryanair",
                supported_terminal_pairs=(("par-cdg", "lis-humberto-delgado"),),
                sellable_through=date(2027, 1, 11),
                observed_at=OBSERVED_AT,
                diagnostic_code="RYANAIR_COVERAGE_OK",
            ),
            "easyjet": CoverageDiscovery(
                source_slug="easyjet",
                supported_terminal_pairs=(("par-ory", "ams-schiphol"),),
                sellable_through=date(2027, 1, 18),
                observed_at=OBSERVED_AT,
                diagnostic_code="EASYJET_COVERAGE_OK",
            ),
        }
        manifest = DailyManifestBuilder(
            origin_terminal_ids=("par-cdg", "par-ory"),
            config_version="coverage-test-v1",
        ).build(
            scheduled_for=RUN_DATE,
            source_slugs=("ryanair", "easyjet"),
            destinations=(
                DestinationTarget("lisbon-pt", ("lis-humberto-delgado",)),
                DestinationTarget("amsterdam-nl", ("ams-schiphol",)),
            ),
            weekend_fridays=(FIRST_WEEKEND, SECOND_WEEKEND),
            include_one_way_tasks=True,
            source_coverage=coverage,
            created_at=OBSERVED_AT,
        )

        self.assertEqual(len(manifest.tasks), 9)
        self.assertEqual(len({task.task_id for task in manifest.tasks}), 9)
        self.assertEqual(
            len({task.request.fingerprint for task in manifest.tasks}), 9
        )
        for task in manifest.tasks:
            pair = (
                task.request.origin_terminal_ids[0],
                task.request.destination_terminal_ids[0],
            )
            self.assertEqual(len(task.request.origin_terminal_ids), 1)
            self.assertEqual(len(task.request.destination_terminal_ids), 1)
            if task.source_slug == "ryanair":
                self.assertEqual(pair, ("par-cdg", "lis-humberto-delgado"))
                self.assertEqual(task.request.weekend_start, FIRST_WEEKEND)
            else:
                self.assertEqual(task.source_slug, "easyjet")
                self.assertEqual(pair, ("par-ory", "ams-schiphol"))

        not_on_sale = [
            cell
            for cell in manifest.coverage_cells
            if cell.preclassified_outcome is SearchOutcome.NOT_ON_SALE
        ]
        unsupported = [
            cell
            for cell in manifest.coverage_cells
            if cell.preclassified_outcome
            is SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE
        ]
        self.assertEqual(len(not_on_sale), 3)
        self.assertEqual(len(unsupported), 12)
        self.assertTrue(all(cell.applicable for cell in not_on_sale))
        self.assertTrue(all(not cell.applicable for cell in unsupported))
        referenced = {
            cell_id for task in manifest.tasks for cell_id in task.coverage_cell_ids
        }
        self.assertFalse(
            referenced
            & {
                cell.cell_id
                for cell in (*not_on_sale, *unsupported)
            }
        )

    def test_discovery_intent_precedes_network_and_rows_keep_provenance(self) -> None:
        engine = create_database_engine("sqlite://")
        Base.metadata.create_all(engine)
        sessions = create_session_factory(engine)
        try:
            with sessions() as session:
                seed_development_data(
                    session,
                    data_dir=ROOT / "data",
                    as_of=RUN_DATE,
                    include_fixture_fares=False,
                )
            discovery = CoverageDiscovery(
                source_slug="ryanair",
                supported_terminal_pairs=(("par-cdg", "lis-humberto-delgado"),),
                sellable_through=date(2027, 1, 31),
                observed_at=OBSERVED_AT,
                diagnostic_code="COVERAGE_FEED_OK",
            )
            adapter = DiscoveryAdapter(
                source_slug="ryanair",
                discovery=discovery,
                session_factory=sessions,
            )
            result = asyncio.run(
                discover_and_persist_coverage(
                    sessions,
                    AdapterRegistry((adapter,)),
                    scheduled_for=RUN_DATE,
                    clock=lambda: OBSERVED_AT,
                )
            )

            self.assertEqual(result, {"ryanair": discovery})
            self.assertEqual(adapter.discovery_calls, 1)
            self.assertTrue(adapter.intent_was_durable)
            with sessions() as session:
                row = session.scalar(select(SourceRouteCoverage))
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row.source_id, "ryanair")
                self.assertEqual(row.origin_terminal_id, "par-cdg")
                self.assertEqual(
                    row.destination_terminal_id, "lis-humberto-delgado"
                )
                self.assertEqual(row.effective_from, RUN_DATE)
                self.assertEqual(row.effective_to, date(2027, 1, 31))
                self.assertEqual(row.discovered_at.replace(tzinfo=UTC), OBSERVED_AT)
                self.assertIn("coverage-discovery-v1", row.provenance or "")
                self.assertIn("coverage-test-v1", row.provenance or "")
                event_types = set(session.scalars(select(HealthEvent.event_type)))
                self.assertEqual(
                    event_types,
                    {
                        "coverage_discovery_started",
                        "coverage_discovery_succeeded",
                    },
                )
        finally:
            engine.dispose()

    def test_malformed_discovery_fails_closed_after_durable_intent(self) -> None:
        engine = create_database_engine("sqlite://")
        Base.metadata.create_all(engine)
        sessions = create_session_factory(engine)
        try:
            with sessions() as session:
                seed_development_data(
                    session,
                    data_dir=ROOT / "data",
                    as_of=RUN_DATE,
                    include_fixture_fares=False,
                )
            duplicate_pair = ("par-cdg", "lis-humberto-delgado")
            adapter = DiscoveryAdapter(
                source_slug="ryanair",
                discovery=CoverageDiscovery(
                    source_slug="ryanair",
                    supported_terminal_pairs=(duplicate_pair, duplicate_pair),
                    sellable_through=date(2027, 1, 31),
                    observed_at=OBSERVED_AT,
                    diagnostic_code="MALFORMED_COVERAGE",
                ),
                session_factory=sessions,
            )

            with self.assertRaises(CoverageDiscoveryError) as rejected:
                asyncio.run(
                    discover_and_persist_coverage(
                        sessions,
                        AdapterRegistry((adapter,)),
                        scheduled_for=RUN_DATE,
                        clock=lambda: OBSERVED_AT,
                    )
                )

            self.assertEqual(adapter.discovery_calls, 1)
            self.assertTrue(adapter.intent_was_durable)
            self.assertEqual(adapter.search_calls, 0)
            with sessions() as session:
                self.assertEqual(
                    session.scalar(select(func.count(SourceRouteCoverage.id))), 0
                )
                self.assertEqual(
                    set(session.scalars(select(HealthEvent.event_type))),
                    {
                        "coverage_discovery_started",
                        "coverage_discovery_failed",
                    },
                )

            source_metadata = tuple(
                adapter.metadata() if item.source_slug == "ryanair" else item
                for item in build_default_registry(
                    include_fixture=False
                ).metadata(include_fixture=False)
            )
            crawl_run_id = record_coverage_discovery_failure(
                sessions,
                scheduled_for=RUN_DATE,
                source_metadata=source_metadata,
                failed_source_slugs=rejected.exception.failed_source_slugs,
                release_version="coverage-test-v1",
                clock=lambda: OBSERVED_AT,
            )
            with sessions() as session:
                crawl_run = session.get(CrawlRun, crawl_run_id)
                extractor_rows = session.scalars(select(ExtractorRun)).all()
                extractor = next(
                    row for row in extractor_rows if row.source_id == "ryanair"
                )
                self.assertIsNotNone(crawl_run)
                assert crawl_run is not None
                self.assertEqual(len(extractor_rows), 14)
                self.assertEqual(crawl_run.status.value, "failed")
                self.assertEqual(
                    crawl_run.diagnostic, "COVERAGE_DISCOVERY_REJECTED"
                )
                self.assertEqual(extractor.status.value, "failed")
                self.assertEqual(
                    extractor.diagnostic, "COVERAGE_DISCOVERY_REJECTED"
                )
                self.assertEqual(extractor.consecutive_failures, 1)
                flixbus = next(
                    row for row in extractor_rows if row.source_id == "flixbus"
                )
                self.assertEqual(flixbus.status.value, "failed")
                self.assertEqual(
                    flixbus.diagnostic, "COVERAGE_DISCOVERY_BATCH_ABORTED"
                )
                self.assertTrue(
                    all(
                        row.status.value == "disabled"
                        for row in extractor_rows
                        if row.source_id not in {"ryanair", "flixbus"}
                    )
                )
                self.assertTrue(
                    all(
                        crawl_run_id == value
                        for value in session.scalars(
                            select(HealthEvent.crawl_run_id)
                        )
                    )
                )

            recovered_discovery = CoverageDiscovery(
                source_slug="ryanair",
                supported_terminal_pairs=(("par-cdg", "lis-humberto-delgado"),),
                sellable_through=date(2027, 1, 31),
                observed_at=OBSERVED_AT,
                diagnostic_code="RECOVERED_COVERAGE",
            )
            recovered_manifest = DailyManifestBuilder(
                origin_terminal_ids=("par-cdg",),
                config_version="coverage-test-v1",
            ).build(
                scheduled_for=RUN_DATE,
                source_slugs=("ryanair",),
                destinations=(
                    DestinationTarget("lisbon-pt", ("lis-humberto-delgado",)),
                ),
                weekend_fridays=(FIRST_WEEKEND,),
                source_coverage={"ryanair": recovered_discovery},
                created_at=OBSERVED_AT,
            )
            recovery = DatabaseRunLifecycle(
                sessions, clock=lambda: OBSERVED_AT
            ).prepare(
                recovered_manifest,
                source_metadata=(adapter.metadata(),),
            )
            self.assertFalse(recovery.is_resume)
            self.assertEqual(recovery.crawl_run_id, crawl_run_id)
            with sessions() as session:
                crawl_run = session.get(CrawlRun, crawl_run_id)
                assert crawl_run is not None
                self.assertEqual(crawl_run.status.value, "running")
                self.assertEqual(
                    crawl_run.manifest_version, recovered_manifest.manifest_id
                )
        finally:
            engine.dispose()

    def test_preclassified_cells_are_durable_without_provider_searches(self) -> None:
        engine = create_database_engine("sqlite://")
        Base.metadata.create_all(engine)
        sessions = create_session_factory(engine)
        try:
            with sessions() as session:
                seed_development_data(
                    session,
                    data_dir=ROOT / "data",
                    as_of=RUN_DATE,
                    include_fixture_fares=False,
                )
            discovery = CoverageDiscovery(
                source_slug="ryanair",
                supported_terminal_pairs=(("par-cdg", "lis-humberto-delgado"),),
                sellable_through=date(2027, 1, 10),
                observed_at=OBSERVED_AT,
                diagnostic_code="SHORT_SALE_HORIZON",
            )
            adapter = DiscoveryAdapter(
                source_slug="ryanair", discovery=discovery
            )
            manifest = DailyManifestBuilder(
                origin_terminal_ids=("par-cdg",),
                config_version="coverage-test-v1",
            ).build(
                scheduled_for=RUN_DATE,
                source_slugs=("ryanair",),
                destinations=(
                    DestinationTarget("lisbon-pt", ("lis-humberto-delgado",)),
                    DestinationTarget("amsterdam-nl", ("ams-schiphol",)),
                ),
                weekend_fridays=(FIRST_WEEKEND,),
                include_one_way_tasks=True,
                source_coverage={"ryanair": discovery},
                created_at=OBSERVED_AT,
            )
            self.assertEqual(manifest.tasks, ())

            lifecycle = DatabaseRunLifecycle(
                sessions, clock=lambda: OBSERVED_AT
            )
            lifecycle.prepare(manifest, source_metadata=(adapter.metadata(),))
            with sessions() as session:
                coverage_rows = session.scalars(
                    select(CrawlCoverageCell)
                ).all()
                self.assertEqual(len(coverage_rows), 4)
                self.assertTrue(
                    all(
                        row.applicable and row.outcome.value == "not_on_sale"
                        for row in coverage_rows
                        if row.city_id == "lisbon-pt"
                    )
                )
                self.assertTrue(
                    all(
                        not row.applicable
                        and row.outcome.value == "skipped_unsupported_route"
                        for row in coverage_rows
                        if row.city_id == "amsterdam-nl"
                    )
                )

            report = asyncio.run(
                BatchCoordinator(
                    AdapterRegistry((adapter,)), clock=lambda: OBSERVED_AT
                ).run(manifest)
            )
            self.assertEqual(adapter.search_calls, 0)
            DatabasePublisher(sessions).publish(
                manifest,
                report,
                source_metadata=(adapter.metadata(),),
            )
            with sessions() as session:
                flight = session.scalar(
                    select(FareCellObservation).where(
                        FareCellObservation.destination_city_id == "lisbon-pt",
                        FareCellObservation.mode_bucket == TransportMode.FLIGHT.value
                    )
                )
                self.assertIsNotNone(flight)
                assert flight is not None
                self.assertEqual(flight.availability.value, "not_on_sale")
                self.assertEqual(flight.execution_coverage.value, "complete")
                unsupported = session.scalar(
                    select(FareCellObservation).where(
                        FareCellObservation.destination_city_id == "amsterdam-nl",
                        FareCellObservation.mode_bucket == TransportMode.FLIGHT.value,
                    )
                )
                self.assertIsNotNone(unsupported)
                assert unsupported is not None
                self.assertEqual(
                    unsupported.availability.value, "no_valid_option"
                )
                self.assertEqual(
                    unsupported.execution_coverage.value, "complete"
                )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
