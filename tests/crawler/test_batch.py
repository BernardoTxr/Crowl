from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, date, datetime

from crowl_crawler.adapters.fixture import FixtureAdapter
from crowl_crawler.adapters.registry import AdapterRegistry, build_default_registry
from crowl_crawler.batch import BatchCoordinator, RetryPolicy
from crowl_crawler.health import HealthEventKind, InMemoryHealthSink
from crowl_crawler.manifest import DailyManifestBuilder, FIXTURE_DESTINATIONS
from crowl_crawler.models import (
    BatchStatus,
    ConnectorResult,
    Direction,
    ExtractorRunStatus,
    SearchKind,
    SearchOutcome,
)


class FlakyFixtureAdapter(FixtureAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.search_count = 0

    async def search(self, request):  # type: ignore[no-untyped-def]
        self.search_count += 1
        if self.search_count == 1:
            return ConnectorResult.failure(
                outcome=SearchOutcome.TIMEOUT,
                diagnostic_code="SIMULATED_TIMEOUT",
                parser_version=self.metadata().parser_version,
                observed_at=datetime.now(UTC),
            )
        return await super().search(request)


class EmptyFixtureAdapter(FixtureAdapter):
    async def search(self, request):  # type: ignore[no-untyped-def]
        return ConnectorResult(
            outcome=SearchOutcome.SUCCESS_EMPTY,
            retryable=False,
            diagnostic_code="SIMULATED_VALID_EMPTY",
            expected_count=1,
            completed_count=1,
            offers=(),
            parser_version=self.metadata().parser_version,
            observed_at=datetime.now(UTC),
        )


class BatchCoordinatorTests(unittest.TestCase):
    def _manifest(self, source: str = "fixture", *, canary_per_source: bool = True):
        return DailyManifestBuilder(
            origin_terminal_ids=(
                "par-cdg",
                "par-ory",
                "par-gare-du-nord",
            ),
            config_version="test-v1",
        ).build(
            scheduled_for=date(2027, 1, 1),
            source_slugs=(source,),
            destinations=(
                FIXTURE_DESTINATIONS["amsterdam-nl"],
                FIXTURE_DESTINATIONS["lisbon-pt"],
            ),
            weekend_fridays=(date(2027, 1, 8),),
            canary_per_source=canary_per_source,
        )

    def test_fixture_batch_succeeds_and_records_health_lifecycle(self) -> None:
        sink = InMemoryHealthSink()
        report = asyncio.run(
            BatchCoordinator(
                build_default_registry(include_fixture=True), health_sink=sink
            ).run(self._manifest())
        )
        self.assertEqual(report.status, BatchStatus.SUCCEEDED)
        self.assertEqual(report.offer_count, 2)
        self.assertEqual(report.source_reports[0].state, ExtractorRunStatus.SUCCEEDED)
        kinds = [event.kind for event in sink.events]
        self.assertEqual(kinds[0], HealthEventKind.BATCH_STARTED)
        self.assertEqual(kinds[-1], HealthEventKind.BATCH_FINISHED)
        self.assertEqual(kinds.count(HealthEventKind.SEARCH_FINISHED), 2)
        self.assertEqual(kinds.count(HealthEventKind.BATCH_HEARTBEAT), 1)
        self.assertNotIn("https://", repr([event.payload for event in sink.events]))

    def test_transient_search_is_retried_with_bounded_attempts(self) -> None:
        adapter = FlakyFixtureAdapter()
        registry = AdapterRegistry((adapter,))
        coordinator = BatchCoordinator(
            registry,
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                initial_backoff_seconds=0,
                maximum_backoff_seconds=0,
            ),
        )
        report = asyncio.run(coordinator.run(self._manifest(canary_per_source=False)))
        first = report.source_reports[0].executions[0]
        self.assertEqual(first.attempts, 2)
        self.assertEqual(first.result.outcome, SearchOutcome.SUCCESS_RESULTS)
        self.assertEqual(report.source_reports[0].physical_request_count, 3)
        self.assertEqual(report.status, BatchStatus.SUCCEEDED)

    def test_disabled_source_is_visible_but_does_not_degrade_execution(self) -> None:
        report = asyncio.run(
            BatchCoordinator(build_default_registry(include_fixture=False)).run(
                self._manifest(source="ryanair")
            )
        )
        self.assertEqual(report.status, BatchStatus.SUCCEEDED)
        source = report.source_reports[0]
        self.assertEqual(source.state, ExtractorRunStatus.DISABLED)
        self.assertEqual(source.attempted_tasks, 0)
        self.assertEqual(source.physical_request_count, 0)
        self.assertEqual(
            source.outcome_counts, {SearchOutcome.SKIPPED_DISABLED.value: 2}
        )

    def test_manifest_can_request_atomic_and_both_directional_prices(self) -> None:
        manifest = DailyManifestBuilder(
            origin_terminal_ids=("par-cdg",),
            config_version="test-v1",
        ).build(
            scheduled_for=date(2027, 1, 1),
            source_slugs=("fixture",),
            destinations=(FIXTURE_DESTINATIONS["lisbon-pt"],),
            weekend_fridays=(date(2027, 1, 8),),
            include_one_way_tasks=True,
        )

        assert len(manifest.tasks) == 3
        assert {cell.direction for cell in manifest.coverage_cells} == {
            "round_trip",
            "outbound",
            "return",
        }
        assert {
            (task.request.search_kind, task.request.direction)
            for task in manifest.tasks
        } == {
            (SearchKind.ROUND_TRIP, None),
            (SearchKind.ONE_WAY, Direction.OUTBOUND),
            (SearchKind.ONE_WAY, Direction.RETURN),
        }

    def test_expired_deadline_cancels_tasks_without_calling_provider(self) -> None:
        adapter = FlakyFixtureAdapter()
        registry = AdapterRegistry((adapter,))
        report = asyncio.run(
            BatchCoordinator(registry).run(
                self._manifest(canary_per_source=False),
                deadline_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        self.assertEqual(adapter.search_count, 0)
        self.assertEqual(report.status, BatchStatus.FAILED)
        self.assertTrue(
            all(
                execution.result.outcome is SearchOutcome.CANCELLED_DEADLINE
                for execution in report.source_reports[0].executions
            )
        )

    def test_all_empty_volume_is_quarantined_after_configured_threshold(self) -> None:
        registry = AdapterRegistry((EmptyFixtureAdapter(),))
        report = asyncio.run(
            BatchCoordinator(registry, suspicious_empty_minimum=2).run(
                self._manifest(canary_per_source=False)
            )
        )
        source = report.source_reports[0]
        self.assertEqual(source.state, ExtractorRunStatus.QUARANTINED)
        self.assertEqual(source.diagnostic_code, "SUSPICIOUS_EMPTY_COLLAPSE")
        self.assertEqual(report.status, BatchStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
