from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from crowl_core.catalog import ensure_default_profile_and_rules
from crowl_core.composition import (
    OneWayCandidate,
    compose_round_trips,
    itinerary_identity,
)
from crowl_core.enums import (
    Availability,
    BatchStatus as DatabaseBatchStatus,
    Direction as DatabaseDirection,
    ExecutionCoverage,
    ExtractorRunStatus as DatabaseExtractorRunStatus,
    PriceCompleteness as DatabasePriceCompleteness,
    PricingSemantics as DatabasePricingSemantics,
    ProtectionState,
    SearchOutcome as DatabaseSearchOutcome,
    TransportMode as DatabaseTransportMode,
)
from crowl_core.models import (
    City,
    CityTerminalLink,
    ConnectorRequest,
    CrawlCoverageCell,
    CrawlRun,
    DurationHistogramDaily,
    ExtractorRun,
    FareCellObservation,
    FareSnapshot,
    FareSnapshotRevision,
    HealthEvent as DatabaseHealthEvent,
    OriginTerminal,
    SnapshotBooking,
    SnapshotLeg,
    Source,
    Terminal,
    TerminalTransfer,
    TravelWindow,
)
from crowl_core.travel_windows import TravelWindowSpec
from crowl_core.validation import (
    Itinerary,
    Segment as ValidatedSegment,
    TripCandidate,
    TripValidationError,
    TransferRule,
    ValidationContext,
    select_best,
    validate_trip,
)

from .batch import BatchReport, SourceReport, TaskExecution
from .health import HealthEvent
from .manifest import BatchManifest, CoverageCell, ManifestTask
from .models import (
    SUCCESSFUL_SEARCH_OUTCOMES,
    AdapterMetadata,
    Direction as CrawlerDirection,
    ExtractorRunStatus,
    NormalizedOffer,
    PriceCompleteness,
    PricingSemantics,
    SearchKind,
    SearchOutcome,
    Segment,
)
from .trust import (
    OfferRejected,
    OfferTrustPolicy,
    TrustedBookingComponent,
)


FIXTURE_SOURCE_SLUG = "fixture"


class PublicationError(RuntimeError):
    """The collected batch cannot be safely published."""


@dataclass(frozen=True, slots=True)
class PublicationResult:
    crawl_run_id: int
    snapshots_created: int
    revisions_created: int
    snapshots_unchanged: int
    coverage_cells_written: int
    observations_written: int
    health_events_written: int

    def as_dict(self) -> dict[str, int]:
        return {
            "crawl_run_id": self.crawl_run_id,
            "snapshots_created": self.snapshots_created,
            "revisions_created": self.revisions_created,
            "snapshots_unchanged": self.snapshots_unchanged,
            "coverage_cells_written": self.coverage_cells_written,
            "observations_written": self.observations_written,
            "health_events_written": self.health_events_written,
        }


@dataclass(frozen=True, slots=True)
class _ValidatedOffer:
    offer: NormalizedOffer
    task: ManifestTask
    candidate: TripCandidate
    bookings: tuple[TrustedBookingComponent, ...]
    travel_window: TravelWindow
    outbound_booking_index: int | None
    inbound_booking_index: int | None


@dataclass(frozen=True, slots=True)
class _DirectionalOffer:
    offer: NormalizedOffer
    task: ManifestTask
    candidate: OneWayCandidate
    bookings: tuple[TrustedBookingComponent, ...]
    travel_window: TravelWindow


@dataclass(frozen=True, slots=True)
class _PublicationQuality:
    execution_coverage: ExecutionCoverage
    source_scope_ratio: float


class DatabasePublisher:
    """Atomically map one completed manifest into Crowl's shared SQL schema.

    Provider collection happens before this boundary. Every ledger record and
    published fare is then committed together, so a validation, constraint, or
    database failure cannot leave a partially visible daily batch.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        trust_policy: OfferTrustPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._trust_policy = trust_policy or OfferTrustPolicy.from_source_catalog()

    def publish(
        self,
        manifest: BatchManifest,
        report: BatchReport,
        *,
        health_events: Iterable[HealthEvent] = (),
        source_metadata: Iterable[AdapterMetadata] = (),
        require_at_least_one_winner: bool = False,
    ) -> PublicationResult:
        if report.manifest_id != manifest.manifest_id:
            raise PublicationError(
                "batch report does not belong to the supplied manifest"
            )
        if report.batch_id != manifest.batch_id:
            raise PublicationError(
                "batch report ID does not match the supplied manifest"
            )

        events = tuple(health_events)
        metadata = tuple(source_metadata)
        if any(event.batch_id != manifest.batch_id for event in events):
            raise PublicationError("health event belongs to a different batch")
        if len({item.source_slug for item in metadata}) != len(metadata):
            raise PublicationError("source metadata contains duplicate slugs")

        with self._session_factory() as session, session.begin():
            return self._publish(
                session,
                manifest,
                report,
                events,
                metadata,
                require_at_least_one_winner=require_at_least_one_winner,
            )

    def _publish(
        self,
        session: Session,
        manifest: BatchManifest,
        report: BatchReport,
        health_events: tuple[HealthEvent, ...],
        source_metadata: tuple[AdapterMetadata, ...],
        *,
        require_at_least_one_winner: bool,
    ) -> PublicationResult:
        _acquire_publication_lock(session, manifest.scheduled_for)
        _validate_source_reports(manifest, report)
        sources = _sync_sources(session, manifest, source_metadata)
        profile, rule = ensure_default_profile_and_rules(session)
        windows = _ensure_travel_windows(session, manifest)
        _validate_catalog_contract(session, manifest, rule.id)

        crawl_run = _upsert_crawl_run(
            session,
            manifest=manifest,
            report=report,
            profile_id=profile.id,
            rule_version_id=rule.id,
        )
        for source_report in report.source_reports:
            _upsert_extractor_run(session, crawl_run, source_report, report)
            _upsert_connector_requests(session, crawl_run, source_report, report)

        coverage_written = _upsert_coverage(
            session,
            crawl_run=crawl_run,
            manifest=manifest,
            report=report,
            windows=windows,
        )
        validated, trust_failures = _validated_offers(
            session,
            manifest=manifest,
            report=report,
            windows=windows,
            rule_version_id=rule.id,
            known_source_slugs=frozenset(sources),
            trust_policy=self._trust_policy,
        )
        winners = _select_daily_winners(validated)
        if require_at_least_one_winner and not winners:
            raise PublicationError(
                "publication requires at least one trusted, valid fare winner"
            )
        created, revisions, unchanged = _publish_winners(
            session,
            winners=winners,
            crawl_run=crawl_run,
            profile_id=profile.id,
            rule_version_id=rule.id,
            observed_on=manifest.scheduled_for,
            report=report,
            sources=sources,
        )
        _upsert_duration_histograms(
            session,
            offers=validated,
            observed_on=manifest.scheduled_for,
            is_synthetic=crawl_run.is_synthetic,
        )
        observations_written = _upsert_cell_observations(
            session,
            crawl_run=crawl_run,
            manifest=manifest,
            report=report,
            windows=windows,
            winners=winners,
            profile_id=profile.id,
            rule_version_id=rule.id,
            sources=sources,
            trust_failures=trust_failures,
        )
        health_written = _write_health_events(
            session,
            crawl_run_id=crawl_run.id,
            known_source_slugs=frozenset(sources),
            events=health_events,
        )

        _finalize_crawl_run(session, crawl_run, manifest, report)
        session.flush()
        return PublicationResult(
            crawl_run_id=crawl_run.id,
            snapshots_created=created,
            revisions_created=revisions,
            snapshots_unchanged=unchanged,
            coverage_cells_written=coverage_written,
            observations_written=observations_written,
            health_events_written=health_written,
        )


def _acquire_publication_lock(session: Session, scheduled_for: date) -> None:
    """Serialize same-day publication on PostgreSQL.

    SQLite remains lock-free for unit tests. The database uniqueness constraints
    are still the final guard, while PostgreSQL's transaction-scoped advisory
    lock prevents two collectors from racing while they revise the same cells.
    """

    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        session.execute(
            select(func.pg_advisory_xact_lock(0x43524F57, scheduled_for.toordinal()))
        )


def _validate_source_reports(
    manifest: BatchManifest, report: BatchReport
) -> dict[str, SourceReport]:
    source_reports = {item.source_slug: item for item in report.source_reports}
    if len(source_reports) != len(report.source_reports):
        raise PublicationError("batch report contains duplicate source reports")
    for source_slug in manifest.expected_source_slugs:
        if source_slug not in source_reports:
            raise PublicationError(f"missing source report: {source_slug}")
    if set(source_reports) != set(manifest.expected_source_slugs):
        raise PublicationError("batch report contains an unexpected source")
    return source_reports


def _sync_sources(
    session: Session,
    manifest: BatchManifest,
    metadata: tuple[AdapterMetadata, ...],
) -> dict[str, Source]:
    metadata_by_slug = {item.source_slug: item for item in metadata}
    unexpected_metadata = set(metadata_by_slug) - set(manifest.expected_source_slugs)
    if unexpected_metadata:
        raise PublicationError(
            "source metadata contains sources outside the manifest: "
            + ", ".join(sorted(unexpected_metadata))
        )

    sources: dict[str, Source] = {}
    for slug in manifest.expected_source_slugs:
        if slug == FIXTURE_SOURCE_SLUG:
            sources[slug] = _ensure_fixture_source(session)
            continue

        source = session.get(Source, slug)
        item = metadata_by_slug.get(slug)
        if source is None and item is None:
            raise PublicationError(f"source catalog is missing: {slug}")
        if source is None:
            assert item is not None
            source = Source(slug=slug)
            session.add(source)
        if item is not None:
            source.name = item.display_name
            source.supported_modes = [mode.value for mode in item.modes]
            source.strategy = item.intended_strategy.value
            source.is_requested = True
            source.enabled = item.enabled
            source.disabled_reason = item.disabled_reason
            source.connector_version = item.connector_version
            source.parser_version = item.parser_version
        sources[slug] = source
    session.flush()
    return sources


def _ensure_fixture_source(session: Session) -> Source:
    source = session.get(Source, FIXTURE_SOURCE_SLUG)
    if source is None:
        source = Source(slug=FIXTURE_SOURCE_SLUG)
        session.add(source)
    source.name = "Crowl deterministic fixture"
    source.supported_modes = ["flight", "train", "bus"]
    source.strategy = "fixture"
    source.is_requested = False
    source.enabled = False
    source.disabled_reason = "Synthetic development data only; never a live provider"
    source.connector_version = "fixture-v1"
    source.parser_version = "fixture-v1"
    session.flush()
    return source


def _ensure_travel_windows(
    session: Session, manifest: BatchManifest
) -> dict[date, TravelWindow]:
    windows: dict[date, TravelWindow] = {}
    for friday in sorted({cell.weekend_start for cell in manifest.coverage_cells}):
        model = session.scalar(
            select(TravelWindow).where(TravelWindow.friday_date == friday)
        )
        spec = TravelWindowSpec.for_friday(friday)
        if model is None:
            model = TravelWindow(friday_date=friday)
            session.add(model)
        model.starts_at = spec.starts_at
        model.ends_at = spec.ends_at
        model.timezone = spec.timezone_name
        model.minimum_stay_minutes = spec.minimum_stay_minutes
        model.maximum_direction_minutes = spec.maximum_direction_minutes
        windows[friday] = model
    session.flush()
    return windows


def _validate_catalog_contract(
    session: Session, manifest: BatchManifest, rule_version_id: int
) -> None:
    city_ids = {cell.destination_city_id for cell in manifest.coverage_cells}
    known_cities = set(
        session.scalars(select(City.id).where(City.id.in_(city_ids))).all()
    )
    if missing := city_ids - known_cities:
        raise PublicationError(
            "catalog is missing cities: " + ", ".join(sorted(missing))
        )

    requested_terminal_ids = {
        terminal_id
        for task in manifest.tasks
        for terminal_id in (
            *task.request.origin_terminal_ids,
            *task.request.destination_terminal_ids,
        )
    }
    known_terminal_ids = set(
        session.scalars(
            select(Terminal.id).where(Terminal.id.in_(requested_terminal_ids))
        ).all()
    )
    if missing := requested_terminal_ids - known_terminal_ids:
        raise PublicationError(
            "terminal catalog is missing IDs: " + ", ".join(sorted(missing))
        )

    allowed_origins = set(
        session.scalars(
            select(OriginTerminal.terminal_id).where(
                OriginTerminal.rule_version_id == rule_version_id,
                OriginTerminal.permits_departure.is_(True),
                OriginTerminal.permits_return.is_(True),
            )
        ).all()
    )
    requested_origins = {
        terminal_id
        for task in manifest.tasks
        for terminal_id in task.request.origin_terminal_ids
    }
    if missing := requested_origins - allowed_origins:
        raise PublicationError(
            "manifest origins are not enabled by the active rule version: "
            + ", ".join(sorted(missing))
        )


def _upsert_crawl_run(
    session: Session,
    *,
    manifest: BatchManifest,
    report: BatchReport,
    profile_id: int,
    rule_version_id: int,
) -> CrawlRun:
    is_synthetic = set(manifest.expected_source_slugs) == {FIXTURE_SOURCE_SLUG}
    crawl_run = session.scalar(
        select(CrawlRun).where(
            CrawlRun.scheduled_for == manifest.scheduled_for,
            CrawlRun.is_synthetic.is_(is_synthetic),
        )
    )
    if crawl_run is None:
        crawl_run = CrawlRun(
            scheduled_for=manifest.scheduled_for,
            is_synthetic=is_synthetic,
        )
        session.add(crawl_run)
    crawl_run.rule_version_id = rule_version_id
    crawl_run.profile_id = profile_id
    crawl_run.manifest_version = manifest.manifest_id
    crawl_run.release_version = f"crawler-{manifest.config_version}"[:64]
    crawl_run.status = DatabaseBatchStatus(report.status.value)
    crawl_run.started_at = report.started_at
    crawl_run.heartbeat_at = report.completed_at
    crawl_run.completed_at = report.completed_at
    crawl_run.published_at = report.completed_at
    if is_synthetic:
        crawl_run.diagnostic = "Synthetic fixture batch; no live provider observation"
    else:
        live_sources = sum(
            item.state is not ExtractorRunStatus.DISABLED
            for item in report.source_reports
        )
        crawl_run.diagnostic = (
            f"{live_sources}/{len(report.source_reports)} provider adapters enabled"
        )
    session.flush()
    return crawl_run


def _upsert_extractor_run(
    session: Session,
    crawl_run: CrawlRun,
    report: SourceReport,
    batch_report: BatchReport,
) -> ExtractorRun:
    extractor = session.scalar(
        select(ExtractorRun).where(
            ExtractorRun.crawl_run_id == crawl_run.id,
            ExtractorRun.source_id == report.source_slug,
        )
    )
    if extractor is None:
        extractor = ExtractorRun(
            crawl_run_id=crawl_run.id,
            source_id=report.source_slug,
            status=DatabaseExtractorRunStatus.PENDING,
        )
        session.add(extractor)

    successful = (
        0
        if report.state is ExtractorRunStatus.DISABLED
        else sum(
            count
            for outcome, count in report.outcome_counts.items()
            if SearchOutcome(outcome) in SUCCESSFUL_SEARCH_OUTCOMES
            or SearchOutcome(outcome) is SearchOutcome.PARTIAL_SUCCESS
        )
    )
    response_times = [
        execution.result.observed_at
        for execution in report.executions
        if execution.attempts > 0
    ]
    success_times = (
        []
        if report.state is ExtractorRunStatus.DISABLED
        else [
            execution.result.observed_at
            for execution in report.executions
            if execution.result.outcome in SUCCESSFUL_SEARCH_OUTCOMES
            or execution.result.outcome is SearchOutcome.PARTIAL_SUCCESS
        ]
    )
    prior_failures = _prior_extractor_failure_streak(
        session,
        source_slug=report.source_slug,
        before_scheduled_for=crawl_run.scheduled_for,
        is_synthetic=crawl_run.is_synthetic,
    )
    extractor.status = DatabaseExtractorRunStatus(report.state.value)
    extractor.started_at = batch_report.started_at
    extractor.last_response_at = max(response_times) if response_times else None
    extractor.last_success_at = max(success_times) if success_times else None
    extractor.completed_at = batch_report.completed_at
    extractor.runtime_ms = max(
        0,
        int(
            (batch_report.completed_at - batch_report.started_at).total_seconds() * 1000
        ),
    )
    extractor.consecutive_failures = (
        0
        if report.state in {ExtractorRunStatus.SUCCEEDED, ExtractorRunStatus.DISABLED}
        else prior_failures + 1
    )
    extractor.expected_searches = report.expected_tasks
    extractor.attempted_searches = report.attempted_tasks
    extractor.successful_searches = successful
    extractor.empty_searches = report.outcome_counts.get(
        SearchOutcome.SUCCESS_EMPTY.value, 0
    )
    extractor.result_searches = report.result_bearing_tasks
    extractor.physical_requests = report.physical_request_count
    extractor.offers_found = sum(
        len(execution.result.offers) for execution in report.executions
    )
    extractor.diagnostic = report.diagnostic_code
    session.flush()
    return extractor


def _prior_extractor_failure_streak(
    session: Session,
    *,
    source_slug: str,
    before_scheduled_for: date,
    is_synthetic: bool,
) -> int:
    """Read the latest earlier daily streak, excluding today's ledger row."""

    return (
        session.scalar(
            select(ExtractorRun.consecutive_failures)
            .join(CrawlRun, CrawlRun.id == ExtractorRun.crawl_run_id)
            .where(
                ExtractorRun.source_id == source_slug,
                CrawlRun.scheduled_for < before_scheduled_for,
                CrawlRun.is_synthetic.is_(is_synthetic),
            )
            .order_by(CrawlRun.scheduled_for.desc(), ExtractorRun.id.desc())
            .limit(1)
        )
        or 0
    )


def _upsert_connector_requests(
    session: Session,
    crawl_run: CrawlRun,
    source_report: SourceReport,
    batch_report: BatchReport,
) -> None:
    for execution in source_report.executions:
        request = session.scalar(
            select(ConnectorRequest).where(
                ConnectorRequest.crawl_run_id == crawl_run.id,
                ConnectorRequest.source_id == execution.source_slug,
                ConnectorRequest.request_fingerprint == execution.request_fingerprint,
            )
        )
        if request is None:
            request = ConnectorRequest(
                crawl_run_id=crawl_run.id,
                source_id=execution.source_slug,
                request_fingerprint=execution.request_fingerprint,
            )
            session.add(request)
        request.attempt_count = execution.attempts
        request.outcome = DatabaseSearchOutcome(execution.result.outcome.value)
        request.claimed_at = batch_report.started_at
        request.completed_at = execution.result.observed_at
        request.safe_diagnostic = execution.result.diagnostic_code
    session.flush()


def _execution_by_coverage_cell(
    report: BatchReport,
) -> dict[str, TaskExecution]:
    mapped: dict[str, TaskExecution] = {}
    for source_report in report.source_reports:
        for execution in source_report.executions:
            for cell_id in execution.coverage_cell_ids:
                if cell_id in mapped:
                    raise PublicationError(
                        f"coverage cell executed more than once: {cell_id}"
                    )
                mapped[cell_id] = execution
    return mapped


def _upsert_coverage(
    session: Session,
    *,
    crawl_run: CrawlRun,
    manifest: BatchManifest,
    report: BatchReport,
    windows: dict[date, TravelWindow],
) -> int:
    execution_by_cell = _execution_by_coverage_cell(report)
    manifest_cell_ids = {cell.cell_id for cell in manifest.coverage_cells}
    if unknown := set(execution_by_cell) - manifest_cell_ids:
        raise PublicationError(
            "report references unknown coverage cells: " + ", ".join(sorted(unknown))
        )

    expanded: dict[
        tuple[str, str, date, DatabaseDirection],
        list[tuple[CoverageCell, TaskExecution | None]],
    ] = {}
    for cell in manifest.coverage_cells:
        execution = execution_by_cell.get(cell.cell_id)
        for direction in _coverage_directions(cell.direction):
            key = (
                cell.source_slug,
                cell.destination_city_id,
                cell.weekend_start,
                direction,
            )
            expanded.setdefault(key, []).append((cell, execution))

    for (
        source_slug,
        destination_city_id,
        weekend_start,
        direction,
    ), observations in expanded.items():
        execution = _best_coverage_execution(
            tuple(item[1] for item in observations if item[1] is not None)
        )
        classifications = [
            (cell.preclassified_outcome, cell.classified_at)
            for cell, item_execution in observations
            if item_execution is None and cell.preclassified_outcome is not None
        ]
        classification = (
            min(
                classifications,
                key=lambda item: (
                    _coverage_outcome_priority(item[0]),
                    -(item[1].timestamp() if item[1] is not None else 0),
                ),
            )
            if len(classifications) == len(observations)
            else None
        )
        row = session.scalar(
            select(CrawlCoverageCell).where(
                CrawlCoverageCell.crawl_run_id == crawl_run.id,
                CrawlCoverageCell.source_id == source_slug,
                CrawlCoverageCell.city_id == destination_city_id,
                CrawlCoverageCell.travel_window_id == windows[weekend_start].id,
                CrawlCoverageCell.direction == direction,
            )
        )
        if row is None:
            row = CrawlCoverageCell(
                crawl_run_id=crawl_run.id,
                source_id=source_slug,
                city_id=destination_city_id,
                travel_window_id=windows[weekend_start].id,
                direction=direction,
            )
            session.add(row)
        row.applicable = any(item[0].applicable for item in observations)
        if execution is not None:
            row.outcome = DatabaseSearchOutcome(execution.result.outcome.value)
            row.completed_at = execution.result.observed_at
        elif classification is not None:
            outcome, classified_at = classification
            row.outcome = DatabaseSearchOutcome(outcome.value)
            row.completed_at = classified_at
        else:
            row.outcome = None
            row.completed_at = None
    session.flush()
    return len(expanded)


def _coverage_directions(value: str) -> tuple[DatabaseDirection, ...]:
    if value == "round_trip":
        return (DatabaseDirection.OUTBOUND, DatabaseDirection.RETURN)
    try:
        return (DatabaseDirection(value),)
    except ValueError as error:
        raise PublicationError(
            f"unknown manifest coverage direction: {value}"
        ) from error


def _best_coverage_execution(
    executions: tuple[TaskExecution, ...],
) -> TaskExecution | None:
    if not executions:
        return None
    return min(
        executions,
        key=lambda item: (
            _coverage_outcome_priority(item.result.outcome),
            -item.result.observed_at.timestamp(),
            item.task_id,
        ),
    )


def _coverage_outcome_priority(outcome: SearchOutcome) -> int:
    if outcome is SearchOutcome.SUCCESS_RESULTS:
        return 0
    if outcome is SearchOutcome.PARTIAL_SUCCESS:
        return 1
    if outcome in {
        SearchOutcome.SUCCESS_EMPTY,
        SearchOutcome.NOT_ON_SALE,
        SearchOutcome.NO_SERVICE,
        SearchOutcome.SOLD_OUT,
        SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE,
    }:
        return 2
    if outcome is SearchOutcome.SKIPPED_DISABLED:
        return 4
    return 3


def _validated_offers(
    session: Session,
    *,
    manifest: BatchManifest,
    report: BatchReport,
    windows: dict[date, TravelWindow],
    rule_version_id: int,
    known_source_slugs: frozenset[str],
    trust_policy: OfferTrustPolicy,
) -> tuple[
    tuple[_ValidatedOffer, ...],
    dict[tuple[str, int, DatabaseTransportMode], frozenset[str]],
]:
    tasks = {task.task_id: task for task in manifest.tasks}
    allowed_origins = frozenset(
        session.scalars(
            select(OriginTerminal.terminal_id).where(
                OriginTerminal.rule_version_id == rule_version_id,
                OriginTerminal.permits_departure.is_(True),
                OriginTerminal.permits_return.is_(True),
            )
        ).all()
    )
    schengen_terminals = frozenset(
        session.scalars(
            select(Terminal.id).where(Terminal.schengen_eligible.is_(True))
        ).all()
    )
    destination_terminals = {
        city_id: frozenset(
            session.scalars(
                select(CityTerminalLink.terminal_id).where(
                    CityTerminalLink.city_id == city_id
                )
            ).all()
        )
        for city_id in {cell.destination_city_id for cell in manifest.coverage_cells}
    }
    transfer_rules = tuple(
        TransferRule(
            from_terminal_id=row.from_terminal_id,
            to_terminal_id=row.to_terminal_id,
            minimum_minutes=row.minimum_minutes,
            cost_eur_cents=row.cost_eur_cents,
            cost_is_complete=row.cost_is_complete,
        )
        for row in session.scalars(
            select(TerminalTransfer).order_by(
                TerminalTransfer.from_terminal_id,
                TerminalTransfer.to_terminal_id,
            )
        ).all()
    )

    validated: list[_ValidatedOffer] = []
    trust_failures: dict[tuple[str, int, DatabaseTransportMode], set[str]] = {}
    directional: dict[
        tuple[str, int],
        dict[CrawlerDirection, list[_DirectionalOffer]],
    ] = {}
    contexts: dict[tuple[str, int], ValidationContext] = {}
    for source_report in report.source_reports:
        for execution in source_report.executions:
            if execution.source_slug != source_report.source_slug:
                raise PublicationError(
                    "execution source does not match its source report"
                )
            task = tasks.get(execution.task_id)
            if task is None:
                raise PublicationError(
                    f"report references unknown task: {execution.task_id}"
                )
            if task.source_slug != execution.source_slug:
                raise PublicationError(
                    "execution source does not match its manifest task"
                )
            for offer in execution.result.offers:
                if offer.source_slug != execution.source_slug:
                    raise PublicationError(
                        "offer source does not match its executed adapter"
                    )
                if offer.destination_city_id != task.request.destination_city_id:
                    raise PublicationError(
                        "offer destination does not match its manifest task"
                    )
                if not destination_terminals[offer.destination_city_id]:
                    raise PublicationError(
                        f"city has no terminal links: {offer.destination_city_id}"
                    )
                for component in offer.booking_components:
                    if component.source_slug not in known_source_slugs:
                        raise PublicationError(
                            "booking component references a source outside the manifest"
                        )
                    if component.source_slug != offer.source_slug:
                        raise PublicationError(
                            "an individual offer must be attributed to its adapter"
                        )

                window = windows[task.request.weekend_start]
                try:
                    trusted = trust_policy.validate(
                        session,
                        offer=offer,
                        passenger=task.request.passenger,
                    )
                except OfferRejected as error:
                    # Untrusted offers remain represented in connector counts,
                    # but can never compete for an advertised best fare.
                    if error.code in _OPERATIONAL_TRUST_FAILURE_CODES:
                        key = (
                            offer.destination_city_id,
                            window.id,
                            _offer_mode_bucket(offer),
                        )
                        trust_failures.setdefault(key, set()).add(error.code)
                    continue

                context = ValidationContext(
                    travel_window=_window_spec(window),
                    allowed_origin_terminal_ids=allowed_origins,
                    destination_terminal_ids=destination_terminals[
                        offer.destination_city_id
                    ],
                    schengen_terminal_ids=schengen_terminals,
                    strict_schengen_path=True,
                    transfer_rules=transfer_rules,
                )
                if task.request.search_kind is SearchKind.ROUND_TRIP:
                    candidate = _as_trip_candidate(
                        offer,
                        task,
                        total_eur_cents=trusted.total_eur_cents,
                        bookings=trusted.bookings,
                    )
                    try:
                        validate_trip(candidate, context)
                    except TripValidationError as error:
                        raise PublicationError(
                            f"offer {offer.provider_offer_id} violates travel rules: "
                            f"{error}"
                        ) from error
                    if not trusted.bookings:
                        outbound_booking_index = None
                        inbound_booking_index = None
                    elif len(trusted.bookings) == 1:
                        outbound_booking_index = 0
                        inbound_booking_index = 0
                    elif (
                        offer.pricing_semantics is PricingSemantics.COMPOSABLE
                        and len(trusted.bookings) == 2
                    ):
                        outbound_booking_index = 0
                        inbound_booking_index = 1
                    else:
                        raise PublicationError(
                            "round-trip booking components cannot be mapped "
                            "unambiguously to itinerary directions"
                        )
                    validated.append(
                        _ValidatedOffer(
                            offer=offer,
                            task=task,
                            candidate=candidate,
                            bookings=trusted.bookings,
                            travel_window=window,
                            outbound_booking_index=outbound_booking_index,
                            inbound_booking_index=inbound_booking_index,
                        )
                    )
                    continue

                if task.request.direction is None:
                    raise PublicationError("one-way task is missing its direction")
                if offer.return_segments:
                    raise PublicationError(
                        "a one-way task returned an atomic round-trip offer"
                    )
                if offer.pricing_semantics is PricingSemantics.ROUND_TRIP_ONLY:
                    raise PublicationError(
                        "a directional offer cannot use round-trip-only pricing"
                    )
                if not offer.booking_components:
                    raise PublicationError(
                        "a directional offer must be independently bookable"
                    )
                itinerary = Itinerary(
                    direction=DatabaseDirection(task.request.direction.value),
                    segments=tuple(
                        _as_validated_segment(segment)
                        for segment in offer.outbound_segments
                    ),
                )
                one_way = OneWayCandidate(
                    offer_key=_offer_signature(offer, trusted.bookings),
                    source_id=offer.source_slug,
                    itinerary=itinerary,
                    price_eur_cents=trusted.total_eur_cents,
                    observed_at=offer.observed_at,
                    price_complete=(
                        offer.price_completeness is PriceCompleteness.COMPLETE
                    ),
                    mandatory_fees_included=offer.mandatory_fees_included,
                    smallest_personal_item_included=(
                        offer.included_baggage == "smallest_included_personal_item"
                    ),
                    passenger_age=offer.passenger_age,
                    student_eligible=offer.passenger_eligibility_confirmed,
                    included_transfer_cost_eur_cents=(
                        offer.included_transfer_cost_eur_cents
                    ),
                    direct_operator_booking=(
                        len(trusted.bookings) == 1
                        and trusted.bookings[0].component.source_slug
                        == offer.source_slug
                    ),
                    displayed_fare_service_fees_may_apply=(
                        offer.source_slug == "flixbus"
                        and offer.price_completeness is PriceCompleteness.ESTIMATED
                        and not offer.mandatory_fees_included
                    ),
                )
                group_key = (offer.destination_city_id, window.id)
                group = directional.setdefault(
                    group_key,
                    {
                        CrawlerDirection.OUTBOUND: [],
                        CrawlerDirection.RETURN: [],
                    },
                )
                group[task.request.direction].append(
                    _DirectionalOffer(
                        offer=offer,
                        task=task,
                        candidate=one_way,
                        bookings=trusted.bookings,
                        travel_window=window,
                    )
                )
                contexts[group_key] = context

    for group_key, by_direction in directional.items():
        outbound = by_direction[CrawlerDirection.OUTBOUND]
        inbound = by_direction[CrawlerDirection.RETURN]
        if not outbound or not inbound:
            continue
        outbound_by_key = {item.candidate.offer_key: item for item in outbound}
        inbound_by_key = {item.candidate.offer_key: item for item in inbound}
        city_id, _ = group_key
        result = compose_round_trips(
            (item.candidate for item in outbound),
            (item.candidate for item in inbound),
            destination_city_id=city_id,
            validation_context=contexts[group_key],
        )
        for candidates in result.by_mode.values():
            for composed in candidates:
                outbound_item = outbound_by_key[composed.outbound.offer_key]
                inbound_item = inbound_by_key[composed.inbound.offer_key]
                combined_offer = _as_composed_offer(outbound_item, inbound_item)
                validated.append(
                    _ValidatedOffer(
                        offer=combined_offer,
                        task=outbound_item.task,
                        candidate=composed.candidate,
                        bookings=(outbound_item.bookings + inbound_item.bookings),
                        travel_window=outbound_item.travel_window,
                        outbound_booking_index=(0 if outbound_item.bookings else None),
                        inbound_booking_index=(
                            len(outbound_item.bookings)
                            if inbound_item.bookings
                            else None
                        ),
                    )
                )
    return tuple(validated), {
        key: frozenset(codes) for key, codes in trust_failures.items()
    }


_OPERATIONAL_TRUST_FAILURE_CODES = frozenset(
    {
        "invalid_converted_amount",
        "invalid_fx_rate",
        "missing_booking_host_policy",
        "missing_fx_rate",
        "stale_fx_rate",
        "unapproved_booking_host",
        "unsafe_booking_host",
        "unsafe_booking_url",
        "unsupported_currency",
    }
)


def _offer_mode_bucket(offer: NormalizedOffer) -> DatabaseTransportMode:
    modes = {
        DatabaseTransportMode(segment.mode.value)
        for segment in (*offer.outbound_segments, *offer.return_segments)
    }
    if len(modes) == 1:
        return next(iter(modes))
    return DatabaseTransportMode.MIXED


def _as_composed_offer(
    outbound: _DirectionalOffer,
    inbound: _DirectionalOffer,
) -> NormalizedOffer:
    observed_at = max(outbound.offer.observed_at, inbound.offer.observed_at)
    expirations = tuple(
        value
        for value in (outbound.offer.expires_at, inbound.offer.expires_at)
        if value is not None
    )
    key = hashlib.sha256(
        (outbound.candidate.offer_key + ":" + inbound.candidate.offer_key).encode()
    ).hexdigest()
    return NormalizedOffer(
        provider_offer_id=f"composed-{key[:24]}",
        source_slug=(
            f"composed:{outbound.offer.source_slug}:{inbound.offer.source_slug}"
        ),
        destination_city_id=outbound.offer.destination_city_id,
        pricing_semantics=PricingSemantics.COMPOSABLE,
        price_completeness=(
            PriceCompleteness.ESTIMATED
            if PriceCompleteness.ESTIMATED
            in {
                outbound.offer.price_completeness,
                inbound.offer.price_completeness,
            }
            else PriceCompleteness.COMPLETE
        ),
        total_amount_minor=(
            outbound.candidate.price_eur_cents + inbound.candidate.price_eur_cents
        ),
        currency="EUR",
        mandatory_fees_included=(
            outbound.offer.mandatory_fees_included
            and inbound.offer.mandatory_fees_included
        ),
        passenger_age=outbound.offer.passenger_age,
        passenger_student=outbound.offer.passenger_student,
        passenger_eligibility_confirmed=True,
        bookable=True,
        is_teaser=False,
        observed_at=observed_at,
        outbound_segments=outbound.offer.outbound_segments,
        return_segments=inbound.offer.outbound_segments,
        booking_components=(
            outbound.offer.booking_components + inbound.offer.booking_components
        ),
        included_baggage="smallest_included_personal_item",
        included_transfer_cost_eur_cents=(
            outbound.candidate.included_transfer_cost_eur_cents
            + inbound.candidate.included_transfer_cost_eur_cents
        ),
        expires_at=min(expirations) if expirations else None,
    )


def _window_spec(window: TravelWindow) -> TravelWindowSpec:
    return TravelWindowSpec(
        friday=window.friday_date,
        starts_at=_as_aware_utc(window.starts_at),
        ends_at=_as_aware_utc(window.ends_at),
        timezone_name=window.timezone,
        minimum_stay_minutes=window.minimum_stay_minutes,
        maximum_direction_minutes=window.maximum_direction_minutes,
    )


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_trip_candidate(
    offer: NormalizedOffer,
    task: ManifestTask,
    *,
    total_eur_cents: int,
    bookings: tuple[TrustedBookingComponent, ...],
) -> TripCandidate:
    if not offer.return_segments:
        raise PublicationError("only complete round trips can be published")
    return TripCandidate(
        outbound=Itinerary(
            direction=DatabaseDirection.OUTBOUND,
            segments=tuple(
                _as_validated_segment(segment) for segment in offer.outbound_segments
            ),
        ),
        inbound=Itinerary(
            direction=DatabaseDirection.RETURN,
            segments=tuple(
                _as_validated_segment(segment) for segment in offer.return_segments
            ),
        ),
        destination_city_id=offer.destination_city_id,
        total_price_eur_cents=total_eur_cents,
        price_complete=offer.price_completeness is PriceCompleteness.COMPLETE,
        passenger_age=offer.passenger_age,
        student_eligible=offer.passenger_eligibility_confirmed,
        mandatory_fees_included=offer.mandatory_fees_included,
        smallest_personal_item_included=(
            offer.included_baggage == "smallest_included_personal_item"
        ),
        included_transfer_cost_eur_cents=(offer.included_transfer_cost_eur_cents),
        itinerary_key=_offer_signature(offer, bookings),
        displayed_fare_service_fees_may_apply=(
            offer.source_slug == "flixbus"
            and offer.price_completeness is PriceCompleteness.ESTIMATED
            and not offer.mandatory_fees_included
        ),
    )


def _as_validated_segment(segment: Segment) -> ValidatedSegment:
    return ValidatedSegment(
        origin_terminal_id=segment.departure_terminal_id,
        destination_terminal_id=segment.arrival_terminal_id,
        departs_at=segment.departs_at,
        arrives_at=segment.arrives_at,
        mode=DatabaseTransportMode(segment.mode.value),
        operator=segment.operator,
        service_number=segment.service_number,
        protection_state=(
            ProtectionState.PROTECTED
            if segment.protected_connection
            else ProtectionState.UNPROTECTED_TRANSFER
        ),
    )


def _offer_signature(
    offer: NormalizedOffer,
    bookings: tuple[TrustedBookingComponent, ...] = (),
) -> str:
    def segment_payload(segment: Segment) -> dict[str, object]:
        return {
            "sequence": segment.sequence,
            "mode": segment.mode.value,
            "operator": segment.operator,
            "service_number": segment.service_number,
            "origin": segment.departure_terminal_id,
            "destination": segment.arrival_terminal_id,
            "departs_at": segment.departs_at.isoformat(),
            "arrives_at": segment.arrives_at.isoformat(),
            "protected": segment.protected_connection,
        }

    payload = {
        "provider_offer_id": offer.provider_offer_id,
        "source": offer.source_slug,
        "destination": offer.destination_city_id,
        "pricing_semantics": offer.pricing_semantics.value,
        "amount": offer.total_amount_minor,
        "currency": offer.currency,
        "mandatory_fees_included": offer.mandatory_fees_included,
        "included_transfer_cost_eur_cents": (offer.included_transfer_cost_eur_cents),
        "passenger_age": offer.passenger_age,
        "passenger_student": offer.passenger_student,
        "passenger_eligibility_confirmed": offer.passenger_eligibility_confirmed,
        "bookable": offer.bookable,
        "is_teaser": offer.is_teaser,
        "outbound": [segment_payload(item) for item in offer.outbound_segments],
        "return": [segment_payload(item) for item in offer.return_segments],
        "bookings": [
            {
                "seller": item.seller,
                "source": item.source_slug,
                "amount": item.amount_minor,
                "currency": item.currency,
                "fare_name": item.fare_name,
                "student": item.student_fare_applied,
                "mandatory_fees_included": item.mandatory_fees_included,
                "price_completeness": item.price_completeness.value,
                "passenger_age": item.passenger_age,
                "passenger_student": item.passenger_student,
                "passenger_eligibility_confirmed": (
                    item.passenger_eligibility_confirmed
                ),
                "bookable": item.bookable,
                "is_teaser": item.is_teaser,
                "url": (
                    bookings[index].booking_url
                    if index < len(bookings)
                    else item.booking_url
                ),
            }
            for index, item in enumerate(offer.booking_components)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _select_daily_winners(
    offers: tuple[_ValidatedOffer, ...],
) -> tuple[_ValidatedOffer, ...]:
    grouped: dict[tuple[str, int, DatabaseTransportMode], list[_ValidatedOffer]] = {}
    for item in offers:
        key = (
            item.offer.destination_city_id,
            item.travel_window.id,
            item.candidate.mode_bucket,
        )
        grouped.setdefault(key, []).append(item)

    winners: list[_ValidatedOffer] = []
    for key in sorted(grouped, key=lambda value: (value[0], value[1], value[2].value)):
        candidates = grouped[key]
        winner = select_best(item.candidate for item in candidates)
        winners.append(next(item for item in candidates if item.candidate is winner))
    return tuple(winners)


def _upsert_duration_histograms(
    session: Session,
    *,
    offers: tuple[_ValidatedOffer, ...],
    observed_on: date,
    is_synthetic: bool,
) -> None:
    """Replace deterministic daily duration aggregates for observed routes.

    Both directions of every valid offer contribute one terminal-to-terminal
    sample when that direction uses a single transport mode.  Itinerary
    identity deduplication prevents the same directional offer from being
    counted repeatedly through multiple round-trip compositions or safe
    same-day reruns. Mixed directions are intentionally excluded from the
    flight/train/bus cards rather than being mislabelled as one of their legs.
    """

    grouped: dict[tuple[str, DatabaseTransportMode], dict[str, int]] = {}
    seen: set[tuple[str, DatabaseTransportMode, str]] = set()
    supported_modes = {
        DatabaseTransportMode.FLIGHT,
        DatabaseTransportMode.TRAIN,
        DatabaseTransportMode.BUS,
    }
    for item in offers:
        city_id = item.offer.destination_city_id
        for itinerary in (item.candidate.outbound, item.candidate.inbound):
            modes = {segment.mode for segment in itinerary.segments}
            if len(modes) != 1:
                continue
            mode = DatabaseTransportMode(next(iter(modes)).value)
            if mode not in supported_modes:
                continue
            identity = itinerary_identity(itinerary)
            marker = (city_id, mode, identity)
            if marker in seen:
                continue
            seen.add(marker)
            minutes = max(1, int(itinerary.elapsed.total_seconds() // 60))
            bucket = max(15, ((minutes + 7) // 15) * 15)
            bins = grouped.setdefault((city_id, mode), {})
            key = str(bucket)
            bins[key] = bins.get(key, 0) + 1

    for (city_id, mode), bins in grouped.items():
        histogram = session.get(
            DurationHistogramDaily,
            (observed_on, city_id, mode, is_synthetic),
        )
        if histogram is None:
            histogram = DurationHistogramDaily(
                observed_on=observed_on,
                city_id=city_id,
                mode=mode,
                is_synthetic=is_synthetic,
            )
            session.add(histogram)
        histogram.bins = dict(sorted(bins.items(), key=lambda item: int(item[0])))
        histogram.sample_count = sum(bins.values())


def _publication_quality(
    mode: DatabaseTransportMode,
    *,
    report: BatchReport,
    sources: dict[str, Source],
) -> _PublicationQuality:
    relevant_source_ids = {
        slug
        for slug, source in sources.items()
        if source.is_requested
        and (
            mode is DatabaseTransportMode.MIXED or mode.value in source.supported_modes
        )
    }
    if not relevant_source_ids:
        # Fixture-only publication has complete deterministic execution but no
        # coverage of the requested live-provider scope.
        return _PublicationQuality(ExecutionCoverage.COMPLETE, 0.0)

    relevant_reports = tuple(
        item
        for item in report.source_reports
        if item.source_slug in relevant_source_ids
    )
    enabled_reports = tuple(
        item
        for item in relevant_reports
        if item.state is not ExtractorRunStatus.DISABLED
    )
    source_scope_ratio = len(enabled_reports) / len(relevant_source_ids)
    if not enabled_reports:
        execution_coverage = ExecutionCoverage.FAILED
    elif all(item.state is ExtractorRunStatus.SUCCEEDED for item in enabled_reports):
        execution_coverage = ExecutionCoverage.COMPLETE
    elif any(
        item.state in {ExtractorRunStatus.SUCCEEDED, ExtractorRunStatus.PARTIAL}
        for item in enabled_reports
    ):
        execution_coverage = ExecutionCoverage.PARTIAL
    else:
        execution_coverage = ExecutionCoverage.FAILED
    return _PublicationQuality(execution_coverage, source_scope_ratio)


def _publish_winners(
    session: Session,
    *,
    winners: tuple[_ValidatedOffer, ...],
    crawl_run: CrawlRun,
    profile_id: int,
    rule_version_id: int,
    observed_on: date,
    report: BatchReport,
    sources: dict[str, Source],
) -> tuple[int, int, int]:
    snapshots_created = 0
    revisions_created = 0
    snapshots_unchanged = 0
    terminal_timezones = {
        terminal.id: terminal.timezone
        for terminal in session.scalars(select(Terminal)).all()
    }

    for item in winners:
        mode = item.candidate.mode_bucket
        quality = _publication_quality(mode, report=report, sources=sources)
        snapshot = session.scalar(
            select(FareSnapshot).where(
                FareSnapshot.observed_on == observed_on,
                FareSnapshot.profile_id == profile_id,
                FareSnapshot.destination_city_id == item.offer.destination_city_id,
                FareSnapshot.travel_window_id == item.travel_window.id,
                FareSnapshot.mode_bucket == mode,
                FareSnapshot.is_synthetic.is_(crawl_run.is_synthetic),
            )
        )
        if snapshot is None:
            snapshot = FareSnapshot(
                observed_on=observed_on,
                profile_id=profile_id,
                destination_city_id=item.offer.destination_city_id,
                travel_window_id=item.travel_window.id,
                mode_bucket=mode,
                is_synthetic=crawl_run.is_synthetic,
            )
            session.add(snapshot)
            session.flush()
            snapshots_created += 1

        current = (
            session.get(FareSnapshotRevision, snapshot.current_revision_id)
            if snapshot.current_revision_id is not None
            else None
        )
        if (
            current is not None
            and current.availability == Availability.AVAILABLE
            and current.total_price_eur_cents is not None
            and current.total_price_eur_cents <= item.candidate.total_price_eur_cents
        ):
            # A daily cell records the lowest trustworthy fare observed that
            # day. Later provider volatility must never replace it with an
            # equal or worse price.
            snapshots_unchanged += 1
            continue

        revision_number = (
            session.scalar(
                select(func.max(FareSnapshotRevision.revision_number)).where(
                    FareSnapshotRevision.fare_snapshot_id == snapshot.id
                )
            )
            or 0
        ) + 1
        outbound = item.candidate.outbound
        inbound = item.candidate.inbound
        observed_at = item.offer.observed_at
        revision = FareSnapshotRevision(
            fare_snapshot_id=snapshot.id,
            revision_number=revision_number,
            crawl_run_id=crawl_run.id,
            rule_version_id=rule_version_id,
            profile_id=profile_id,
            mode_bucket=mode,
            total_price_eur_cents=item.candidate.total_price_eur_cents,
            availability=Availability.AVAILABLE,
            execution_coverage=quality.execution_coverage,
            source_scope_ratio=quality.source_scope_ratio,
            observed_at=observed_at,
            published_at=crawl_run.published_at or observed_at,
            outbound_departure_at=outbound.departs_at,
            outbound_arrival_at=outbound.arrives_at,
            return_departure_at=inbound.departs_at,
            return_arrival_at=inbound.arrives_at,
            outbound_duration_minutes=int(outbound.elapsed.total_seconds() // 60),
            return_duration_minutes=int(inbound.elapsed.total_seconds() // 60),
            stay_duration_minutes=int(
                (
                    inbound.departs_at.astimezone(UTC)
                    - outbound.arrives_at.astimezone(UTC)
                ).total_seconds()
                // 60
            ),
            transfer_count=len(outbound.segments) + len(inbound.segments) - 2,
            itinerary_key=item.candidate.itinerary_key,
        )
        session.add(revision)
        session.flush()

        bookings: list[SnapshotBooking] = []
        for index, trusted_booking in enumerate(item.bookings):
            component = trusted_booking.component
            booking = SnapshotBooking(
                revision_id=revision.id,
                component_order=index,
                source_id=component.source_slug,
                seller=component.seller,
                fare_name=component.fare_name,
                pricing_semantics=DatabasePricingSemantics(
                    item.offer.pricing_semantics.value
                ),
                original_amount_minor=component.amount_minor,
                original_currency=component.currency,
                eur_amount_cents=trusted_booking.eur_amount_cents,
                fx_rate=trusted_booking.fx_rate,
                fx_rate_date=trusted_booking.fx_rate_date,
                student_fare_used=component.student_fare_applied,
                price_completeness=DatabasePriceCompleteness(
                    component.price_completeness.value
                ),
                mandatory_fees_included=component.mandatory_fees_included,
                passenger_age=component.passenger_age,
                passenger_student=component.passenger_student,
                passenger_eligibility_confirmed=(
                    component.passenger_eligibility_confirmed
                ),
                bookable_at_observation=component.bookable,
                is_teaser=component.is_teaser,
                booking_url=trusted_booking.booking_url,
                expires_at=item.offer.expires_at,
            )
            session.add(booking)
            bookings.append(booking)
        session.flush()

        outbound_booking_id = (
            bookings[item.outbound_booking_index].id
            if item.outbound_booking_index is not None
            else None
        )
        inbound_booking_id = (
            bookings[item.inbound_booking_index].id
            if item.inbound_booking_index is not None
            else None
        )
        for direction, segments in (
            (DatabaseDirection.OUTBOUND, item.offer.outbound_segments),
            (DatabaseDirection.RETURN, item.offer.return_segments),
        ):
            for index, segment in enumerate(segments):
                session.add(
                    SnapshotLeg(
                        revision_id=revision.id,
                        booking_id=(
                            outbound_booking_id
                            if direction is DatabaseDirection.OUTBOUND
                            else inbound_booking_id
                        ),
                        direction=direction,
                        segment_order=index,
                        mode=DatabaseTransportMode(segment.mode.value),
                        operator=segment.operator,
                        service_number=segment.service_number,
                        origin_terminal_id=segment.departure_terminal_id,
                        destination_terminal_id=segment.arrival_terminal_id,
                        departs_at=segment.departs_at,
                        arrives_at=segment.arrives_at,
                        origin_timezone=terminal_timezones[
                            segment.departure_terminal_id
                        ],
                        destination_timezone=terminal_timezones[
                            segment.arrival_terminal_id
                        ],
                        protection_state=(
                            ProtectionState.PROTECTED
                            if segment.protected_connection
                            else ProtectionState.UNPROTECTED_TRANSFER
                        ),
                    )
                )
        snapshot.current_revision_id = revision.id
        revisions_created += 1
    session.flush()
    return snapshots_created, revisions_created, snapshots_unchanged


_OBSERVED_MODES = (
    DatabaseTransportMode.FLIGHT,
    DatabaseTransportMode.TRAIN,
    DatabaseTransportMode.BUS,
    DatabaseTransportMode.MIXED,
)

_COMPLETE_COVERAGE_OUTCOMES = frozenset(
    {
        DatabaseSearchOutcome.SUCCESS_RESULTS,
        DatabaseSearchOutcome.SUCCESS_EMPTY,
        DatabaseSearchOutcome.NOT_ON_SALE,
        DatabaseSearchOutcome.NO_SERVICE,
        DatabaseSearchOutcome.SOLD_OUT,
        DatabaseSearchOutcome.SKIPPED_UNSUPPORTED_ROUTE,
    }
)


def _source_supports_observation_mode(
    source: Source, mode: DatabaseTransportMode
) -> bool:
    supported = set(source.supported_modes or ())
    if mode is DatabaseTransportMode.MIXED:
        return bool(
            supported
            & {
                DatabaseTransportMode.FLIGHT.value,
                DatabaseTransportMode.TRAIN.value,
                DatabaseTransportMode.BUS.value,
            }
        )
    return mode.value in supported


def _cell_coverage(
    *,
    mode: DatabaseTransportMode,
    city_id: str,
    window_id: int,
    coverage_by_cell: dict[tuple[str, str, int], tuple[CrawlCoverageCell, ...]],
    reports_by_source: dict[str, SourceReport],
    sources: dict[str, Source],
) -> _PublicationQuality:
    execution_sources = {
        slug
        for slug, source in sources.items()
        if _source_supports_observation_mode(source, mode)
    }
    requested_sources = {
        slug for slug in execution_sources if sources[slug].is_requested
    }
    enabled_sources = {
        slug
        for slug in execution_sources
        if reports_by_source[slug].state is not ExtractorRunStatus.DISABLED
    }
    source_scope_ratio = (
        len(enabled_sources & requested_sources) / len(requested_sources)
        if requested_sources
        else 0.0
    )

    # A fixture-only run is deterministic, and a source set with no adapter for
    # a mode has no unexecuted provider work. In both cases scope stays at zero.
    if not execution_sources:
        return _PublicationQuality(ExecutionCoverage.COMPLETE, source_scope_ratio)
    if not enabled_sources:
        return _PublicationQuality(ExecutionCoverage.FAILED, source_scope_ratio)

    source_coverages: list[ExecutionCoverage] = []
    for source_slug in sorted(enabled_sources):
        rows = coverage_by_cell.get((source_slug, city_id, window_id), ())
        if not rows:
            source_coverages.append(ExecutionCoverage.FAILED)
            continue

        row_coverages: list[ExecutionCoverage] = []
        for row in rows:
            if not row.applicable:
                row_coverages.append(ExecutionCoverage.COMPLETE)
            elif row.outcome in _COMPLETE_COVERAGE_OUTCOMES:
                row_coverages.append(ExecutionCoverage.COMPLETE)
            elif row.outcome is DatabaseSearchOutcome.PARTIAL_SUCCESS:
                row_coverages.append(ExecutionCoverage.PARTIAL)
            else:
                row_coverages.append(ExecutionCoverage.FAILED)

        if all(item is ExecutionCoverage.COMPLETE for item in row_coverages):
            source_coverages.append(ExecutionCoverage.COMPLETE)
        elif any(
            item in {ExecutionCoverage.COMPLETE, ExecutionCoverage.PARTIAL}
            for item in row_coverages
        ):
            source_coverages.append(ExecutionCoverage.PARTIAL)
        else:
            source_coverages.append(ExecutionCoverage.FAILED)

    if all(item is ExecutionCoverage.COMPLETE for item in source_coverages):
        execution_coverage = ExecutionCoverage.COMPLETE
    elif any(
        item in {ExecutionCoverage.COMPLETE, ExecutionCoverage.PARTIAL}
        for item in source_coverages
    ):
        execution_coverage = ExecutionCoverage.PARTIAL
    else:
        execution_coverage = ExecutionCoverage.FAILED
    return _PublicationQuality(execution_coverage, source_scope_ratio)


def _cell_availability(
    *,
    has_current_fare: bool,
    quality: _PublicationQuality,
    mode: DatabaseTransportMode,
    city_id: str,
    window_id: int,
    coverage_by_cell: dict[tuple[str, str, int], tuple[CrawlCoverageCell, ...]],
    reports_by_source: dict[str, SourceReport],
    sources: dict[str, Source],
) -> Availability:
    if has_current_fare:
        return Availability.AVAILABLE
    if quality.execution_coverage is not ExecutionCoverage.COMPLETE:
        return Availability.UNKNOWN_DUE_FAILURE

    outcomes = {
        row.outcome
        for source_slug, source in sources.items()
        if _source_supports_observation_mode(source, mode)
        and reports_by_source[source_slug].state is not ExtractorRunStatus.DISABLED
        for row in coverage_by_cell.get((source_slug, city_id, window_id), ())
        if row.applicable and row.outcome is not None
    }
    if DatabaseSearchOutcome.SOLD_OUT in outcomes:
        return Availability.SOLD_OUT
    if DatabaseSearchOutcome.NOT_ON_SALE in outcomes:
        return Availability.NOT_ON_SALE
    return Availability.NO_VALID_OPTION


def _latest_trusted_revisions(
    session: Session,
    *,
    profile_id: int,
    rule_version_id: int,
    observed_on: date,
    is_synthetic: bool,
) -> dict[tuple[str, int, DatabaseTransportMode], FareSnapshotRevision]:
    latest: dict[
        tuple[str, int, DatabaseTransportMode],
        tuple[date, int, FareSnapshotRevision],
    ] = {}
    rows = session.execute(
        select(FareSnapshot, FareSnapshotRevision)
        .join(
            FareSnapshotRevision,
            FareSnapshot.current_revision_id == FareSnapshotRevision.id,
        )
        .where(
            FareSnapshot.profile_id == profile_id,
            FareSnapshot.observed_on <= observed_on,
            FareSnapshot.is_synthetic.is_(is_synthetic),
            FareSnapshotRevision.rule_version_id == rule_version_id,
            FareSnapshotRevision.availability == Availability.AVAILABLE,
            FareSnapshotRevision.total_price_eur_cents.is_not(None),
        )
    ).all()
    for snapshot, revision in rows:
        key = (
            snapshot.destination_city_id,
            snapshot.travel_window_id,
            snapshot.mode_bucket,
        )
        rank = (snapshot.observed_on, revision.id)
        current = latest.get(key)
        if current is None or rank > current[:2]:
            latest[key] = (*rank, revision)
    return {key: value[2] for key, value in latest.items()}


def _upsert_cell_observations(
    session: Session,
    *,
    crawl_run: CrawlRun,
    manifest: BatchManifest,
    report: BatchReport,
    windows: dict[date, TravelWindow],
    winners: tuple[_ValidatedOffer, ...],
    profile_id: int,
    rule_version_id: int,
    sources: dict[str, Source],
    trust_failures: dict[tuple[str, int, DatabaseTransportMode], frozenset[str]],
) -> int:
    """Publish a current state for every monitored city/weekend/mode cell.

    Availability belongs to this execution. The linked revision is independent:
    it is the latest trustworthy fare and therefore survives an empty or failed
    run without making the old price look newly observed.
    """

    reports_by_source = {item.source_slug: item for item in report.source_reports}
    coverage_rows = session.scalars(
        select(CrawlCoverageCell).where(CrawlCoverageCell.crawl_run_id == crawl_run.id)
    ).all()
    coverage_lists: dict[tuple[str, str, int], list[CrawlCoverageCell]] = {}
    for row in coverage_rows:
        coverage_lists.setdefault(
            (row.source_id, row.city_id, row.travel_window_id), []
        ).append(row)
    coverage_by_cell = {key: tuple(value) for key, value in coverage_lists.items()}

    current_fare_cells = {
        (
            item.offer.destination_city_id,
            item.travel_window.id,
            item.candidate.mode_bucket,
        )
        for item in winners
    }
    trusted_revisions = _latest_trusted_revisions(
        session,
        profile_id=profile_id,
        rule_version_id=rule_version_id,
        observed_on=manifest.scheduled_for,
        is_synthetic=crawl_run.is_synthetic,
    )
    monitored_cells = sorted(
        {
            (cell.destination_city_id, windows[cell.weekend_start].id)
            for cell in manifest.coverage_cells
        }
    )

    written = 0
    for city_id, window_id in monitored_cells:
        for mode in _OBSERVED_MODES:
            key = (city_id, window_id, mode)
            trusted = trusted_revisions.get(key)
            has_current_fare = key in current_fare_cells
            if has_current_fare and trusted is None:
                raise PublicationError(
                    "a validated winning fare has no persisted trusted revision"
                )
            quality = _cell_coverage(
                mode=mode,
                city_id=city_id,
                window_id=window_id,
                coverage_by_cell=coverage_by_cell,
                reports_by_source=reports_by_source,
                sources=sources,
            )
            failure_codes = trust_failures.get(key, frozenset())
            if (
                failure_codes
                and not has_current_fare
                and quality.execution_coverage is ExecutionCoverage.COMPLETE
            ):
                quality = _PublicationQuality(
                    ExecutionCoverage.PARTIAL,
                    quality.source_scope_ratio,
                )
            availability = _cell_availability(
                has_current_fare=has_current_fare,
                quality=quality,
                mode=mode,
                city_id=city_id,
                window_id=window_id,
                coverage_by_cell=coverage_by_cell,
                reports_by_source=reports_by_source,
                sources=sources,
            )
            completion_times = [
                row.completed_at
                for (
                    source_slug,
                    row_city_id,
                    row_window_id,
                ), rows in coverage_by_cell.items()
                if row_city_id == city_id
                and row_window_id == window_id
                and _source_supports_observation_mode(sources[source_slug], mode)
                for row in rows
                if row.completed_at is not None
            ]
            observed_at = max(completion_times, default=report.completed_at)

            observation = session.scalar(
                select(FareCellObservation).where(
                    FareCellObservation.crawl_run_id == crawl_run.id,
                    FareCellObservation.profile_id == profile_id,
                    FareCellObservation.destination_city_id == city_id,
                    FareCellObservation.travel_window_id == window_id,
                    FareCellObservation.mode_bucket == mode,
                )
            )
            if observation is None:
                observation = FareCellObservation(
                    crawl_run_id=crawl_run.id,
                    profile_id=profile_id,
                    destination_city_id=city_id,
                    travel_window_id=window_id,
                    mode_bucket=mode,
                )
                session.add(observation)
            observation.rule_version_id = rule_version_id
            observation.availability = availability
            observation.execution_coverage = quality.execution_coverage
            observation.source_scope_ratio = quality.source_scope_ratio
            observation.observed_at = observed_at
            observation.trusted_revision_id = (
                trusted.id if trusted is not None else None
            )
            if failure_codes and not has_current_fare:
                observation.diagnostic = (
                    "OFFER_TRUST_REJECTED:"
                    + "|".join(code.upper() for code in sorted(failure_codes))
                )[:500]
            else:
                observation.diagnostic = {
                    Availability.AVAILABLE: "CURRENT_FARE_AVAILABLE",
                    Availability.NO_VALID_OPTION: "NO_VALID_OPTION",
                    Availability.NOT_ON_SALE: "NOT_ON_SALE",
                    Availability.SOLD_OUT: "SOLD_OUT",
                    Availability.UNKNOWN_DUE_FAILURE: "UNKNOWN_DUE_FAILURE",
                }[availability]
            written += 1

    session.flush()
    return written


def _write_health_events(
    session: Session,
    *,
    crawl_run_id: int,
    known_source_slugs: frozenset[str],
    events: tuple[HealthEvent, ...],
) -> int:
    for event in events:
        source_id = event.source_slug
        if source_id is not None and source_id not in known_source_slugs:
            raise PublicationError("health event uses a source outside the manifest")
        session.add(
            DatabaseHealthEvent(
                crawl_run_id=crawl_run_id,
                source_id=source_id,
                event_type=event.kind.value,
                severity=_health_severity(event),
                diagnostic=_health_diagnostic(event),
            )
        )
    session.flush()
    return len(events)


def _health_severity(event: HealthEvent) -> str:
    state = str(
        event.payload.get("state")
        or event.payload.get("status")
        or event.payload.get("outcome")
        or ""
    )
    if state in {
        "failed",
        "blocked",
        "quarantined",
        "timeout",
        "upstream_error",
        "parse_schema_error",
        "configuration_error",
        "cancelled_deadline",
    }:
        return "error"
    if state in {"partial", "partial_success", "rate_limited"}:
        return "warning"
    return "info"


def _health_diagnostic(event: HealthEvent) -> str:
    parts = [event.kind.value]
    for key in (
        "diagnostic_code",
        "state",
        "status",
        "outcome",
        "task_id",
        "manifest_id",
    ):
        value = event.payload.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    return "; ".join(parts)[:500]


def _finalize_crawl_run(
    session: Session,
    crawl_run: CrawlRun,
    manifest: BatchManifest,
    report: BatchReport,
) -> None:
    executions = _execution_by_coverage_cell(report)
    logical: dict[
        tuple[str, str, date, DatabaseDirection],
        list[tuple[SearchOutcome, datetime]],
    ] = {}
    for cell in manifest.coverage_cells:
        execution = executions.get(cell.cell_id)
        for direction in _coverage_directions(cell.direction):
            key = (
                cell.source_slug,
                cell.destination_city_id,
                cell.weekend_start,
                direction,
            )
            if execution is not None:
                logical.setdefault(key, []).append(
                    (execution.result.outcome, execution.result.observed_at)
                )
            elif (
                cell.preclassified_outcome is not None
                and cell.classified_at is not None
            ):
                logical.setdefault(key, []).append(
                    (cell.preclassified_outcome, cell.classified_at)
                )
            else:
                logical.setdefault(key, [])
    best = tuple(
        min(
            items,
            key=lambda item: (
                _coverage_outcome_priority(item[0]),
                -item[1].timestamp(),
            ),
        )
        if items
        else None
        for items in logical.values()
    )
    expected = len(logical)
    completed = sum(item is not None for item in best)
    successful = sum(
        item is not None
        and (
            item[0] in SUCCESSFUL_SEARCH_OUTCOMES
            or item[0] is SearchOutcome.PARTIAL_SUCCESS
        )
        and item[0] is not SearchOutcome.SKIPPED_DISABLED
        for item in best
    )
    crawl_run.status = DatabaseBatchStatus(report.status.value)
    crawl_run.expected_cells = expected
    crawl_run.completed_cells = completed
    crawl_run.successful_cells = successful
    crawl_run.heartbeat_at = report.completed_at
    crawl_run.completed_at = report.completed_at
    crawl_run.published_at = report.completed_at
