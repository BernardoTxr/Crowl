from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select

from crowl_core import Base, create_database_engine, create_session_factory
from crowl_core.catalog import seed_development_data
from crowl_core.models import (
    ConnectorRequest,
    CrawlCoverageCell,
    CrawlRun,
    ExtractorRun,
)
from crowl_crawler.adapters.fixture import FixtureAdapter
from crowl_crawler.adapters.registry import AdapterRegistry
from crowl_crawler.batch import BatchCoordinator
from crowl_crawler.lifecycle import DailyRunLease, DatabaseRunLifecycle
from crowl_crawler.manifest import DailyManifestBuilder, FIXTURE_DESTINATIONS
from crowl_crawler.models import ConnectorResult, SearchOutcome
from crowl_crawler.persistence import DatabasePublisher


ROOT = Path(__file__).resolve().parents[2]
RUN_DATE = date(2027, 1, 1)
WEEKEND = date(2027, 1, 8)
OBSERVED_AT = datetime(2027, 1, 1, 2, tzinfo=UTC)


class EmptyCountingAdapter(FixtureAdapter):
    def __init__(self) -> None:
        super().__init__(clock=lambda: OBSERVED_AT)
        self.search_count = 0

    async def search(self, request):  # type: ignore[no-untyped-def]
        self.search_count += 1
        return ConnectorResult(
            outcome=SearchOutcome.SUCCESS_EMPTY,
            retryable=False,
            diagnostic_code="TRUSTWORTHY_EMPTY",
            expected_count=1,
            completed_count=1,
            offers=(),
            parser_version=self.metadata().parser_version,
            observed_at=OBSERVED_AT,
        )


class CountingFixtureAdapter(FixtureAdapter):
    def __init__(self) -> None:
        super().__init__(clock=lambda: OBSERVED_AT)
        self.search_count = 0

    async def search(self, request):  # type: ignore[no-untyped-def]
        self.search_count += 1
        return await super().search(request)


class SimulatedInterruption(RuntimeError):
    pass


class RunLifecycleTests(unittest.TestCase):
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
        self.lifecycle = DatabaseRunLifecycle(self.sessions, clock=lambda: OBSERVED_AT)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _manifest(self, *, destinations: int = 2):  # type: ignore[no-untyped-def]
        requested = (
            FIXTURE_DESTINATIONS["amsterdam-nl"],
            FIXTURE_DESTINATIONS["lisbon-pt"],
        )[:destinations]
        return DailyManifestBuilder(
            origin_terminal_ids=("par-cdg", "par-ory"),
            config_version="lifecycle-test-v1",
        ).build(
            scheduled_for=RUN_DATE,
            source_slugs=("fixture",),
            destinations=requested,
            weekend_fridays=(WEEKEND,),
            canary_per_source=False,
            created_at=OBSERVED_AT,
        )

    def test_prepare_commits_full_pending_ledger_before_collection(self) -> None:
        adapter = EmptyCountingAdapter()
        manifest = self._manifest()

        plan = self.lifecycle.prepare(manifest, source_metadata=(adapter.metadata(),))

        self.assertFalse(plan.is_resume)
        self.assertEqual(adapter.search_count, 0)
        with self.sessions() as session:
            crawl = session.get(CrawlRun, plan.crawl_run_id)
            self.assertIsNotNone(crawl)
            assert crawl is not None
            self.assertEqual(crawl.status.value, "running")
            self.assertEqual(crawl.manifest_version, manifest.manifest_id)
            self.assertEqual(crawl.expected_cells, 4)
            extractor = session.scalar(select(ExtractorRun))
            self.assertIsNotNone(extractor)
            assert extractor is not None
            self.assertEqual(extractor.status.value, "pending")
            self.assertEqual(extractor.expected_searches, 2)
            self.assertEqual(session.scalar(select(func.count(ConnectorRequest.id))), 2)
            self.assertEqual(
                session.scalar(select(func.count(CrawlCoverageCell.id))), 4
            )
            self.assertEqual(
                session.scalar(
                    select(func.count(ConnectorRequest.id)).where(
                        ConnectorRequest.outcome.is_not(None)
                    )
                ),
                0,
            )

    def test_interrupted_empty_result_is_reused_without_duplicate_rows(self) -> None:
        manifest = self._manifest()
        first_adapter = EmptyCountingAdapter()
        first_plan = self.lifecycle.prepare(
            manifest, source_metadata=(first_adapter.metadata(),)
        )
        recorded = 0

        def record_then_interrupt(execution):  # type: ignore[no-untyped-def]
            nonlocal recorded
            durable = self.lifecycle.record_execution(manifest, execution)
            recorded += 1
            if recorded == 1:
                raise SimulatedInterruption
            return durable

        with self.assertRaises(SimulatedInterruption):
            asyncio.run(
                BatchCoordinator(
                    AdapterRegistry((first_adapter,)),
                    execution_recorder=record_then_interrupt,
                    clock=lambda: OBSERVED_AT,
                ).run(manifest)
            )
        self.lifecycle.mark_abandoned(manifest, "SIMULATED_INTERRUPTION")

        second_adapter = EmptyCountingAdapter()
        second_plan = self.lifecycle.prepare(
            manifest, source_metadata=(second_adapter.metadata(),)
        )
        self.assertTrue(second_plan.is_resume)
        self.assertEqual(second_plan.crawl_run_id, first_plan.crawl_run_id)
        self.assertEqual(len(second_plan.resumed_executions), 1)

        report = asyncio.run(
            BatchCoordinator(
                AdapterRegistry((second_adapter,)),
                execution_recorder=lambda execution: self.lifecycle.record_execution(
                    manifest, execution
                ),
                clock=lambda: OBSERVED_AT,
            ).run(
                manifest,
                resumed_executions=second_plan.resumed_executions,
                prior_attempts=second_plan.prior_attempts,
            )
        )
        self.assertEqual(second_adapter.search_count, 1)
        self.assertEqual(len(report.source_reports[0].executions), 2)
        publication = DatabasePublisher(self.sessions).publish(
            manifest,
            report,
            source_metadata=(second_adapter.metadata(),),
        )
        self.assertEqual(publication.crawl_run_id, first_plan.crawl_run_id)

        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count(ConnectorRequest.id))), 2)
            requests = session.scalars(select(ConnectorRequest)).all()
            self.assertEqual(sorted(item.attempt_count for item in requests), [1, 1])
            self.assertTrue(
                all(item.outcome.value == "success_empty" for item in requests)
            )

    def test_result_bearing_request_is_rerun_but_attempts_are_retained(self) -> None:
        manifest = self._manifest(destinations=1)
        first_adapter = CountingFixtureAdapter()
        first_plan = self.lifecycle.prepare(
            manifest, source_metadata=(first_adapter.metadata(),)
        )
        first_report = asyncio.run(
            BatchCoordinator(
                AdapterRegistry((first_adapter,)),
                execution_recorder=lambda execution: self.lifecycle.record_execution(
                    manifest, execution
                ),
                clock=lambda: OBSERVED_AT,
            ).run(manifest)
        )
        self.assertEqual(first_report.offer_count, 1)
        self.lifecycle.mark_abandoned(manifest, "BEFORE_PUBLICATION_INTERRUPTION")

        second_adapter = CountingFixtureAdapter()
        second_plan = self.lifecycle.prepare(
            manifest, source_metadata=(second_adapter.metadata(),)
        )
        self.assertEqual(second_plan.crawl_run_id, first_plan.crawl_run_id)
        self.assertEqual(second_plan.resumed_executions, {})
        self.assertEqual(tuple(second_plan.prior_attempts.values()), (1,))

        second_report = asyncio.run(
            BatchCoordinator(
                AdapterRegistry((second_adapter,)),
                execution_recorder=lambda execution: self.lifecycle.record_execution(
                    manifest, execution
                ),
                clock=lambda: OBSERVED_AT,
            ).run(
                manifest,
                resumed_executions=second_plan.resumed_executions,
                prior_attempts=second_plan.prior_attempts,
            )
        )
        self.assertEqual(second_adapter.search_count, 1)
        self.assertEqual(second_report.source_reports[0].physical_request_count, 2)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count(ConnectorRequest.id))), 1)
            request = session.scalar(select(ConnectorRequest))
            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request.attempt_count, 2)
            self.assertEqual(request.outcome.value, "success_results")

    def test_sqlite_daily_lease_is_nonblocking_and_released(self) -> None:
        first = DailyRunLease(self.engine, RUN_DATE)
        overlapping = DailyRunLease(self.engine, RUN_DATE)
        after_release = DailyRunLease(self.engine, RUN_DATE)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(overlapping.acquire())
            first.release()
            self.assertTrue(after_release.acquire())
        finally:
            first.release()
            overlapping.release()
            after_release.release()

    def test_postgres_lease_uses_session_try_lock_and_unlock(self) -> None:
        statements: list[str] = []

        class FakeConnection:
            def scalar(self, statement):  # type: ignore[no-untyped-def]
                rendered = str(statement)
                statements.append(rendered)
                return "pg_try_advisory_lock" in rendered

            def commit(self) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeDialect:
            name = "postgresql"

        class FakeEngine:
            dialect = FakeDialect()

            def connect(self):  # type: ignore[no-untyped-def]
                return FakeConnection()

        lease = DailyRunLease(FakeEngine(), RUN_DATE)  # type: ignore[arg-type]
        self.assertTrue(lease.acquire())
        lease.release()
        self.assertTrue(any("pg_try_advisory_lock" in item for item in statements))
        self.assertTrue(any("pg_advisory_unlock" in item for item in statements))


if __name__ == "__main__":
    unittest.main()
