from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from crowl_crawler.adapters.catalog import SourceSpec
from crowl_crawler.adapters.providers.eurostar import EurostarAdapter
from crowl_crawler.adapters.providers.ouigo import OuigoAdapter
from crowl_crawler.adapters.providers.rail_html import HtmlPage
from crowl_crawler.adapters.providers.sncf_connect import SncfConnectAdapter
from crowl_crawler.adapters.providers.trenitalia_france import TrenitaliaFranceAdapter
from crowl_crawler.manifest import DailyManifestBuilder, DestinationTarget
from crowl_crawler.models import (
    ExtractionStrategy,
    SearchOutcome,
    TransportMode,
)


ROOT = Path(__file__).resolve().parents[2]
OBSERVED_AT = datetime(2027, 1, 1, 3, tzinfo=UTC)
PROVIDERS = (
    ("ouigo", "OUIGO", "ouigo.com", OuigoAdapter),
    ("sncf_connect", "SNCF Connect", "sncf-connect.com", SncfConnectAdapter),
    (
        "trenitalia_france",
        "Trenitalia France",
        "trenitalia.com",
        TrenitaliaFranceAdapter,
    ),
    ("eurostar", "Eurostar", "eurostar.com", EurostarAdapter),
)


class FakeHtmlLoader:
    def __init__(self, page: HtmlPage | BaseException) -> None:
        self.page = page
        self.calls: list[str] = []
        self.closed = False

    async def load(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        timeout_seconds: float,
        maximum_bytes: int,
    ) -> HtmlPage:
        self.calls.append(url)
        if isinstance(self.page, BaseException):
            raise self.page
        return self.page

    async def close(self) -> None:
        self.closed = True


def _spec(
    slug: str,
    name: str,
    host: str,
    *,
    enabled: bool = True,
) -> SourceSpec:
    return SourceSpec(
        slug=slug,
        name=name,
        modes=(TransportMode.TRAIN,),
        intended_strategy=ExtractionStrategy.PLAYWRIGHT,
        access_basis="ordinary public HTML rendering",
        disabled_reason="Catalog kill switch is active.",
        state="enabled" if enabled else "disabled",
        configured_enabled=enabled,
        kill_switch=not enabled,
        config_version="test-v1",
        booking_hosts=(host,),
    )


def _request():
    manifest = DailyManifestBuilder(origin_terminal_ids=("par-gare-du-nord",)).build(
        scheduled_for=date(2027, 1, 1),
        source_slugs=("fixture",),
        destinations=(DestinationTarget("amsterdam-nl", ("ams-centraal",)),),
        weekend_fridays=(date(2027, 1, 8),),
    )
    return manifest.tasks[0].request


def _fixture(slug: str, host: str, operator: str) -> bytes:
    template = (
        ROOT / "tests" / "crawler" / "fixtures" / "rail" / "schemaorg_round_trip.html"
    ).read_text(encoding="utf-8")
    return (
        template.replace("__SOURCE__", slug)
        .replace("__HOST__", host)
        .replace("__OPERATOR__", operator)
        .encode("utf-8")
    )


def _environment(slug: str, host: str) -> dict[str, str]:
    return {
        f"CROWL_{slug.upper()}_HTML_URL_TEMPLATE": (
            f"https://www.{host}/search?from={{origin}}&"
            "to={destination}&out={outbound_date}&return={return_date}&"
            "adults={adults}&currency={currency}"
        ),
        f"CROWL_{slug.upper()}_TERMINAL_ALIASES_JSON": json.dumps(
            {
                "par-gare-du-nord": "PAR_PROVIDER",
                "ams-centraal": "AMS_PROVIDER",
            }
        ),
    }


class RailHtmlProviderTests(unittest.TestCase):
    def test_all_four_rail_modules_parse_complete_public_html_offers(self) -> None:
        for slug, name, host, adapter_type in PROVIDERS:
            with self.subTest(source=slug):
                page = HtmlPage(
                    status=200,
                    content_type="text/html",
                    body=_fixture(slug, host, name),
                    final_url=f"https://www.{host}/search",
                )
                loader = FakeHtmlLoader(page)
                adapter = adapter_type(
                    _spec(slug, name, host),
                    loader=loader,
                    clock=lambda: OBSERVED_AT,
                    environment=_environment(slug, host),
                    coverage_pairs=(("par-gare-du-nord", "ams-centraal"),),
                    sellable_through=date(2027, 7, 1),
                )

                coverage = asyncio.run(adapter.discover_coverage())
                result = asyncio.run(adapter.search(_request()))
                asyncio.run(adapter.close())

                self.assertTrue(adapter.metadata().enabled)
                self.assertEqual(
                    coverage.supported_terminal_pairs,
                    (("par-gare-du-nord", "ams-centraal"),),
                )
                self.assertEqual(result.outcome, SearchOutcome.SUCCESS_RESULTS)
                self.assertEqual(result.expected_count, 8)
                self.assertEqual(result.completed_count, 8)
                self.assertEqual(len(result.offers), 1)
                offer = result.offers[0]
                self.assertEqual(offer.total_amount_minor, 7_840)
                self.assertEqual(offer.currency, "EUR")
                self.assertTrue(offer.mandatory_fees_included)
                self.assertEqual(
                    offer.outbound_segments[0].departure_terminal_id,
                    "par-gare-du-nord",
                )
                self.assertEqual(
                    offer.return_segments[0].arrival_terminal_id,
                    "par-gare-du-nord",
                )
                self.assertIn("out=2027-01-08", loader.calls[0])
                self.assertIn("from=PAR_PROVIDER", loader.calls[0])
                self.assertIn("to=AMS_PROVIDER", loader.calls[0])
                self.assertNotIn("par-gare-du-nord", loader.calls[0])
                self.assertEqual(len(loader.calls), 8)
                self.assertTrue(
                    any(
                        "out=2027-01-10" in url and "return=2027-01-11" in url
                        for url in loader.calls
                    )
                )
                self.assertTrue(loader.closed)

    def test_catalog_disabled_rail_adapter_never_loads_a_page(self) -> None:
        loader = FakeHtmlLoader(AssertionError("network attempted"))
        adapter = EurostarAdapter(
            _spec("eurostar", "Eurostar", "eurostar.com", enabled=False),
            loader=loader,
            clock=lambda: OBSERVED_AT,
            environment={},
        )
        result = asyncio.run(adapter.search(_request()))
        self.assertEqual(result.outcome, SearchOutcome.SKIPPED_DISABLED)
        self.assertEqual(loader.calls, [])

    def test_coverage_loads_from_railway_friendly_json_configuration(self) -> None:
        environment = _environment("eurostar", "eurostar.com")
        environment["CROWL_EUROSTAR_COVERAGE_JSON"] = json.dumps(
            {
                "source": "eurostar",
                "routes": [["par-gare-du-nord", "ams-centraal"]],
                "sellable_through": "2027-07-01",
            }
        )
        adapter = EurostarAdapter(
            _spec("eurostar", "Eurostar", "eurostar.com"),
            loader=FakeHtmlLoader(AssertionError("coverage attempted network")),
            clock=lambda: OBSERVED_AT,
            environment=environment,
        )
        coverage = asyncio.run(adapter.discover_coverage())
        self.assertEqual(
            coverage.supported_terminal_pairs,
            (("par-gare-du-nord", "ams-centraal"),),
        )
        self.assertEqual(coverage.sellable_through, date(2027, 7, 1))

    def test_challenge_and_rate_limit_are_not_bypassed(self) -> None:
        cases = (
            (
                HtmlPage(
                    200,
                    "text/html",
                    b"<html><body><h1>Captcha</h1>Verify you are human</body></html>",
                    "https://www.eurostar.com/search",
                ),
                SearchOutcome.CAPTCHA_CHALLENGE,
            ),
            (
                HtmlPage(
                    403,
                    "text/html",
                    b"<html><body><h1>Access denied</h1></body></html>",
                    "https://www.eurostar.com/search",
                ),
                SearchOutcome.BLOCKED,
            ),
            (
                HtmlPage(
                    429,
                    "text/html",
                    b"<html><body>Too many requests</body></html>",
                    "https://www.eurostar.com/search",
                ),
                SearchOutcome.RATE_LIMITED,
            ),
        )
        for page, expected in cases:
            with self.subTest(expected=expected):
                adapter = EurostarAdapter(
                    _spec("eurostar", "Eurostar", "eurostar.com"),
                    loader=FakeHtmlLoader(page),
                    clock=lambda: OBSERVED_AT,
                    environment=_environment("eurostar", "eurostar.com"),
                )
                result = asyncio.run(adapter.search(_request()))
                self.assertEqual(result.outcome, expected)

    def test_explicit_empty_page_differs_from_schema_drift(self) -> None:
        empty = HtmlPage(
            200,
            "text/html",
            b"<html><body>No trains available for this search.</body></html>",
            "https://www.eurostar.com/search",
        )
        malformed_offer = {
            "@context": "https://schema.org",
            "@type": "Offer",
            "identifier": "teaser",
            "price": "29.00",
            "priceCurrency": "EUR",
            "url": "https://www.eurostar.com/search",
        }
        malformed = HtmlPage(
            200,
            "text/html",
            (
                "<html><script type='application/ld+json'>"
                + json.dumps(malformed_offer)
                + "</script><body>Prices from EUR 29</body></html>"
            ).encode(),
            "https://www.eurostar.com/search",
        )
        for page, expected in (
            (empty, SearchOutcome.SUCCESS_EMPTY),
            (malformed, SearchOutcome.PARSE_SCHEMA_ERROR),
        ):
            with self.subTest(expected=expected):
                adapter = EurostarAdapter(
                    _spec("eurostar", "Eurostar", "eurostar.com"),
                    loader=FakeHtmlLoader(page),
                    clock=lambda: OBSERVED_AT,
                    environment=_environment("eurostar", "eurostar.com"),
                )
                result = asyncio.run(adapter.search(_request()))
                self.assertEqual(result.outcome, expected)

    def test_unrelated_page_copy_does_not_classify_the_search(self) -> None:
        extras = (
            b'<script>const error = "Verify that you are human";</script>'
            b'<div hidden><p>Access denied</p></div>'
            b'<footer><label>CAPTCHA</label><p>No trains available</p></footer>'
        )
        for body, expected in (
            (
                _fixture("eurostar", "eurostar.com", "Eurostar").replace(
                    b"</body>", extras + b"</body>"
                ),
                SearchOutcome.SUCCESS_RESULTS,
            ),
            (
                b"<html><body>Loading" + extras + b"</body></html>",
                SearchOutcome.PARSE_SCHEMA_ERROR,
            ),
        ):
            with self.subTest(expected=expected):
                adapter = EurostarAdapter(
                    _spec("eurostar", "Eurostar", "eurostar.com"),
                    loader=FakeHtmlLoader(
                        HtmlPage(200, "text/html", body, "https://www.eurostar.com/search")
                    ),
                    clock=lambda: OBSERVED_AT,
                    environment=_environment("eurostar", "eurostar.com"),
                )
                result = asyncio.run(adapter.search(_request()))
                self.assertEqual(result.outcome, expected)

    def test_eurostar_reports_rendered_search_error_as_upstream_failure(self) -> None:
        page = HtmlPage(
            200,
            "text/html",
            "<html><body><h1>Oups, une erreur s’est produite</h1></body></html>".encode(),
            "https://www.eurostar.com/search",
        )
        adapter = EurostarAdapter(
            _spec("eurostar", "Eurostar", "eurostar.com"),
            loader=FakeHtmlLoader(page),
            clock=lambda: OBSERVED_AT,
            environment=_environment("eurostar", "eurostar.com"),
        )
        result = asyncio.run(adapter.search(_request()))
        self.assertEqual(result.outcome, SearchOutcome.UPSTREAM_ERROR)
        self.assertEqual(result.diagnostic_code, "RAIL_HTML_RESULTS_UNAVAILABLE")
        self.assertEqual(result.completed_count, 0)

    def test_unapproved_result_url_is_a_schema_failure(self) -> None:
        page = HtmlPage(
            200,
            "text/html",
            _fixture("eurostar", "eurostar.com", "Eurostar"),
            "https://attacker.example/search",
        )
        adapter = EurostarAdapter(
            _spec("eurostar", "Eurostar", "eurostar.com"),
            loader=FakeHtmlLoader(page),
            clock=lambda: OBSERVED_AT,
            environment=_environment("eurostar", "eurostar.com"),
        )
        result = asyncio.run(adapter.search(_request()))
        self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)

    def test_unconfirmed_fees_or_passenger_eligibility_fail_closed(self) -> None:
        base = _fixture("eurostar", "eurostar.com", "Eurostar")
        invalid_pages = (
            base.replace(
                b"All mandatory taxes and fees included.",
                b"Some fees may be added during checkout.",
            ),
            base.replace(
                b"All mandatory taxes and fees included.",
                b"Taxes and mandatory fees are not included.",
            ),
            base.replace(
                b'"eligibleCustomerType": "Adult"',
                b'"eligibleCustomerType": "Youth"',
            ),
            base.replace(
                b'"value": 1',
                b'"value": 2',
            ),
            base.replace(
                b'"requiresPaidMembership": false',
                b'"requiresPaidMembership": true',
            ),
            base.replace(
                b'"name": "Standard",',
                b'"name": "Standard",\n        "price": "12.00",',
            ),
            base.replace(b'"@type": "TrainTrip"', b'"@type": "Trip"', 1),
            base.replace(
                b'"@type": "TrainTrip",',
                b'"@type": "TrainTrip",\n            "hasPart": [],',
                1,
            ),
        )
        for body in invalid_pages:
            with self.subTest(fragment=body[:80]):
                page = HtmlPage(
                    200,
                    "text/html",
                    body,
                    "https://www.eurostar.com/search",
                )
                adapter = EurostarAdapter(
                    _spec("eurostar", "Eurostar", "eurostar.com"),
                    loader=FakeHtmlLoader(page),
                    clock=lambda: OBSERVED_AT,
                    environment=_environment("eurostar", "eurostar.com"),
                )
                result = asyncio.run(adapter.search(_request()))
                self.assertEqual(result.outcome, SearchOutcome.PARSE_SCHEMA_ERROR)
                self.assertEqual(result.offers, ())

    def test_api_path_is_rejected_before_page_loading(self) -> None:
        loader = FakeHtmlLoader(AssertionError("API path reached the loader"))
        adapter = EurostarAdapter(
            _spec("eurostar", "Eurostar", "eurostar.com"),
            loader=loader,
            clock=lambda: OBSERVED_AT,
            environment={
                "CROWL_EUROSTAR_HTML_URL_TEMPLATE": (
                    "https://www.eurostar.com/api/search?from={origin_terminal_id}"
                ),
                "CROWL_EUROSTAR_TERMINAL_ALIASES_JSON": json.dumps(
                    {
                        "par-gare-du-nord": "PAR_PROVIDER",
                        "ams-centraal": "AMS_PROVIDER",
                    }
                ),
            },
        )
        result = asyncio.run(adapter.search(_request()))
        self.assertEqual(result.outcome, SearchOutcome.CONFIGURATION_ERROR)
        self.assertEqual(loader.calls, [])


if __name__ == "__main__":
    unittest.main()
