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
from crowl_core.models import (
    City,
    CityTerminalLink,
    CrawlRun,
    OriginTerminal,
    Source,
    Terminal,
)
from crowl_core.travel_windows import generate_travel_windows
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .adapters.registry import (
    REQUESTED_SOURCE_SPECS,
    AdapterRegistry,
    CanaryPolicyError,
    build_default_registry,
)
from .batch import BatchCoordinator, BatchReport
from .canary import (
    CanaryConfigurationError,
    CanaryExecutionError,
    CanaryInputError,
    canary_succeeded,
    run_live_canary,
)
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
from .models import (
    BatchStatus,
    CoverageDiscovery,
    ExtractorRunStatus,
    SearchKind,
    SearchOutcome,
)
from .persistence import DatabasePublisher, PublicationError


_ACTIVATION_PILOT_SOURCE = "flixbus"
_ACTIVATION_PILOT_TIMEOUT_SECONDS = 120.0
_REQUESTED_SOURCE_SLUGS = tuple(spec.slug for spec in REQUESTED_SOURCE_SPECS)


class ActivationPilotRefusal(ValueError):
    """The activation pilot is unsafe or outside its explicitly bounded scope."""


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

    canary = subparsers.add_parser(
        "live-canary",
        help="Run one real public-page query without database persistence.",
    )
    canary.add_argument("--source", required=True, help="One source slug.")
    canary.add_argument(
        "--origin-terminal", required=True, help="One Paris/Massy terminal ID."
    )
    canary.add_argument(
        "--destination-city", required=True, help="One destination city ID."
    )
    canary.add_argument(
        "--destination-terminal", required=True, help="One linked terminal ID."
    )
    canary.add_argument(
        "--weekend",
        required=True,
        type=_iso_date,
        help="Weekend Friday (YYYY-MM-DD).",
    )
    canary.add_argument(
        "--override-kill-switch-for-canary",
        action="store_true",
        help=(
            "Allow this one DB-free invocation despite catalog disablement; "
            "does not edit sources.yaml or enable production."
        ),
    )
    canary.add_argument(
        "--pretty", action="store_true", help="Pretty-print the canary report."
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

    pilot = subparsers.add_parser(
        "activation-pilot-publish",
        help=(
            "Consume one operational daily slot to collect and atomically publish "
            "one explicitly scoped FlixBus round trip."
        ),
    )
    pilot.add_argument(
        "--source",
        required=True,
        choices=("flixbus",),
        help="The only provider permitted for the activation pilot.",
    )
    pilot.add_argument(
        "--origin-terminal",
        required=True,
        help="One exact allowed Paris/Massy origin terminal ID.",
    )
    pilot.add_argument(
        "--destination-city",
        required=True,
        help="One exact monitored destination city ID.",
    )
    pilot.add_argument(
        "--destination-terminal",
        required=True,
        help="One exact terminal ID linked to the destination city.",
    )
    pilot.add_argument(
        "--weekend",
        required=True,
        type=_iso_date,
        help="Weekend Friday (YYYY-MM-DD).",
    )
    pilot.add_argument(
        "--date",
        type=_iso_date,
        help="Operational daily slot (YYYY-MM-DD); defaults to today in Europe/Paris.",
    )
    pilot.add_argument(
        "--acknowledge-daily-slot",
        required=True,
        action="store_true",
        help=(
            "Acknowledge that the pilot permanently consumes the date's one "
            "non-synthetic crawl slot, including on a no-offer result."
        ),
    )
    pilot.add_argument(
        "--pretty", action="store_true", help="Pretty-print the publication report."
    )

    first_weekend = subparsers.add_parser(
        "first-weekend-publish",
        help=(
            "Collect one provider across all catalog cities for one weekend and "
            "atomically publish the bounded operational batch."
        ),
    )
    first_weekend.add_argument(
        "--source",
        required=True,
        choices=("ryanair",),
        help="The bounded provider to collect.",
    )
    first_weekend.add_argument(
        "--weekend", required=True, type=_iso_date, help="Weekend Friday (YYYY-MM-DD)."
    )
    first_weekend.add_argument(
        "--date", required=True, type=_iso_date, help="Unused operational batch date."
    )
    first_weekend.add_argument(
        "--acknowledge-daily-slot",
        required=True,
        action="store_true",
        help="Acknowledge that this bounded activation consumes the supplied date slot.",
    )
    first_weekend.add_argument(
        "--pretty", action="store_true", help="Pretty-print the publication report."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sources":
        return _list_sources(as_json=args.json, include_fixture=args.include_fixture)
    if args.command == "run":
        return asyncio.run(_production_run(scheduled_for=args.date, pretty=args.pretty))
    if args.command == "live-canary":
        return asyncio.run(
            _live_canary_command(
                source_slug=args.source,
                origin_terminal_id=args.origin_terminal,
                destination_city_id=args.destination_city,
                destination_terminal_id=args.destination_terminal,
                weekend_start=args.weekend,
                override_kill_switch_for_canary=(args.override_kill_switch_for_canary),
                pretty=args.pretty,
            )
        )
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
    if args.command == "activation-pilot-publish":
        return asyncio.run(
            _activation_pilot_publish(
                source_slug=args.source,
                origin_terminal_id=args.origin_terminal,
                destination_city_id=args.destination_city,
                destination_terminal_id=args.destination_terminal,
                weekend_start=args.weekend,
                scheduled_for=args.date,
                acknowledge_daily_slot=args.acknowledge_daily_slot,
                pretty=args.pretty,
            )
        )
    if args.command == "first-weekend-publish":
        return asyncio.run(
            _first_weekend_publish(
                source_slug=args.source,
                weekend_start=args.weekend,
                scheduled_for=args.date,
                acknowledge_daily_slot=args.acknowledge_daily_slot,
                pretty=args.pretty,
            )
        )
    raise AssertionError(f"unhandled command: {args.command}")


async def _live_canary_command(
    *,
    source_slug: str,
    origin_terminal_id: str,
    destination_city_id: str,
    destination_terminal_id: str,
    weekend_start: date,
    override_kill_switch_for_canary: bool,
    pretty: bool,
) -> int:
    try:
        report = await run_live_canary(
            source_slug=source_slug,
            origin_terminal_id=origin_terminal_id,
            destination_city_id=destination_city_id,
            destination_terminal_id=destination_terminal_id,
            weekend_start=weekend_start,
            override_kill_switch_for_canary=override_kill_switch_for_canary,
        )
    except (
        CanaryPolicyError,
        CanaryInputError,
        CanaryConfigurationError,
    ) as error:
        print(f"live-canary refused: {error}", file=sys.stderr)
        return 2
    except CanaryExecutionError as error:
        print(f"live-canary failed: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            report,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )
    return 0 if canary_succeeded(report) else 1


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


async def _activation_pilot_publish(
    *,
    source_slug: str,
    origin_terminal_id: str,
    destination_city_id: str,
    destination_terminal_id: str,
    weekend_start: date,
    scheduled_for: date | None,
    acknowledge_daily_slot: bool,
    pretty: bool,
    registry: AdapterRegistry | None = None,
) -> int:
    """Publish one real, explicitly acknowledged FlixBus round-trip cell.

    This path deliberately consumes the normal non-synthetic daily slot.  It
    never overrides catalog policy, never resumes or replaces another daily
    run, and calls the publisher only after one successful result contains at
    least one normalized offer.
    """

    if not acknowledge_daily_slot:
        print(
            "activation-pilot-publish refused: --acknowledge-daily-slot is required",
            file=sys.stderr,
        )
        return 2
    if source_slug != _ACTIVATION_PILOT_SOURCE:
        print(
            "activation-pilot-publish refused: only --source flixbus is permitted",
            file=sys.stderr,
        )
        return 2
    if weekend_start.weekday() != 4:
        print(
            "activation-pilot-publish refused: --weekend must be a Friday",
            file=sys.stderr,
        )
        return 2

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("activation-pilot-publish requires DATABASE_URL", file=sys.stderr)
        return 2

    paris = ZoneInfo("Europe/Paris")
    run_date = scheduled_for or datetime.now(paris).date()
    operational_weekends = {
        window.friday for window in generate_travel_windows(run_date, months_ahead=6)
    }
    if weekend_start not in operational_weekends:
        print(
            "activation-pilot-publish refused: --weekend must be inside the "
            "configured six-month look-ahead",
            file=sys.stderr,
        )
        return 2

    configured_registry = registry or build_default_registry(include_fixture=False)
    source_metadata = configured_registry.metadata(include_fixture=False)
    metadata_by_slug = {item.source_slug: item for item in source_metadata}
    if (
        len(_REQUESTED_SOURCE_SLUGS) != 14
        or len(metadata_by_slug) != len(_REQUESTED_SOURCE_SLUGS)
        or set(metadata_by_slug) != set(_REQUESTED_SOURCE_SLUGS)
    ):
        print(
            "activation-pilot-publish refused: registry must contain all 14 requested sources",
            file=sys.stderr,
        )
        return 2
    selected_metadata = metadata_by_slug[source_slug]
    if not selected_metadata.enabled:
        print(
            "activation-pilot-publish refused: FlixBus is not enabled by source policy",
            file=sys.stderr,
        )
        return 2
    enabled_source_slugs = {
        item.source_slug for item in source_metadata if item.enabled
    }
    if enabled_source_slugs != {_ACTIVATION_PILOT_SOURCE}:
        print(
            "activation-pilot-publish refused: registry must enable exactly FlixBus",
            file=sys.stderr,
        )
        return 2

    engine = create_database_engine(database_url)
    sessions = create_session_factory(engine)
    lease = DailyRunLease(engine, run_date)
    lifecycle: DatabaseRunLifecycle | None = None
    manifest: BatchManifest | None = None
    prepared = False
    try:
        if not lease.acquire():
            raise ActivationPilotRefusal(
                "the non-synthetic daily slot is currently leased"
            )

        with sessions() as session:
            existing = session.scalar(
                select(CrawlRun).where(
                    CrawlRun.scheduled_for == run_date,
                    CrawlRun.is_synthetic.is_(False),
                )
            )
            if existing is not None:
                raise ActivationPilotRefusal(
                    "a non-synthetic crawl run already exists for this date"
                )
            _validate_activation_pilot_scope(
                session,
                source_slug=source_slug,
                origin_terminal_id=origin_terminal_id,
                destination_city_id=destination_city_id,
                destination_terminal_id=destination_terminal_id,
            )

        release = os.environ.get("CROWL_CRAWLER_RELEASE", "activation-pilot-v1").strip()
        if not release:
            raise ActivationPilotRefusal("CROWL_CRAWLER_RELEASE may not be empty")
        executable = DailyManifestBuilder(
            origin_terminal_ids=(origin_terminal_id,),
            config_version=release,
        ).build(
            scheduled_for=run_date,
            source_slugs=(source_slug,),
            destinations=(
                DestinationTarget(
                    destination_city_id,
                    (destination_terminal_id,),
                ),
            ),
            weekend_fridays=(weekend_start,),
            canary_per_source=False,
            include_one_way_tasks=False,
        )
        manifest = BatchManifest.create(
            batch_id=executable.batch_id,
            scheduled_for=executable.scheduled_for,
            timezone=executable.timezone,
            config_version=executable.config_version,
            expected_source_slugs=_REQUESTED_SOURCE_SLUGS,
            coverage_cells=executable.coverage_cells,
            tasks=executable.tasks,
            created_at=executable.created_at,
        )
        _validate_activation_pilot_manifest(manifest, source_slug=source_slug)

        lifecycle = DatabaseRunLifecycle(sessions)
        resume_plan = lifecycle.prepare(manifest, source_metadata=source_metadata)
        if resume_plan.is_resume:
            raise PublicationError("activation pilot may not resume a daily run")
        prepared = True
        health_sink = InMemoryHealthSink()
        coordinator = BatchCoordinator(
            configured_registry,
            health_sink=health_sink,
            request_timeout_seconds=_ACTIVATION_PILOT_TIMEOUT_SECONDS,
            execution_recorder=lambda execution: lifecycle.record_execution(
                manifest, execution
            ),
        )
        report = await coordinator.run(
            manifest,
            deadline_at=(
                datetime.now(UTC) + timedelta(seconds=_ACTIVATION_PILOT_TIMEOUT_SECONDS)
            ),
        )
        if not _activation_pilot_has_publishable_result(
            report, source_slug=source_slug
        ):
            lifecycle.finalize_failed_without_publication(
                manifest,
                report,
                health_events=health_sink.events,
                diagnostic_code="PILOT_NO_PUBLISHABLE_OFFER",
            )
            print(
                "activation-pilot-publish stopped: FlixBus did not return one "
                "successful result with a normalized offer; nothing was published",
                file=sys.stderr,
            )
            return 1

        publication = DatabasePublisher(sessions).publish(
            manifest,
            report,
            health_events=health_sink.events,
            source_metadata=source_metadata,
            require_at_least_one_winner=True,
        )
        if publication.snapshots_created < 1:
            # The publisher guard above must reject this before commit. Keep a
            # defensive postcondition in case a custom publisher violates it.
            raise PublicationError("activation pilot published no fare snapshot")
    except ActivationPilotRefusal as error:
        print(f"activation-pilot-publish refused: {error}", file=sys.stderr)
        return 2
    except PublicationError as error:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_failed(manifest, "PILOT_PUBLICATION_REJECTED")
        print(f"activation-pilot-publish rejected: {error}", file=sys.stderr)
        return 1
    except SQLAlchemyError as error:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_failed(manifest, "PILOT_DATABASE_ERROR")
        print(
            f"activation-pilot-publish database error: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_abandoned(manifest, "PILOT_COLLECTION_INTERRUPTED")
        print(
            f"activation-pilot-publish interrupted: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    except BaseException:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_abandoned(manifest, "PILOT_PROCESS_INTERRUPTED")
        raise
    finally:
        lease.release()
        engine.dispose()

    output = _operational_report(report.as_dict())
    output["kind"] = "activation_pilot_publish"
    output["scope"] = {
        "source": source_slug,
        "origin_terminal_id": origin_terminal_id,
        "destination_city_id": destination_city_id,
        "destination_terminal_id": destination_terminal_id,
        "weekend": weekend_start.isoformat(),
        "scheduled_for": run_date.isoformat(),
        "search_kind": SearchKind.ROUND_TRIP.value,
    }
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


def _validate_activation_pilot_scope(
    session: Session,
    *,
    source_slug: str,
    origin_terminal_id: str,
    destination_city_id: str,
    destination_terminal_id: str,
) -> None:
    source = session.get(Source, source_slug)
    if source is None or not source.is_requested or not source.enabled:
        raise ActivationPilotRefusal(
            "FlixBus is not enabled in the persisted source catalog"
        )

    _, rule = ensure_default_profile_and_rules(session)
    origin = session.get(Terminal, origin_terminal_id)
    if (
        origin is None
        or not origin.active
        or not origin.schengen_eligible
        or origin.kind.value != "bus"
    ):
        raise ActivationPilotRefusal(
            "origin terminal is not an active Schengen bus terminal"
        )
    allowed_origin = session.scalar(
        select(OriginTerminal).where(
            OriginTerminal.rule_version_id == rule.id,
            OriginTerminal.terminal_id == origin_terminal_id,
            OriginTerminal.permits_departure.is_(True),
            OriginTerminal.permits_return.is_(True),
        )
    )
    if allowed_origin is None:
        raise ActivationPilotRefusal(
            "origin terminal is not an allowed Paris/Massy round-trip terminal"
        )

    destination_city = session.get(City, destination_city_id)
    destination = session.get(Terminal, destination_terminal_id)
    if destination_city is None or not destination_city.active:
        raise ActivationPilotRefusal("destination city is not active")
    if (
        destination is None
        or not destination.active
        or not destination.schengen_eligible
        or destination.kind.value != "bus"
    ):
        raise ActivationPilotRefusal(
            "destination terminal is not an active Schengen bus terminal"
        )
    if origin_terminal_id == destination_terminal_id:
        raise ActivationPilotRefusal("origin and destination terminals must differ")
    linked = session.scalar(
        select(CityTerminalLink).where(
            CityTerminalLink.city_id == destination_city_id,
            CityTerminalLink.terminal_id == destination_terminal_id,
        )
    )
    if linked is None:
        raise ActivationPilotRefusal(
            "destination terminal is not linked to the selected destination city"
        )


def _validate_activation_pilot_manifest(
    manifest: BatchManifest, *, source_slug: str
) -> None:
    if tuple(manifest.expected_source_slugs) != _REQUESTED_SOURCE_SLUGS:
        raise PublicationError(
            "activation pilot must retain all 14 requested source slugs"
        )
    if len(manifest.coverage_cells) != 1 or len(manifest.tasks) != 1:
        raise PublicationError(
            "activation pilot must contain exactly one logical cell and task"
        )
    task = manifest.tasks[0]
    cell = manifest.coverage_cells[0]
    if (
        task.source_slug != source_slug
        or cell.source_slug != source_slug
        or task.request.search_kind is not SearchKind.ROUND_TRIP
        or task.request.direction is not None
        or task.is_canary
        or cell.direction != SearchKind.ROUND_TRIP.value
    ):
        raise PublicationError(
            "activation pilot executable work must be one FlixBus round trip"
        )


def _activation_pilot_has_publishable_result(
    report: BatchReport, *, source_slug: str
) -> bool:
    if report.status is not BatchStatus.SUCCEEDED:
        return False
    source_reports = tuple(
        item for item in report.source_reports if item.source_slug == source_slug
    )
    if len(source_reports) != 1:
        return False
    source_report = source_reports[0]
    if (
        source_report.state is not ExtractorRunStatus.SUCCEEDED
        or len(source_report.executions) != 1
    ):
        return False
    result = source_report.executions[0].result
    return (
        result.outcome is SearchOutcome.SUCCESS_RESULTS
        and result.completed_count >= 1
        and len(result.offers) >= 1
    )


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

    Only enabled providers with validated coverage receive fare-search tasks.
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


async def _first_weekend_publish(
    *,
    source_slug: str,
    weekend_start: date,
    scheduled_for: date,
    acknowledge_daily_slot: bool,
    pretty: bool,
) -> int:
    """Publish one real provider/weekend across every editorial city."""

    if not acknowledge_daily_slot:
        print(
            "first-weekend-publish refused: --acknowledge-daily-slot is required",
            file=sys.stderr,
        )
        return 2
    if weekend_start.weekday() != 4:
        print("first-weekend-publish refused: --weekend must be a Friday", file=sys.stderr)
        return 2
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("first-weekend-publish requires DATABASE_URL", file=sys.stderr)
        return 2
    if weekend_start not in {
        item.friday for item in generate_travel_windows(scheduled_for, months_ahead=6)
    }:
        print(
            "first-weekend-publish refused: weekend is outside the six-month window",
            file=sys.stderr,
        )
        return 2

    registry = build_default_registry(include_fixture=False)
    source_metadata = registry.metadata(include_fixture=False)
    metadata = next(
        (item for item in source_metadata if item.source_slug == source_slug), None
    )
    if metadata is None or not metadata.enabled:
        print(
            f"first-weekend-publish refused: {source_slug} is not enabled by source policy",
            file=sys.stderr,
        )
        return 2

    engine = create_database_engine(database_url)
    sessions = create_session_factory(engine)
    lease = DailyRunLease(engine, scheduled_for)
    lifecycle: DatabaseRunLifecycle | None = None
    manifest: BatchManifest | None = None
    prepared = False
    try:
        if not lease.acquire():
            raise PublicationError("the selected operational date is currently leased")
        with sessions() as session:
            existing = session.scalar(
                select(CrawlRun).where(
                    CrawlRun.scheduled_for == scheduled_for,
                    CrawlRun.is_synthetic.is_(False),
                )
            )
            if existing is not None:
                raise PublicationError("the selected operational date is already occupied")

        selected_registry = AdapterRegistry((registry.get(source_slug),))
        coverage = await discover_and_persist_coverage(
            sessions,
            selected_registry,
            scheduled_for=scheduled_for,
        )
        manifest = _build_production_manifest(
            sessions,
            registry,
            scheduled_for=scheduled_for,
            source_coverage=coverage,
            enabled_source_filter=frozenset({source_slug}),
            weekend_fridays_override=(weekend_start,),
        )
        lifecycle = DatabaseRunLifecycle(sessions)
        resume_plan = lifecycle.prepare(manifest, source_metadata=source_metadata)
        if resume_plan.is_resume:
            raise PublicationError("bounded first-weekend activation may not resume")
        prepared = True
        health_sink = InMemoryHealthSink()
        coordinator = BatchCoordinator(
            registry,
            health_sink=health_sink,
            execution_recorder=lambda execution: lifecycle.record_execution(
                manifest, execution
            ),
        )
        report = await coordinator.run(manifest, deadline_at=_batch_deadline())
        publication = DatabasePublisher(sessions).publish(
            manifest,
            report,
            health_events=health_sink.events,
            source_metadata=source_metadata,
            require_at_least_one_winner=True,
        )
    except (PublicationError, CoverageDiscoveryError) as error:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_failed(manifest, "FIRST_WEEKEND_PUBLICATION_REJECTED")
        print(f"first-weekend-publish rejected: {error}", file=sys.stderr)
        return 1
    except SQLAlchemyError as error:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_failed(manifest, "FIRST_WEEKEND_DATABASE_ERROR")
        print(
            f"first-weekend-publish database error: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        if prepared and lifecycle is not None and manifest is not None:
            lifecycle.mark_abandoned(manifest, "FIRST_WEEKEND_INTERRUPTED")
        print(
            f"first-weekend-publish interrupted: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    finally:
        lease.release()
        engine.dispose()

    output = _operational_report(report.as_dict())
    output["kind"] = "first_weekend_publish"
    output["scope"] = {
        "source": source_slug,
        "weekend": weekend_start.isoformat(),
        "scheduled_for": scheduled_for.isoformat(),
        "catalog_cities": 150,
    }
    output["publication"] = publication.as_dict()
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
    enabled_source_filter: frozenset[str] | None = None,
    weekend_fridays_override: tuple[date, ...] | None = None,
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
    source_metadata = registry.metadata(include_fixture=False)
    source_slugs = tuple(item.source_slug for item in source_metadata)
    if not source_slugs:
        raise PublicationError("no requested provider adapters are configured")
    # Every editorial city receives a logical coverage cell. A city without a
    # terminal cannot create a network task, but validated source coverage can
    # still classify it honestly as an unsupported route.
    destinations = tuple(
        DestinationTarget(city.id, tuple(terminals_by_city[city.id]))
        for city in cities
    )
    weekend_fridays = weekend_fridays_override or tuple(
        window.friday for window in generate_travel_windows(scheduled_for, months_ahead=6)
    )
    release = os.environ.get("CROWL_CRAWLER_RELEASE", "production-v1").strip()
    if not release:
        raise PublicationError("CROWL_CRAWLER_RELEASE may not be empty")
    enabled_source_slugs = tuple(
        item.source_slug
        for item in source_metadata
        if item.enabled
        and (enabled_source_filter is None or item.source_slug in enabled_source_filter)
    )
    coverage = source_coverage or {}
    if set(coverage) - set(enabled_source_slugs) or (
        enabled_source_slugs and not coverage
    ):
        raise PublicationError(
            "validated coverage discovery does not match enabled providers"
        )
    executable_manifest = DailyManifestBuilder(
        origin_terminal_ids=origin_ids,
        config_version=release,
    ).build(
        scheduled_for=scheduled_for,
        source_slugs=tuple(slug for slug in enabled_source_slugs if slug in coverage),
        destinations=destinations,
        weekend_fridays=weekend_fridays,
        # A single provider's round-trip search already returns both exact
        # directions.  Creating two additional one-way tasks would triple the
        # public-page load without improving that provider-only batch.  Keep
        # one-way components when multiple sources are enabled so cross-source
        # outbound/return composition remains available.
        include_one_way_tasks=len(enabled_source_slugs) > 1,
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
        coverage_failed_source_slugs=set(enabled_source_slugs) - set(coverage),
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
