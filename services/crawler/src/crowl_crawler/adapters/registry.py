from __future__ import annotations

from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Iterable, cast

from .base import SourceAdapter
from .catalog import SourceSpec, load_source_specs
from .disabled import DisabledSourceAdapter
from .fixture import FixtureAdapter
from ..models import AdapterMetadata, ExtractionStrategy


# Compatibility/export for callers that inspect the requested source catalog.
# Unlike the former hand-written tuple, this value is derived from sources.yaml.
REQUESTED_SOURCE_SPECS: tuple[SourceSpec, ...] = load_source_specs()


class AdapterRegistry:
    def __init__(self, adapters: Iterable[SourceAdapter] = ()) -> None:
        self._adapters: dict[str, SourceAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: SourceAdapter, *, replace: bool = False) -> None:
        slug = adapter.metadata().source_slug
        if slug in self._adapters and not replace:
            raise ValueError(f"adapter already registered: {slug}")
        self._adapters[slug] = adapter

    def get(self, source_slug: str) -> SourceAdapter:
        try:
            return self._adapters[source_slug]
        except KeyError as error:
            raise KeyError(f"unknown source adapter: {source_slug}") from error

    def all(self, *, include_fixture: bool = True) -> tuple[SourceAdapter, ...]:
        return tuple(
            adapter
            for adapter in self._adapters.values()
            if include_fixture or adapter.metadata().source_slug != "fixture"
        )

    def metadata(self, *, include_fixture: bool = True) -> tuple[AdapterMetadata, ...]:
        return tuple(
            adapter.metadata() for adapter in self.all(include_fixture=include_fixture)
        )


def build_default_registry(
    *,
    include_fixture: bool = True,
    source_catalog: str | Path | None = None,
) -> AdapterRegistry:
    specs = load_source_specs(source_catalog)
    adapters = [_provider_adapter(spec) for spec in specs]
    if include_fixture:
        adapters.append(FixtureAdapter())
    return AdapterRegistry(adapters)


def _provider_adapter(spec: SourceSpec) -> SourceAdapter:
    """Load the provider's named module, then enforce catalog policy."""

    module = import_module(f"{__package__}.providers.{spec.slug}")
    adapter_type = getattr(module, "ADAPTER_CLASS", None)
    if not isinstance(adapter_type, type) or not issubclass(adapter_type, SourceAdapter):
        raise TypeError(
            f"provider module {spec.slug} must export a SourceAdapter as ADAPTER_CLASS"
        )
    adapter = cast(SourceAdapter, adapter_type(spec))
    metadata = adapter.metadata()
    _validate_catalog_binding(spec, metadata)

    # This guard remains in place when a provider module is later replaced by
    # a live implementation. An enabled class can never overrule YAML policy.
    if metadata.enabled and not spec.catalog_enabled:
        return DisabledSourceAdapter(
            replace(
                metadata,
                strategy=ExtractionStrategy.DISABLED,
                enabled=False,
                disabled_reason=spec.disabled_reason,
            )
        )
    return adapter


def _validate_catalog_binding(spec: SourceSpec, metadata: AdapterMetadata) -> None:
    expected = (
        spec.slug,
        spec.name,
        spec.modes,
        spec.intended_strategy,
        spec.access_basis,
    )
    actual = (
        metadata.source_slug,
        metadata.display_name,
        metadata.modes,
        metadata.intended_strategy,
        metadata.access_basis,
    )
    if actual != expected:
        raise ValueError(
            f"provider adapter metadata does not match sources.yaml for {spec.slug}"
        )
