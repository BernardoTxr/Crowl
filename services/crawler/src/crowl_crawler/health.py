from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol


class HealthEventKind(StrEnum):
    BATCH_STARTED = "batch_started"
    BATCH_HEARTBEAT = "batch_heartbeat"
    EXTRACTOR_STARTED = "extractor_started"
    SEARCH_FINISHED = "search_finished"
    EXTRACTOR_FINISHED = "extractor_finished"
    BATCH_FINISHED = "batch_finished"


@dataclass(frozen=True, slots=True)
class HealthEvent:
    kind: HealthEventKind
    batch_id: str
    occurred_at: datetime
    source_slug: str | None
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("health event timestamp must be timezone-aware")
        if not self.batch_id:
            raise ValueError("health event batch ID is required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "batch_id": self.batch_id,
            "occurred_at": self.occurred_at.isoformat(),
            "source_slug": self.source_slug,
            "payload": dict(self.payload),
        }


class HealthSink(Protocol):
    """Persistence boundary implemented by JSONL, memory, or PostgreSQL sinks."""

    async def write(self, event: HealthEvent) -> None: ...


class NullHealthSink:
    async def write(self, event: HealthEvent) -> None:
        return None


class InMemoryHealthSink:
    def __init__(self) -> None:
        self.events: list[HealthEvent] = []
        self._lock = asyncio.Lock()

    async def write(self, event: HealthEvent) -> None:
        async with self._lock:
            self.events.append(event)


class JsonLinesHealthSink:
    """Local diagnostic sink; a DB-backed sink can implement the same protocol."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def write(self, event: HealthEvent) -> None:
        line = json.dumps(event.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)


class CompositeHealthSink:
    def __init__(self, *sinks: HealthSink) -> None:
        self.sinks = sinks

    async def write(self, event: HealthEvent) -> None:
        for sink in self.sinks:
            await sink.write(event)


def health_event(
    kind: HealthEventKind,
    *,
    batch_id: str,
    source_slug: str | None = None,
    payload: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> HealthEvent:
    return HealthEvent(
        kind=kind,
        batch_id=batch_id,
        occurred_at=occurred_at or datetime.now(UTC),
        source_slug=source_slug,
        payload=payload or {},
    )
