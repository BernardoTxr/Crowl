from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .models import (
    CoverageDiscovery,
    Direction,
    PassengerProfile,
    SearchKind,
    SearchOutcome,
    SearchRequest,
    TimeWindow,
)


@dataclass(frozen=True, slots=True)
class DestinationTarget:
    city_id: str
    terminal_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.city_id:
            raise ValueError("destination target needs a city")


@dataclass(frozen=True, slots=True)
class CoverageCell:
    cell_id: str
    source_slug: str
    destination_city_id: str
    weekend_start: date
    direction: str
    applicable: bool = True
    origin_terminal_id: str | None = None
    destination_terminal_id: str | None = None
    preclassified_outcome: SearchOutcome | None = None
    classified_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.cell_id or not self.source_slug or not self.destination_city_id:
            raise ValueError("coverage cell identifiers are required")
        if (self.origin_terminal_id is None) != (
            self.destination_terminal_id is None
        ):
            raise ValueError("coverage route terminals must be supplied together")
        if (self.preclassified_outcome is None) != (self.classified_at is None):
            raise ValueError(
                "preclassified coverage needs both an outcome and timestamp"
            )
        if self.classified_at is not None and (
            self.classified_at.tzinfo is None
            or self.classified_at.utcoffset() is None
        ):
            raise ValueError("coverage classification timestamp must be timezone-aware")
        if self.preclassified_outcome not in {
            None,
            SearchOutcome.NOT_ON_SALE,
            SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE,
        }:
            raise ValueError("unsupported preclassified coverage outcome")
        if (
            self.preclassified_outcome is SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE
            and self.applicable
        ):
            raise ValueError("unsupported-route coverage may not be applicable")
        if (
            self.preclassified_outcome is SearchOutcome.NOT_ON_SALE
            and not self.applicable
        ):
            raise ValueError("not-on-sale coverage remains applicable")


@dataclass(frozen=True, slots=True)
class ManifestTask:
    task_id: str
    source_slug: str
    request: SearchRequest
    coverage_cell_ids: tuple[str, ...]
    is_canary: bool = False

    def __post_init__(self) -> None:
        if not self.task_id or not self.source_slug or not self.coverage_cell_ids:
            raise ValueError(
                "manifest task identifiers and coverage cells are required"
            )


@dataclass(frozen=True, slots=True)
class BatchManifest:
    manifest_id: str
    batch_id: str
    scheduled_for: date
    timezone: str
    config_version: str
    created_at: datetime
    expected_source_slugs: tuple[str, ...]
    coverage_cells: tuple[CoverageCell, ...]
    tasks: tuple[ManifestTask, ...]
    coverage_failed_source_slugs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("manifest created_at must be timezone-aware")
        if len(set(self.expected_source_slugs)) != len(self.expected_source_slugs):
            raise ValueError("manifest source slugs must be unique")
        failed_sources = set(self.coverage_failed_source_slugs)
        if not failed_sources.issubset(self.expected_source_slugs):
            raise ValueError("coverage failure source is not in the manifest")
        if any(cell.source_slug in failed_sources for cell in self.coverage_cells):
            raise ValueError("coverage failure source may not have coverage cells")
        cell_ids = {cell.cell_id for cell in self.coverage_cells}
        if len(cell_ids) != len(self.coverage_cells):
            raise ValueError("coverage cell IDs must be unique")
        task_ids = {task.task_id for task in self.tasks}
        if len(task_ids) != len(self.tasks):
            raise ValueError("manifest task IDs must be unique")
        cells_by_id = {cell.cell_id: cell for cell in self.coverage_cells}
        for cell in self.coverage_cells:
            if cell.source_slug not in self.expected_source_slugs:
                raise ValueError(
                    f"coverage cell source is not expected: {cell.source_slug}"
                )
        referenced_cells: set[str] = set()
        for task in self.tasks:
            if task.source_slug not in self.expected_source_slugs:
                raise ValueError(f"task source is not expected: {task.source_slug}")
            if task.request.batch_id != self.batch_id:
                raise ValueError("task request batch ID does not match manifest")
            if not set(task.coverage_cell_ids).issubset(cell_ids):
                raise ValueError("task references an unknown coverage cell")
            for cell_id in task.coverage_cell_ids:
                if cell_id in referenced_cells:
                    raise ValueError("coverage cell may be executed only once")
                referenced_cells.add(cell_id)
                cell = cells_by_id[cell_id]
                if cell.source_slug != task.source_slug:
                    raise ValueError("task references another source's coverage cell")
                if cell.preclassified_outcome is not None:
                    raise ValueError("preclassified coverage may not have a task")
                if cell.origin_terminal_id is not None and (
                    task.request.origin_terminal_ids
                    != (cell.origin_terminal_id,)
                    or task.request.destination_terminal_ids
                    != (cell.destination_terminal_id,)
                ):
                    raise ValueError("route-specific task does not match its coverage cell")

    @classmethod
    def create(
        cls,
        *,
        batch_id: str,
        scheduled_for: date,
        timezone: str,
        config_version: str,
        expected_source_slugs: Iterable[str],
        coverage_cells: Iterable[CoverageCell],
        tasks: Iterable[ManifestTask],
        created_at: datetime | None = None,
        coverage_failed_source_slugs: Iterable[str] = (),
    ) -> "BatchManifest":
        sources_tuple = tuple(expected_source_slugs)
        cells_tuple = tuple(coverage_cells)
        tasks_tuple = tuple(tasks)
        failed_sources = tuple(sorted(set(coverage_failed_source_slugs)))
        payload = {
            "batch_id": batch_id,
            "scheduled_for": scheduled_for.isoformat(),
            "timezone": timezone,
            "config_version": config_version,
            "sources": sources_tuple,
            "cells": [
                {
                    "id": cell.cell_id,
                    "source": cell.source_slug,
                    "city": cell.destination_city_id,
                    "weekend": cell.weekend_start.isoformat(),
                    "direction": cell.direction,
                    "applicable": cell.applicable,
                    "origin_terminal": cell.origin_terminal_id,
                    "destination_terminal": cell.destination_terminal_id,
                    "preclassified_outcome": (
                        cell.preclassified_outcome.value
                        if cell.preclassified_outcome is not None
                        else None
                    ),
                }
                for cell in cells_tuple
            ],
            "tasks": [
                {
                    "id": task.task_id,
                    "source": task.source_slug,
                    "request": task.request.fingerprint,
                    "cells": task.coverage_cell_ids,
                    "canary": task.is_canary,
                }
                for task in tasks_tuple
            ],
        }
        if failed_sources:
            payload["coverage_failed_sources"] = failed_sources
        manifest_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(
            manifest_id=manifest_id,
            batch_id=batch_id,
            scheduled_for=scheduled_for,
            timezone=timezone,
            config_version=config_version,
            created_at=created_at or datetime.now(UTC),
            expected_source_slugs=sources_tuple,
            coverage_cells=cells_tuple,
            tasks=tasks_tuple,
            coverage_failed_source_slugs=failed_sources,
        )


class DailyManifestBuilder:
    """Builds deterministic logical cells and provider requests for one daily run."""

    def __init__(
        self,
        *,
        timezone: str = "Europe/Paris",
        origin_terminal_ids: tuple[str, ...],
        passenger: PassengerProfile | None = None,
        config_version: str = "development",
    ) -> None:
        self.timezone = timezone
        self.origin_terminal_ids = origin_terminal_ids
        self.passenger = passenger or PassengerProfile()
        self.config_version = config_version
        if not origin_terminal_ids:
            raise ValueError("at least one origin terminal is required")
        ZoneInfo(timezone)

    def build(
        self,
        *,
        scheduled_for: date,
        source_slugs: Iterable[str],
        destinations: Iterable[DestinationTarget],
        weekend_fridays: Iterable[date],
        canary_per_source: bool = True,
        include_one_way_tasks: bool = False,
        source_coverage: Mapping[str, CoverageDiscovery] | None = None,
        created_at: datetime | None = None,
    ) -> BatchManifest:
        sources = tuple(source_slugs)
        destination_tuple = tuple(destinations)
        weekends = tuple(weekend_fridays)
        batch_id = f"crawl-{scheduled_for.isoformat()}"
        cells: list[CoverageCell] = []
        tasks: list[ManifestTask] = []
        first_for_source: set[str] = set()

        if source_coverage is not None:
            if set(source_coverage) != set(sources):
                raise ValueError("coverage discovery must match the enabled source set")
            for source_slug, discovery in source_coverage.items():
                if discovery.source_slug != source_slug:
                    raise ValueError("coverage discovery source does not match its key")

        for source_slug in sources:
            for destination in destination_tuple:
                for weekend_start in weekends:
                    if weekend_start.weekday() != 4:
                        raise ValueError("weekend identity must be a Friday")
                    outbound_window, return_window = _weekend_search_windows(
                        weekend_start, self.timezone
                    )
                    request_specs: list[
                        tuple[
                            SearchKind,
                            Direction | None,
                            TimeWindow,
                            TimeWindow | None,
                            str,
                        ]
                    ] = [
                        (
                            SearchKind.ROUND_TRIP,
                            None,
                            outbound_window,
                            return_window,
                            "round_trip",
                        )
                    ]
                    if include_one_way_tasks:
                        request_specs.extend(
                            (
                                (
                                    SearchKind.ONE_WAY,
                                    Direction.OUTBOUND,
                                    outbound_window,
                                    None,
                                    Direction.OUTBOUND.value,
                                ),
                                (
                                    SearchKind.ONE_WAY,
                                    Direction.RETURN,
                                    return_window,
                                    None,
                                    Direction.RETURN.value,
                                ),
                            )
                        )

                    discovery = (
                        source_coverage[source_slug]
                        if source_coverage is not None
                        else None
                    )
                    route_pairs: tuple[tuple[str, str], ...] | tuple[None, ...]
                    preclassified_outcome: SearchOutcome | None = None
                    applicable = True
                    classified_at: datetime | None = None
                    if discovery is None:
                        # The legacy/fixture builder sends one request containing
                        # the configured terminal sets. Production always passes
                        # validated provider coverage and therefore takes the
                        # route-specific branch below.
                        if not destination.terminal_ids:
                            raise ValueError(
                                "a destination without terminals requires "
                                "validated provider coverage"
                            )
                        route_pairs = (None,)
                    else:
                        matching_pairs = tuple(
                            sorted(
                                pair
                                for pair in discovery.supported_terminal_pairs
                                if pair[0] in self.origin_terminal_ids
                                and pair[1] in destination.terminal_ids
                            )
                        )
                        if not matching_pairs:
                            route_pairs = (None,)
                            applicable = False
                            preclassified_outcome = (
                                SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE
                            )
                            classified_at = discovery.observed_at
                        elif (
                            discovery.sellable_through is None
                            or weekend_start + timedelta(days=3)
                            > discovery.sellable_through
                        ):
                            route_pairs = (None,)
                            preclassified_outcome = SearchOutcome.NOT_ON_SALE
                            classified_at = discovery.observed_at
                        else:
                            route_pairs = matching_pairs

                    for (
                        search_kind,
                        direction,
                        search_window,
                        paired_window,
                        cell_direction,
                    ) in request_specs:
                        for route_pair in route_pairs:
                            route_key = (
                                "all"
                                if route_pair is None
                                else f"{route_pair[0]}>{route_pair[1]}"
                            )
                            cell_key = (
                                f"{source_slug}:{destination.city_id}:"
                                f"{weekend_start.isoformat()}:{cell_direction}:"
                                f"{route_key}"
                            )
                            cell_id = hashlib.sha256(cell_key.encode()).hexdigest()[:24]
                            cells.append(
                                CoverageCell(
                                    cell_id=cell_id,
                                    source_slug=source_slug,
                                    destination_city_id=destination.city_id,
                                    weekend_start=weekend_start,
                                    direction=cell_direction,
                                    applicable=applicable,
                                    origin_terminal_id=(
                                        route_pair[0]
                                        if route_pair is not None
                                        else None
                                    ),
                                    destination_terminal_id=(
                                        route_pair[1]
                                        if route_pair is not None
                                        else None
                                    ),
                                    preclassified_outcome=preclassified_outcome,
                                    classified_at=classified_at,
                                )
                            )
                            if preclassified_outcome is not None:
                                continue

                            origin_terminal_ids = (
                                (route_pair[0],)
                                if route_pair is not None
                                else self.origin_terminal_ids
                            )
                            destination_terminal_ids = (
                                (route_pair[1],)
                                if route_pair is not None
                                else destination.terminal_ids
                            )
                            query_id = hashlib.sha256(
                                f"{batch_id}:{cell_key}".encode()
                            ).hexdigest()[:24]
                            request = SearchRequest(
                                batch_id=batch_id,
                                query_id=query_id,
                                destination_city_id=destination.city_id,
                                weekend_start=weekend_start,
                                search_kind=search_kind,
                                direction=direction,
                                outbound_window=search_window,
                                return_window=paired_window,
                                origin_terminal_ids=origin_terminal_ids,
                                destination_terminal_ids=destination_terminal_ids,
                                passenger=self.passenger,
                            )
                            task_id = hashlib.sha256(
                                f"{source_slug}:{request.fingerprint}".encode()
                            ).hexdigest()[:24]
                            is_canary = (
                                canary_per_source
                                and source_slug not in first_for_source
                            )
                            first_for_source.add(source_slug)
                            tasks.append(
                                ManifestTask(
                                    task_id=task_id,
                                    source_slug=source_slug,
                                    request=request,
                                    coverage_cell_ids=(cell_id,),
                                    is_canary=is_canary,
                                )
                            )

        return BatchManifest.create(
            batch_id=batch_id,
            scheduled_for=scheduled_for,
            timezone=self.timezone,
            config_version=self.config_version,
            expected_source_slugs=sources,
            coverage_cells=cells,
            tasks=tasks,
            created_at=created_at,
        )


def _weekend_search_windows(
    friday: date, timezone_name: str
) -> tuple[TimeWindow, TimeWindow]:
    timezone = ZoneInfo(timezone_name)
    outbound_start = datetime.combine(friday, time(19, 0), timezone)
    # Broad source search windows; crowl-core owns final itinerary validation.
    outbound_end = datetime.combine(friday + timedelta(days=2), time(18, 0), timezone)
    return_start = datetime.combine(friday + timedelta(days=1), time(7, 0), timezone)
    return_end = datetime.combine(friday + timedelta(days=3), time(7, 0), timezone)
    return (
        TimeWindow(outbound_start, outbound_end, timezone_name),
        TimeWindow(return_start, return_end, timezone_name),
    )


FIXTURE_DESTINATIONS: Mapping[str, DestinationTarget] = {
    "amsterdam-nl": DestinationTarget("amsterdam-nl", ("ams-centraal", "ams-schiphol")),
    "lisbon-pt": DestinationTarget("lisbon-pt", ("lis-humberto-delgado",)),
    "vienna-at": DestinationTarget("vienna-at", ("vie-schwechat",)),
    "reykjavik-is": DestinationTarget("reykjavik-is", ("kef-keflavik",)),
}
