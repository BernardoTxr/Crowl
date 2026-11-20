from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from .base import SourceAdapter
from ..models import (
    AdapterMetadata,
    ConnectorResult,
    CoverageDiscovery,
    SearchOutcome,
    SearchRequest,
)


class DisabledSourceAdapter(SourceAdapter):
    """Visible source slot that intentionally performs no network activity."""

    def __init__(
        self,
        metadata: AdapterMetadata,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if metadata.enabled:
            raise ValueError("DisabledSourceAdapter requires disabled metadata")
        self._metadata = metadata
        self._clock = clock or (lambda: datetime.now(UTC))

    def metadata(self) -> AdapterMetadata:
        return self._metadata

    async def discover_coverage(self) -> CoverageDiscovery:
        return CoverageDiscovery(
            source_slug=self._metadata.source_slug,
            supported_terminal_pairs=(),
            sellable_through=None,
            observed_at=self._clock(),
            diagnostic_code="SOURCE_DISABLED",
        )

    async def canary(
        self, requests: Sequence[SearchRequest]
    ) -> tuple[ConnectorResult, ...]:
        return tuple(self._disabled_result() for _ in requests)

    async def search(self, request: SearchRequest) -> ConnectorResult:
        return self._disabled_result()

    def _disabled_result(self) -> ConnectorResult:
        return ConnectorResult(
            outcome=SearchOutcome.SKIPPED_DISABLED,
            retryable=False,
            diagnostic_code="SOURCE_DISABLED",
            expected_count=1,
            completed_count=1,
            offers=(),
            parser_version=self._metadata.parser_version,
            observed_at=self._clock(),
        )

    async def close(self) -> None:
        return None
