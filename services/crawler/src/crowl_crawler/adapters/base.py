from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from ..models import AdapterMetadata, ConnectorResult, CoverageDiscovery, SearchRequest


class SourceAdapter(ABC):
    """Versioned connector contract implemented by every source slot."""

    search_timeout_seconds: float | None = None
    maximum_concurrent_searches: int | None = None

    @abstractmethod
    def metadata(self) -> AdapterMetadata:
        raise NotImplementedError

    @abstractmethod
    async def discover_coverage(self) -> CoverageDiscovery:
        raise NotImplementedError

    @abstractmethod
    async def canary(
        self, requests: Sequence[SearchRequest]
    ) -> tuple[ConnectorResult, ...]:
        raise NotImplementedError

    @abstractmethod
    async def search(self, request: SearchRequest) -> ConnectorResult:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError
