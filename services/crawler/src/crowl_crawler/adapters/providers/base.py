"""Shared safe base for provider-specific connector slots."""

from __future__ import annotations

from typing import ClassVar

from ..catalog import SourceSpec
from ..disabled import DisabledSourceAdapter
from ...models import AdapterMetadata, ExtractionStrategy


class CatalogDisabledProviderAdapter(DisabledSourceAdapter):
    """Fallback provider slot that cannot perform network requests.

    Crowl's requested providers have concrete public-page adapters. This base
    remains only as a fail-closed fallback for a future catalog entry whose
    parser has not yet been written.
    """

    source_slug: ClassVar[str]

    def __init__(self, spec: SourceSpec) -> None:
        if spec.slug != self.source_slug:
            raise ValueError(
                f"provider class {type(self).__name__} handles {self.source_slug}, "
                f"not {spec.slug}"
            )
        metadata = AdapterMetadata(
            source_slug=spec.slug,
            display_name=spec.name,
            modes=spec.modes,
            strategy=ExtractionStrategy.DISABLED,
            intended_strategy=spec.intended_strategy,
            connector_version=f"{spec.config_version}-slot",
            parser_version="disabled-slot-v1",
            enabled=False,
            disabled_reason=spec.disabled_reason,
            access_basis=spec.access_basis,
        )
        super().__init__(metadata)
