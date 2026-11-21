from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Callable, Iterable

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from crowl_core.catalog import ensure_default_profile_and_rules
from crowl_core.enums import (
    BatchStatus as DatabaseBatchStatus,
    Direction as DatabaseDirection,
    ExtractorRunStatus as DatabaseExtractorRunStatus,
    SearchOutcome as DatabaseSearchOutcome,
)
from crowl_core.models import (
    ConnectorRequest,
    CrawlCoverageCell,
    CrawlRun,
    ExtractorRun,
    Source,
    TravelWindow,
)

from .batch import BatchReport, TaskExecution
from .health import HealthEvent
from .manifest import BatchManifest, CoverageCell, ManifestTask
from .models import (
    SUCCESSFUL_SEARCH_OUTCOMES,
    AdapterMetadata,
    ConnectorResult,
    SearchOutcome,
    validate_diagnostic_code,
)
from .persistence import (
    PublicationError,
    _coverage_directions,
    _coverage_outcome_priority,
    _ensure_travel_windows,
    _sync_sources,
    _upsert_extractor_run,
    _validate_catalog_contract,
    _validate_source_reports,
    _write_health_events,
)


# This is deliberately distinct from the transaction-scoped publication lock.
# The collection lock is held by a dedicated connection while publication uses
# a normal session/connection from the pool.
_COLLECTION_LOCK_NAMESPACE = 0x43524F4C
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[tuple[str, int], threading.Lock] = {}

_REUSABLE_NO_OFFER_OUTCOMES = frozenset(
    {
        SearchOutcome.SUCCESS_EMPTY,
        SearchOutcome.NOT_ON_SALE,
        SearchOutcome.NO_SERVICE,
        SearchOutcome.SOLD_OUT,
        SearchOutcome.SKIPPED_DISABLED,
        SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE,
    }
)


class DailyRunLease:
    """Hold the one-per-day collection lease for the entire crawler run.

    PostgreSQL uses a session advisory lock on a dedicated connection. Tests
    and local SQLite runs use a non-blocking in-process lock; SQLite cannot
    coordinate separate operating-system processes, so database uniqueness is
    still the final local safeguard.
    """

    def __init__(self, engine: Engine, scheduled_for: date) -> None:
        self._engine = engine
        self._scheduled_for = scheduled_for
        self._connection: Connection | None = None
        self._process_key: tuple[str, int] | None = None
        self._process_lock: threading.Lock | None = None
        self.acquired = False

    def acquire(self) -> bool:
        if self.acquired:
            return True
        if self._engine.dialect.name == "postgresql":
            connection = self._engine.connect()
            try:
                acquired = bool(
                    connection.scalar(
                        select(
                            func.pg_try_advisory_lock(
                                _COLLECTION_LOCK_NAMESPACE,
                                self._scheduled_for.toordinal(),
                            )
                        )
                    )
                )
                connection.commit()
            except BaseException:
                connection.close()
                raise
            if not acquired:
                connection.close()
                return False
            self._connection = connection
            self.acquired = True
            return True

        key = (
            self._engine.url.render_as_string(hide_password=True),
            self._scheduled_for.toordinal(),
        )
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(key, threading.Lock())
        if not process_lock.acquire(blocking=False):
            return False
        self._process_key = key
        self._process_lock = process_lock
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        connection = self._connection
        process_lock = self._process_lock
        process_key = self._process_key
        try:
            if connection is not None:
                try:
                    connection.scalar(
                        select(
                            func.pg_advisory_unlock(
                                _COLLECTION_LOCK_NAMESPACE,
                                self._scheduled_for.toordinal(),
                            )
                        )
                    )
                    connection.commit()
                finally:
                    connection.close()
            elif process_lock is not None:
                process_lock.release()
                if process_key is not None:
                    with _PROCESS_LOCKS_GUARD:
                        if (
                            _PROCESS_LOCKS.get(process_key) is process_lock
                            and not process_lock.locked()
                        ):
                            _PROCESS_LOCKS.pop(process_key, None)
        finally:
            self._connection = None
            self._process_lock = None
            self._process_key = None
            self.acquired = False

    def __enter__(self) -> "DailyRunLease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class ResumePlan:
    crawl_run_id: int
    resumed_executions: dict[str, TaskExecution]
    prior_attempts: dict[str, int]
    is_resume: bool


@dataclass(frozen=True, slots=True)
class _CoverageSeed:
    applicable: bool
    outcome: SearchOutcome | None
    completed_at: datetime | None


class DatabaseRunLifecycle:
    """Durable pre-network ledger and incremental collection checkpoints."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def prepare(
        self,
        manifest: BatchManifest,
        *,
        source_metadata: Iterable[AdapterMetadata],
    ) -> ResumePlan:
        """Create and commit the complete logical ledger before collection.

        A stale running state is first made durably abandoned. A second
        transaction reconciles the immutable manifest and marks it running,
        ensuring adapters are never invoked before all request rows exist.
        """

        metadata = tuple(source_metadata)
        if len({item.source_slug for item in metadata}) != len(metadata):
            raise PublicationError("source metadata contains duplicate slugs")
        now = self._clock()
        _require_aware_clock(now)
        is_synthetic = set(manifest.expected_source_slugs) == {"fixture"}

        with self._session_factory() as session, session.begin():
            stale = session.scalar(
                select(CrawlRun).where(
                    CrawlRun.scheduled_for == manifest.scheduled_for,
                    CrawlRun.is_synthetic.is_(is_synthetic),
                )
            )
            if stale is not None and stale.status is DatabaseBatchStatus.RUNNING:
                stale.status = DatabaseBatchStatus.ABANDONED
                stale.heartbeat_at = now
                stale.completed_at = now
                stale.diagnostic = "STALE_RUN_RECLAIMED"

        with self._session_factory() as session, session.begin():
            sources = _sync_sources(session, manifest, metadata)
            profile, rule = ensure_default_profile_and_rules(session)
            windows = _ensure_travel_windows(session, manifest)
            _validate_catalog_contract(session, manifest, rule.id)

            crawl_run = session.scalar(
                select(CrawlRun).where(
                    CrawlRun.scheduled_for == manifest.scheduled_for,
                    CrawlRun.is_synthetic.is_(is_synthetic),
                )
            )
            replacing_discovery_failure = False
            if (
                crawl_run is not None
                and crawl_run.manifest_version != manifest.manifest_id
            ):
                replacing_discovery_failure = bool(
                    crawl_run.published_at is None
                    and crawl_run.diagnostic == "COVERAGE_DISCOVERY_REJECTED"
                    and not session.scalar(
                        select(func.count(ConnectorRequest.id)).where(
                            ConnectorRequest.crawl_run_id == crawl_run.id
                        )
                    )
                    and not session.scalar(
                        select(func.count(CrawlCoverageCell.id)).where(
                            CrawlCoverageCell.crawl_run_id == crawl_run.id
                        )
                    )
                )
                if not replacing_discovery_failure:
                    raise PublicationError(
                        "the daily crawl already has a different immutable manifest"
                    )
                # The failed discovery marker is a pre-manifest operational
                # ledger, not a fare-search manifest. A fixed same-day retry may
                # atomically promote it to the first immutable fare manifest.
                crawl_run.manifest_version = manifest.manifest_id

            previous_status = crawl_run.status if crawl_run is not None else None
            is_resume = bool(
                crawl_run is not None
                and not replacing_discovery_failure
                and crawl_run.published_at is None
                and previous_status
                in {
                    DatabaseBatchStatus.QUEUED,
                    DatabaseBatchStatus.RUNNING,
                    DatabaseBatchStatus.FAILED,
                    DatabaseBatchStatus.ABANDONED,
                }
            )
            if crawl_run is None:
                crawl_run = CrawlRun(
                    scheduled_for=manifest.scheduled_for,
                    is_synthetic=is_synthetic,
                    rule_version_id=rule.id,
                    profile_id=profile.id,
                    manifest_version=manifest.manifest_id,
                    release_version=f"crawler-{manifest.config_version}"[:64],
                    status=DatabaseBatchStatus.QUEUED,
                )
                session.add(crawl_run)
                session.flush()

            crawl_run.rule_version_id = rule.id
            crawl_run.profile_id = profile.id
            crawl_run.release_version = f"crawler-{manifest.config_version}"[:64]
            crawl_run.status = DatabaseBatchStatus.RUNNING
            crawl_run.started_at = (
                crawl_run.started_at if is_resume and crawl_run.started_at else now
            )
            crawl_run.heartbeat_at = now
            crawl_run.completed_at = None
            crawl_run.published_at = None
            crawl_run.diagnostic = "RESUME_RUNNING" if is_resume else "RUNNING"

            task_counts: dict[str, int] = defaultdict(int)
            for task in manifest.tasks:
                task_counts[task.source_slug] += 1
            for source_slug in manifest.expected_source_slugs:
                extractor = session.scalar(
                    select(ExtractorRun).where(
                        ExtractorRun.crawl_run_id == crawl_run.id,
                        ExtractorRun.source_id == source_slug,
                    )
                )
                if extractor is None:
                    extractor = ExtractorRun(
                        crawl_run_id=crawl_run.id,
                        source_id=source_slug,
                        status=DatabaseExtractorRunStatus.PENDING,
                    )
                    session.add(extractor)
                extractor.status = DatabaseExtractorRunStatus.PENDING
                extractor.expected_searches = task_counts[source_slug]
                extractor.started_at = None
                extractor.completed_at = None
                extractor.runtime_ms = None
                extractor.diagnostic = "RESUME_PENDING" if is_resume else "PENDING"
                if not is_resume:
                    extractor.last_response_at = None
                    extractor.last_success_at = None
                    extractor.attempted_searches = 0
                    extractor.successful_searches = 0
                    extractor.empty_searches = 0
                    extractor.result_searches = 0
                    extractor.physical_requests = 0
                    extractor.offers_found = 0

            logical_cells = _logical_coverage_cells(manifest)
            for (
                source_slug,
                city_id,
                weekend_start,
                direction,
            ), seed in logical_cells.items():
                row = session.scalar(
                    select(CrawlCoverageCell).where(
                        CrawlCoverageCell.crawl_run_id == crawl_run.id,
                        CrawlCoverageCell.source_id == source_slug,
                        CrawlCoverageCell.city_id == city_id,
                        CrawlCoverageCell.travel_window_id == windows[weekend_start].id,
                        CrawlCoverageCell.direction == direction,
                    )
                )
                if row is None:
                    row = CrawlCoverageCell(
                        crawl_run_id=crawl_run.id,
                        source_id=source_slug,
                        city_id=city_id,
                        travel_window_id=windows[weekend_start].id,
                        direction=direction,
                    )
                    session.add(row)
                row.applicable = seed.applicable
                if not is_resume:
                    row.outcome = (
                        DatabaseSearchOutcome(seed.outcome.value)
                        if seed.outcome is not None
                        else None
                    )
                    row.completed_at = seed.completed_at

            resumed: dict[str, TaskExecution] = {}
            prior_attempts: dict[str, int] = {}
            metadata_by_slug = {item.source_slug: item for item in metadata}
            seen_requests: set[tuple[str, str]] = set()
            for task in manifest.tasks:
                request_key = (task.source_slug, task.request.fingerprint)
                if request_key in seen_requests:
                    raise PublicationError(
                        "manifest contains duplicate logical connector requests"
                    )
                seen_requests.add(request_key)
                request = session.scalar(
                    select(ConnectorRequest).where(
                        ConnectorRequest.crawl_run_id == crawl_run.id,
                        ConnectorRequest.source_id == task.source_slug,
                        ConnectorRequest.request_fingerprint
                        == task.request.fingerprint,
                    )
                )
                if request is None:
                    request = ConnectorRequest(
                        crawl_run_id=crawl_run.id,
                        source_id=task.source_slug,
                        request_fingerprint=task.request.fingerprint,
                    )
                    session.add(request)
                if not is_resume:
                    request.attempt_count = 0
                    request.outcome = None
                    request.claimed_at = None
                    request.completed_at = None
                    request.safe_diagnostic = None
                    continue

                prior_attempts[task.task_id] = request.attempt_count or 0
                execution = _reusable_execution(
                    task,
                    request,
                    metadata_by_slug.get(task.source_slug),
                )
                if execution is not None:
                    resumed[task.task_id] = execution

            crawl_run.expected_cells = len(logical_cells)
            _refresh_crawl_counts(session, crawl_run)
            # Flush now so the returned ID and every pending row are guaranteed
            # to be part of this transaction's durable commit.
            session.flush()
            crawl_run_id = crawl_run.id

            # Accessing the mapping ensures source reconciliation happened for
            # every immutable manifest source before this method returns.
            if set(sources) != set(manifest.expected_source_slugs):
                raise PublicationError("source ledger does not match the manifest")

        return ResumePlan(
            crawl_run_id=crawl_run_id,
            resumed_executions=resumed,
            prior_attempts=prior_attempts,
            is_resume=is_resume,
        )

    def record_execution(
        self,
        manifest: BatchManifest,
        execution: TaskExecution,
    ) -> TaskExecution:
        """Commit one connector result before the coordinator accepts it."""

        task = _task_for_execution(manifest, execution)
        now = self._clock()
        _require_aware_clock(now)
        with self._session_factory() as session, session.begin():
            crawl_run = _matching_run(session, manifest)
            request = session.scalar(
                select(ConnectorRequest).where(
                    ConnectorRequest.crawl_run_id == crawl_run.id,
                    ConnectorRequest.source_id == execution.source_slug,
                    ConnectorRequest.request_fingerprint
                    == execution.request_fingerprint,
                )
            )
            if request is None:
                raise PublicationError("connector result has no pre-network ledger row")
            request.attempt_count = max(request.attempt_count or 0, execution.attempts)
            request.outcome = DatabaseSearchOutcome(execution.result.outcome.value)
            if execution.attempts > 0 and request.claimed_at is None:
                request.claimed_at = execution.result.observed_at
            request.completed_at = execution.result.observed_at
            request.safe_diagnostic = execution.result.diagnostic_code

            cell_by_id = {cell.cell_id: cell for cell in manifest.coverage_cells}
            for cell_id in task.coverage_cell_ids:
                cell = cell_by_id[cell_id]
                for direction in _coverage_directions(cell.direction):
                    coverage = _coverage_for_cell(
                        session, crawl_run.id, cell, direction
                    )
                    if coverage is None:
                        raise PublicationError(
                            "connector result has no pre-network coverage row"
                        )
                    if _should_replace_coverage(
                        coverage, execution.result.outcome, execution.result.observed_at
                    ):
                        coverage.outcome = DatabaseSearchOutcome(
                            execution.result.outcome.value
                        )
                        coverage.completed_at = execution.result.observed_at

            extractor = session.scalar(
                select(ExtractorRun).where(
                    ExtractorRun.crawl_run_id == crawl_run.id,
                    ExtractorRun.source_id == execution.source_slug,
                )
            )
            if extractor is None:
                raise PublicationError(
                    "connector result has no pre-network extractor row"
                )
            _refresh_extractor(session, crawl_run, extractor, now, execution)
            crawl_run.status = DatabaseBatchStatus.RUNNING
            crawl_run.heartbeat_at = now
            crawl_run.completed_at = None
            crawl_run.diagnostic = "RUNNING"
            _refresh_crawl_counts(session, crawl_run)

        return execution

    def mark_failed(self, manifest: BatchManifest, diagnostic_code: str) -> None:
        self._mark_terminal(manifest, DatabaseBatchStatus.FAILED, diagnostic_code)

    def mark_abandoned(self, manifest: BatchManifest, diagnostic_code: str) -> None:
        self._mark_terminal(manifest, DatabaseBatchStatus.ABANDONED, diagnostic_code)

    def finalize_failed_without_publication(
        self,
        manifest: BatchManifest,
        report: BatchReport,
        *,
        health_events: Iterable[HealthEvent],
        diagnostic_code: str,
    ) -> None:
        """Terminalize a collected batch while preserving its audit evidence.

        This is intentionally separate from ``mark_failed``. The latter is an
        interruption path and must fail unfinished extractors. Here collection
        has completed and publication is being withheld, so the coordinator's
        source states and health events are the authoritative record.
        """

        validate_diagnostic_code(diagnostic_code)
        if report.manifest_id != manifest.manifest_id:
            raise PublicationError(
                "batch report does not belong to the supplied manifest"
            )
        if report.batch_id != manifest.batch_id:
            raise PublicationError(
                "batch report ID does not match the supplied manifest"
            )
        _validate_source_reports(manifest, report)
        events = tuple(health_events)
        if any(event.batch_id != manifest.batch_id for event in events):
            raise PublicationError("health event belongs to a different batch")

        with self._session_factory() as session, session.begin():
            crawl_run = _matching_run(session, manifest)
            if (
                crawl_run.status is not DatabaseBatchStatus.RUNNING
                or crawl_run.published_at is not None
            ):
                raise PublicationError(
                    "only an unpublished running batch may be terminalized"
                )

            for source_report in report.source_reports:
                _upsert_extractor_run(session, crawl_run, source_report, report)
            _write_health_events(
                session,
                crawl_run_id=crawl_run.id,
                known_source_slugs=frozenset(manifest.expected_source_slugs),
                events=events,
            )

            crawl_run.status = DatabaseBatchStatus.FAILED
            crawl_run.started_at = report.started_at
            crawl_run.heartbeat_at = report.completed_at
            crawl_run.completed_at = report.completed_at
            crawl_run.published_at = None
            crawl_run.diagnostic = diagnostic_code
            _refresh_crawl_counts(session, crawl_run)

    def _mark_terminal(
        self,
        manifest: BatchManifest,
        status: DatabaseBatchStatus,
        diagnostic_code: str,
    ) -> None:
        validate_diagnostic_code(diagnostic_code)
        now = self._clock()
        _require_aware_clock(now)
        with self._session_factory() as session, session.begin():
            crawl_run = _matching_run(session, manifest)
            crawl_run.status = status
            crawl_run.heartbeat_at = now
            crawl_run.completed_at = now
            crawl_run.diagnostic = diagnostic_code
            extractors = session.scalars(
                select(ExtractorRun).where(
                    ExtractorRun.crawl_run_id == crawl_run.id,
                    ExtractorRun.status.in_(
                        {
                            DatabaseExtractorRunStatus.PENDING,
                            DatabaseExtractorRunStatus.RUNNING,
                        }
                    ),
                )
            ).all()
            for extractor in extractors:
                extractor.status = DatabaseExtractorRunStatus.FAILED
                extractor.completed_at = now
                extractor.diagnostic = diagnostic_code


def _logical_coverage_cells(
    manifest: BatchManifest,
) -> dict[tuple[str, str, date, DatabaseDirection], _CoverageSeed]:
    grouped: dict[
        tuple[str, str, date, DatabaseDirection], list[CoverageCell]
    ] = defaultdict(list)
    for cell in manifest.coverage_cells:
        for direction in _coverage_directions(cell.direction):
            key = (
                cell.source_slug,
                cell.destination_city_id,
                cell.weekend_start,
                direction,
            )
            grouped[key].append(cell)

    logical: dict[tuple[str, str, date, DatabaseDirection], _CoverageSeed] = {}
    for key, cells in grouped.items():
        classifications = [
            (cell.preclassified_outcome, cell.classified_at)
            for cell in cells
            if cell.preclassified_outcome is not None
        ]
        # A logical direction is preclassified only when every route/search
        # representation is. Otherwise an actual connector task still owns it.
        if len(classifications) == len(cells):
            outcome, completed_at = min(
                classifications,
                key=lambda item: (
                    _coverage_outcome_priority(item[0]),
                    -(item[1].timestamp() if item[1] is not None else 0),
                ),
            )
        else:
            outcome, completed_at = None, None
        logical[key] = _CoverageSeed(
            applicable=any(cell.applicable for cell in cells),
            outcome=outcome,
            completed_at=completed_at,
        )
    return logical


def _reusable_execution(
    task: ManifestTask,
    request: ConnectorRequest,
    metadata: AdapterMetadata | None,
) -> TaskExecution | None:
    if request.outcome is None or request.completed_at is None:
        return None
    outcome = SearchOutcome(request.outcome.value)
    if outcome not in _REUSABLE_NO_OFFER_OUTCOMES:
        return None
    diagnostic = request.safe_diagnostic
    if not diagnostic:
        return None
    observed_at = _as_aware(request.completed_at)
    return TaskExecution(
        task_id=task.task_id,
        source_slug=task.source_slug,
        coverage_cell_ids=task.coverage_cell_ids,
        request_fingerprint=task.request.fingerprint,
        attempts=request.attempt_count or 0,
        result=ConnectorResult(
            outcome=outcome,
            retryable=False,
            diagnostic_code=diagnostic,
            expected_count=1,
            completed_count=1,
            offers=(),
            parser_version=(
                metadata.parser_version if metadata is not None else "durable-ledger-v1"
            ),
            observed_at=observed_at,
        ),
    )


def _task_for_execution(
    manifest: BatchManifest, execution: TaskExecution
) -> ManifestTask:
    task = next(
        (item for item in manifest.tasks if item.task_id == execution.task_id), None
    )
    if task is None:
        raise PublicationError("connector result references an unknown manifest task")
    if (
        task.source_slug != execution.source_slug
        or task.request.fingerprint != execution.request_fingerprint
        or task.coverage_cell_ids != execution.coverage_cell_ids
    ):
        raise PublicationError("connector result does not match its manifest task")
    return task


def _matching_run(session: Session, manifest: BatchManifest) -> CrawlRun:
    is_synthetic = set(manifest.expected_source_slugs) == {"fixture"}
    crawl_run = session.scalar(
        select(CrawlRun).where(
            CrawlRun.scheduled_for == manifest.scheduled_for,
            CrawlRun.is_synthetic.is_(is_synthetic),
        )
    )
    if crawl_run is None or crawl_run.manifest_version != manifest.manifest_id:
        raise PublicationError("no matching durable daily crawl ledger")
    return crawl_run


def _coverage_for_cell(
    session: Session,
    crawl_run_id: int,
    cell: CoverageCell,
    direction: DatabaseDirection,
) -> CrawlCoverageCell | None:
    return session.scalar(
        select(CrawlCoverageCell)
        .join(TravelWindow, TravelWindow.id == CrawlCoverageCell.travel_window_id)
        .where(
            CrawlCoverageCell.crawl_run_id == crawl_run_id,
            CrawlCoverageCell.source_id == cell.source_slug,
            CrawlCoverageCell.city_id == cell.destination_city_id,
            TravelWindow.friday_date == cell.weekend_start,
            CrawlCoverageCell.direction == direction,
        )
    )


def _should_replace_coverage(
    coverage: CrawlCoverageCell,
    new_outcome: SearchOutcome,
    observed_at: datetime,
) -> bool:
    if coverage.outcome is None:
        return True
    current = SearchOutcome(coverage.outcome.value)
    new_priority = _coverage_outcome_priority(new_outcome)
    current_priority = _coverage_outcome_priority(current)
    if new_priority != current_priority:
        return new_priority < current_priority
    if coverage.completed_at is None:
        return True
    return observed_at >= _as_aware(coverage.completed_at)


def _refresh_extractor(
    session: Session,
    crawl_run: CrawlRun,
    extractor: ExtractorRun,
    now: datetime,
    execution: TaskExecution,
) -> None:
    requests = session.scalars(
        select(ConnectorRequest).where(
            ConnectorRequest.crawl_run_id == crawl_run.id,
            ConnectorRequest.source_id == extractor.source_id,
        )
    ).all()
    source = session.get(Source, extractor.source_id)
    enabled = bool(source and source.enabled)
    outcomes = [
        SearchOutcome(item.outcome.value)
        for item in requests
        if item.outcome is not None
    ]
    response_times = [
        _as_aware(item.completed_at)
        for item in requests
        if item.completed_at is not None and (item.attempt_count or 0) > 0
    ]
    success_times = [
        _as_aware(item.completed_at)
        for item in requests
        if item.completed_at is not None
        and item.outcome is not None
        and (
            SearchOutcome(item.outcome.value) in SUCCESSFUL_SEARCH_OUTCOMES
            or SearchOutcome(item.outcome.value) is SearchOutcome.PARTIAL_SUCCESS
        )
    ]
    extractor.status = DatabaseExtractorRunStatus.RUNNING
    extractor.started_at = extractor.started_at or now
    extractor.completed_at = None
    extractor.attempted_searches = sum(
        (item.attempt_count or 0) > 0 for item in requests
    )
    extractor.physical_requests = sum(item.attempt_count or 0 for item in requests)
    extractor.successful_searches = (
        sum(
            outcome in SUCCESSFUL_SEARCH_OUTCOMES
            or outcome is SearchOutcome.PARTIAL_SUCCESS
            for outcome in outcomes
        )
        if enabled
        else 0
    )
    extractor.empty_searches = outcomes.count(SearchOutcome.SUCCESS_EMPTY)
    extractor.result_searches = outcomes.count(SearchOutcome.SUCCESS_RESULTS)
    extractor.offers_found = max(
        extractor.offers_found or 0, len(execution.result.offers)
    )
    extractor.last_response_at = max(response_times) if response_times else None
    extractor.last_success_at = max(success_times) if success_times else None
    extractor.diagnostic = "RUNNING"


def _refresh_crawl_counts(session: Session, crawl_run: CrawlRun) -> None:
    rows = session.scalars(
        select(CrawlCoverageCell).where(CrawlCoverageCell.crawl_run_id == crawl_run.id)
    ).all()
    crawl_run.completed_cells = sum(row.outcome is not None for row in rows)
    crawl_run.successful_cells = sum(
        row.outcome is not None
        and (
            SearchOutcome(row.outcome.value) in SUCCESSFUL_SEARCH_OUTCOMES
            or SearchOutcome(row.outcome.value) is SearchOutcome.PARTIAL_SUCCESS
        )
        for row in rows
    )


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def _require_aware_clock(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("lifecycle clock must return timezone-aware datetimes")
