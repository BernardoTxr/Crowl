from __future__ import annotations

import asyncio
import importlib
import tempfile
import unittest
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from unittest.mock import patch

import yaml

from crowl_core.catalog import load_source_catalog

from crowl_crawler.adapters.base import SourceAdapter
from crowl_crawler.adapters.registry import (
    REQUESTED_SOURCE_SPECS,
    build_default_registry,
)
from crowl_crawler.manifest import DailyManifestBuilder, FIXTURE_DESTINATIONS
from crowl_crawler.models import (
    AdapterMetadata,
    ConnectorResult,
    CoverageDiscovery,
    ExtractionStrategy,
    SearchOutcome,
    SearchRequest,
)


ROOT = Path(__file__).resolve().parents[2]


EXPECTED_SOURCE_SLUGS = {
    "ryanair",
    "easyjet",
    "transavia",
    "wizzair",
    "vueling",
    "volotea",
    "flixbus",
    "blablacar_bus",
    "itabus",
    "alsa",
    "ouigo",
    "sncf_connect",
    "trenitalia_france",
    "eurostar",
}


class RegistryTests(unittest.TestCase):
    def test_all_fourteen_requested_slots_come_from_yaml_and_have_named_modules(
        self,
    ) -> None:
        self.assertEqual(len(REQUESTED_SOURCE_SPECS), 14)
        registry = build_default_registry(include_fixture=False)
        metadata = registry.metadata(include_fixture=False)
        catalog = load_source_catalog(ROOT / "data" / "sources.yaml")
        self.assertEqual({item.source_slug for item in metadata}, EXPECTED_SOURCE_SLUGS)
        self.assertEqual(
            tuple(item.source_slug for item in metadata),
            tuple(item["id"] for item in catalog),
        )
        for item, source in zip(metadata, catalog, strict=True):
            if item.source_slug in {"flixbus", "ryanair"}:
                self.assertTrue(item.enabled)
                self.assertEqual(item.strategy, ExtractionStrategy.PLAYWRIGHT)
                self.assertIsNone(item.disabled_reason)
            else:
                self.assertFalse(item.enabled)
                self.assertEqual(item.strategy, ExtractionStrategy.DISABLED)
                self.assertTrue(item.disabled_reason.startswith("Catalog kill switch"))
            self.assertEqual(item.intended_strategy.value, source["strategy"])
            self.assertEqual(item.display_name, source["name"])
            self.assertEqual(item.access_basis, source["access_basis"])
            adapter = registry.get(item.source_slug)
            self.assertEqual(
                type(adapter).__module__,
                f"crowl_crawler.adapters.providers.{item.source_slug}",
            )
            module = importlib.import_module(type(adapter).__module__)
            self.assertIs(type(adapter), module.ADAPTER_CLASS)

    def test_kill_switched_adapters_return_typed_results_without_network(self) -> None:
        registry = build_default_registry(include_fixture=False)
        builder = DailyManifestBuilder(origin_terminal_ids=("par-cdg",))
        manifest = builder.build(
            scheduled_for=date(2027, 1, 1),
            source_slugs=tuple(sorted(EXPECTED_SOURCE_SLUGS)),
            destinations=(FIXTURE_DESTINATIONS["lisbon-pt"],),
            weekend_fridays=(date(2027, 1, 8),),
        )
        requests = {
            task.source_slug: task.request
            for task in manifest.tasks
        }

        async def exercise() -> None:
            # Patch after asyncio has created its private socketpair; any new
            # socket inside an adapter is therefore an outbound-network bug.
            with patch("socket.socket", side_effect=AssertionError("network attempted")):
                for slug in EXPECTED_SOURCE_SLUGS - {"flixbus", "ryanair"}:
                    adapter = registry.get(slug)
                    coverage = await adapter.discover_coverage()
                    canary = await adapter.canary((requests[slug],))
                    result = await adapter.search(requests[slug])
                    self.assertEqual(coverage.diagnostic_code, "SOURCE_DISABLED")
                    self.assertEqual(len(canary), 1)
                    self.assertEqual(canary[0].outcome, SearchOutcome.SKIPPED_DISABLED)
                    self.assertEqual(result.outcome, SearchOutcome.SKIPPED_DISABLED)
                    self.assertEqual(result.diagnostic_code, "SOURCE_DISABLED")
                    self.assertFalse(result.retryable)
                    await adapter.close()

                self.assertTrue(registry.get("flixbus").metadata().enabled)
                self.assertTrue(registry.get("ryanair").metadata().enabled)

        asyncio.run(exercise())

    def test_catalog_kill_switch_overrides_an_enabled_implementation(self) -> None:
        class EnabledRyanairAdapter(SourceAdapter):
            def __init__(self, spec) -> None:
                self._metadata = AdapterMetadata(
                    source_slug=spec.slug,
                    display_name=spec.name,
                    modes=spec.modes,
                    strategy=spec.intended_strategy,
                    intended_strategy=spec.intended_strategy,
                    connector_version="test-live",
                    parser_version="test-live",
                    enabled=True,
                    disabled_reason=None,
                    access_basis=spec.access_basis,
                )

            def metadata(self) -> AdapterMetadata:
                return self._metadata

            async def discover_coverage(self) -> CoverageDiscovery:
                raise AssertionError("kill-switched adapter attempted coverage discovery")

            async def canary(
                self, requests: Sequence[SearchRequest]
            ) -> tuple[ConnectorResult, ...]:
                raise AssertionError("kill-switched adapter attempted a canary")

            async def search(self, request: SearchRequest) -> ConnectorResult:
                raise AssertionError("kill-switched adapter attempted a search")

            async def close(self) -> None:
                raise AssertionError("kill-switched adapter attempted close")

        payload = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text())
        ryanair = next(item for item in payload["sources"] if item["id"] == "ryanair")
        ryanair["state"] = "enabled"
        ryanair["enabled"] = True
        ryanair["kill_switch"] = True

        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "sources.yaml"
            catalog_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
            module = importlib.import_module(
                "crowl_crawler.adapters.providers.ryanair"
            )
            with patch.object(module, "ADAPTER_CLASS", EnabledRyanairAdapter):
                registry = build_default_registry(
                    include_fixture=False,
                    source_catalog=catalog_path,
                )

        adapter = registry.get("ryanair")
        metadata = adapter.metadata()
        self.assertFalse(metadata.enabled)
        self.assertEqual(metadata.strategy, ExtractionStrategy.DISABLED)
        self.assertTrue(metadata.disabled_reason.startswith("Catalog kill switch"))
        builder = DailyManifestBuilder(origin_terminal_ids=("par-cdg",))
        manifest = builder.build(
            scheduled_for=date(2027, 1, 1),
            source_slugs=("ryanair",),
            destinations=(FIXTURE_DESTINATIONS["lisbon-pt"],),
            weekend_fridays=(date(2027, 1, 8),),
        )
        result = asyncio.run(adapter.search(manifest.tasks[0].request))
        self.assertEqual(result.outcome, SearchOutcome.SKIPPED_DISABLED)


if __name__ == "__main__":
    unittest.main()
