from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crowl_crawler.adapters.catalog import load_source_specs
from crowl_crawler.adapters.providers.easyjet import EasyJetAdapter
from crowl_crawler.adapters.providers.flight_http import (
    FlightConfigurationError,
    FlightHttpRequest,
    FlightHttpResponse,
)
from crowl_crawler.adapters.providers.ryanair import RyanairAdapter
from crowl_crawler.adapters.providers.transavia import TransaviaAdapter
from crowl_crawler.adapters.providers.volotea import VoloteaAdapter
from crowl_crawler.adapters.providers.vueling import VuelingAdapter
from crowl_crawler.adapters.providers.wizzair import WizzAirAdapter
from crowl_crawler.manifest import DailyManifestBuilder, DestinationTarget
from crowl_crawler.models import (
    Direction,
    ExtractionStrategy,
    PriceCompleteness,
    PricingSemantics,
    SearchOutcome,
    TransportMode,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("flight_fixtures")
OBSERVED_AT = datetime(2027, 1, 1, 2, tzinfo=UTC)


class StaticPageClient:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        final_url: str | None = None,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.body = body
        self.status = status
        self.final_url = final_url
        self.content_type = content_type
        self.requests: list[FlightHttpRequest] = []

    async def send(self, request: FlightHttpRequest) -> FlightHttpResponse:
        self.requests.append(request)
        return FlightHttpResponse(
            status=self.status,
            headers={"content-type": self.content_type},
            body=self.body,
            final_url=self.final_url or request.url,
        )

    async def close(self) -> None:
        return None


PROVIDERS = (
    (
        "ryanair",
        RyanairAdapter,
        "https://www.ryanair.com/gb/en/trip/flights/select",
        "par-bva",
        8_458,
        "Ryanair",
    ),
    (
        "easyjet",
        EasyJetAdapter,
        "https://www.easyjet.com/en/book/index",
        "par-ory",
        11_848,
        "easyJet",
    ),
    (
        "transavia",
        TransaviaAdapter,
        "https://www.transavia.com/book/en-fr/search-a-flight",
        "par-ory",
        12_620,
        "Transavia",
    ),
    (
        "wizzair",
        WizzAirAdapter,
        "https://www.wizzair.com/en-gb/booking/select-flight",
        "par-ory",
        9_498,
        "Wizz Air",
    ),
    (
        "vueling",
        VuelingAdapter,
        "https://www.vueling.com/en/booking",
        "par-ory",
        10_998,
        "Vueling",
    ),
    (
        "volotea",
        VoloteaAdapter,
        "https://www.volotea.com/en/booking",
        "par-ory",
        13_740,
        "Volotea",
    ),
)


def _enabled_spec(slug: str):  # type: ignore[no-untyped-def]
    spec = next(item for item in load_source_specs() if item.slug == slug)
    return replace(
        spec,
        state="enabled",
        configured_enabled=True,
        kill_switch=False,
    )


def _request(source_slug: str, origin_terminal_id: str):  # type: ignore[no-untyped-def]
    manifest = DailyManifestBuilder(
        origin_terminal_ids=(origin_terminal_id,),
    ).build(
        scheduled_for=date(2027, 1, 1),
        source_slugs=(source_slug,),
        destinations=(DestinationTarget("lisbon-pt", ("lis-humberto-delgado",)),),
        weekend_fridays=(date(2027, 1, 8),),
        canary_per_source=False,
    )
    return manifest.tasks[0].request


def _one_way_return_request(source_slug: str, origin_terminal_id: str):  # type: ignore[no-untyped-def]
    manifest = DailyManifestBuilder(
        origin_terminal_ids=(origin_terminal_id,),
    ).build(
        scheduled_for=date(2027, 1, 1),
        source_slugs=(source_slug,),
        destinations=(DestinationTarget("lisbon-pt", ("lis-humberto-delgado",)),),
        weekend_fridays=(date(2027, 1, 8),),
        canary_per_source=False,
        include_one_way_tasks=True,
    )
    return next(
        task.request
        for task in manifest.tasks
        if task.request.direction is Direction.RETURN
    )


def _adapter(
    slug: str,
    adapter_type,  # type: ignore[no-untyped-def]
    public_page_url: str,
    client: StaticPageClient,
    *,
    origin: str = "par-ory",
):
    return adapter_type(
        _enabled_spec(slug),
        http_client=client,
        clock=lambda: OBSERVED_AT,
        terminal_catalog_path=ROOT / "data" / "terminals.json",
        public_page_url=public_page_url,
        supported_terminal_pairs=((origin, "lis-humberto-delgado"),),
    )


class FlightProviderTests(unittest.TestCase):
    def test_each_airline_parses_sanitized_public_page_without_live_network(
        self,
    ) -> None:
        for (
            slug,
            adapter_type,
            public_page_url,
            origin,
            amount_minor,
            operator,
        ) in PROVIDERS:
            with self.subTest(source=slug):
                client = StaticPageClient((FIXTURES / f"{slug}.html").read_bytes())
                adapter = _adapter(
                    slug,
                    adapter_type,
                    public_page_url,
                    client,
                    origin=origin,
                )

                result = asyncio.run(adapter.search(_request(slug, origin)))

                self.assertEqual(result.outcome, SearchOutcome.SUCCESS_RESULTS)
                self.assertFalse(result.retryable)
                self.assertEqual(result.observed_at, OBSERVED_AT)
                self.assertTrue(result.response_fingerprint)
                self.assertTrue(result.schema_fingerprint)
                self.assertEqual(len(result.offers), 1)
                offer = result.offers[0]
                self.assertEqual(offer.total_amount_minor, amount_minor)
                self.assertEqual(offer.currency, "EUR")
                self.assertEqual(offer.price_completeness, PriceCompleteness.COMPLETE)
                self.assertEqual(
                    offer.pricing_semantics, PricingSemantics.ROUND_TRIP_ONLY
                )
                self.assertTrue(offer.mandatory_fees_included)
                self.assertEqual(offer.passenger_age, 22)
                self.assertTrue(offer.passenger_student)
                self.assertTrue(offer.passenger_eligibility_confirmed)
                self.assertTrue(offer.bookable)
                self.assertFalse(offer.is_teaser)
                self.assertEqual(offer.outbound_segments[0].operator, operator)
                self.assertEqual(offer.outbound_segments[0].mode, TransportMode.FLIGHT)
                self.assertEqual(
                    offer.outbound_segments[0].departure_terminal_id, origin
                )
                self.assertEqual(
                    offer.outbound_segments[-1].arrival_terminal_id,
                    "lis-humberto-delgado",
                )
                self.assertEqual(
                    offer.return_segments[0].departure_terminal_id,
                    "lis-humberto-delgado",
                )
                self.assertEqual(offer.return_segments[-1].arrival_terminal_id, origin)
                self.assertEqual(offer.booking_components[0].amount_minor, amount_minor)
                self.assertEqual(len(client.requests), 8)
                searched_dates: set[tuple[str, str]] = set()
                for outgoing in client.requests:
                    self.assertEqual(outgoing.method, "GET")
                    self.assertIsNone(outgoing.body)
                    self.assertEqual(outgoing.timeout_seconds, 15)
                    self.assertEqual(outgoing.max_response_bytes, 2_000_000)
                    self.assertEqual(
                        outgoing.headers["Accept"],
                        "text/html,application/xhtml+xml",
                    )
                    query = parse_qs(urlsplit(outgoing.url).query)
                    self.assertEqual(
                        query.get("adults", query.get("adultCount")), ["1"]
                    )
                    self.assertFalse({"password", "token", "apiKey"} & query.keys())
                    departure = (
                        query.get("dateOut")
                        or query.get("outbound")
                        or query.get("departureDate")
                    )
                    returning = (
                        query.get("dateIn")
                        or query.get("inbound")
                        or query.get("returnDate")
                    )
                    self.assertIsNotNone(departure)
                    self.assertIsNotNone(returning)
                    searched_dates.add((departure[0], returning[0]))
                self.assertEqual(
                    searched_dates,
                    {
                        ("2027-01-08", "2027-01-09"),
                        ("2027-01-08", "2027-01-10"),
                        ("2027-01-08", "2027-01-11"),
                        ("2027-01-09", "2027-01-09"),
                        ("2027-01-09", "2027-01-10"),
                        ("2027-01-09", "2027-01-11"),
                        ("2027-01-10", "2027-01-10"),
                        ("2027-01-10", "2027-01-11"),
                    },
                )

    def test_return_one_way_pages_are_normalized_for_mixed_composition(self) -> None:
        client = StaticPageClient(
            (FIXTURES / "easyjet-one-way-return.html").read_bytes()
        )
        adapter = _adapter(
            "easyjet",
            EasyJetAdapter,
            "https://www.easyjet.com/en/book/index",
            client,
        )

        result = asyncio.run(
            adapter.search(_one_way_return_request("easyjet", "par-ory"))
        )

        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_RESULTS)
        self.assertEqual(len(result.offers), 1)
        offer = result.offers[0]
        self.assertEqual(offer.pricing_semantics, PricingSemantics.ONE_WAY)
        self.assertEqual(
            offer.outbound_segments[0].departure_terminal_id,
            "lis-humberto-delgado",
        )
        self.assertEqual(offer.outbound_segments[-1].arrival_terminal_id, "par-ory")
        self.assertEqual(offer.return_segments, ())
        self.assertEqual(len(client.requests), 3)
        searched_departures = {
            parse_qs(urlsplit(item.url).query)["outbound"][0]
            for item in client.requests
        }
        self.assertEqual(
            searched_departures,
            {"2027-01-09", "2027-01-10", "2027-01-11"},
        )

    def test_default_catalog_remains_network_inert_with_live_settings(self) -> None:
        client = StaticPageClient((FIXTURES / "ryanair.html").read_bytes())
        disabled_spec = next(
            item for item in load_source_specs() if item.slug == "ryanair"
        )
        adapter = RyanairAdapter(
            disabled_spec,
            http_client=client,
            clock=lambda: OBSERVED_AT,
            terminal_catalog_path=ROOT / "data" / "terminals.json",
            public_page_url="https://www.ryanair.com/gb/en/trip/flights/select",
        )

        result = asyncio.run(adapter.search(_request("ryanair", "par-bva")))

        self.assertFalse(adapter.metadata().enabled)
        self.assertEqual(adapter.metadata().strategy, ExtractionStrategy.DISABLED)
        self.assertEqual(result.outcome, SearchOutcome.SKIPPED_DISABLED)
        self.assertEqual(client.requests, [])

    def test_failures_are_typed_not_collapsed_into_empty_fares(self) -> None:
        cases = (
            (
                b"<html>rate limited</html>",
                429,
                SearchOutcome.RATE_LIMITED,
                "EASYJET_RATE_LIMITED",
                True,
            ),
            (
                b"<html><div class='g-recaptcha'>captcha</div></html>",
                200,
                SearchOutcome.CAPTCHA_CHALLENGE,
                "EASYJET_CAPTCHA_CHALLENGE",
                False,
            ),
            (
                b"<html>Cookie consent required</html>",
                200,
                SearchOutcome.BLOCKED,
                "EASYJET_CONSENT_REQUIRED",
                False,
            ),
            (
                b"<html>Access denied. Your request was blocked.</html>",
                200,
                SearchOutcome.BLOCKED,
                "EASYJET_ACCESS_BLOCKED",
                False,
            ),
            (
                b"<html>Service unavailable for maintenance.</html>",
                200,
                SearchOutcome.UPSTREAM_ERROR,
                "EASYJET_MAINTENANCE",
                True,
            ),
            (
                b"<html><script type='application/json'>"
                b'{"unexpected":true}'
                b"</script></html>",
                200,
                SearchOutcome.PARSE_SCHEMA_ERROR,
                "EASYJET_SCHEMA_MISMATCH",
                False,
            ),
            (
                b"",
                200,
                SearchOutcome.PARSE_SCHEMA_ERROR,
                "EASYJET_EMPTY_RESPONSE",
                False,
            ),
        )
        for body, status, outcome, diagnostic, retryable in cases:
            with self.subTest(outcome=outcome):
                client = StaticPageClient(body, status=status)
                adapter = _adapter(
                    "easyjet",
                    EasyJetAdapter,
                    "https://www.easyjet.com/en/book/index",
                    client,
                )

                result = asyncio.run(adapter.search(_request("easyjet", "par-ory")))

                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.diagnostic_code, diagnostic)
                self.assertEqual(result.retryable, retryable)
                self.assertEqual(result.offers, ())
                if outcome in {
                    SearchOutcome.BLOCKED,
                    SearchOutcome.CAPTCHA_CHALLENGE,
                    SearchOutcome.RATE_LIMITED,
                }:
                    self.assertEqual(len(client.requests), 1)

    def test_empty_flight_arrays_need_an_explicit_final_state(self) -> None:
        pages = (
            (
                b"<html><script type='application/json'>"
                b'{"searchResults":{"journeys":[]}}'
                b"</script></html>",
                SearchOutcome.PARSE_SCHEMA_ERROR,
            ),
            (
                b"<html><script type='application/json'>"
                b'{"searchResults":{"journeys":[],"status":"NO_RESULTS"}}'
                b"</script></html>",
                SearchOutcome.SUCCESS_EMPTY,
            ),
        )
        for body, expected in pages:
            with self.subTest(expected=expected):
                client = StaticPageClient(body)
                adapter = _adapter(
                    "easyjet",
                    EasyJetAdapter,
                    "https://www.easyjet.com/en/book/index",
                    client,
                )
                result = asyncio.run(adapter.search(_request("easyjet", "par-ory")))
                self.assertEqual(result.outcome, expected)
                self.assertEqual(result.offers, ())

    def test_party_prices_and_paid_membership_fares_fail_closed(self) -> None:
        base = (FIXTURES / "easyjet.html").read_bytes()
        invalid_pages = (
            base.replace(b'"eligibleQuantity":1', b'"eligibleQuantity":2'),
            base.replace(
                b'"requiresPaidMembership":false',
                b'"requiresPaidMembership":true',
            ),
        )
        for body in invalid_pages:
            with self.subTest(fragment=body[-200:]):
                client = StaticPageClient(body)
                adapter = _adapter(
                    "easyjet",
                    EasyJetAdapter,
                    "https://www.easyjet.com/en/book/index",
                    client,
                )
                result = asyncio.run(adapter.search(_request("easyjet", "par-ory")))
                self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)
                self.assertEqual(result.offers, ())

    def test_off_host_redirect_fails_without_leaking_url(self) -> None:
        client = StaticPageClient(
            (FIXTURES / "easyjet.html").read_bytes(),
            final_url="https://untrusted.example/redirected",
        )
        adapter = _adapter(
            "easyjet",
            EasyJetAdapter,
            "https://www.easyjet.com/en/book/index",
            client,
        )

        result = asyncio.run(adapter.search(_request("easyjet", "par-ory")))

        self.assertEqual(result.outcome, SearchOutcome.CONFIGURATION_ERROR)
        self.assertEqual(result.diagnostic_code, "EASYJET_CONFIGURATION_ERROR")
        self.assertNotIn("untrusted", result.diagnostic_code.lower())

    def test_coverage_is_explicit_and_does_not_scrape_inventory(self) -> None:
        client = StaticPageClient((FIXTURES / "transavia.html").read_bytes())
        adapter = _adapter(
            "transavia",
            TransaviaAdapter,
            "https://www.transavia.com/book/en-fr/search-a-flight",
            client,
        )

        coverage = asyncio.run(adapter.discover_coverage())

        self.assertEqual(
            coverage.supported_terminal_pairs,
            (("par-ory", "lis-humberto-delgado"),),
        )
        self.assertEqual(coverage.sellable_through, date(2027, 7, 30))
        self.assertEqual(coverage.diagnostic_code, "TRANSAVIA_COVERAGE_OK")
        self.assertEqual(client.requests, [])

    def test_unknown_airport_is_schema_error_not_fabricated_terminal(self) -> None:
        body = (FIXTURES / "easyjet.html").read_bytes().replace(b'"LIS"', b'"XXX"')
        client = StaticPageClient(body)
        adapter = _adapter(
            "easyjet",
            EasyJetAdapter,
            "https://www.easyjet.com/en/book/index",
            client,
        )

        result = asyncio.run(adapter.search(_request("easyjet", "par-ory")))

        self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)
        self.assertEqual(result.diagnostic_code, "EASYJET_SCHEMA_MISMATCH")

    def test_round_trip_under_twelve_hours_at_destination_is_filtered(self) -> None:
        body = (
            (FIXTURES / "easyjet.html")
            .read_bytes()
            .replace(b"2027-01-10T18:00:00", b"2027-01-09T08:00:00")
            .replace(b"2027-01-10T21:30:00", b"2027-01-09T11:30:00")
        )
        client = StaticPageClient(body)
        adapter = _adapter(
            "easyjet",
            EasyJetAdapter,
            "https://www.easyjet.com/en/book/index",
            client,
        )

        result = asyncio.run(adapter.search(_request("easyjet", "par-ory")))

        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_EMPTY)
        self.assertEqual(result.diagnostic_code, "EASYJET_RESULTS_FILTERED")
        self.assertEqual(result.offers, ())

    def test_known_but_unrequested_airport_is_filtered(self) -> None:
        body = (FIXTURES / "easyjet.html").read_bytes().replace(b'"LIS"', b'"BCN"')
        client = StaticPageClient(body)
        adapter = _adapter(
            "easyjet",
            EasyJetAdapter,
            "https://www.easyjet.com/en/book/index",
            client,
        )

        result = asyncio.run(adapter.search(_request("easyjet", "par-ory")))

        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_EMPTY)
        self.assertEqual(result.diagnostic_code, "EASYJET_RESULTS_FILTERED")
        self.assertEqual(result.offers, ())

    def test_incomplete_price_is_not_returned_as_a_valid_fare(self) -> None:
        body = (
            (FIXTURES / "easyjet.html")
            .read_bytes()
            .replace(
                b'"includesTaxesAndMandatoryFees":true',
                b'"includesTaxesAndMandatoryFees":false',
            )
        )
        client = StaticPageClient(body)
        adapter = _adapter(
            "easyjet",
            EasyJetAdapter,
            "https://www.easyjet.com/en/book/index",
            client,
        )

        result = asyncio.run(adapter.search(_request("easyjet", "par-ory")))

        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_EMPTY)
        self.assertEqual(result.diagnostic_code, "EASYJET_RESULTS_FILTERED")
        self.assertEqual(result.offers, ())

    def test_enabled_source_without_curated_routes_is_network_inert(self) -> None:
        client = StaticPageClient((FIXTURES / "easyjet.html").read_bytes())
        adapter = EasyJetAdapter(
            _enabled_spec("easyjet"),
            http_client=client,
            clock=lambda: OBSERVED_AT,
            terminal_catalog_path=ROOT / "data" / "terminals.json",
            public_page_url="https://www.easyjet.com/en/book/index",
            supported_terminal_pairs=(),
        )

        result = asyncio.run(adapter.search(_request("easyjet", "par-ory")))

        self.assertEqual(result.outcome, SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE)
        self.assertEqual(client.requests, [])

    def test_json_and_api_paths_are_rejected(self) -> None:
        json_client = StaticPageClient(
            (FIXTURES / "easyjet.html").read_bytes(),
            content_type="application/json",
        )
        adapter = _adapter(
            "easyjet",
            EasyJetAdapter,
            "https://www.easyjet.com/en/book/index",
            json_client,
        )

        result = asyncio.run(adapter.search(_request("easyjet", "par-ory")))

        self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)
        self.assertEqual(result.diagnostic_code, "EASYJET_NON_HTML_RESPONSE")

        for public_page_url in (
            "https://www.easyjet.com/api/search",
            "https://www.easyjet.com/rest/search",
            "https://www.easyjet.com/search/results.json",
        ):
            with self.subTest(public_page_url=public_page_url):
                with self.assertRaises(FlightConfigurationError):
                    EasyJetAdapter(
                        _enabled_spec("easyjet"),
                        http_client=json_client,
                        terminal_catalog_path=ROOT / "data" / "terminals.json",
                        public_page_url=public_page_url,
                        supported_terminal_pairs=(("par-ory", "lis-humberto-delgado"),),
                    )


if __name__ == "__main__":
    unittest.main()
