"""Conservative public-HTML extraction for Crowl's rail providers.

No provider API is called.  The adapter reads a public search-result page (or
the DOM produced by ordinary Playwright rendering) and accepts only complete,
bookable schema.org ``Offer`` + ``TrainTrip`` markup.  A source without that
markup is reported as parser drift and can stay kill-switched; the extractor
never falls back to guessing prices from marketing copy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import ssl
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from ..base import SourceAdapter
from ..catalog import DATA_DIRECTORY_ENV, SourceSpec
from ...models import (
    AdapterMetadata,
    BookingComponent,
    ConnectorResult,
    CoverageDiscovery,
    Direction,
    ExtractionStrategy,
    NormalizedOffer,
    PriceCompleteness,
    PricingSemantics,
    SearchKind,
    SearchOutcome,
    SearchRequest,
    Segment,
    TransportMode,
)
from ...trust import OfferRejected, sanitize_booking_url


MAXIMUM_HTML_BYTES = 3_000_000
MAXIMUM_JSON_SCRIPTS = 200
MAXIMUM_OFFERS = 500
DEFAULT_TIMEOUT_SECONDS = 25.0
MAXIMUM_SEARCH_PAGES = 8
SEARCH_DEADLINE_SECONDS = 120
_CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "vérifiez que vous êtes humain",
    "enable javascript and disable any ad blocker",
    "access denied",
    "unusual traffic",
)
_TEASER_MARKERS = (
    "from price",
    "prices from",
    "starting at",
    "à partir de",
    "dès ",
)
_FEE_INCLUDED = re.compile(
    r"(?:mandatory\s+)?(?:tax(?:es)?|fees?).{0,60}included|"
    r"included.{0,60}(?:mandatory\s+)?(?:tax(?:es)?|fees?)",
    re.IGNORECASE,
)
_FEE_EXCLUDED = re.compile(
    r"(?:tax(?:es)?|fees?).{0,60}(?:not|never)\s+(?:be\s+)?included|"
    r"(?:does|do)\s+not\s+include.{0,60}(?:tax(?:es)?|fees?)|"
    r"(?:tax(?:es)?|fees?).{0,60}(?:excluded|extra|additional|separate)|"
    r"(?:excluded|excluding|extra|additional).{0,60}(?:tax(?:es)?|fees?)|"
    r"(?:tax(?:es)?|fees?).{0,60}(?:added|calculated).{0,30}(?:checkout|later)",
    re.IGNORECASE,
)
_API_PATH = re.compile(r"(?:^|/)(?:api|graphql|rest)(?:/|$)", re.IGNORECASE)
_CONSENT_LABELS = (
    "Accept all",
    "Accept cookies",
    "Tout accepter",
    "Accepter tout",
    "Accetta tutto",
    "Aceptar todo",
)


class RailHtmlConfigurationError(ValueError):
    """A public-page adapter is enabled without its local crawl recipe."""


class RailHtmlTooLarge(ValueError):
    """A public result page exceeded the bounded parser input."""


@dataclass(frozen=True, slots=True)
class HtmlPage:
    status: int
    content_type: str
    body: bytes
    final_url: str


class HtmlPageLoader(Protocol):
    async def load(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        timeout_seconds: float,
        maximum_bytes: int,
    ) -> HtmlPage: ...

    async def close(self) -> None: ...


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class PublicHtmlLoader:
    """GET a public HTML page without credentials, cookies, or redirects."""

    async def load(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        timeout_seconds: float,
        maximum_bytes: int,
    ) -> HtmlPage:
        _validate_public_url(url, allowed_hosts)
        return await asyncio.to_thread(
            self._load_sync,
            url,
            timeout_seconds=timeout_seconds,
            maximum_bytes=maximum_bytes,
        )

    @staticmethod
    def _load_sync(
        url: str,
        *,
        timeout_seconds: float,
        maximum_bytes: int,
    ) -> HtmlPage:
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Crowl/1 public-fare-monitor",
            },
        )
        opener = build_opener(_NoRedirects())
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(maximum_bytes + 1)
                status = int(response.status)
                content_type = response.headers.get_content_type().lower()
                final_url = response.geturl()
        except HTTPError as error:
            body = error.read(maximum_bytes + 1)
            status = int(error.code)
            content_type = error.headers.get_content_type().lower()
            final_url = error.geturl()
        if len(body) > maximum_bytes:
            raise RailHtmlTooLarge("public rail HTML exceeded the size limit")
        return HtmlPage(status, content_type, body, final_url)

    async def close(self) -> None:
        return None


class PlaywrightHtmlLoader:
    """Render a normal public page; no stealth, proxy, challenge, or login logic."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None

    async def load(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        timeout_seconds: float,
        maximum_bytes: int,
    ) -> HtmlPage:
        _validate_public_url(url, allowed_hosts)
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise RailHtmlConfigurationError(
                "Playwright is not installed in the crawler image"
            ) from error
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
        try:
            context = await self._browser.new_context(
                locale="en-GB",
                timezone_id="Europe/Paris",
                service_workers="block",
            )
            page = await context.new_page()
            page.set_default_timeout(timeout_seconds * 1000)
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_seconds * 1000,
                )
                for label in _CONSENT_LABELS:
                    button = page.get_by_role(
                        "button", name=re.compile(rf"^{re.escape(label)}$", re.I)
                    ).first
                    try:
                        if await button.is_visible(timeout=250):
                            await button.click(timeout=1_500)
                            break
                    except PlaywrightTimeoutError:
                        continue
                await page.wait_for_timeout(1200)
                final_url = page.url
                _validate_public_url(final_url, allowed_hosts)
                html = (await page.content()).encode("utf-8")
                if len(html) > maximum_bytes:
                    raise RailHtmlTooLarge("rendered rail HTML exceeded the size limit")
                return HtmlPage(
                    status=response.status if response is not None else 0,
                    content_type="text/html",
                    body=html,
                    final_url=final_url,
                )
            finally:
                await context.close()
        except (RailHtmlConfigurationError, RailHtmlTooLarge):
            raise
        except PlaywrightTimeoutError as error:
            raise TimeoutError("public rail page timed out") from error
        except PlaywrightError as error:
            raise OSError("public rail page could not be rendered") from error

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


class _StructuredHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.text_parts: list[str] = []
        self._capturing_json = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script" or len(self.scripts) >= MAXIMUM_JSON_SCRIPTS:
            return
        attributes = {key.lower(): value for key, value in attrs}
        script_type = (attributes.get("type") or "").lower().split(";", 1)[0]
        if script_type == "application/ld+json":
            self._capturing_json = True
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing_json:
            self._script_parts.append(data)
        elif data.strip():
            self.text_parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing_json:
            self.scripts.append("".join(self._script_parts))
            self._capturing_json = False
            self._script_parts = []

    @property
    def normalized_text(self) -> str:
        return " ".join(" ".join(self.text_parts).lower().split())[:250_000]


@dataclass(frozen=True, slots=True)
class HtmlRailConfiguration:
    url_template: str
    terminal_aliases: Mapping[str, str]
    timeout_seconds: float

    @classmethod
    def from_environment(
        cls, source_slug: str, environment: Mapping[str, str]
    ) -> "HtmlRailConfiguration":
        prefix = f"CROWL_{source_slug.upper()}"
        url_template = environment.get(f"{prefix}_HTML_URL_TEMPLATE", "").strip()
        if not url_template:
            raise RailHtmlConfigurationError(
                f"{prefix}_HTML_URL_TEMPLATE is required for public-page crawling"
            )
        aliases_key = f"{prefix}_TERMINAL_ALIASES_JSON"
        raw_aliases = environment.get(aliases_key, "").strip()
        if not raw_aliases:
            raise RailHtmlConfigurationError(
                f"{aliases_key} is required for public-page crawling"
            )
        try:
            terminal_aliases = json.loads(raw_aliases)
        except json.JSONDecodeError as error:
            raise RailHtmlConfigurationError(
                "rail terminal aliases JSON is invalid"
            ) from error
        if (
            not isinstance(terminal_aliases, dict)
            or not terminal_aliases
            or any(
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(value, str)
                or not value.strip()
                for key, value in terminal_aliases.items()
            )
        ):
            raise RailHtmlConfigurationError("rail terminal aliases are invalid")
        terminal_aliases = {
            key.strip(): value.strip() for key, value in terminal_aliases.items()
        }
        raw_timeout = environment.get(
            f"{prefix}_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)
        ).strip()
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError as error:
            raise RailHtmlConfigurationError("HTML timeout must be numeric") from error
        if not 1 <= timeout_seconds <= 60:
            raise RailHtmlConfigurationError(
                "HTML timeout must be between 1 and 60 seconds"
            )
        return cls(
            url_template=url_template,
            terminal_aliases=terminal_aliases,
            timeout_seconds=timeout_seconds,
        )


class PublicHtmlRailAdapter(SourceAdapter):
    """Provider slot for exact fares present in ordinary public HTML."""

    source_slug: ClassVar[str]
    operator_name: ClassVar[str]
    parser_version: ClassVar[str] = "schemaorg-rail-v1"
    extra_page_hosts: ClassVar[tuple[str, ...]] = ()
    empty_markers: ClassVar[tuple[str, ...]] = (
        "no trains found",
        "no journeys found",
        "no travel solutions",
    )

    def __init__(
        self,
        spec: SourceSpec,
        *,
        loader: HtmlPageLoader | None = None,
        clock: Callable[[], datetime] | None = None,
        environment: Mapping[str, str] | None = None,
        coverage_pairs: tuple[tuple[str, str], ...] | None = None,
        sellable_through: date | None = None,
        terminal_names: Mapping[str, str] | None = None,
    ) -> None:
        if spec.slug != self.source_slug:
            raise ValueError(
                f"provider class {type(self).__name__} handles {self.source_slug}, "
                f"not {spec.slug}"
            )
        self._spec = spec
        self._clock = clock or (lambda: datetime.now(UTC))
        self._environment = environment if environment is not None else os.environ
        self._coverage_pairs = coverage_pairs
        self._sellable_through = sellable_through
        self._terminal_names = dict(terminal_names or _load_terminal_names())
        self._closed = False
        if loader is not None:
            self._loader = loader
        elif spec.intended_strategy is ExtractionStrategy.PLAYWRIGHT:
            self._loader = PlaywrightHtmlLoader()
        else:
            self._loader = PublicHtmlLoader()
        enabled = spec.catalog_enabled
        self._metadata = AdapterMetadata(
            source_slug=spec.slug,
            display_name=spec.name,
            modes=spec.modes,
            strategy=(
                spec.intended_strategy if enabled else ExtractionStrategy.DISABLED
            ),
            intended_strategy=spec.intended_strategy,
            connector_version=f"{spec.config_version}-public-html-v1",
            parser_version=f"{spec.slug}-{self.parser_version}",
            enabled=enabled,
            disabled_reason=None if enabled else spec.disabled_reason,
            access_basis=spec.access_basis,
        )

    def metadata(self) -> AdapterMetadata:
        return self._metadata

    async def discover_coverage(self) -> CoverageDiscovery:
        self._ensure_open()
        observed_at = self._observed_at()
        if not self._metadata.enabled:
            return CoverageDiscovery(
                source_slug=self.source_slug,
                supported_terminal_pairs=(),
                sellable_through=None,
                observed_at=observed_at,
                diagnostic_code="SOURCE_DISABLED",
            )
        pairs = self._coverage_pairs
        horizon = self._sellable_through
        if pairs is None:
            pairs, horizon = _load_coverage_configuration(
                self.source_slug, self._environment
            )
        if pairs and horizon is None:
            raise RailHtmlConfigurationError(
                "public-page coverage needs a sellable-through date"
            )
        return CoverageDiscovery(
            source_slug=self.source_slug,
            supported_terminal_pairs=tuple(sorted(pairs)),
            sellable_through=horizon,
            observed_at=observed_at,
            diagnostic_code="RAIL_HTML_COVERAGE_OK",
        )

    async def canary(
        self, requests: Sequence[SearchRequest]
    ) -> tuple[ConnectorResult, ...]:
        return tuple([await self.search(request) for request in requests])

    async def search(self, request: SearchRequest) -> ConnectorResult:
        self._ensure_open()
        observed_at = self._observed_at()
        if not self._metadata.enabled:
            return _empty_result(
                SearchOutcome.SKIPPED_DISABLED,
                "SOURCE_DISABLED",
                self._metadata.parser_version,
                observed_at,
            )
        try:
            configuration = HtmlRailConfiguration.from_environment(
                self.source_slug, self._environment
            )
            urls = _render_urls(
                configuration.url_template,
                request,
                terminal_aliases=configuration.terminal_aliases,
            )
            allowed_hosts = tuple(
                dict.fromkeys((*self._spec.booking_hosts, *self.extra_page_hosts))
            )
            for url in urls:
                _validate_public_url(url, allowed_hosts)
        except (KeyError, RailHtmlConfigurationError, ValueError):
            return ConnectorResult.failure(
                outcome=SearchOutcome.CONFIGURATION_ERROR,
                diagnostic_code="RAIL_HTML_CONFIG_INVALID",
                parser_version=self._metadata.parser_version,
                observed_at=observed_at,
                retryable=False,
            )
        page_results: list[ConnectorResult] = []
        try:
            async with asyncio.timeout(SEARCH_DEADLINE_SECONDS):
                for index, url in enumerate(urls):
                    page = await self._loader.load(
                        url,
                        allowed_hosts=allowed_hosts,
                        timeout_seconds=configuration.timeout_seconds,
                        maximum_bytes=MAXIMUM_HTML_BYTES,
                    )
                    failure = _status_result(
                        page.status,
                        parser_version=self._metadata.parser_version,
                        observed_at=observed_at,
                    )
                    if failure is not None:
                        return replace(
                            failure,
                            expected_count=len(urls),
                            completed_count=index,
                        )
                    if page.content_type not in {
                        "text/html",
                        "application/xhtml+xml",
                    }:
                        failure = ConnectorResult.failure(
                            outcome=SearchOutcome.PARSE_SCHEMA_ERROR,
                            diagnostic_code="RAIL_HTML_CONTENT_TYPE",
                            parser_version=self._metadata.parser_version,
                            observed_at=observed_at,
                            retryable=False,
                        )
                        return replace(
                            failure,
                            expected_count=len(urls),
                            completed_count=index,
                        )
                    try:
                        _validate_public_url(page.final_url, allowed_hosts)
                    except RailHtmlConfigurationError:
                        failure = ConnectorResult.failure(
                            outcome=SearchOutcome.PARSE_SCHEMA_ERROR,
                            diagnostic_code="RAIL_HTML_UNEXPECTED_REDIRECT",
                            parser_version=self._metadata.parser_version,
                            observed_at=observed_at,
                            retryable=False,
                        )
                        return replace(
                            failure,
                            expected_count=len(urls),
                            completed_count=index,
                        )
                    parsed = _parse_html_page(
                        page.body,
                        spec=self._spec,
                        request=request,
                        operator_name=self.operator_name,
                        parser_version=self._metadata.parser_version,
                        observed_at=observed_at,
                        terminal_names=self._terminal_names,
                        empty_markers=self.empty_markers,
                    )
                    if parsed.outcome not in {
                        SearchOutcome.SUCCESS_RESULTS,
                        SearchOutcome.SUCCESS_EMPTY,
                        SearchOutcome.PARTIAL_SUCCESS,
                    }:
                        return replace(
                            parsed,
                            expected_count=len(urls),
                            completed_count=index + parsed.completed_count,
                        )
                    page_results.append(parsed)
        except (TimeoutError, socket.timeout):
            failure = ConnectorResult.failure(
                outcome=SearchOutcome.TIMEOUT,
                diagnostic_code="RAIL_HTML_TIMEOUT",
                parser_version=self._metadata.parser_version,
                observed_at=observed_at,
            )
            return replace(
                failure,
                expected_count=len(urls),
                completed_count=len(page_results),
            )
        except RailHtmlTooLarge:
            failure = ConnectorResult.failure(
                outcome=SearchOutcome.PARSE_SCHEMA_ERROR,
                diagnostic_code="RAIL_HTML_TOO_LARGE",
                parser_version=self._metadata.parser_version,
                observed_at=observed_at,
                retryable=False,
            )
            return replace(
                failure,
                expected_count=len(urls),
                completed_count=len(page_results),
            )
        except RailHtmlConfigurationError:
            failure = ConnectorResult.failure(
                outcome=SearchOutcome.CONFIGURATION_ERROR,
                diagnostic_code="RAIL_RENDERER_UNAVAILABLE",
                parser_version=self._metadata.parser_version,
                observed_at=observed_at,
                retryable=False,
            )
            return replace(
                failure,
                expected_count=len(urls),
                completed_count=len(page_results),
            )
        except (URLError, OSError, ssl.SSLError):
            failure = ConnectorResult.failure(
                outcome=SearchOutcome.UPSTREAM_ERROR,
                diagnostic_code="RAIL_HTML_NETWORK_ERROR",
                parser_version=self._metadata.parser_version,
                observed_at=observed_at,
            )
            return replace(
                failure,
                expected_count=len(urls),
                completed_count=len(page_results),
            )
        except (TypeError, ValueError, OfferRejected, UnicodeError):
            failure = ConnectorResult.failure(
                outcome=SearchOutcome.PARSE_SCHEMA_ERROR,
                diagnostic_code="RAIL_HTML_SCHEMA_REJECTED",
                parser_version=self._metadata.parser_version,
                observed_at=observed_at,
                retryable=False,
            )
            return replace(
                failure,
                expected_count=len(urls),
                completed_count=len(page_results),
            )
        try:
            return _combine_page_results(
                page_results,
                parser_version=self._metadata.parser_version,
                observed_at=observed_at,
            )
        except ValueError:
            failure = ConnectorResult.failure(
                outcome=SearchOutcome.PARSE_SCHEMA_ERROR,
                diagnostic_code="RAIL_HTML_SCHEMA_REJECTED",
                parser_version=self._metadata.parser_version,
                observed_at=observed_at,
                retryable=False,
            )
            return replace(
                failure,
                expected_count=len(urls),
                completed_count=len(page_results),
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._loader.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("rail HTML adapter is closed")

    def _observed_at(self) -> datetime:
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("rail HTML clock must be timezone-aware")
        return observed_at


def _render_urls(
    template: str,
    request: SearchRequest,
    *,
    terminal_aliases: Mapping[str, str],
) -> tuple[str, ...]:
    outbound_dates = _window_dates(request.outbound_window)
    date_pairs: list[tuple[date, date | None]] = []
    if request.search_kind is SearchKind.ROUND_TRIP:
        if request.return_window is None:
            raise RailHtmlConfigurationError("round-trip return window is missing")
        for outbound_date in outbound_dates:
            for return_date in _window_dates(request.return_window):
                if return_date >= outbound_date:
                    date_pairs.append((outbound_date, return_date))
    else:
        date_pairs.extend((outbound_date, None) for outbound_date in outbound_dates)
    if not date_pairs or len(date_pairs) > MAXIMUM_SEARCH_PAGES:
        raise RailHtmlConfigurationError("rail public-page date search is unbounded")
    return tuple(
        _render_url(
            template,
            request,
            outbound_date=outbound_date,
            return_date=return_date,
            terminal_aliases=terminal_aliases,
        )
        for outbound_date, return_date in date_pairs
    )


def _render_url(
    template: str,
    request: SearchRequest,
    *,
    outbound_date: date,
    return_date: date | None,
    terminal_aliases: Mapping[str, str],
) -> str:
    if (
        len(request.origin_terminal_ids) != 1
        or len(request.destination_terminal_ids) != 1
    ):
        raise RailHtmlConfigurationError(
            "public-page searches require route-specific terminal pairs"
        )
    is_return = (
        request.search_kind is SearchKind.ONE_WAY
        and request.direction is Direction.RETURN
    )
    origin = (
        request.destination_terminal_ids[0]
        if is_return
        else request.origin_terminal_ids[0]
    )
    destination = (
        request.origin_terminal_ids[0]
        if is_return
        else request.destination_terminal_ids[0]
    )
    try:
        origin_alias = terminal_aliases[origin]
        destination_alias = terminal_aliases[destination]
    except KeyError as error:
        raise RailHtmlConfigurationError(
            "rail terminal alias is missing for the requested route"
        ) from error
    values = {
        "origin": quote(origin_alias, safe=""),
        "destination": quote(destination_alias, safe=""),
        # Retain the established placeholder names, but deliberately render
        # provider aliases rather than leaking Crowl's canonical identifiers.
        "origin_terminal_id": quote(origin_alias, safe=""),
        "destination_terminal_id": quote(destination_alias, safe=""),
        "outbound_date": outbound_date.isoformat(),
        "return_date": return_date.isoformat() if return_date is not None else "",
        "direction": request.direction.value if request.direction else "round_trip",
        "search_kind": request.search_kind.value,
        "adults": str(request.passenger.adults),
        "age": str(request.passenger.age),
        "currency": request.currency,
    }
    try:
        rendered = template.format_map(values)
    except (KeyError, ValueError) as error:
        raise RailHtmlConfigurationError("HTML URL template is invalid") from error
    return rendered


def _window_dates(window: Any) -> tuple[date, ...]:
    timezone = ZoneInfo(window.timezone)
    first = window.starts_at.astimezone(timezone).date()
    last = window.ends_at.astimezone(timezone).date()
    dates = tuple(
        first + timedelta(days=offset) for offset in range((last - first).days + 1)
    )
    if not 1 <= len(dates) <= 3:
        raise RailHtmlConfigurationError(
            "rail public-page window must cover one to three dates"
        )
    return dates


def _combine_page_results(
    results: Sequence[ConnectorResult],
    *,
    parser_version: str,
    observed_at: datetime,
) -> ConnectorResult:
    if not results:
        raise ValueError("rail page results cannot be empty")
    unique: dict[str, NormalizedOffer] = {}
    saw_partial = False
    for result in results:
        saw_partial = saw_partial or result.outcome is SearchOutcome.PARTIAL_SUCCESS
        for offer in result.offers:
            existing = unique.get(offer.provider_offer_id)
            if existing is not None and existing != offer:
                raise ValueError("rail offer identifier changed across date pages")
            unique[offer.provider_offer_id] = offer
    offers = tuple(unique.values())
    outcome = (
        SearchOutcome.PARTIAL_SUCCESS
        if saw_partial and offers
        else SearchOutcome.SUCCESS_RESULTS
        if offers
        else SearchOutcome.SUCCESS_EMPTY
    )
    return ConnectorResult(
        outcome=outcome,
        retryable=False,
        diagnostic_code=(
            "RAIL_HTML_RESULTS_PARTIAL"
            if outcome is SearchOutcome.PARTIAL_SUCCESS
            else "RAIL_HTML_RESULTS_OK"
            if outcome is SearchOutcome.SUCCESS_RESULTS
            else "RAIL_HTML_RESULTS_EMPTY"
        ),
        expected_count=len(results),
        completed_count=len(results),
        offers=offers,
        parser_version=parser_version,
        observed_at=observed_at,
        response_fingerprint=_combine_hashes(
            result.response_fingerprint for result in results
        ),
        schema_fingerprint=_combine_hashes(
            result.schema_fingerprint for result in results
        ),
    )


def _combine_hashes(values: Iterable[str | None]) -> str | None:
    present = sorted(value for value in values if value)
    if not present:
        return None
    return hashlib.sha256(":".join(present).encode()).hexdigest()


def _validate_public_url(url: str, allowed_hosts: tuple[str, ...]) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise RailHtmlConfigurationError("public HTML URL is malformed") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise RailHtmlConfigurationError("public HTML URL must be plain HTTPS")
    host = parsed.hostname.rstrip(".").lower()
    if host.startswith("api.") or ".api." in host:
        raise RailHtmlConfigurationError("API hosts are outside the HTML contract")
    path = parsed.path.rstrip("/") or "/"
    if _API_PATH.search(path) or path.lower().endswith(".json"):
        raise RailHtmlConfigurationError("API paths are outside the HTML contract")
    normalized = tuple(item.rstrip(".").lower() for item in allowed_hosts)
    if not any(host == item or host.endswith(f".{item}") for item in normalized):
        raise RailHtmlConfigurationError("public HTML host is not allow-listed")


def _parse_html_page(
    raw_html: bytes,
    *,
    spec: SourceSpec,
    request: SearchRequest,
    operator_name: str,
    parser_version: str,
    observed_at: datetime,
    terminal_names: Mapping[str, str],
    empty_markers: tuple[str, ...],
) -> ConnectorResult:
    if not raw_html or len(raw_html) > MAXIMUM_HTML_BYTES:
        raise ValueError("rail HTML body is invalid")
    decoded = raw_html.decode("utf-8", errors="strict")
    parser = _StructuredHtmlParser()
    parser.feed(decoded)
    page_text = parser.normalized_text
    if any(marker in page_text for marker in _CHALLENGE_MARKERS):
        outcome = (
            SearchOutcome.CAPTCHA_CHALLENGE
            if "captcha" in page_text or "human" in page_text
            else SearchOutcome.BLOCKED
        )
        return ConnectorResult.failure(
            outcome=outcome,
            diagnostic_code=(
                "RAIL_HTML_CAPTCHA"
                if outcome is SearchOutcome.CAPTCHA_CHALLENGE
                else "RAIL_HTML_BLOCKED"
            ),
            parser_version=parser_version,
            observed_at=observed_at,
            retryable=False,
        )
    if any(marker in page_text for marker in _TEASER_MARKERS):
        # A page may contain marketing copy alongside valid exact offers.  The
        # individual offer parser below still requires complete price markup.
        pass

    raw_offers: list[Mapping[str, Any]] = []
    decoded_scripts: list[object] = []
    for script in parser.scripts:
        try:
            value = json.loads(script)
        except json.JSONDecodeError:
            continue
        decoded_scripts.append(value)
        raw_offers.extend(_find_offer_objects(value))
        if len(raw_offers) > MAXIMUM_OFFERS:
            raise ValueError("rail page exposes too many offers")

    response_fingerprint = hashlib.sha256(raw_html).hexdigest()
    offers: list[NormalizedOffer] = []
    invalid = 0
    for raw_offer in raw_offers:
        try:
            offers.append(
                _normalize_offer(
                    raw_offer,
                    spec=spec,
                    request=request,
                    operator_name=operator_name,
                    observed_at=observed_at,
                    terminal_names=terminal_names,
                )
            )
        except (KeyError, TypeError, ValueError, OfferRejected, InvalidOperation):
            invalid += 1
    unique: dict[str, NormalizedOffer] = {}
    for offer in offers:
        if offer.provider_offer_id in unique:
            raise ValueError("rail page contains duplicate provider offer IDs")
        unique[offer.provider_offer_id] = offer
    offers = list(unique.values())
    schema_fingerprint = _schema_fingerprint(decoded_scripts)
    if offers:
        return ConnectorResult(
            outcome=(
                SearchOutcome.PARTIAL_SUCCESS
                if invalid
                else SearchOutcome.SUCCESS_RESULTS
            ),
            retryable=False,
            diagnostic_code=(
                "RAIL_HTML_RESULTS_PARTIAL" if invalid else "RAIL_HTML_RESULTS_OK"
            ),
            expected_count=1,
            completed_count=1,
            offers=tuple(offers),
            parser_version=parser_version,
            observed_at=observed_at,
            response_fingerprint=response_fingerprint,
            schema_fingerprint=schema_fingerprint,
        )
    if raw_offers:
        raise ValueError("rail page offers did not satisfy the search contract")
    if any(marker in page_text for marker in empty_markers):
        return ConnectorResult(
            outcome=SearchOutcome.SUCCESS_EMPTY,
            retryable=False,
            diagnostic_code="RAIL_HTML_RESULTS_EMPTY",
            expected_count=1,
            completed_count=1,
            offers=(),
            parser_version=parser_version,
            observed_at=observed_at,
            response_fingerprint=response_fingerprint,
            schema_fingerprint=schema_fingerprint,
        )
    raise ValueError("rail page has neither offers nor an explicit empty state")


def _find_offer_objects(value: object) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_find_offer_objects(item))
        return found
    if not isinstance(value, dict):
        return found
    types = value.get("@type")
    type_names = {types} if isinstance(types, str) else set(types or ())
    if "Offer" in type_names and "AggregateOffer" not in type_names:
        found.append(value)
        return found
    for item in value.values():
        if isinstance(item, (dict, list)):
            found.extend(_find_offer_objects(item))
    return found


def _normalize_offer(
    raw: Mapping[str, Any],
    *,
    spec: SourceSpec,
    request: SearchRequest,
    operator_name: str,
    observed_at: datetime,
    terminal_names: Mapping[str, str],
) -> NormalizedOffer:
    price_specification = _object(raw.get("priceSpecification"), "price specification")
    price_type = price_specification.get("@type")
    price_types = {price_type} if isinstance(price_type, str) else set(price_type or ())
    if "UnitPriceSpecification" not in price_types:
        raise ValueError("offer does not expose one exact price")
    if price_specification.get("valueAddedTaxIncluded") is not True:
        raise ValueError("offer does not explicitly include mandatory taxes")
    fee_evidence = " ".join(
        value
        for value in (
            raw.get("description"),
            price_specification.get("description"),
        )
        if isinstance(value, str)
    )
    if _FEE_EXCLUDED.search(fee_evidence) or not _FEE_INCLUDED.search(fee_evidence):
        raise ValueError("offer does not explicitly include mandatory fees")
    if any(key in price_specification for key in ("minPrice", "maxPrice")):
        raise ValueError("aggregate or teaser prices are not exact")
    currency = _required_string(
        price_specification.get("priceCurrency") or raw.get("priceCurrency"),
        "currency",
    ).upper()
    if currency != request.currency:
        raise ValueError("offer currency does not match the request")
    if "price" not in price_specification:
        raise ValueError("exact price is missing from the price specification")
    raw_price = price_specification["price"]
    amount_minor = _money_minor(raw_price)
    if "price" in raw and _money_minor(raw["price"]) != amount_minor:
        raise ValueError("offer price conflicts with its exact specification")
    if (
        "priceCurrency" in raw
        and _required_string(raw["priceCurrency"], "offer currency").upper() != currency
    ):
        raise ValueError("offer currency conflicts with its exact specification")
    availability = str(raw.get("availability", ""))
    if not availability or availability.rsplit("/", 1)[-1].lower() not in {
        "instock",
        "limitedavailability",
        "onlineonly",
    }:
        raise ValueError("offer is not available to book")
    customer_type = _required_string(
        raw.get("eligibleCustomerType"), "eligible customer type"
    ).casefold()
    if customer_type not in {"adult", "one adult", "adult passenger"}:
        raise ValueError("offer eligibility is not an adult fare")
    if _eligible_quantity(raw.get("eligibleQuantity")) != 1:
        raise ValueError("offer is not explicitly priced for one passenger")
    if raw.get("requiresPaidMembership") is not False:
        raise ValueError("offer does not explicitly exclude paid entitlements")
    booking_url = sanitize_booking_url(
        _required_string(raw.get("url"), "booking URL"), spec.booking_hosts
    )
    _validate_public_url(booking_url, spec.booking_hosts)
    provider_offer_id = _required_string(
        raw.get("identifier") or raw.get("sku"), "offer identifier"
    )
    fare_name = _required_string(raw.get("name") or "Standard", "fare name")
    item_offered = raw.get("itemOffered")
    trips = item_offered if isinstance(item_offered, list) else [item_offered]
    trips = [trip for trip in trips if trip is not None]
    expected_count = 2 if request.search_kind is SearchKind.ROUND_TRIP else 1
    if len(trips) != expected_count:
        raise ValueError("offer does not contain the requested journey directions")
    outbound = _normalize_trip(
        _object(trips[0], "outbound trip"),
        request=request,
        operator_name=operator_name,
        terminal_names=terminal_names,
        return_direction=(
            request.search_kind is SearchKind.ONE_WAY
            and request.direction is Direction.RETURN
        ),
    )
    return_segments: tuple[Segment, ...] = ()
    if request.search_kind is SearchKind.ROUND_TRIP:
        return_segments = _normalize_trip(
            _object(trips[1], "return trip"),
            request=request,
            operator_name=operator_name,
            terminal_names=terminal_names,
            return_direction=True,
        )
        stay = return_segments[0].departs_at.astimezone(UTC) - outbound[
            -1
        ].arrives_at.astimezone(UTC)
        if stay < timedelta(hours=12):
            raise ValueError("destination stay is shorter than 12 hours")
    component = BookingComponent(
        seller=spec.name,
        source_slug=spec.slug,
        amount_minor=amount_minor,
        currency=currency,
        booking_url=booking_url,
        fare_name=fare_name,
        mandatory_fees_included=True,
        passenger_age=request.passenger.age,
        passenger_student=request.passenger.student,
        passenger_eligibility_confirmed=True,
        bookable=True,
        is_teaser=False,
        student_fare_applied=False,
    )
    return NormalizedOffer(
        provider_offer_id=provider_offer_id,
        source_slug=spec.slug,
        destination_city_id=request.destination_city_id,
        pricing_semantics=(
            PricingSemantics.ROUND_TRIP_ONLY
            if request.search_kind is SearchKind.ROUND_TRIP
            else PricingSemantics.ONE_WAY
        ),
        price_completeness=PriceCompleteness.COMPLETE,
        total_amount_minor=amount_minor,
        currency=currency,
        mandatory_fees_included=True,
        passenger_age=request.passenger.age,
        passenger_student=request.passenger.student,
        passenger_eligibility_confirmed=True,
        bookable=True,
        is_teaser=False,
        observed_at=observed_at,
        outbound_segments=outbound,
        return_segments=return_segments,
        booking_components=(component,),
    )


def _normalize_trip(
    raw: Mapping[str, Any],
    *,
    request: SearchRequest,
    operator_name: str,
    terminal_names: Mapping[str, str],
    return_direction: bool,
) -> tuple[Segment, ...]:
    types = raw.get("@type")
    type_names = {types} if isinstance(types, str) else set(types or ())
    if "TrainTrip" not in type_names:
        raise ValueError("itemOffered is not a train trip")
    if any(key in raw for key in ("hasPart", "itinerary", "subTrip", "subTrips")):
        raise ValueError("connected rail markup needs explicit segment normalization")
    departure = _aware_datetime(raw.get("departureTime"), "departure time")
    arrival = _aware_datetime(raw.get("arrivalTime"), "arrival time")
    expected_origins = (
        request.destination_terminal_ids
        if return_direction
        else request.origin_terminal_ids
    )
    expected_destinations = (
        request.origin_terminal_ids
        if return_direction
        else request.destination_terminal_ids
    )
    if len(expected_origins) != 1 or len(expected_destinations) != 1:
        raise ValueError("rail HTML offer requires one exact terminal pair")
    origin_id, destination_id = expected_origins[0], expected_destinations[0]
    origin_name = terminal_names.get(origin_id)
    destination_name = terminal_names.get(destination_id)
    if not origin_name or not destination_name:
        raise ValueError("rail HTML offer references an unknown terminal")
    departure_station = _object(raw.get("departureStation"), "departure station")
    arrival_station = _object(raw.get("arrivalStation"), "arrival station")
    upstream_origin = _required_string(
        departure_station.get("name"), "departure station name"
    )
    upstream_destination = _required_string(
        arrival_station.get("name"), "arrival station name"
    )
    if not _station_names_compatible(origin_name, upstream_origin):
        raise ValueError("HTML departure station does not match the requested terminal")
    if not _station_names_compatible(destination_name, upstream_destination):
        raise ValueError("HTML arrival station does not match the requested terminal")
    window = (
        request.return_window
        if return_direction and request.return_window is not None
        else request.outbound_window
    )
    if departure < window.starts_at or arrival > window.ends_at:
        raise ValueError("HTML journey falls outside the requested window")
    if arrival.astimezone(UTC) <= departure.astimezone(UTC):
        raise ValueError("HTML journey has invalid times")
    if arrival.astimezone(UTC) - departure.astimezone(UTC) > timedelta(
        minutes=request.maximum_direction_minutes
    ):
        raise ValueError("HTML journey exceeds the duration limit")
    operator = raw.get("provider")
    if isinstance(operator, dict):
        operator = operator.get("name")
    operator_text = (
        _required_string(operator, "operator")
        if operator is not None
        else operator_name
    )
    service_number = raw.get("trainNumber")
    if service_number is not None:
        service_number = _required_string(service_number, "train number")
    return (
        Segment(
            sequence=1,
            mode=TransportMode.TRAIN,
            operator=operator_text,
            service_number=service_number,
            departure_terminal_id=origin_id,
            departure_terminal_name=origin_name,
            arrival_terminal_id=destination_id,
            arrival_terminal_name=destination_name,
            departs_at=departure,
            arrives_at=arrival,
            protected_connection=True,
        ),
    )


def _station_names_compatible(expected: str, actual: str) -> bool:
    def tokens(value: str) -> set[str]:
        folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
        normalized = re.sub(r"[^a-z0-9]+", " ", folded.lower())
        ignored = {
            "airport",
            "gare",
            "station",
            "railway",
            "train",
            "the",
            "de",
            "du",
            "des",
            "la",
            "le",
        }
        return {token for token in normalized.split() if token not in ignored}

    expected_tokens, actual_tokens = tokens(expected), tokens(actual)
    return bool(expected_tokens and expected_tokens == actual_tokens)


def _eligible_quantity(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError("eligible quantity must be numeric")
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise TypeError("eligible quantity must be an integer")


def _money_minor(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise TypeError("price must be numeric")
    amount = Decimal(str(value).replace(",", "."))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("price must be positive")
    minor = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if minor <= 0:
        raise ValueError("price rounds to zero")
    return minor


def _status_result(
    status: int, *, parser_version: str, observed_at: datetime
) -> ConnectorResult | None:
    if 200 <= status < 300:
        return None
    if status in {401, 403}:
        outcome, diagnostic, retryable = (
            SearchOutcome.BLOCKED,
            "RAIL_HTML_ACCESS_DENIED",
            False,
        )
    elif status == 429:
        outcome, diagnostic, retryable = (
            SearchOutcome.RATE_LIMITED,
            "RAIL_HTML_RATE_LIMITED",
            True,
        )
    elif status in {408, 504}:
        outcome, diagnostic, retryable = (
            SearchOutcome.TIMEOUT,
            "RAIL_HTML_UPSTREAM_TIMEOUT",
            True,
        )
    elif status >= 500:
        outcome, diagnostic, retryable = (
            SearchOutcome.UPSTREAM_ERROR,
            "RAIL_HTML_UPSTREAM_ERROR",
            True,
        )
    else:
        outcome, diagnostic, retryable = (
            SearchOutcome.UPSTREAM_ERROR,
            "RAIL_HTML_REQUEST_REJECTED",
            False,
        )
    return ConnectorResult.failure(
        outcome=outcome,
        diagnostic_code=diagnostic,
        parser_version=parser_version,
        observed_at=observed_at,
        retryable=retryable,
    )


def _empty_result(
    outcome: SearchOutcome,
    diagnostic_code: str,
    parser_version: str,
    observed_at: datetime,
) -> ConnectorResult:
    return ConnectorResult(
        outcome=outcome,
        retryable=False,
        diagnostic_code=diagnostic_code,
        expected_count=1,
        completed_count=1,
        offers=(),
        parser_version=parser_version,
        observed_at=observed_at,
    )


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise TypeError(f"{label} must be a non-empty string")
    return value.strip()


def _aware_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include an offset")
    return parsed


def _schema_fingerprint(values: object) -> str:
    def shape(value: object) -> object:
        if isinstance(value, dict):
            return {key: shape(item) for key, item in sorted(value.items())}
        if isinstance(value, list):
            return [shape(value[0])] if value else []
        if value is None:
            return "null"
        return type(value).__name__

    encoded = json.dumps(shape(values), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_terminal_names() -> dict[str, str]:
    data_directory = os.environ.get(DATA_DIRECTORY_ENV, "").strip()
    candidates = []
    if data_directory:
        candidates.append(Path(data_directory) / "terminals.json")
    candidates.append(Path.cwd() / "data" / "terminals.json")
    candidates.extend(
        parent / "data" / "terminals.json"
        for parent in Path(__file__).resolve().parents
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        terminals = payload.get("terminals")
        if not isinstance(terminals, list):
            break
        return {
            item["id"]: item["name"]
            for item in terminals
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("name"), str)
        }
    return {}


def _load_coverage_configuration(
    source_slug: str, environment: Mapping[str, str]
) -> tuple[tuple[tuple[str, str], ...], date | None]:
    key = f"CROWL_{source_slug.upper()}_COVERAGE_JSON"
    raw_payload = environment.get(key, "").strip()
    if not raw_payload:
        raise RailHtmlConfigurationError(
            f"{key} is required before enabling public-page crawling"
        )
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as error:
        raise RailHtmlConfigurationError(
            "rail HTML coverage JSON is invalid"
        ) from error
    if not isinstance(payload, dict) or payload.get("source") != source_slug:
        raise RailHtmlConfigurationError("rail HTML coverage has the wrong source")
    raw_routes = payload.get("routes")
    if not isinstance(raw_routes, list):
        raise RailHtmlConfigurationError("rail HTML coverage routes are invalid")
    routes: list[tuple[str, str]] = []
    for item in raw_routes:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) and value for value in item)
        ):
            raise RailHtmlConfigurationError("rail HTML coverage route is invalid")
        routes.append((item[0], item[1]))
    if len(routes) != len(set(routes)):
        raise RailHtmlConfigurationError("rail HTML coverage routes contain duplicates")
    raw_horizon = payload.get("sellable_through")
    try:
        horizon = date.fromisoformat(raw_horizon) if raw_horizon else None
    except (TypeError, ValueError) as error:
        raise RailHtmlConfigurationError(
            "rail HTML coverage horizon is invalid"
        ) from error
    return tuple(routes), horizon
