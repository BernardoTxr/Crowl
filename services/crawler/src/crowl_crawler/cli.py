from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from crowl_core.catalog import ensure_default_profile_and_rules
from crowl_core.db import create_database_engine, create_session_factory
from crowl_core.models import City, CityTerminalLink, OriginTerminal, Terminal
from crowl_core.travel_windows import generate_travel_windows
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .adapters.registry import AdapterRegistry, build_default_registry
from .batch import BatchCoordinator
from .coverage import (
    CoverageDiscoveryError,
    discover_and_persist_coverage,
    record_coverage_discovery_failure,
)
from .fx import FxSyncError, FxSyncResult, sync_ecb_reference_rates
from .health import CompositeHealthSink, InMemoryHealthSink, JsonLinesHealthSink
from .lifecycle import DailyRunLease, DatabaseRunLifecycle
from .manifest import (
    BatchManifest,
    DailyManifestBuilder,
    DestinationTarget,
    FIXTURE_DESTINATIONS,
)
from .models import BatchStatus, CoverageDiscovery
from .persistence import DatabasePublisher, PublicationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crowl-crawler",
        description="Run and inspect Crowl's one-shot fare crawler.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sources = subparsers.add_parser("sources", help="List known source adapter slots.")
    sources.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    sources.add_argument(
        "--include-fixture",
        action="store_true",
        help="Include the local fixture adapter in addition to the 14 requested sources.",
    )

    run = subparsers.add_parser(
        "run",
        help="Run and publish the configured six-month provider batch.",
    )
    run.add_argument(
        "--date",
        type=_iso_date,
        help="Scheduled batch date (YYYY-MM-DD); defaults to today in Europe/Paris.",
    )
    run.add_argument(
        "--pretty", action="store_true", help="Pretty-print the publication report."
    )

    fixture = subparsers.add_parser(
        "fixture-batch", help="Run a deterministic two-city fixture batch."
    )
    fixture.add_argument(
        "--date",
        type=_iso_date,
        help="Scheduled batch date (YYYY-MM-DD); defaults to today in Europe/Paris.",
    )
    fixture.add_argument(
        "--health-jsonl",
        type=Path,
        help="Optional file receiving sanitized health events as JSON Lines.",
    )
    fixture.add_argument(
        "--pretty", action="store_true", help="Pretty-print the batch report."
    )

    publish = subparsers.add_parser(
        "fixture-publish",
        help="Run and atomically publish the deterministic six-month fixture batch.",
    )
    publish.add_argument(
        "--date",
        type=_iso_date,
        help="Scheduled batch date (YYYY-MM-DD); defaults to today in Europe/Paris.",
    )
    publish.add_argument(
        "--pretty", action="store_true", help="Pretty-print the publication report."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sources":
        return _list_sources(as_json=args.json, include_fixture=args.include_fixture)
    if args.command == "run":
        return asyncio.run(_production_run(scheduled_for=args.date, pretty=args.pretty))
    if args.command == "fixture-batch":
        return asyncio.run(
            _fixture_batch(
                scheduled_for=args.date,
                health_jsonl=args.health_jsonl,
                pretty=args.pretty,
            )
        )
    if args.command == "fixture-publish":
        return asyncio.run(
            _fixture_publish(scheduled_for=args.date, pretty=args.pretty)
        )
    raise AssertionError(f"unhandled command: {args.command}")


def entrypoint() -> None:
    raise SystemExit(main())


def _list_sources(*, as_json: bool, include_fixture: bool) -> int:
    registry = build_default_registry(include_fixture=include_fixture)
    metadata = registry.metadata()
    if as_json:
        rows = [_jsonable(asdict(item)) for item in metadata]
        print(json.dumps(rows, sort_keys=True, indent=2))
        return 0

    headings = ("SOURCE", "MODE", "STATE", "STRATEGY", "INTENDED STRATEGY")
    rendered = [
        (
            item.source_slug,
            ",".join(mode.value for mode in item.modes),
            "enabled" if item.enabled else "disabled",
            item.strategy.value,
            item.intended_strategy.value,
        )
        for item in metadata
    ]
    widths = [
        max(len(headings[index]), *(len(str(row[index])) for row in rendered))
        for index in range(len(headings))
    ]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(headings)))
    for row in rendered:
        print(
            "  ".join(
                str(value).ljust(widths[index]) for index, value in enumerate(row)
            )
        )
    return 0


async def _fixture_batch(
    *, scheduled_for: date | None, health_jsonl: Path | None, pretty: bool
) -> int:
    paris = ZoneInfo("Europe/Paris")
    run_date = scheduled_for or datetime.now(paris).date()
    first_friday = run_date + timedelta(days=(4 - run_date.weekday()) % 7)
    builder = DailyManifestBuilder(
        origin_terminal_ids=(
            "par-cdg",
            "par-ory",
            "par-gare-du-nord",
        ),
        config_version="fixture-v1",
    )
    manifest = builder.build(
        scheduled_for=run_date,
        source_slugs=("fixture",),
        destinations=(
            FIXTURE_DESTINATIONS["amsterdam-nl"],
            FIXTURE_DESTINATIONS["lisbon-pt"],
        ),
        weekend_fridays=(first_friday, first_friday + timedelta(days=7)),
    )
    memory_sink = InMemoryHealthSink()
    health_sink = (
        CompositeHealthSink(memory_sink, JsonLinesHealthSink(health_jsonl))
        if health_jsonl
        else memory_sink
    )
    coordinator = BatchCoordinator(
        build_default_registry(include_fixture=True), health_sink=health_sink
    )
    report = await coordinator.run(manifest)
    output = report.as_dict()
    output["health_event_count"] = len(memory_sink.events)
    print(
        json.dumps(
            output,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )
    return 0 if report.status in {BatchStatus.SUCCEEDED, BatchStatus.PARTIAL} else 1


async def _fixture_publish(*, scheduled_for: date | None, pretty: bool) -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("fixture-publish requires DATABASE_URL", file=sys.stderr)
        return 2

    paris = ZoneInfo("Europe/Paris")
    run_date = scheduled_for or datetime.now(paris).date()
    weekend_fridays = tuple(
        window.friday for window in generate_travel_windows(run_date, months_ahead=6)
    )
    builder = DailyManifestBuilder(
        origin_terminal_ids=("par-cdg", "par-ory", "par-gare-du-nord"),
        config_version="fixture-v1",
    )
    manifest = builder.build(
        scheduled_for=run_date,
        source_slugs=("fixture",),
        destinations=tuple(FIXTURE_DESTINATIONS.values()),
        weekend_fridays=weekend_fridays,
    )
    health_sink = InMemoryHealthSink()
    coordinator = BatchCoordinator(
        build_default_registry(include_fixture=True), health_sink=health_sink
    )
    report = await coordinator.run(manifest)
    if report.status not in {BatchStatus.SUCCEEDED, BatchStatus.PARTIAL}:
        print(json.dumps(report.as_dict(), sort_keys=True), file=sys.stderr)
        return 1

    engine = create_database_engine(database_url)
    try:
        publication = DatabasePublisher(create_session_factory(engine)).publish(
            manifest,
            report,
            health_events=health_sink.events,
        )
    except PublicationError as error:
        print(f"fixture publication rejected: {error}", file=sys.stderr)
        return 1
    except SQLAlchemyError as error:
        print(
            f"fixture publication database error: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()

    output = report.as_dict()
    output["publication"] = publication.as_dict()
    print(
        json.dumps(
            output,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )
    return 0


async def _production_run(
    *,
    scheduled_for: date | None,
    pretty: bool,
    registry: AdapterRegistry | None = None,
    fx_syncer: Callable[[sessionmaker[Session]], FxSyncResult] | None = (
        sync_ecb_reference_rates
    ),
) -> int:
    """Execute the configured providers, including honest disabled reports.

    The default registry intentionally contains no live provider until an
    authorized adapter is installed. Such a run still persists the fourteen
    disabled extractor states and zero live-source scope instead of fabricating
    observations or failing the daily Railway job.
    """

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("run requires DATABASE_URL", file=sys.stderr)
        return 2

    paris = ZoneInfo("Europe/Paris")
    run_date = scheduled_for or datetime.now(paris).date()
    configured_registry = registry or build_default_registry(include_fixture=False)
    source_metadata = configured_registry.metadata(include_fixture=False)
    engine = create_database_engine(database_url)
    sessions = create_session_factory(engine)
    lease = DailyRunLease(engine, run_date)
    lifecycle: DatabaseRunLifecycle | None = None
    manifest: BatchManifest | None = None
    prepared = False
    fx_sync_payload: dict[str, object] = {"status": "skipped"}
    try:
        if not lease.acquire():
            output = {
                "scheduled_for": run_date.isoformat(),
                "status": "already_running",
            }
            print(
                json.dumps(
                    output,
                    sort_keys=True,
                    indent=2 if pretty else None,
                    separators=None if pretty else (",", ":"),
                )
            )
            return 0
        if fx_syncer is not None:
            try:
                fx_sync_payload = fx_syncer(sessions).as_dict()
            except FxSyncError as error:
                fx_sync_payload = {
                    "status": "failed",
                    "diagnostic": f"FX_SYNC_{error.code}",
                }
                print(
                    f"FX reference-rate sync warning: FX_SYNC_{error.code}",
                    file=sys.stderr,
                )
            except SQLAlchemyError:
                fx_sync_payload = {
                    "status": "failed",
                    "diagnostic": "FX_SYNC_DATABASE_ERROR",
                }
                print(
                    "FX reference-rate sync warning: FX_SYNC_DATABASE_ERROR",
                    file=sys.stderr,
                )
        coverage = await discover_and_persist_coverage(
            sessions,
            configured_registry,
            scheduled_for=run_date,
        )
        manifest = _build_production_manifest(
            sessions,
            configured_registry,
            scheduled_for=run_date,
            source_coverage=coverage,
        )
        lifecycle = DatabaseRunLifecycle(sessions)
        resume_plan = lifecycle.prepare(
            manifest,
            source_metadata=source_metadata,
        )
        prepared = True
        health_sink = InMemoryHealthSink()
        coordinator = BatchCoordinator(
            configured_registry,
            health_sink=health_sink,
            execution_recorder=lambda execution: lifecycle.record_execution(
                manifest, execution
            ),
        )
        report = await coordinator.run(
            manifest,
            deadline_at=_batch_deadline(),
            resumed_executions=resume_plan.resumed_executions,
            prior_attempts=resume_plan.prior_attempts,
        )
        publication = DatabasePublisher(sessions).publish(
            manifest,
            report,
            health_events=health_sink.events,
            source_metadata=source_metadata,
        )
    except CoverageDiscoveryError as error:
        release = os.environ.get("CROWL_CRAWLER_RELEASE", "production-v1").strip()
        record_coverage_discovery_failure(
            sessions,
            scheduled_for=run_date,
            source_metadata=source_metadata,
            failed_source_slugs=error.failed_source_slugs,
            release_version=release or "invalid-release",
        )
        print(f"coverage discovery rejected: {error}", file=sys.stderr)
        return 1
    except PublicationError as error:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_failed(manifest, "PUBLICATION_REJECTED")
        print(f"publication rejected: {error}", file=sys.stderr)
        return 1
    except SQLAlchemyError as error:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_failed(manifest, "DATABASE_ERROR")
        print(
            f"crawler database error: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_abandoned(manifest, "COLLECTION_INTERRUPTED")
        print(
            f"crawler collection interrupted: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    except BaseException:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_abandoned(manifest, "PROCESS_INTERRUPTED")
        raise
    finally:
        lease.release()
        engine.dispose()

    output = _operational_report(report.as_dict())
    output["publication"] = publication.as_dict()
    output["fx_sync"] = fx_sync_payload
    print(
        json.dumps(
            output,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )
    return 0 if report.status in {BatchStatus.SUCCEEDED, BatchStatus.PARTIAL} else 1


def _build_production_manifest(
    session_factory: sessionmaker[Session],
    registry: AdapterRegistry,
    *,
    scheduled_for: date,
    source_coverage: Mapping[str, CoverageDiscovery] | None = None,
) -> BatchManifest:
    """Resolve the immutable daily manifest from the seeded SQL catalog."""

    with session_factory() as session, session.begin():
        _, rule = ensure_default_profile_and_rules(session)
        origin_ids = tuple(
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
                .order_by(OriginTerminal.terminal_id)
            ).all()
        )
        cities = session.scalars(
            select(City).where(City.active.is_(True)).order_by(City.id)
        ).all()
        links = session.execute(
            select(CityTerminalLink.city_id, CityTerminalLink.terminal_id)
            .join(Terminal, Terminal.id == CityTerminalLink.terminal_id)
            .where(
                CityTerminalLink.city_id.in_([city.id for city in cities]),
                Terminal.active.is_(True),
                Terminal.schengen_eligible.is_(True),
            )
            .order_by(CityTerminalLink.city_id, CityTerminalLink.terminal_id)
        ).all()

    if not origin_ids:
        raise PublicationError(
            "catalog has no active Schengen origin terminals for the current rule"
        )
    if not cities:
        raise PublicationError("catalog has no active destination cities")

    terminals_by_city: dict[str, list[str]] = {city.id: [] for city in cities}
    for city_id, terminal_id in links:
        terminals_by_city[city_id].append(terminal_id)
    missing = sorted(
        city_id
        for city_id, terminal_ids in terminals_by_city.items()
        if not terminal_ids
    )
    if missing:
        raise PublicationError(
            "active cities are missing Schengen terminal links: " + ", ".join(missing)
        )

    source_metadata = registry.metadata(include_fixture=False)
    source_slugs = tuple(item.source_slug for item in source_metadata)
    if not source_slugs:
        raise PublicationError("no requested provider adapters are configured")
    destinations = tuple(
        DestinationTarget(city.id, tuple(terminals_by_city[city.id])) for city in cities
    )
    weekend_fridays = tuple(
        window.friday
        for window in generate_travel_windows(scheduled_for, months_ahead=6)
    )
    release = os.environ.get("CROWL_CRAWLER_RELEASE", "production-v1").strip()
    if not release:
        raise PublicationError("CROWL_CRAWLER_RELEASE may not be empty")
    enabled_source_slugs = tuple(
        item.source_slug for item in source_metadata if item.enabled
    )
    coverage = source_coverage or {}
    if set(coverage) != set(enabled_source_slugs):
        raise PublicationError(
            "validated coverage discovery does not match enabled providers"
        )
    executable_manifest = DailyManifestBuilder(
        origin_terminal_ids=origin_ids,
        config_version=release,
    ).build(
        scheduled_for=scheduled_for,
        source_slugs=enabled_source_slugs,
        destinations=destinations,
        weekend_fridays=weekend_fridays,
        include_one_way_tasks=True,
        source_coverage=coverage,
    )
    # Disabled adapters remain expected sources so the coordinator and database
    # expose their state, but they do not create tens of thousands of fake
    # per-route search/coverage rows.
    return BatchManifest.create(
        batch_id=executable_manifest.batch_id,
        scheduled_for=executable_manifest.scheduled_for,
        timezone=executable_manifest.timezone,
        config_version=executable_manifest.config_version,
        expected_source_slugs=source_slugs,
        coverage_cells=executable_manifest.coverage_cells,
        tasks=executable_manifest.tasks,
        created_at=executable_manifest.created_at,
    )


def _batch_deadline() -> datetime:
    raw_hours = os.environ.get("CROWL_BATCH_DEADLINE_HOURS", "20")
    try:
        hours = float(raw_hours)
    except ValueError as error:
        raise PublicationError(
            "CROWL_BATCH_DEADLINE_HOURS must be a positive number"
        ) from error
    if hours <= 0:
        raise PublicationError("CROWL_BATCH_DEADLINE_HOURS must be positive")
    return datetime.now(UTC) + timedelta(hours=hours)


def _operational_report(payload: dict[str, object]) -> dict[str, object]:
    """Keep cron output bounded while retaining every per-source state."""

    sources = payload.get("sources", [])
    compact_sources = []
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                compact_sources.append(
                    {key: value for key, value in source.items() if key != "executions"}
                )
    return {**payload, "sources": compact_sources}


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


if __name__ == "__main__":
    sys.exit(main())
