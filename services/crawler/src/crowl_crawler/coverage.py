from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from crowl_core.catalog import ensure_default_profile_and_rules
from crowl_core.enums import (
    BatchStatus as DatabaseBatchStatus,
    ExtractorRunStatus as DatabaseExtractorRunStatus,
)
from crowl_core.models import (
    City,
    CityTerminalLink,
    CrawlRun,
    ExtractorRun,
    HealthEvent,
    OriginTerminal,
    Source,
    SourceRouteCoverage,
    Terminal,
)

from .adapters.registry import AdapterRegistry
from .models import AdapterMetadata, CoverageDiscovery


class CoverageDiscoveryError(RuntimeError):
    """Raised when provider coverage cannot be trusted for task generation."""

    def __init__(
        self, message: str, *, failed_source_slugs: Iterable[str] = ()
    ) -> None:
        super().__init__(message)
        self.failed_source_slugs = tuple(sorted(set(failed_source_slugs)))


async def discover_and_persist_coverage(
    session_factory: sessionmaker[Session],
    registry: AdapterRegistry,
    *,
    scheduled_for: date,
    clock: Callable[[], datetime] | None = None,
    maximum_age: timedelta = timedelta(hours=48),
) -> dict[str, CoverageDiscovery]:
    """Record discovery intent, discover enabled providers, and persist results.

    The intent rows are committed for every enabled source before any adapter is
    invoked. This gives coverage discovery the same pre-network audit property
    as fare collection, whose complete request ledger is committed later by
    ``DatabaseRunLifecycle.prepare``.
    """

    now = (clock or (lambda: datetime.now(UTC)))()
    _require_aware(now, "coverage discovery clock")
    if maximum_age <= timedelta(0):
        raise ValueError("maximum coverage age must be positive")

    metadata = registry.metadata(include_fixture=False)
    if len({item.source_slug for item in metadata}) != len(metadata):
        raise CoverageDiscoveryError("duplicate provider metadata")
    enabled_metadata = tuple(item for item in metadata if item.enabled)
    if not enabled_metadata:
        return {}

    metadata_by_slug = {item.source_slug: item for item in enabled_metadata}
    adapters = tuple(registry.get(item.source_slug) for item in enabled_metadata)

    with session_factory() as session, session.begin():
        allowed_origins, allowed_destinations = _coverage_catalog_scope(session)
        _sync_discovery_sources(session, metadata)
        for item in enabled_metadata:
            _record_health_once(
                session,
                source_slug=item.source_slug,
                event_type="coverage_discovery_started",
                severity="info",
                diagnostic=_intent_diagnostic(scheduled_for, item),
            )

    # All intents above are durably committed before the first provider network
    # operation. Providers are independent, so discovery can run concurrently.
    raw_results = await asyncio.gather(
        *(adapter.discover_coverage() for adapter in adapters),
        return_exceptions=True,
    )

    validated: dict[str, CoverageDiscovery] = {}
    failures: list[str] = []
    for adapter, raw_result in zip(adapters, raw_results, strict=True):
        source_slug = adapter.metadata().source_slug
        try:
            if isinstance(raw_result, BaseException):
                raise CoverageDiscoveryError("adapter discovery failed")
            validated[source_slug] = _validate_discovery(
                raw_result,
                metadata=metadata_by_slug[source_slug],
                allowed_origins=allowed_origins,
                allowed_destinations=allowed_destinations,
                now=now,
                maximum_age=maximum_age,
            )
        except (CoverageDiscoveryError, TypeError, ValueError):
            failures.append(source_slug)

    with session_factory() as session, session.begin():
        for source_slug, discovery in validated.items():
            item = metadata_by_slug[source_slug]
            session.execute(
                delete(SourceRouteCoverage).where(
                    SourceRouteCoverage.source_id == source_slug,
                    SourceRouteCoverage.effective_from == scheduled_for,
                )
            )
            provenance = _coverage_provenance(
                discovery,
                metadata=item,
                scheduled_for=scheduled_for,
            )
            for origin_terminal_id, destination_terminal_id in (
                discovery.supported_terminal_pairs
            ):
                session.add(
                    SourceRouteCoverage(
                        source_id=source_slug,
                        origin_terminal_id=origin_terminal_id,
                        destination_terminal_id=destination_terminal_id,
                        effective_from=scheduled_for,
                        effective_to=discovery.sellable_through,
                        discovered_at=discovery.observed_at,
                        provenance=provenance,
                    )
                )
            _record_health_once(
                session,
                source_slug=source_slug,
                event_type="coverage_discovery_succeeded",
                severity="info",
                diagnostic=(
                    "COVERAGE_DISCOVERY_SUCCEEDED; "
                    f"scheduled_for={scheduled_for.isoformat()}; "
                    f"fingerprint={_coverage_fingerprint(discovery)}"
                ),
            )

        for source_slug in failures:
            _record_health_once(
                session,
                source_slug=source_slug,
                event_type="coverage_discovery_failed",
                severity="error",
                diagnostic=(
                    "COVERAGE_DISCOVERY_REJECTED; "
                    f"scheduled_for={scheduled_for.isoformat()}"
                ),
            )

    if failures:
        # This run will not reach the coordinator, which normally owns adapter
        # cleanup after fare searches.
        await asyncio.gather(
            *(adapter.close() for adapter in adapters),
            return_exceptions=True,
        )
        raise CoverageDiscoveryError(
            "coverage discovery rejected for: " + ", ".join(sorted(failures)),
            failed_source_slugs=failures,
        )
    return validated


def record_coverage_discovery_failure(
    session_factory: sessionmaker[Session],
    *,
    scheduled_for: date,
    source_metadata: Iterable[AdapterMetadata],
    failed_source_slugs: Iterable[str] = (),
    release_version: str = "production-v1",
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Expose a rejected discovery as today's failed batch and extractor state."""

    now = (clock or (lambda: datetime.now(UTC)))()
    _require_aware(now, "coverage failure clock")
    metadata = tuple(source_metadata)
    if len({item.source_slug for item in metadata}) != len(metadata):
        raise CoverageDiscoveryError("duplicate provider metadata")
    failed = frozenset(failed_source_slugs)
    unknown = failed - {item.source_slug for item in metadata}
    if unknown:
        raise CoverageDiscoveryError("failure references an unknown provider")
    enabled = frozenset(item.source_slug for item in metadata if item.enabled)
    rejected = failed or enabled

    with session_factory() as session, session.begin():
        _sync_discovery_sources(session, metadata)
        profile, rule = ensure_default_profile_and_rules(session)
        crawl_run = session.scalar(
            select(CrawlRun).where(
                CrawlRun.scheduled_for == scheduled_for,
                CrawlRun.is_synthetic.is_(False),
            )
        )
        if crawl_run is not None and crawl_run.published_at is not None:
            # A failed manual retry may not erase an already published daily run.
            return crawl_run.id
        if crawl_run is None:
            crawl_run = CrawlRun(
                scheduled_for=scheduled_for,
                is_synthetic=False,
                rule_version_id=rule.id,
                profile_id=profile.id,
                manifest_version=_discovery_failure_manifest(
                    scheduled_for, metadata
                ),
                release_version=f"crawler-{release_version}"[:64],
                status=DatabaseBatchStatus.FAILED,
            )
            session.add(crawl_run)
            session.flush()
        crawl_run.rule_version_id = rule.id
        crawl_run.profile_id = profile.id
        crawl_run.manifest_version = _discovery_failure_manifest(
            scheduled_for, metadata
        )
        crawl_run.release_version = f"crawler-{release_version}"[:64]
        crawl_run.status = DatabaseBatchStatus.FAILED
        crawl_run.started_at = crawl_run.started_at or now
        crawl_run.heartbeat_at = now
        crawl_run.completed_at = now
        crawl_run.published_at = None
        crawl_run.expected_cells = 0
        crawl_run.completed_cells = 0
        crawl_run.successful_cells = 0
        crawl_run.diagnostic = "COVERAGE_DISCOVERY_REJECTED"

        for item in metadata:
            extractor = session.scalar(
                select(ExtractorRun).where(
                    ExtractorRun.crawl_run_id == crawl_run.id,
                    ExtractorRun.source_id == item.source_slug,
                )
            )
            if extractor is None:
                extractor = ExtractorRun(
                    crawl_run_id=crawl_run.id,
                    source_id=item.source_slug,
                    status=DatabaseExtractorRunStatus.PENDING,
                )
                session.add(extractor)
            if item.enabled:
                extractor.status = DatabaseExtractorRunStatus.FAILED
                extractor.diagnostic = (
                    "COVERAGE_DISCOVERY_REJECTED"
                    if item.source_slug in rejected
                    else "COVERAGE_DISCOVERY_BATCH_ABORTED"
                )
                extractor.consecutive_failures = (
                    _prior_failure_streak(
                        session,
                        source_slug=item.source_slug,
                        before_scheduled_for=scheduled_for,
                    )
                    + 1
                )
            else:
                extractor.status = DatabaseExtractorRunStatus.DISABLED
                extractor.diagnostic = "SOURCE_DISABLED"
                extractor.consecutive_failures = 0
            extractor.started_at = now
            extractor.completed_at = now
            extractor.runtime_ms = 0
            extractor.expected_searches = 0
            extractor.attempted_searches = 0
            extractor.successful_searches = 0
            extractor.empty_searches = 0
            extractor.result_searches = 0
            extractor.physical_requests = 0
            extractor.offers_found = 0

        # Attach the already-committed pre-network discovery audit trail to the
        # newly visible daily failure record.
        event_rows = session.scalars(
            select(HealthEvent).where(
                HealthEvent.crawl_run_id.is_(None),
                HealthEvent.source_id.in_([item.source_slug for item in metadata]),
                HealthEvent.diagnostic.contains(
                    f"scheduled_for={scheduled_for.isoformat()}"
                ),
            )
        ).all()
        for event in event_rows:
            event.crawl_run_id = crawl_run.id
        session.flush()
        return crawl_run.id


def _coverage_catalog_scope(session: Session) -> tuple[frozenset[str], frozenset[str]]:
    _, rule = ensure_default_profile_and_rules(session)
    origins = frozenset(
        session.scalars(
            select(OriginTerminal.terminal_id)
            .join(Terminal, Terminal.id == OriginTerminal.terminal_id)
            .where(
                OriginTerminal.rule_version_id == rule.id,
                OriginTerminal.permits_departure.is_(True),
                OriginTerminal.permits_return.is_(True),
                Terminal.active.is_(True),
                Terminal.schengen_eligible.is_(True),
            )
        ).all()
    )
    destinations = frozenset(
        session.scalars(
            select(CityTerminalLink.terminal_id)
            .join(City, City.id == CityTerminalLink.city_id)
            .join(Terminal, Terminal.id == CityTerminalLink.terminal_id)
            .where(
                City.active.is_(True),
                Terminal.active.is_(True),
                Terminal.schengen_eligible.is_(True),
            )
        ).all()
    )
    if not origins or not destinations:
        raise CoverageDiscoveryError(
            "coverage discovery requires active origin and destination terminals"
        )
    return origins, destinations


def _sync_discovery_sources(
    session: Session, metadata: Iterable[AdapterMetadata]
) -> None:
    for item in metadata:
        source = session.get(Source, item.source_slug)
        if source is None:
            source = Source(slug=item.source_slug)
            session.add(source)
        source.name = item.display_name
        source.supported_modes = [mode.value for mode in item.modes]
        source.strategy = item.intended_strategy.value
        source.is_requested = True
        source.enabled = item.enabled
        source.disabled_reason = item.disabled_reason
        source.connector_version = item.connector_version
        source.parser_version = item.parser_version
    session.flush()


def _validate_discovery(
    discovery: object,
    *,
    metadata: AdapterMetadata,
    allowed_origins: frozenset[str],
    allowed_destinations: frozenset[str],
    now: datetime,
    maximum_age: timedelta,
) -> CoverageDiscovery:
    if not isinstance(discovery, CoverageDiscovery):
        raise CoverageDiscoveryError("adapter returned the wrong coverage type")
    if discovery.source_slug != metadata.source_slug:
        raise CoverageDiscoveryError("coverage source does not match its adapter")
    _require_aware(discovery.observed_at, "coverage observed_at")
    observed_at = discovery.observed_at.astimezone(UTC)
    now_utc = now.astimezone(UTC)
    if observed_at > now_utc + timedelta(minutes=5):
        raise CoverageDiscoveryError("coverage timestamp is in the future")
    if now_utc - observed_at > maximum_age:
        raise CoverageDiscoveryError("coverage result is stale")
    if discovery.sellable_through is not None and type(
        discovery.sellable_through
    ) is not date:
        raise CoverageDiscoveryError("coverage sale horizon is not a date")

    normalized_pairs: list[tuple[str, str]] = []
    for pair in discovery.supported_terminal_pairs:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(value, str) and value for value in pair)
        ):
            raise CoverageDiscoveryError("coverage route pair is malformed")
        origin_terminal_id, destination_terminal_id = pair
        if origin_terminal_id not in allowed_origins:
            raise CoverageDiscoveryError("coverage has an unknown origin terminal")
        if destination_terminal_id not in allowed_destinations:
            raise CoverageDiscoveryError("coverage has an unknown destination terminal")
        normalized_pairs.append((origin_terminal_id, destination_terminal_id))
    if len(set(normalized_pairs)) != len(normalized_pairs):
        raise CoverageDiscoveryError("coverage contains duplicate terminal pairs")
    if normalized_pairs and discovery.sellable_through is None:
        raise CoverageDiscoveryError("supported routes need a sale horizon")

    return CoverageDiscovery(
        source_slug=discovery.source_slug,
        supported_terminal_pairs=tuple(sorted(normalized_pairs)),
        sellable_through=discovery.sellable_through,
        observed_at=discovery.observed_at,
        diagnostic_code=discovery.diagnostic_code,
    )


def _intent_diagnostic(scheduled_for: date, metadata: AdapterMetadata) -> str:
    return (
        "COVERAGE_DISCOVERY_PENDING; "
        f"scheduled_for={scheduled_for.isoformat()}; "
        f"connector={metadata.connector_version}; parser={metadata.parser_version}"
    )[:500]


def _coverage_provenance(
    discovery: CoverageDiscovery,
    *,
    metadata: AdapterMetadata,
    scheduled_for: date,
) -> str:
    return (
        "coverage-discovery-v1; "
        f"diagnostic={discovery.diagnostic_code}; "
        f"connector={metadata.connector_version}; parser={metadata.parser_version}; "
        f"scheduled_for={scheduled_for.isoformat()}; "
        f"observed_at={discovery.observed_at.isoformat()}; "
        f"fingerprint={_coverage_fingerprint(discovery)}"
    )[:500]


def _coverage_fingerprint(discovery: CoverageDiscovery) -> str:
    payload = {
        "source": discovery.source_slug,
        "pairs": discovery.supported_terminal_pairs,
        "sellable_through": (
            discovery.sellable_through.isoformat()
            if discovery.sellable_through is not None
            else None
        ),
        "observed_at": discovery.observed_at.isoformat(),
        "diagnostic": discovery.diagnostic_code,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _discovery_failure_manifest(
    scheduled_for: date, metadata: tuple[AdapterMetadata, ...]
) -> str:
    payload = {
        "phase": "coverage_discovery_failed",
        "scheduled_for": scheduled_for.isoformat(),
        "sources": [
            {
                "slug": item.source_slug,
                "enabled": item.enabled,
                "connector": item.connector_version,
                "parser": item.parser_version,
            }
            for item in sorted(metadata, key=lambda value: value.source_slug)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _prior_failure_streak(
    session: Session, *, source_slug: str, before_scheduled_for: date
) -> int:
    return (
        session.scalar(
            select(ExtractorRun.consecutive_failures)
            .join(CrawlRun, CrawlRun.id == ExtractorRun.crawl_run_id)
            .where(
                ExtractorRun.source_id == source_slug,
                CrawlRun.scheduled_for < before_scheduled_for,
                CrawlRun.is_synthetic.is_(False),
            )
            .order_by(CrawlRun.scheduled_for.desc(), ExtractorRun.id.desc())
            .limit(1)
        )
        or 0
    )


def _record_health_once(
    session: Session,
    *,
    source_slug: str,
    event_type: str,
    severity: str,
    diagnostic: str,
) -> None:
    existing = session.scalar(
        select(HealthEvent.id).where(
            HealthEvent.crawl_run_id.is_(None),
            HealthEvent.source_id == source_slug,
            HealthEvent.event_type == event_type,
            HealthEvent.diagnostic == diagnostic,
        )
    )
    if existing is None:
        session.add(
            HealthEvent(
                crawl_run_id=None,
                source_id=source_slug,
                event_type=event_type,
                severity=severity,
                diagnostic=diagnostic[:500],
            )
        )


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CoverageDiscoveryError(f"{label} must be timezone-aware")
