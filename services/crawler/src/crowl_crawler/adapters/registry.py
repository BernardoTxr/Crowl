from __future__ import annotations

from dataclasses import dataclass, replace
from importlib import import_module
from pathlib import Path
from typing import Iterable, cast

from .base import SourceAdapter
from .catalog import SourceSpec, load_source_specs
from .disabled import DisabledSourceAdapter
from .fixture import FixtureAdapter
from ..models import AdapterMetadata, ExtractionStrategy, TransportMode


# Compatibility/export for callers that inspect the requested source catalog.
# Unlike the former hand-written tuple, this value is derived from sources.yaml.
REQUESTED_SOURCE_SPECS: tuple[SourceSpec, ...] = load_source_specs()


class CanaryPolicyError(ValueError):
    """A live canary was not explicitly allowed by catalog policy or the CLI."""


@dataclass(frozen=True, slots=True)
class CanaryAdapterBinding:
    """One invocation-local provider binding for a non-persistent canary."""

    adapter: SourceAdapter
    catalog_spec: SourceSpec
    catalog_override_used: bool


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


def build_canary_adapter(
    source_slug: str,
    *,
    override_kill_switch_for_canary: bool = False,
    terminal_pair: tuple[str, str] | None = None,
    source_catalog: str | Path | None = None,
) -> CanaryAdapterBinding:
    """Build one provider solely for an explicitly bounded live canary.

    Catalog policy wins by default.  The opt-in creates an in-memory copy of
    the selected ``SourceSpec`` with its operational gates open; it never edits
    ``sources.yaml`` and is not reachable from ``build_default_registry`` or
    the production batch command.  Flight adapters receive only the requested
    route pair, so a canary cannot inherit broad route coverage from the
    environment.
    """

    specs = load_source_specs(source_catalog)
    spec = next((item for item in specs if item.slug == source_slug), None)
    if spec is None:
        raise CanaryPolicyError(f"unknown source: {source_slug}")

    override_used = False
    runtime_spec = spec
    if not spec.catalog_enabled:
        if not override_kill_switch_for_canary:
            raise CanaryPolicyError(
                "source is disabled by the catalog; pass "
                "--override-kill-switch-for-canary for a single DB-free canary"
            )
        runtime_spec = replace(
            spec,
            state="enabled",
            configured_enabled=True,
            kill_switch=False,
        )
        override_used = True

    adapter_kwargs: dict[str, object] = {}
    if TransportMode.FLIGHT in runtime_spec.modes:
        if terminal_pair is None:
            raise CanaryPolicyError("flight canaries require one exact terminal pair")
        adapter_kwargs["supported_terminal_pairs"] = (terminal_pair,)

    adapter = _provider_adapter(runtime_spec, adapter_kwargs=adapter_kwargs)
    return CanaryAdapterBinding(
        adapter=adapter,
        catalog_spec=spec,
        catalog_override_used=override_used,
    )


def _provider_adapter(
    spec: SourceSpec, *, adapter_kwargs: dict[str, object] | None = None
) -> SourceAdapter:
    """Load the provider's named module, then enforce catalog policy."""

    module = import_module(f"{__package__}.providers.{spec.slug}")
    adapter_type = getattr(module, "ADAPTER_CLASS", None)
    if not isinstance(adapter_type, type) or not issubclass(adapter_type, SourceAdapter):
        raise TypeError(
            f"provider module {spec.slug} must export a SourceAdapter as ADAPTER_CLASS"
        )
    adapter = cast(SourceAdapter, adapter_type(spec, **(adapter_kwargs or {})))
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
