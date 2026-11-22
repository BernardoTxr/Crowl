from __future__ import annotations

import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from crowl_crawler.adapters.base import SourceAdapter
from crowl_crawler.adapters.catalog import load_source_specs
from crowl_crawler.adapters.registry import (
    CanaryAdapterBinding,
    CanaryPolicyError,
    build_canary_adapter,
    build_default_registry,
)
from crowl_crawler.canary import canary_succeeded, run_live_canary
from crowl_crawler.cli import main
from crowl_crawler.models import (
    AdapterMetadata,
    BookingComponent,
    ConnectorResult,
    CoverageDiscovery,
    ExtractionStrategy,
    NormalizedOffer,
    PriceCompleteness,
    PricingSemantics,
    SearchOutcome,
    SearchRequest,
    Segment,
    TransportMode,
)


ROOT = Path(__file__).resolve().parents[2]
OBSERVED_AT = datetime(2027, 1, 1, tzinfo=UTC)


class _FakeCanaryAdapter(SourceAdapter):
    def __init__(self, result: ConnectorResult, metadata: AdapterMetadata) -> None:
        self.result = result
        self._metadata = metadata
        self.requests: tuple[SearchRequest, ...] = ()
        self.closed = False

    def metadata(self) -> AdapterMetadata:
        return self._metadata

    async def discover_coverage(self) -> CoverageDiscovery:
        raise AssertionError("a bounded canary must not discover broad coverage")

    async def canary(
        self, requests: tuple[SearchRequest, ...]
    ) -> tuple[ConnectorResult, ...]:
        self.requests = requests
        return (self.result,)

    async def search(self, request: SearchRequest) -> ConnectorResult:
        raise AssertionError("the canary entrypoint must use adapter.canary")

    async def close(self) -> None:
        self.closed = True


class LiveCanaryTests(unittest.TestCase):
    def test_catalog_kill_switch_refuses_canary_without_explicit_override(self) -> None:
        before = (ROOT / "data" / "sources.yaml").read_bytes()
        with self.assertRaisesRegex(CanaryPolicyError, "single DB-free canary"):
            build_canary_adapter(
                "easyjet",
                terminal_pair=("par-ory", "lis-humberto-delgado"),
            )
        self.assertEqual((ROOT / "data" / "sources.yaml").read_bytes(), before)

    def test_override_is_invocation_local_and_does_not_enable_default_registry(
        self,
    ) -> None:
        source_file = ROOT / "data" / "sources.yaml"
        before = source_file.read_bytes()
        with patch.dict(
            os.environ,
            {
                "CROWL_EASYJET_PUBLIC_PAGE_URL": (
                    "https://www.easyjet.com/en/book/index"
                )
            },
            clear=False,
        ):
            binding = build_canary_adapter(
                "easyjet",
                override_kill_switch_for_canary=True,
                terminal_pair=("par-ory", "lis-humberto-delgado"),
            )
        self.assertTrue(binding.catalog_override_used)
        self.assertTrue(binding.adapter.metadata().enabled)
        asyncio.run(binding.adapter.close())
        self.assertFalse(
            build_default_registry(include_fixture=False)
            .get("easyjet")
            .metadata()
            .enabled
        )
        self.assertEqual(source_file.read_bytes(), before)

    def test_db_free_canary_emits_bounded_sanitized_real_price_shape(self) -> None:
        spec = next(item for item in load_source_specs() if item.slug == "ryanair")
        result = _successful_result()
        adapter = _FakeCanaryAdapter(
            result,
            AdapterMetadata(
                source_slug=spec.slug,
                display_name=spec.name,
                modes=spec.modes,
                strategy=ExtractionStrategy.PLAYWRIGHT,
                intended_strategy=spec.intended_strategy,
                connector_version="canary-test-v1",
                parser_version="canary-test-v1",
                enabled=True,
                disabled_reason=None,
                access_basis=spec.access_basis,
            ),
        )
        binding = CanaryAdapterBinding(adapter, spec, True)
        with (
            patch("crowl_crawler.canary._build_binding", return_value=binding),
            patch.dict(os.environ, {"DATABASE_URL": "invalid://must-not-be-used"}),
        ):
            report = asyncio.run(
                run_live_canary(
                    source_slug="ryanair",
                    origin_terminal_id="par-cdg",
                    destination_city_id="lisbon-pt",
                    destination_terminal_id="lis-humberto-delgado",
                    weekend_start=date(2027, 1, 8),
                    override_kill_switch_for_canary=True,
                )
            )

        self.assertTrue(adapter.closed)
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(adapter.requests[0].origin_terminal_ids, ("par-cdg",))
        self.assertEqual(
            adapter.requests[0].destination_terminal_ids,
            ("lis-humberto-delgado",),
        )
        self.assertEqual(report["persistence"], "none")
        self.assertTrue(canary_succeeded(report))
        encoded = json.dumps(report)
        self.assertNotIn("booking_url", encoded)
        self.assertNotIn("checkout-token", encoded)
        self.assertEqual(report["result"]["offer_count"], 1)  # type: ignore[index]

    def test_cli_refuses_disabled_source_by_default_without_network(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = main(
                (
                    "live-canary",
                    "--source",
                    "easyjet",
                    "--origin-terminal",
                    "par-ory",
                    "--destination-city",
                    "lisbon-pt",
                    "--destination-terminal",
                    "lis-humberto-delgado",
                    "--weekend",
                    "2027-01-08",
                )
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("--override-kill-switch-for-canary", errors.getvalue())


def _successful_result() -> ConnectorResult:
    outbound = Segment(
        sequence=1,
        mode=TransportMode.FLIGHT,
        operator="Ryanair",
        service_number="FR100",
        departure_terminal_id="par-cdg",
        departure_terminal_name="Paris Charles de Gaulle Airport",
        arrival_terminal_id="lis-humberto-delgado",
        arrival_terminal_name="Lisbon Humberto Delgado Airport",
        departs_at=datetime(2027, 1, 8, 20, tzinfo=UTC),
        arrives_at=datetime(2027, 1, 8, 22, tzinfo=UTC),
    )
    inbound = Segment(
        sequence=1,
        mode=TransportMode.FLIGHT,
        operator="Ryanair",
        service_number="FR101",
        departure_terminal_id="lis-humberto-delgado",
        departure_terminal_name="Lisbon Humberto Delgado Airport",
        arrival_terminal_id="par-cdg",
        arrival_terminal_name="Paris Charles de Gaulle Airport",
        departs_at=datetime(2027, 1, 10, 18, tzinfo=UTC),
        arrives_at=datetime(2027, 1, 10, 20, tzinfo=UTC),
    )
    booking = BookingComponent(
        seller="Ryanair",
        source_slug="ryanair",
        amount_minor=4_299,
        currency="EUR",
        booking_url="https://www.ryanair.com/booking?token=checkout-token",
        fare_name="Basic",
        mandatory_fees_included=True,
        passenger_age=22,
        passenger_student=True,
        passenger_eligibility_confirmed=True,
        bookable=True,
        is_teaser=False,
    )
    offer = NormalizedOffer(
        provider_offer_id="private-provider-id",
        source_slug="ryanair",
        destination_city_id="lisbon-pt",
        pricing_semantics=PricingSemantics.ROUND_TRIP_ONLY,
        price_completeness=PriceCompleteness.COMPLETE,
        total_amount_minor=4_299,
        currency="EUR",
        mandatory_fees_included=True,
        passenger_age=22,
        passenger_student=True,
        passenger_eligibility_confirmed=True,
        bookable=True,
        is_teaser=False,
        observed_at=OBSERVED_AT,
        outbound_segments=(outbound,),
        return_segments=(inbound,),
        booking_components=(booking,),
    )
    return ConnectorResult(
        outcome=SearchOutcome.SUCCESS_RESULTS,
        retryable=False,
        diagnostic_code="RYANAIR_RESULTS_OK",
        expected_count=1,
        completed_count=1,
        offers=(offer,),
        parser_version="canary-test-v1",
        observed_at=OBSERVED_AT,
        response_fingerprint="a" * 64,
        schema_fingerprint="b" * 64,
    )


if __name__ == "__main__":
    unittest.main()
