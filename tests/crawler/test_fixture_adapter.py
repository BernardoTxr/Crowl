from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, date, datetime

from crowl_crawler.adapters.fixture import FixtureAdapter
from crowl_crawler.manifest import DailyManifestBuilder, DestinationTarget
from crowl_crawler.models import (
    PriceCompleteness,
    PricingSemantics,
    SearchOutcome,
    TransportMode,
)


class FixtureAdapterTests(unittest.TestCase):
    def test_realistic_round_trip_offer_has_exact_terminals_and_complete_price(
        self,
    ) -> None:
        observed_at = datetime(2027, 1, 1, 2, 0, tzinfo=UTC)
        adapter = FixtureAdapter(clock=lambda: observed_at)
        manifest = DailyManifestBuilder(
            origin_terminal_ids=("par-cdg", "par-ory")
        ).build(
            scheduled_for=date(2027, 1, 1),
            source_slugs=("fixture",),
            destinations=(DestinationTarget("lisbon-pt", ("lis-humberto-delgado",)),),
            weekend_fridays=(date(2027, 1, 8),),
        )

        result = asyncio.run(adapter.search(manifest.tasks[0].request))

        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_RESULTS)
        self.assertEqual(result.observed_at, observed_at)
        self.assertEqual(len(result.offers), 1)
        offer = result.offers[0]
        self.assertEqual(offer.price_completeness, PriceCompleteness.COMPLETE)
        self.assertEqual(offer.pricing_semantics, PricingSemantics.ROUND_TRIP_ONLY)
        self.assertGreater(offer.total_amount_minor, 0)
        self.assertEqual(offer.currency, "EUR")
        self.assertEqual(offer.outbound_segments[0].mode, TransportMode.FLIGHT)
        self.assertEqual(offer.outbound_segments[0].departure_terminal_id, "par-cdg")
        self.assertEqual(
            offer.outbound_segments[0].arrival_terminal_id,
            "lis-humberto-delgado",
        )
        self.assertEqual(
            offer.return_segments[0].departure_terminal_id,
            "lis-humberto-delgado",
        )
        self.assertEqual(offer.return_segments[0].arrival_terminal_id, "par-ory")
        self.assertLess(
            offer.outbound_segments[0].arrives_at,
            offer.return_segments[0].departs_at,
        )
        self.assertTrue(offer.booking_components[0].booking_url.startswith("https://"))

    def test_unknown_destination_is_a_valid_unsupported_route_skip(self) -> None:
        adapter = FixtureAdapter()
        manifest = DailyManifestBuilder(origin_terminal_ids=("par-cdg",)).build(
            scheduled_for=date(2027, 1, 1),
            source_slugs=("fixture",),
            destinations=(DestinationTarget("unknown-fr", ("unknown-terminal",)),),
            weekend_fridays=(date(2027, 1, 8),),
        )
        result = asyncio.run(adapter.search(manifest.tasks[0].request))
        self.assertEqual(result.outcome, SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE)
        self.assertEqual(result.offers, ())


if __name__ == "__main__":
    unittest.main()
