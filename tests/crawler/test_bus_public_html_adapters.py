from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from crowl_crawler.adapters.catalog import SourceSpec, load_source_specs
from crowl_crawler.adapters.providers._bus_public_html import RenderedPage
from crowl_crawler.adapters.providers.alsa import AlsaAdapter
from crowl_crawler.adapters.providers.blablacar_bus import BlaBlaCarBusAdapter
from crowl_crawler.adapters.providers.flixbus import (
    FlixBusAdapter,
    parse_flixbus_rendered_candidates,
)
from crowl_crawler.adapters.providers.itabus import ItabusAdapter
from crowl_crawler.models import (
    Direction,
    ExtractionStrategy,
    PassengerProfile,
    PriceCompleteness,
    PricingSemantics,
    SearchKind,
    SearchOutcome,
    SearchRequest,
    TimeWindow,
    TransportMode,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "crawler" / "fixtures" / "bus"
OBSERVED_AT = datetime(2027, 1, 1, 2, tzinfo=UTC)


@dataclass(frozen=True)
class ProviderCase:
    adapter_type: type
    slug: str
    display_name: str
    provider_name: str
    host: str
    booking_url: str
    intended_strategy: ExtractionStrategy


CASES = (
    ProviderCase(
        FlixBusAdapter,
        "flixbus",
        "FlixBus",
        "FlixBus",
        "shop.global.flixbus.com",
        "https://shop.global.flixbus.com/search?offer=fixture",
        ExtractionStrategy.PLAYWRIGHT,
    ),
    ProviderCase(
        BlaBlaCarBusAdapter,
        "blablacar_bus",
        "BlaBlaCar Bus",
        "BlaBlaCar Bus",
        "www.blablacar.com",
        "https://www.blablacar.com/bus/booking?offer=fixture",
        ExtractionStrategy.PLAYWRIGHT,
    ),
    ProviderCase(
        ItabusAdapter,
        "itabus",
        "Itabus",
        "Itabus",
        "www.itabus.it",
        "https://www.itabus.it/en/checkout?offer=fixture",
        ExtractionStrategy.HTTP_STRUCTURED,
    ),
    ProviderCase(
        AlsaAdapter,
        "alsa",
        "Alsa",
        "ALSA",
        "www.alsa.com",
        "https://www.alsa.com/en/checkout?offer=fixture",
        ExtractionStrategy.PLAYWRIGHT,
    ),
)


class FixturePageLoader:
    def __init__(
        self,
        case: ProviderCase,
        *,
        override_html: str | None = None,
        status: int = 200,
    ) -> None:
        self.case = case
        self.override_html = override_html
        self.status = status
        self.calls: list[str] = []
        self.closed = False

    async def load(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        consent_labels: tuple[str, ...],
    ) -> RenderedPage:
        self.calls.append(url)
        if self.override_html is not None:
            html = self.override_html
        elif "date=2027-01-11" in url:
            html = _fixture("public-no-results.html")
        elif "direction=return" in url:
            html = _provider_fixture("public-return.html", self.case)
        else:
            html = _provider_fixture("public-outbound.html", self.case)
        return RenderedPage(
            requested_url=url,
            final_url=url,
            status=self.status,
            content_type="text/html; charset=utf-8",
            html=html,
        )

    async def close(self) -> None:
        self.closed = True


class ExplodingLoader:
    async def load(self, *args: Any, **kwargs: Any) -> RenderedPage:
        raise AssertionError("disabled adapter attempted page rendering")

    async def close(self) -> None:
        return None


class FlixBusRenderedFixtureLoader:
    def __init__(self, *, return_html: str | None = None) -> None:
        self.calls: list[str] = []
        self.return_html = return_html or _fixture("flixbus-rendered-return.html")
        self.active = 0
        self.max_active = 0

    async def load(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        consent_labels: tuple[str, ...],
    ) -> RenderedPage:
        self.calls.append(url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if "direction=return" in url and "date=2027-01-11" in url:
                html = _fixture("public-no-results.html")
            elif "direction=return" in url:
                html = self.return_html
            else:
                html = _fixture("flixbus-rendered-results.html")
            return RenderedPage(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html; charset=utf-8",
                html=html,
            )
        finally:
            self.active -= 1

    async def close(self) -> None:
        return None


class BusPublicHtmlAdapterTests(unittest.TestCase):
    def test_flixbus_rendered_dom_cards_parse_only_stable_public_attributes(
        self,
    ) -> None:
        candidates = parse_flixbus_rendered_candidates(
            _fixture("flixbus-rendered-results.html")
        )

        self.assertEqual(len(candidates), 2)
        direct, connection = candidates
        self.assertEqual(direct.connection_kind, "direct")
        self.assertEqual(direct.amount_minor, 2_398)
        self.assertEqual(direct.currency, "EUR")
        self.assertEqual(direct.departure_station_name, "Paris (Bercy Seine)")
        self.assertEqual(direct.arrival_station_name, "Amsterdam Sloterdijk")
        self.assertEqual(
            direct.departs_at,
            datetime(2027, 1, 8, 19, 30, tzinfo=UTC),
        )
        self.assertEqual(
            direct.arrives_at,
            datetime(2027, 1, 9, 2, 15, tzinfo=UTC),
        )
        self.assertEqual(direct.reserve_button_count, 2)
        self.assertEqual(connection.connection_kind, "connection")
        self.assertEqual(connection.amount_minor, 4_297)

    def test_flixbus_normalizes_only_direct_visible_one_adult_card(self) -> None:
        case = CASES[0]
        request = replace(
            _request(),
            search_kind=SearchKind.ONE_WAY,
            direction=Direction.OUTBOUND,
            return_window=None,
        )
        environment = _environment(case)
        environment["CROWL_FLIXBUS_STOP_ALIASES_JSON"] = json.dumps(
            {
                "Paris (Bercy Seine)": "par-bercy-seine-bus",
                "Amsterdam Sloterdijk": "ams-sloterdijk-bus",
            }
        )
        loader = FixturePageLoader(
            case,
            override_html=_fixture("flixbus-rendered-results.html"),
        )
        adapter = FlixBusAdapter(
            _spec(case),
            loader=loader,
            clock=lambda: OBSERVED_AT,
            environ=environment,
            minimum_request_interval_seconds=0,
        )

        result = asyncio.run(adapter.search(request))

        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_RESULTS)
        self.assertEqual(result.diagnostic_code, "FLIXBUS_RESULTS_OK")
        self.assertEqual(result.expected_count, 1)
        self.assertEqual(result.completed_count, 1)
        self.assertEqual(len(result.offers), 1)
        offer = result.offers[0]
        self.assertEqual(offer.total_amount_minor, 2_398)
        self.assertEqual(offer.price_completeness, PriceCompleteness.ESTIMATED)
        self.assertFalse(offer.mandatory_fees_included)
        self.assertTrue(offer.passenger_eligibility_confirmed)
        self.assertEqual(len(offer.outbound_segments), 1)
        self.assertEqual(offer.outbound_segments[0].operator, "FlixBus")
        self.assertEqual(
            offer.outbound_segments[0].departure_terminal_id,
            "par-bercy-seine-bus",
        )
        self.assertEqual(
            offer.outbound_segments[0].arrival_terminal_id,
            "ams-sloterdijk-bus",
        )
        self.assertEqual(len(offer.booking_components), 1)
        self.assertIsNone(offer.booking_components[0].booking_url)
        self.assertEqual(
            offer.booking_components[0].price_completeness,
            PriceCompleteness.ESTIMATED,
        )
        self.assertIn("adult=1", loader.calls[0])
        self.assertIn("currency=EUR", loader.calls[0])
        self.assertIsNotNone(result.response_fingerprint)
        self.assertIsNotNone(result.schema_fingerprint)

    def test_flixbus_candidate_diagnostic_requires_separate_exact_stop_config(
        self,
    ) -> None:
        case = CASES[0]
        request = replace(
            _request(),
            search_kind=SearchKind.ONE_WAY,
            direction=Direction.OUTBOUND,
            return_window=None,
        )
        adapter = FlixBusAdapter(
            _spec(case),
            loader=FixturePageLoader(
                case,
                override_html=_fixture("flixbus-rendered-results.html"),
            ),
            clock=lambda: OBSERVED_AT,
            environ=_environment(case),
            minimum_request_interval_seconds=0,
        )

        result = asyncio.run(adapter.search(request))

        self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)
        self.assertEqual(
            result.diagnostic_code,
            "FLIXBUS_CANDIDATES_2_STOPS_UNCONFIGURED",
        )
        self.assertEqual(result.offers, ())

    def test_flixbus_rendered_card_schema_drift_fails_loudly(self) -> None:
        case = CASES[0]
        html = _fixture("flixbus-rendered-results.html").replace(
            'datetime="2027-01-08T19:30:00.000Z"',
            "",
            1,
        )

        result = _run(case, override_html=html)

        self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)
        self.assertEqual(result.diagnostic_code, "FLIXBUS_PUBLIC_MARKUP_INVALID")
        self.assertEqual(result.offers, ())

    def test_flixbus_pairs_direct_cards_with_twelve_hour_stay(self) -> None:
        case = CASES[0]
        loader = FlixBusRenderedFixtureLoader()
        adapter = FlixBusAdapter(
            _spec(case),
            loader=loader,
            clock=lambda: OBSERVED_AT,
            environ=_flixbus_environment(case),
            minimum_request_interval_seconds=0,
        )

        result = asyncio.run(adapter.search(_request()))

        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_RESULTS)
        self.assertEqual(len(result.offers), 1)
        offer = result.offers[0]
        self.assertEqual(offer.pricing_semantics, PricingSemantics.COMPOSABLE)
        self.assertEqual(offer.total_amount_minor, 4_997)
        self.assertEqual(offer.price_completeness, PriceCompleteness.ESTIMATED)
        self.assertFalse(offer.mandatory_fees_included)
        self.assertEqual(len(offer.outbound_segments), 1)
        self.assertEqual(len(offer.return_segments), 1)
        self.assertGreaterEqual(
            offer.return_segments[0].departs_at
            - offer.outbound_segments[0].arrives_at,
            timedelta(hours=12),
        )
        self.assertEqual(
            [component.amount_minor for component in offer.booking_components],
            [2_398, 2_599],
        )
        self.assertTrue(
            all(component.booking_url is None for component in offer.booking_components)
        )
        # The cheaper connecting return card must never become an inferred leg.
        self.assertNotEqual(offer.total_amount_minor, 2_398 + 1_200)

    def test_flixbus_rejects_round_trip_below_minimum_stay(self) -> None:
        case = CASES[0]
        paris = ZoneInfo("Europe/Paris")
        request = replace(
            _request(),
            return_window=TimeWindow(
                datetime(2027, 1, 9, 6, tzinfo=paris),
                datetime(2027, 1, 9, 23, 59, tzinfo=paris),
                "Europe/Paris",
            ),
        )
        return_html = (
            _fixture("flixbus-rendered-return.html")
            .replace(
                'datetime="2027-01-10T17:00:00.000Z"',
                'datetime="2027-01-09T10:00:00.000Z"',
                1,
            )
            .replace(
                'datetime="2027-01-11T04:30:00.000Z"',
                'datetime="2027-01-09T21:30:00.000Z"',
                1,
            )
        )
        adapter = FlixBusAdapter(
            _spec(case),
            loader=FlixBusRenderedFixtureLoader(return_html=return_html),
            clock=lambda: OBSERVED_AT,
            environ=_flixbus_environment(case),
            minimum_request_interval_seconds=0,
        )

        result = asyncio.run(adapter.search(request))

        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_EMPTY)
        self.assertEqual(
            result.diagnostic_code,
            "FLIXBUS_NO_ELIGIBLE_DISPLAYED_FARES",
        )
        self.assertEqual(result.offers, ())

    def test_flixbus_rejects_direct_card_over_twelve_hours(self) -> None:
        case = CASES[0]
        request = replace(
            _request(),
            search_kind=SearchKind.ONE_WAY,
            direction=Direction.OUTBOUND,
            return_window=None,
        )
        html = _fixture("flixbus-rendered-results.html").replace(
            'datetime="2027-01-09T02:15:00.000Z"',
            'datetime="2027-01-09T08:15:00.000Z"',
            1,
        )
        environment = _flixbus_environment(case)
        adapter = FlixBusAdapter(
            _spec(case),
            loader=FixturePageLoader(case, override_html=html),
            clock=lambda: OBSERVED_AT,
            environ=environment,
            minimum_request_interval_seconds=0,
        )

        result = asyncio.run(adapter.search(request))

        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_EMPTY)
        self.assertEqual(result.offers, ())

    def test_flixbus_requires_one_adult_public_query(self) -> None:
        case = CASES[0]
        environment = _flixbus_environment(case)
        environment["CROWL_FLIXBUS_PUBLIC_SEARCH_URL_TEMPLATE"] = (
            "https://shop.global.flixbus.com/public-search?origin={origin}"
            "&destination={destination}&date={date}&direction={direction}"
            "&currency=EUR"
        )
        adapter = FlixBusAdapter(
            _spec(case),
            loader=ExplodingLoader(),
            clock=lambda: OBSERVED_AT,
            environ=environment,
            minimum_request_interval_seconds=0,
        )

        result = asyncio.run(adapter.search(_request()))

        self.assertEqual(result.outcome, SearchOutcome.CONFIGURATION_ERROR)
        self.assertEqual(
            result.diagnostic_code,
            "FLIXBUS_PUBLIC_PAGE_CONFIG_INVALID",
        )

    def test_flixbus_serializes_concurrent_searches_before_deadline(self) -> None:
        case = CASES[0]
        loader = FlixBusRenderedFixtureLoader()
        request = replace(
            _request(),
            search_kind=SearchKind.ONE_WAY,
            direction=Direction.OUTBOUND,
            return_window=None,
        )
        adapter = FlixBusAdapter(
            _spec(case),
            loader=loader,
            clock=lambda: OBSERVED_AT,
            environ=_flixbus_environment(case),
            minimum_request_interval_seconds=0,
        )

        async def run_both() -> list[Any]:
            return await asyncio.gather(
                adapter.search(request),
                adapter.search(request),
            )

        first, second = asyncio.run(run_both())

        self.assertEqual(first.outcome, SearchOutcome.SUCCESS_RESULTS)
        self.assertEqual(second.outcome, SearchOutcome.SUCCESS_RESULTS)
        self.assertEqual(loader.max_active, 1)

    def test_structured_provider_parsers_normalize_exact_public_round_trip_fares(
        self,
    ) -> None:
        request = _request()
        for case in CASES[1:]:
            with self.subTest(source=case.slug):
                loader = FixturePageLoader(case)
                adapter = case.adapter_type(
                    _spec(case),
                    loader=loader,
                    clock=lambda: OBSERVED_AT,
                    environ=_environment(case),
                )

                result = asyncio.run(adapter.search(request))

                self.assertEqual(result.outcome, SearchOutcome.SUCCESS_RESULTS)
                self.assertFalse(result.retryable)
                self.assertEqual(result.expected_count, 3)
                self.assertEqual(result.completed_count, 3)
                self.assertIsNotNone(result.response_fingerprint)
                self.assertIsNotNone(result.schema_fingerprint)
                self.assertEqual(len(result.offers), 1)
                offer = result.offers[0]
                self.assertEqual(offer.source_slug, case.slug)
                self.assertEqual(offer.pricing_semantics, PricingSemantics.COMPOSABLE)
                self.assertEqual(offer.total_amount_minor, 4_798)
                self.assertEqual(offer.currency, "EUR")
                self.assertTrue(offer.mandatory_fees_included)
                self.assertTrue(offer.passenger_eligibility_confirmed)
                self.assertEqual(len(offer.booking_components), 2)
                self.assertEqual(
                    sum(item.amount_minor for item in offer.booking_components),
                    offer.total_amount_minor,
                )
                self.assertEqual(
                    offer.outbound_segments[0].departure_terminal_id,
                    "par-bercy-seine-bus",
                )
                self.assertEqual(
                    offer.return_segments[0].arrival_terminal_id,
                    "par-bercy-seine-bus",
                )
                self.assertLessEqual(
                    offer.return_segments[-1].arrives_at,
                    request.return_window.ends_at,  # type: ignore[union-attr]
                )

    def test_teaser_aggregate_price_is_never_treated_as_an_exact_fare(self) -> None:
        case = CASES[2]
        html = _provider_fixture("public-outbound.html", case).replace(
            '"@type": "Offer"', '"@type": "AggregateOffer"', 1
        )
        result = _run(case, override_html=html)
        self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)
        self.assertEqual(result.diagnostic_code, "ITABUS_PUBLIC_MARKUP_INVALID")
        self.assertEqual(result.offers, ())

    def test_reused_offer_id_cannot_overwrite_a_different_bus_trip(self) -> None:
        case = CASES[2]
        base = _provider_fixture("public-outbound.html", case)
        script_start = base.index('<script type="application/ld+json">')
        script_end = base.index("</script>", script_start) + len("</script>")
        original_script = base[script_start:script_end]
        conflicting_script = original_script.replace(
            '"arrivalTime": "2027-01-09T04:00:00+01:00"',
            '"arrivalTime": "2027-01-09T05:00:00+01:00"',
        )
        html = base[:script_end] + conflicting_script + base[script_end:]

        result = _run(case, override_html=html)

        self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)
        self.assertEqual(result.offers, ())

    def test_directional_return_search_reverses_terminals_and_stays_one_way(
        self,
    ) -> None:
        case = CASES[2]
        round_trip = _request()
        request = replace(
            round_trip,
            search_kind=SearchKind.ONE_WAY,
            direction=Direction.RETURN,
            outbound_window=round_trip.return_window,
            return_window=None,
        )
        adapter = case.adapter_type(
            _spec(case),
            loader=FixturePageLoader(case),
            clock=lambda: OBSERVED_AT,
            environ=_environment(case),
        )
        result = asyncio.run(adapter.search(request))
        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_RESULTS)
        self.assertEqual(result.offers[0].pricing_semantics, PricingSemantics.ONE_WAY)
        self.assertEqual(result.offers[0].total_amount_minor, 2_299)
        self.assertEqual(
            result.offers[0].outbound_segments[0].departure_terminal_id,
            "ams-sloterdijk-bus",
        )
        self.assertEqual(
            result.offers[0].outbound_segments[-1].arrival_terminal_id,
            "par-bercy-seine-bus",
        )

    def test_unconfirmed_fees_and_cross_host_booking_links_fail_closed(self) -> None:
        case = CASES[2]
        base = _provider_fixture("public-outbound.html", case)
        invalid_pages = (
            base.replace(
                "All mandatory taxes and fees included.",
                "Taxes may be calculated during checkout.",
            ),
            base.replace(
                "All mandatory taxes and fees included.",
                "Taxes and mandatory fees are not included.",
            ),
            base.replace(case.booking_url, "https://attacker.example/checkout"),
            base.replace('"value": 1', '"value": 2'),
            base.replace(
                '"requiresPaidMembership": false',
                '"requiresPaidMembership": true',
            ),
            base.replace(
                (
                    '"price": "24.99",\n'
                    '                "priceCurrency": "EUR",\n'
                    '                "valueAddedTaxIncluded"'
                ),
                (
                    '"price": "19.99",\n'
                    '                "priceCurrency": "EUR",\n'
                    '                "valueAddedTaxIncluded"'
                ),
            ),
        )
        for html in invalid_pages:
            with self.subTest(html=html[-100:]):
                result = _run(case, override_html=html)
                self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)
                self.assertFalse(result.retryable)
                self.assertEqual(result.offers, ())

    def test_explicit_no_service_page_is_an_honest_empty_outcome(self) -> None:
        case = CASES[3]
        result = _run(case, override_html=_fixture("public-no-results.html"))
        self.assertEqual(result.outcome, SearchOutcome.NO_SERVICE)
        self.assertEqual(result.diagnostic_code, "ALSA_NO_SERVICE")
        self.assertEqual(result.offers, ())

    def test_captcha_and_http_block_are_typed_and_never_bypassed(self) -> None:
        case = CASES[1]
        captcha = _run(
            case,
            override_html="<html><body>Verify you are human — hCaptcha</body></html>",
        )
        self.assertEqual(captcha.outcome, SearchOutcome.CAPTCHA_CHALLENGE)
        self.assertEqual(captcha.diagnostic_code, "BLABLACAR_BUS_CAPTCHA_CHALLENGE")
        self.assertFalse(captcha.retryable)

        blocked = _run(case, override_html="<html></html>", status=403)
        self.assertEqual(blocked.outcome, SearchOutcome.BLOCKED)
        self.assertEqual(blocked.diagnostic_code, "BLABLACAR_BUS_PAGE_BLOCKED")
        self.assertFalse(blocked.retryable)

    def test_captcha_words_in_bundles_or_hidden_nodes_are_not_challenges(self) -> None:
        case = CASES[0]
        html = """
        <html><body>
          <script>window.copy = {captcha: "verify that you are human"};</script>
          <div aria-hidden="true">hCaptcha security challenge</div>
          <main>No trips available</main>
        </body></html>
        """

        result = _run(case, override_html=html)

        self.assertEqual(result.outcome, SearchOutcome.NO_SERVICE)
        self.assertEqual(result.diagnostic_code, "FLIXBUS_NO_SERVICE")

    def test_hidden_empty_message_does_not_confirm_no_service(self) -> None:
        html = """
        <html><body>
          <script>window.copy = {empty: "No trips available"};</script>
          <div hidden>No trips available</div>
          <main>Loading results</main>
        </body></html>
        """

        for case in CASES:
            with self.subTest(source=case.slug):
                result = _run(case, override_html=html)
                self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)
                self.assertEqual(result.offers, ())

    def test_flixbus_city_aliases_may_cover_multiple_exact_stops(self) -> None:
        case = CASES[0]
        environment = _flixbus_environment(case)
        environment["CROWL_FLIXBUS_TERMINAL_ALIASES_JSON"] = json.dumps(
            {
                "par-bercy-seine-bus": "PAR_PROVIDER",
                "par-porte-maillot-bus": "PAR_PROVIDER",
                "ams-sloterdijk-bus": "AMS_PROVIDER",
            }
        )
        adapter = FlixBusAdapter(
            _spec(case),
            loader=FlixBusRenderedFixtureLoader(),
            clock=lambda: OBSERVED_AT,
            environ=environment,
            minimum_request_interval_seconds=0,
        )

        result = asyncio.run(adapter.search(_request()))

        self.assertEqual(result.outcome, SearchOutcome.SUCCESS_RESULTS)
        self.assertEqual(len(result.offers), 1)
        self.assertEqual(
            result.offers[0].outbound_segments[0].departure_terminal_id,
            "par-bercy-seine-bus",
        )

    def test_missing_public_page_configuration_fails_before_rendering(self) -> None:
        case = CASES[2]
        adapter = case.adapter_type(
            _spec(case),
            loader=ExplodingLoader(),
            clock=lambda: OBSERVED_AT,
            environ={},
        )
        result = asyncio.run(adapter.search(_request()))
        self.assertEqual(result.outcome, SearchOutcome.CONFIGURATION_ERROR)
        self.assertEqual(result.diagnostic_code, "ITABUS_PUBLIC_PAGE_CONFIG_INVALID")
        self.assertFalse(result.retryable)

    def test_catalog_kill_switch_keeps_disabled_bus_adapters_network_inert(self) -> None:
        specs = {item.slug: item for item in load_source_specs()}
        for case in CASES[1:]:
            with self.subTest(source=case.slug):
                adapter = case.adapter_type(
                    specs[case.slug],
                    loader=ExplodingLoader(),
                    clock=lambda: OBSERVED_AT,
                    environ={},
                )
                self.assertFalse(adapter.metadata().enabled)
                self.assertEqual(
                    adapter.metadata().strategy, ExtractionStrategy.DISABLED
                )
                result = asyncio.run(adapter.search(_request()))
                self.assertEqual(result.outcome, SearchOutcome.SKIPPED_DISABLED)
                self.assertEqual(result.diagnostic_code, "SOURCE_DISABLED")

        self.assertTrue(specs["flixbus"].catalog_enabled)

    def test_coverage_is_explicit_config_not_inferred_from_marketing_pages(
        self,
    ) -> None:
        case = CASES[0]
        environment = _environment(case)
        environment["CROWL_FLIXBUS_COVERAGE_JSON"] = json.dumps(
            {
                "supported_terminal_pairs": [
                    ["par-bercy-seine-bus", "ams-sloterdijk-bus"]
                ],
                "sellable_through": "2027-06-30",
            }
        )
        adapter = case.adapter_type(
            _spec(case),
            loader=FixturePageLoader(case),
            clock=lambda: OBSERVED_AT,
            environ=environment,
        )
        coverage = asyncio.run(adapter.discover_coverage())
        self.assertEqual(coverage.diagnostic_code, "FLIXBUS_COVERAGE_OK")
        self.assertEqual(
            coverage.supported_terminal_pairs,
            (("par-bercy-seine-bus", "ams-sloterdijk-bus"),),
        )
        self.assertEqual(coverage.sellable_through, date(2027, 6, 30))

    def test_close_releases_loader_and_rejects_future_searches(self) -> None:
        case = CASES[0]
        loader = FixturePageLoader(case)
        adapter = case.adapter_type(
            _spec(case),
            loader=loader,
            clock=lambda: OBSERVED_AT,
            environ=_environment(case),
        )
        asyncio.run(adapter.close())
        self.assertTrue(loader.closed)
        with self.assertRaisesRegex(RuntimeError, "adapter is closed"):
            asyncio.run(adapter.search(_request()))


def _run(
    case: ProviderCase,
    *,
    override_html: str,
    status: int = 200,
):
    adapter_kwargs: dict[str, Any] = {}
    if case.adapter_type is FlixBusAdapter:
        adapter_kwargs["minimum_request_interval_seconds"] = 0
    adapter = case.adapter_type(
        _spec(case),
        loader=FixturePageLoader(
            case,
            override_html=override_html,
            status=status,
        ),
        clock=lambda: OBSERVED_AT,
        environ=_environment(case),
        **adapter_kwargs,
    )
    return asyncio.run(adapter.search(_request()))


def _request() -> SearchRequest:
    paris = ZoneInfo("Europe/Paris")
    return SearchRequest(
        batch_id="crawl-2027-01-01",
        query_id="bus-public-page-test",
        destination_city_id="amsterdam-nl",
        weekend_start=date(2027, 1, 8),
        search_kind=SearchKind.ROUND_TRIP,
        direction=None,
        outbound_window=TimeWindow(
            datetime(2027, 1, 8, 19, tzinfo=paris),
            datetime(2027, 1, 8, 23, 59, tzinfo=paris),
            "Europe/Paris",
        ),
        return_window=TimeWindow(
            datetime(2027, 1, 10, 12, tzinfo=paris),
            datetime(2027, 1, 11, 7, tzinfo=paris),
            "Europe/Paris",
        ),
        origin_terminal_ids=("par-bercy-seine-bus",),
        destination_terminal_ids=("ams-sloterdijk-bus",),
        passenger=PassengerProfile(adults=1, age=22, student=True),
        currency="EUR",
        baggage="smallest_included_personal_item",
        maximum_direction_minutes=720,
    )


def _spec(case: ProviderCase) -> SourceSpec:
    return SourceSpec(
        slug=case.slug,
        name=case.display_name,
        modes=(TransportMode.BUS,),
        intended_strategy=case.intended_strategy,
        access_basis="ordinary public provider page test",
        disabled_reason="test source disabled",
        state="enabled",
        configured_enabled=True,
        kill_switch=False,
        config_version="test-v1",
        booking_hosts=(case.host.removeprefix("www.").removeprefix("shop.global."),),
    )


def _environment(case: ProviderCase) -> dict[str, str]:
    prefix = f"CROWL_{case.slug.upper()}"
    return {
        f"{prefix}_PUBLIC_SEARCH_URL_TEMPLATE": (
            f"https://{case.host}/public-search?origin={{origin}}"
            "&destination={destination}&date={date}&direction={direction}"
            "&adult=1&currency=EUR"
        ),
        f"{prefix}_TERMINAL_ALIASES_JSON": json.dumps(
            {
                "par-bercy-seine-bus": "PAR_PROVIDER",
                "ams-sloterdijk-bus": "AMS_PROVIDER",
            }
        ),
    }


def _flixbus_environment(case: ProviderCase) -> dict[str, str]:
    environment = _environment(case)
    environment["CROWL_FLIXBUS_STOP_ALIASES_JSON"] = json.dumps(
        {
            "Paris (Bercy Seine)": "par-bercy-seine-bus",
            "Amsterdam Sloterdijk": "ams-sloterdijk-bus",
        }
    )
    return environment


def _provider_fixture(filename: str, case: ProviderCase) -> str:
    return (
        _fixture(filename)
        .replace("__SOURCE__", case.slug)
        .replace("__PROVIDER__", case.provider_name)
        .replace("__BOOKING_URL__", case.booking_url.replace("&", "&amp;"))
    )


def _fixture(filename: str) -> str:
    return (FIXTURES / filename).read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
