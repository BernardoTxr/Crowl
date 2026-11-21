from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Callable, Mapping

from .adapters.base import SourceAdapter
from .adapters.registry import AdapterRegistry
from .health import HealthEventKind, HealthSink, NullHealthSink, health_event
from .manifest import BatchManifest, ManifestTask
from .models import (
    BLOCKING_SEARCH_OUTCOMES,
    SUCCESSFUL_SEARCH_OUTCOMES,
    BatchStatus,
    ConnectorResult,
    ExtractorRunStatus,
    SearchOutcome,
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    maximum_attempts: int = 2
    initial_backoff_seconds: float = 0.05
    maximum_backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be at least one")
        if self.initial_backoff_seconds < 0 or self.maximum_backoff_seconds < 0:
            raise ValueError("retry backoff may not be negative")


@dataclass(frozen=True, slots=True)
class TaskExecution:
    task_id: str
    source_slug: str
    coverage_cell_ids: tuple[str, ...]
    request_fingerprint: str
    attempts: int
    result: ConnectorResult

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "source_slug": self.source_slug,
            "coverage_cell_ids": list(self.coverage_cell_ids),
            "request_fingerprint": self.request_fingerprint,
            "attempts": self.attempts,
            "outcome": self.result.outcome.value,
            "diagnostic_code": self.result.diagnostic_code,
            "offer_count": len(self.result.offers),
            "completed_count": self.result.completed_count,
            "expected_count": self.result.expected_count,
        }


@dataclass(frozen=True, slots=True)
class SourceReport:
    source_slug: str
    state: ExtractorRunStatus
    expected_tasks: int
    attempted_tasks: int
    physical_request_count: int
    completed_tasks: int
    result_bearing_tasks: int
    outcome_counts: Mapping[str, int]
    diagnostic_code: str
    executions: tuple[TaskExecution, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "source_slug": self.source_slug,
            "state": self.state.value,
            "expected_tasks": self.expected_tasks,
            "attempted_tasks": self.attempted_tasks,
            "physical_request_count": self.physical_request_count,
            "completed_tasks": self.completed_tasks,
            "result_bearing_tasks": self.result_bearing_tasks,
            "outcome_counts": dict(self.outcome_counts),
            "diagnostic_code": self.diagnostic_code,
            "executions": [execution.as_dict() for execution in self.executions],
        }


@dataclass(frozen=True, slots=True)
class BatchReport:
    manifest_id: str
    batch_id: str
    status: BatchStatus
    started_at: datetime
    completed_at: datetime
    source_reports: tuple[SourceReport, ...]

    @property
    def result_count(self) -> int:
        return sum(report.result_bearing_tasks for report in self.source_reports)

    @property
    def offer_count(self) -> int:
        return sum(
            len(execution.result.offers)
            for report in self.source_reports
            for execution in report.executions
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "manifest_id": self.manifest_id,
            "batch_id": self.batch_id,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "result_count": self.result_count,
            "offer_count": self.offer_count,
            "sources": [report.as_dict() for report in self.source_reports],
        }


class BatchCoordinator:
    """Executes one immutable daily manifest and emits sanitized ledger events."""

    def __init__(
        self,
        registry: AdapterRegistry,
        *,
        health_sink: HealthSink | None = None,
        retry_policy: RetryPolicy | None = None,
        request_timeout_seconds: float | None = None,
        maximum_concurrent_sources: int = 4,
        maximum_concurrent_requests: int = 8,
        suspicious_empty_minimum: int = 10,
        clock: Callable[[], datetime] | None = None,
        execution_recorder: Callable[[TaskExecution], TaskExecution] | None = None,
    ) -> None:
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        if maximum_concurrent_sources < 1 or maximum_concurrent_requests < 1:
            raise ValueError("concurrency limits must be positive")
        if suspicious_empty_minimum < 1:
            raise ValueError("suspicious-empty minimum must be positive")
        self.registry = registry
        self.health_sink = health_sink or NullHealthSink()
        self.retry_policy = retry_policy or RetryPolicy()
        self.request_timeout_seconds = request_timeout_seconds
        self.maximum_concurrent_sources = maximum_concurrent_sources
        self.maximum_concurrent_requests = maximum_concurrent_requests
        self.suspicious_empty_minimum = suspicious_empty_minimum
        self.clock = clock or (lambda: datetime.now(UTC))
        self.execution_recorder = execution_recorder

    async def run(
        self,
        manifest: BatchManifest,
        *,
        deadline_at: datetime | None = None,
        resumed_executions: Mapping[str, TaskExecution] | None = None,
        prior_attempts: Mapping[str, int] | None = None,
    ) -> BatchReport:
        if deadline_at is not None:
            if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
                raise ValueError("deadline_at must be timezone-aware")

        resumed = dict(resumed_executions or {})
        attempts_before_run = dict(prior_attempts or {})
        self._validate_resume_state(manifest, resumed, attempts_before_run)

        adapters: dict[str, SourceAdapter] = {}
        for source_slug in manifest.expected_source_slugs:
            adapters[source_slug] = self.registry.get(source_slug)

        started_at = self.clock()
        await self.health_sink.write(
            health_event(
                HealthEventKind.BATCH_STARTED,
                batch_id=manifest.batch_id,
                occurred_at=started_at,
                payload={
                    "manifest_id": manifest.manifest_id,
                    "scheduled_for": manifest.scheduled_for.isoformat(),
                    "config_version": manifest.config_version,
                    "expected_sources": len(manifest.expected_source_slugs),
                    "expected_tasks": len(manifest.tasks),
                    "expected_cells": len(manifest.coverage_cells),
                    "status": BatchStatus.RUNNING.value,
                },
            )
        )

        task_groups = {
            source_slug: tuple(
                task for task in manifest.tasks if task.source_slug == source_slug
            )
            for source_slug in manifest.expected_source_slugs
        }
        source_semaphore = asyncio.Semaphore(self.maximum_concurrent_sources)
        request_semaphore = asyncio.Semaphore(self.maximum_concurrent_requests)

        async def guarded_source(source_slug: str) -> SourceReport:
            async with source_semaphore:
                return await self._run_source(
                    manifest,
                    adapters[source_slug],
                    task_groups[source_slug],
                    request_semaphore=request_semaphore,
                    deadline_at=deadline_at,
                    resumed_executions={
                        task_id: execution
                        for task_id, execution in resumed.items()
                        if execution.source_slug == source_slug
                    },
                    prior_attempts={
                        task.task_id: attempts_before_run.get(task.task_id, 0)
                        for task in task_groups[source_slug]
                    },
                )

        source_reports = tuple(
            await asyncio.gather(
                *(guarded_source(slug) for slug in manifest.expected_source_slugs)
            )
        )
        status = _batch_status(source_reports)
        completed_at = self.clock()
        enabled_reports = tuple(
            report
            for report in source_reports
            if report.state is not ExtractorRunStatus.DISABLED
        )
        enabled_expected_tasks = sum(
            report.expected_tasks for report in enabled_reports
        )
        enabled_completed_tasks = sum(
            report.completed_tasks for report in enabled_reports
        )
        await self.health_sink.write(
            health_event(
                HealthEventKind.BATCH_FINISHED,
                batch_id=manifest.batch_id,
                occurred_at=completed_at,
                payload={
                    "manifest_id": manifest.manifest_id,
                    "status": status.value,
                    "enabled_sources": len(enabled_reports),
                    "requested_sources": len(source_reports),
                    "source_scope_ratio": (
                        len(enabled_reports) / len(source_reports)
                        if source_reports
                        else 1.0
                    ),
                    "enabled_expected_tasks": enabled_expected_tasks,
                    "enabled_completed_tasks": enabled_completed_tasks,
                    "execution_coverage": (
                        enabled_completed_tasks / enabled_expected_tasks
                        if enabled_expected_tasks
                        else 1.0
                    ),
                },
            )
        )
        return BatchReport(
            manifest_id=manifest.manifest_id,
            batch_id=manifest.batch_id,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            source_reports=source_reports,
        )

    async def _run_source(
        self,
        manifest: BatchManifest,
        adapter: SourceAdapter,
        tasks: tuple[ManifestTask, ...],
        *,
        request_semaphore: asyncio.Semaphore,
        deadline_at: datetime | None,
        resumed_executions: Mapping[str, TaskExecution],
        prior_attempts: Mapping[str, int],
    ) -> SourceReport:
        metadata = adapter.metadata()
        started_at = self.clock()
        initial_state = (
            ExtractorRunStatus.RUNNING
            if metadata.enabled
            else ExtractorRunStatus.DISABLED
        )
        await self.health_sink.write(
            health_event(
                HealthEventKind.EXTRACTOR_STARTED,
                batch_id=manifest.batch_id,
                source_slug=metadata.source_slug,
                occurred_at=started_at,
                payload={
                    "state": initial_state.value,
                    "enabled": metadata.enabled,
                    "strategy": metadata.strategy.value,
                    "intended_strategy": metadata.intended_strategy.value,
                    "connector_version": metadata.connector_version,
                    "parser_version": metadata.parser_version,
                    "expected_tasks": len(tasks),
                },
            )
        )

        executions_by_id: dict[str, TaskExecution] = {}
        state = initial_state
        diagnostic_code = "SOURCE_DISABLED" if not metadata.enabled else "SOURCE_OK"

        async def accept(execution: TaskExecution, *, resumed: bool) -> TaskExecution:
            if not resumed:
                previous_attempts = prior_attempts.get(execution.task_id, 0)
                if previous_attempts:
                    execution = replace(
                        execution, attempts=previous_attempts + execution.attempts
                    )
                if self.execution_recorder is not None:
                    execution = self.execution_recorder(execution)
            executions_by_id[execution.task_id] = execution
            await self._record_search(manifest, execution, resumed=resumed)
            return execution

        try:
            if metadata.source_slug in manifest.coverage_failed_source_slugs:
                state = ExtractorRunStatus.FAILED
                diagnostic_code = "COVERAGE_DISCOVERY_REJECTED"
            elif not metadata.enabled:
                for task in tasks:
                    execution = resumed_executions.get(task.task_id)
                    if execution is not None:
                        await accept(execution, resumed=True)
                        continue
                    execution = await self._execute_task_once(
                        adapter,
                        task,
                        request_semaphore=request_semaphore,
                        deadline_at=deadline_at,
                    )
                    await accept(replace(execution, attempts=0), resumed=False)
            else:
                canary_tasks = tuple(task for task in tasks if task.is_canary)
                normal_tasks = tuple(task for task in tasks if not task.is_canary)
                for task in canary_tasks:
                    if execution := resumed_executions.get(task.task_id):
                        await accept(execution, resumed=True)
                pending_canaries = tuple(
                    task
                    for task in canary_tasks
                    if task.task_id not in resumed_executions
                )
                canary_executions = await self._execute_canaries(
                    adapter,
                    pending_canaries,
                    request_semaphore=request_semaphore,
                    deadline_at=deadline_at,
                )
                for execution in canary_executions:
                    await accept(execution, resumed=False)

                canary_outcomes = tuple(
                    executions_by_id[task.task_id].result.outcome
                    for task in canary_tasks
                )
                abort_source = bool(canary_outcomes) and all(
                    outcome in BLOCKING_SEARCH_OUTCOMES
                    or outcome in {
                        SearchOutcome.PARSE_SCHEMA_ERROR,
                        SearchOutcome.CONFIGURATION_ERROR,
                        SearchOutcome.RATE_LIMITED,
                    }
                    for outcome in canary_outcomes
                )
                if abort_source:
                    if SearchOutcome.RATE_LIMITED in canary_outcomes:
                        state = ExtractorRunStatus.BLOCKED
                        diagnostic_code = "CANARY_RATE_LIMITED"
                    elif SearchOutcome.CONFIGURATION_ERROR in canary_outcomes:
                        state = ExtractorRunStatus.FAILED
                        diagnostic_code = "CANARY_CONFIGURATION_ERROR"
                    elif all(
                        outcome in BLOCKING_SEARCH_OUTCOMES
                        for outcome in canary_outcomes
                    ):
                        state = ExtractorRunStatus.BLOCKED
                        diagnostic_code = "CANARY_ACCESS_BLOCKED"
                    else:
                        state = ExtractorRunStatus.QUARANTINED
                        diagnostic_code = "CANARY_SCHEMA_FAILURE"
                    for task in normal_tasks:
                        if execution := resumed_executions.get(task.task_id):
                            await accept(execution, resumed=True)
                else:
                    for task in normal_tasks:
                        if execution := resumed_executions.get(task.task_id):
                            await accept(execution, resumed=True)
                    pending_normal_tasks = tuple(
                        task
                        for task in normal_tasks
                        if task.task_id not in resumed_executions
                    )
                    source_requests = asyncio.Semaphore(
                        adapter.maximum_concurrent_searches
                        or self.maximum_concurrent_requests
                    )
                    access_restricted = False

                    async def guarded_search(task: ManifestTask) -> TaskExecution | None:
                        nonlocal access_restricted
                        async with source_requests:
                            if access_restricted:
                                return None
                            execution = await self._execute_task_with_retry(
                                adapter,
                                task,
                                request_semaphore=request_semaphore,
                                deadline_at=deadline_at,
                            )
                            outcome = execution.result.outcome
                            if (
                                outcome in BLOCKING_SEARCH_OUTCOMES
                                or outcome is SearchOutcome.RATE_LIMITED
                            ):
                                access_restricted = True
                            return execution

                    searches = tuple(
                        asyncio.create_task(guarded_search(task))
                        for task in pending_normal_tasks
                    )
                    try:
                        for search in searches:
                            if execution := await search:
                                await accept(execution, resumed=False)
                    finally:
                        for search in searches:
                            if not search.done():
                                search.cancel()
                        await asyncio.gather(*searches, return_exceptions=True)
                    executions = [
                        executions_by_id[task.task_id]
                        for task in tasks
                        if task.task_id in executions_by_id
                    ]
                    state, diagnostic_code = self._classify_source(
                        expected_tasks=len(tasks), executions=executions
                    )
        finally:
            try:
                await adapter.close()
            except Exception:
                if state is ExtractorRunStatus.SUCCEEDED:
                    state = ExtractorRunStatus.PARTIAL
                diagnostic_code = "ADAPTER_CLOSE_FAILED"

        executions = [
            executions_by_id[task.task_id]
            for task in tasks
            if task.task_id in executions_by_id
        ]
        counts = Counter(execution.result.outcome.value for execution in executions)
        report = SourceReport(
            source_slug=metadata.source_slug,
            state=state,
            expected_tasks=len(tasks),
            attempted_tasks=sum(execution.attempts > 0 for execution in executions),
            physical_request_count=sum(execution.attempts for execution in executions),
            completed_tasks=sum(
                execution.result.completed_count > 0 for execution in executions
            ),
            result_bearing_tasks=sum(
                bool(execution.result.offers) for execution in executions
            ),
            outcome_counts=dict(sorted(counts.items())),
            diagnostic_code=diagnostic_code,
            executions=tuple(executions),
        )
        await self.health_sink.write(
            health_event(
                HealthEventKind.EXTRACTOR_FINISHED,
                batch_id=manifest.batch_id,
                source_slug=metadata.source_slug,
                occurred_at=self.clock(),
                payload={
                    "state": report.state.value,
                    "expected_tasks": report.expected_tasks,
                    "attempted_tasks": report.attempted_tasks,
                    "physical_request_count": report.physical_request_count,
                    "completed_tasks": report.completed_tasks,
                    "result_bearing_tasks": report.result_bearing_tasks,
                    "outcome_counts": dict(report.outcome_counts),
                    "diagnostic_code": report.diagnostic_code,
                },
            )
        )
        await self.health_sink.write(
            health_event(
                HealthEventKind.BATCH_HEARTBEAT,
                batch_id=manifest.batch_id,
                occurred_at=self.clock(),
                payload={
                    "completed_source": metadata.source_slug,
                    "completed_sources_increment": 1,
                },
            )
        )
        return report

    async def _execute_canaries(
        self,
        adapter: SourceAdapter,
        tasks: tuple[ManifestTask, ...],
        *,
        request_semaphore: asyncio.Semaphore,
        deadline_at: datetime | None,
    ) -> tuple[TaskExecution, ...]:
        if not tasks:
            return ()
        if self._deadline_reached(deadline_at):
            return tuple(self._deadline_execution(task, adapter) for task in tasks)
        try:
            async with request_semaphore:
                if self._deadline_reached(deadline_at):
                    return tuple(self._deadline_execution(task, adapter) for task in tasks)
                results = await asyncio.wait_for(
                    adapter.canary(tuple(task.request for task in tasks)),
                    timeout=self._effective_timeout(adapter, deadline_at, len(tasks)),
                )
        except TimeoutError:
            results = tuple(self._timeout_result(adapter) for _ in tasks)
        except Exception:
            results = tuple(self._upstream_error_result(adapter) for _ in tasks)
        if len(results) != len(tasks):
            results = tuple(self._configuration_error_result(adapter) for _ in tasks)
        return tuple(
            TaskExecution(
                task_id=task.task_id,
                source_slug=task.source_slug,
                coverage_cell_ids=task.coverage_cell_ids,
                request_fingerprint=task.request.fingerprint,
                attempts=1,
                result=result,
            )
            for task, result in zip(tasks, results, strict=True)
        )

    async def _execute_task_once(
        self,
        adapter: SourceAdapter,
        task: ManifestTask,
        *,
        request_semaphore: asyncio.Semaphore,
        deadline_at: datetime | None,
    ) -> TaskExecution:
        if self._deadline_reached(deadline_at):
            return self._deadline_execution(task, adapter)
        try:
            async with request_semaphore:
                if self._deadline_reached(deadline_at):
                    return self._deadline_execution(task, adapter)
                result = await asyncio.wait_for(
                    adapter.search(task.request),
                    timeout=self._effective_timeout(adapter, deadline_at),
                )
        except TimeoutError:
            result = self._timeout_result(adapter)
        except Exception:
            result = self._upstream_error_result(adapter)
        return TaskExecution(
            task_id=task.task_id,
            source_slug=task.source_slug,
            coverage_cell_ids=task.coverage_cell_ids,
            request_fingerprint=task.request.fingerprint,
            attempts=1,
            result=result,
        )

    async def _execute_task_with_retry(
        self,
        adapter: SourceAdapter,
        task: ManifestTask,
        *,
        request_semaphore: asyncio.Semaphore,
        deadline_at: datetime | None,
    ) -> TaskExecution:
        attempts = 0
        result: ConnectorResult | None = None
        for attempt in range(1, self.retry_policy.maximum_attempts + 1):
            execution = await self._execute_task_once(
                adapter,
                task,
                request_semaphore=request_semaphore,
                deadline_at=deadline_at,
            )
            attempts += execution.attempts
            result = execution.result
            if (
                not result.retryable
                or result.outcome is SearchOutcome.RATE_LIMITED
                or self._deadline_reached(deadline_at)
            ):
                break
            if attempt < self.retry_policy.maximum_attempts:
                delay = min(
                    self.retry_policy.initial_backoff_seconds * (2 ** (attempt - 1)),
                    self.retry_policy.maximum_backoff_seconds,
                )
                await asyncio.sleep(delay)
        assert result is not None
        return TaskExecution(
            task_id=task.task_id,
            source_slug=task.source_slug,
            coverage_cell_ids=task.coverage_cell_ids,
            request_fingerprint=task.request.fingerprint,
            attempts=attempts,
            result=result,
        )

    async def _record_search(
        self,
        manifest: BatchManifest,
        execution: TaskExecution,
        *,
        resumed: bool = False,
    ) -> None:
        result = execution.result
        await self.health_sink.write(
            health_event(
                HealthEventKind.SEARCH_FINISHED,
                batch_id=manifest.batch_id,
                source_slug=execution.source_slug,
                occurred_at=result.observed_at,
                payload={
                    "task_id": execution.task_id,
                    "request_fingerprint": execution.request_fingerprint,
                    "coverage_cell_ids": list(execution.coverage_cell_ids),
                    "attempts": execution.attempts,
                    "outcome": result.outcome.value,
                    "retryable": result.retryable,
                    "diagnostic_code": result.diagnostic_code,
                    "expected_count": result.expected_count,
                    "completed_count": result.completed_count,
                    "offer_count": len(result.offers),
                    "parser_version": result.parser_version,
                    "response_fingerprint": result.response_fingerprint,
                    "schema_fingerprint": result.schema_fingerprint,
                    "resumed": resumed,
                },
            )
        )

    def _validate_resume_state(
        self,
        manifest: BatchManifest,
        resumed_executions: Mapping[str, TaskExecution],
        prior_attempts: Mapping[str, int],
    ) -> None:
        tasks = {task.task_id: task for task in manifest.tasks}
        if unknown := (set(resumed_executions) | set(prior_attempts)) - set(tasks):
            raise ValueError(
                "resume state references unknown tasks: " + ", ".join(sorted(unknown))
            )
        if any(value < 0 for value in prior_attempts.values()):
            raise ValueError("prior attempt counts may not be negative")
        for task_id, execution in resumed_executions.items():
            task = tasks[task_id]
            if (
                execution.task_id != task_id
                or execution.source_slug != task.source_slug
                or execution.coverage_cell_ids != task.coverage_cell_ids
                or execution.request_fingerprint != task.request.fingerprint
            ):
                raise ValueError(
                    f"resumed execution does not match manifest task: {task_id}"
                )
            if prior_attempts.get(task_id, execution.attempts) != execution.attempts:
                raise ValueError(
                    f"resumed execution attempt count is inconsistent: {task_id}"
                )

    def _classify_source(
        self, *, expected_tasks: int, executions: list[TaskExecution]
    ) -> tuple[ExtractorRunStatus, str]:
        if expected_tasks == 0:
            return ExtractorRunStatus.SUCCEEDED, "NO_APPLICABLE_TASKS"
        outcomes = tuple(execution.result.outcome for execution in executions)
        if len(outcomes) >= self.suspicious_empty_minimum and all(
            outcome is SearchOutcome.SUCCESS_EMPTY for outcome in outcomes
        ):
            return ExtractorRunStatus.QUARANTINED, "SUSPICIOUS_EMPTY_COLLAPSE"
        if outcomes and all(
            outcome in SUCCESSFUL_SEARCH_OUTCOMES for outcome in outcomes
        ):
            return ExtractorRunStatus.SUCCEEDED, "SOURCE_OK"
        if outcomes and all(
            outcome in BLOCKING_SEARCH_OUTCOMES for outcome in outcomes
        ):
            return ExtractorRunStatus.BLOCKED, "SOURCE_ACCESS_BLOCKED"
        has_success = any(
            outcome in SUCCESSFUL_SEARCH_OUTCOMES
            or outcome is SearchOutcome.PARTIAL_SUCCESS
            for outcome in outcomes
        )
        if SearchOutcome.RATE_LIMITED in outcomes:
            if has_success:
                return ExtractorRunStatus.PARTIAL, "SOURCE_RATE_LIMITED"
            return ExtractorRunStatus.BLOCKED, "SOURCE_RATE_LIMITED"
        if has_success:
            return ExtractorRunStatus.PARTIAL, "SOURCE_PARTIAL"
        return ExtractorRunStatus.FAILED, "SOURCE_FAILED"

    def _deadline_reached(self, deadline_at: datetime | None) -> bool:
        return deadline_at is not None and self.clock() >= deadline_at

    def _effective_timeout(
        self,
        adapter: SourceAdapter,
        deadline_at: datetime | None,
        search_count: int = 1,
    ) -> float:
        timeout = self.request_timeout_seconds
        if timeout is None:
            timeout = (adapter.search_timeout_seconds or 30.0) * search_count
        if deadline_at is None:
            return timeout
        remaining = (deadline_at - self.clock()).total_seconds()
        return max(0.001, min(timeout, remaining))

    def _deadline_execution(
        self, task: ManifestTask, adapter: SourceAdapter
    ) -> TaskExecution:
        result = ConnectorResult.failure(
            outcome=SearchOutcome.CANCELLED_DEADLINE,
            diagnostic_code="BATCH_DEADLINE_REACHED",
            parser_version=adapter.metadata().parser_version,
            observed_at=self.clock(),
            retryable=False,
        )
        return TaskExecution(
            task_id=task.task_id,
            source_slug=task.source_slug,
            coverage_cell_ids=task.coverage_cell_ids,
            request_fingerprint=task.request.fingerprint,
            attempts=0,
            result=result,
        )

    def _timeout_result(self, adapter: SourceAdapter) -> ConnectorResult:
        return ConnectorResult.failure(
            outcome=SearchOutcome.TIMEOUT,
            diagnostic_code="REQUEST_TIMEOUT",
            parser_version=adapter.metadata().parser_version,
            observed_at=self.clock(),
        )

    def _upstream_error_result(self, adapter: SourceAdapter) -> ConnectorResult:
        return ConnectorResult.failure(
            outcome=SearchOutcome.UPSTREAM_ERROR,
            diagnostic_code="ADAPTER_UNHANDLED_ERROR",
            parser_version=adapter.metadata().parser_version,
            observed_at=self.clock(),
        )

    def _configuration_error_result(self, adapter: SourceAdapter) -> ConnectorResult:
        return ConnectorResult.failure(
            outcome=SearchOutcome.CONFIGURATION_ERROR,
            diagnostic_code="CANARY_RESULT_COUNT_MISMATCH",
            parser_version=adapter.metadata().parser_version,
            observed_at=self.clock(),
            retryable=False,
        )


def _batch_status(reports: tuple[SourceReport, ...]) -> BatchStatus:
    enabled = tuple(
        report for report in reports if report.state is not ExtractorRunStatus.DISABLED
    )
    if not enabled:
        return BatchStatus.SUCCEEDED
    if all(report.state is ExtractorRunStatus.SUCCEEDED for report in enabled):
        return BatchStatus.SUCCEEDED
    if all(
        report.state
        in {
            ExtractorRunStatus.FAILED,
            ExtractorRunStatus.BLOCKED,
            ExtractorRunStatus.QUARANTINED,
        }
        for report in enabled
    ):
        return BatchStatus.FAILED
    return BatchStatus.PARTIAL
