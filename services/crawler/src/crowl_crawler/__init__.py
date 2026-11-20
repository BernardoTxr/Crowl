"""Crowl's isolated one-shot crawler service."""

from .batch import BatchCoordinator, BatchReport, RetryPolicy
from .manifest import BatchManifest, CoverageCell, DailyManifestBuilder, ManifestTask
from .models import (
    AdapterMetadata,
    BatchStatus,
    ConnectorResult,
    ExtractionStrategy,
    ExtractorRunStatus,
    NormalizedOffer,
    SearchOutcome,
    SearchRequest,
    TransportMode,
)
from .persistence import DatabasePublisher, PublicationError, PublicationResult

__all__ = [
    "AdapterMetadata",
    "BatchCoordinator",
    "BatchManifest",
    "BatchReport",
    "BatchStatus",
    "ConnectorResult",
    "CoverageCell",
    "DatabasePublisher",
    "DailyManifestBuilder",
    "ExtractionStrategy",
    "ExtractorRunStatus",
    "ManifestTask",
    "NormalizedOffer",
    "PublicationError",
    "PublicationResult",
    "RetryPolicy",
    "SearchOutcome",
    "SearchRequest",
    "TransportMode",
]

__version__ = "0.1.0"
