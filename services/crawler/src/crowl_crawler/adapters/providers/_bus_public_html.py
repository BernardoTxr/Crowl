"""Public-page extraction support for bus providers.

Only ordinary provider pages are in scope.  The implementation never calls a
private/partner API, logs in, persists browser state, or tries to defeat a
challenge.  A challenge is a typed monitoring result, not something to solve.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, ClassVar, Protocol
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

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
from ..base import SourceAdapter
from ..catalog import SourceSpec

MAX_PAGE_BYTES = 4_000_000
MAX_SEARCH_PAGES = 8
MAX_TRIPS_PER_SEARCH = 250
PAGE_TIMEOUT_MS = 25_000
SEARCH_DEADLINE_SECONDS = 90
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_FEE_INCLUDED = re.compile(
    r"(?:mandatory\s+)?(?:tax(?:es)?|fees?).{0,50}included|"
    r"included.{0,50}(?:mandatory\s+)?(?:tax(?:es)?|fees?)",
    re.IGNORECASE,
)
_FEE_EXCLUDED = re.compile(
    r"(?:tax(?:es)?|fees?).{0,50}(?:not|never)\s+(?:be\s+)?included|"
    r"(?:does|do)\s+not\s+include.{0,50}(?:tax(?:es)?|fees?)|"
    r"(?:tax(?:es)?|fees?).{0,50}(?:excluded|extra|additional|separate)|"
    r"(?:excluded|excluding|extra|additional).{0,50}(?:tax(?:es)?|fees?)|"
    r"(?:tax(?:es)?|fees?).{0,50}(?:added|calculated).{0,30}(?:checkout|later)",
    re.IGNORECASE,
)
_NO_RESULTS = re.compile(
    r"\b(no (?:trips?|journeys?|rides?|connections?|results?) (?:found|available)|"
    r"no services? available|sold out)\b",
    re.IGNORECASE,
)
_CAPTCHA = re.compile(
    r"\b(captcha|recaptcha|hcaptcha|verify (?:that )?you are human|"
    r"security challenge|unusual traffic)\b",
    re.IGNORECASE,
)
_BLOCKED = re.compile(
    r"\b(access denied|request blocked|temporarily blocked|forbidden)\b",
    re.IGNORECASE,
)
_MAINTENANCE = re.compile(
    r"\b(under maintenance|temporarily unavailable|service unavailable|"
    r"realizando mejoras en nuestro sistema)\b",
    re.IGNORECASE,
)
_SCHEMA_FINGERPRINT = hashlib.sha256(
    b"public-html-jsonld-v1:BusTrip:Offer:UnitPriceSpecification"
).hexdigest()
_API_PATH = re.compile(r"(?:^|/)(?:api|graphql|rest)(?:/|$)", re.IGNORECASE)
_NON_VISIBLE_TEXT_TAGS = frozenset({"script", "style", "template", "noscript"})
_HTML_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class PublicPageSchemaError(ValueError):
    pass


class PublicPageConfigurationError(ValueError):
    pass


class PublicPageLoadError(RuntimeError):
    def __init__(self, outcome: SearchOutcome, diagnostic_suffix: str) -> None:
        super().__init__(diagnostic_suffix)
        self.outcome = outcome
        self.diagnostic_suffix = diagnostic_suffix


@dataclass(frozen=True, slots=True)
class RenderedPage:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    html: str


class PublicPageLoader(Protocol):
    async def load(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        consent_labels: tuple[str, ...],
    ) -> RenderedPage: ...

    async def close(self) -> None: ...


class PlaywrightPublicPageLoader:
    """Ephemeral Chromium renderer for ordinary public search pages.

    Playwright is imported lazily so disabled sources remain dependency- and
    network-free.  Only a visible cookie-consent button may be accepted.
    """

    async def load(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        consent_labels: tuple[str, ...],
    ) -> RenderedPage:
        try:
            from playwright.async_api import (  # type: ignore[import-not-found]
                TimeoutError as PlaywrightTimeoutError,
            )
            from playwright.async_api import (
                async_playwright,
            )
        except ImportError as error:
            raise PublicPageLoadError(
                SearchOutcome.CONFIGURATION_ERROR, "PLAYWRIGHT_UNAVAILABLE"
            ) from error

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        locale="en-GB",
                        timezone_id="Europe/Paris",
                        user_agent=(
                            "Mozilla/5.0 (compatible; Crowl/0.1; public fare monitor)"
                        ),
                    )
                    page = await context.new_page()
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=PAGE_TIMEOUT_MS,
                    )
                    for label in consent_labels:
                        button = page.get_by_role(
                            "button",
                            name=re.compile(rf"^{re.escape(label)}$", re.IGNORECASE),
                        ).first
                        try:
                            if await button.is_visible(timeout=300):
                                await button.click(timeout=1_500)
                                break
                        except PlaywrightTimeoutError:
                            continue
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5_000)
                    except PlaywrightTimeoutError:
                        # Some public pages keep analytics sockets open.  The
                        # DOM snapshot below is still deterministic to parse.
                        pass
                    final_url = page.url
                    if not _url_host_allowed(final_url, allowed_hosts):
                        raise PublicPageLoadError(
                            SearchOutcome.BLOCKED, "UNEXPECTED_REDIRECT"
                        )
                    html = await page.content()
                    if len(html.encode("utf-8")) > MAX_PAGE_BYTES:
                        raise PublicPageLoadError(
                            SearchOutcome.UPSTREAM_ERROR, "PAGE_TOO_LARGE"
                        )
                    headers = await response.all_headers() if response else {}
                    return RenderedPage(
                        requested_url=url,
                        final_url=final_url,
                        status=response.status if response else 0,
                        content_type=headers.get("content-type", "text/html"),
                        html=html,
                    )
                finally:
                    await browser.close()
        except PublicPageLoadError:
            raise
        except PlaywrightTimeoutError as error:
            raise PublicPageLoadError(SearchOutcome.TIMEOUT, "PAGE_TIMEOUT") from error
        except Exception as error:
            # Exception text may contain URLs, browser internals, or challenge
            # tokens.  It is deliberately not propagated to diagnostics.
            raise PublicPageLoadError(
                SearchOutcome.UPSTREAM_ERROR, "PAGE_LOAD_FAILED"
            ) from error

    async def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _SearchPage:
    direction: Direction
    url: str


@dataclass(frozen=True, slots=True)
class _ParsedOneWay:
    provider_offer_id: str
    amount_minor: int
    currency: str
    booking_url: str
    fare_name: str
    segments: tuple[Segment, ...]


@dataclass(frozen=True, slots=True)
class RenderedCandidateInspection:
    """Typed evidence found in a provider's rendered result cards.

    Most providers use this only for fail-closed canary diagnostics. A provider
    with an explicit displayed-card policy may also return normalized offers;
    the shared loader, challenge handling, and response fingerprinting still
    remain centralized here.
    """

    candidate_count: int
    eligible_count: int
    diagnostic_suffix: str
    schema_fingerprint: str
    offers: tuple[NormalizedOffer, ...] = ()
    normalization_complete: bool = False

    def __post_init__(self) -> None:
        if self.candidate_count <= 0:
            raise ValueError("candidate count must be positive")
        if not 0 <= self.eligible_count <= self.candidate_count:
            raise ValueError("eligible candidate count is invalid")
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.:-]{0,54}", self.diagnostic_suffix):
            raise ValueError("candidate diagnostic suffix is unsafe")
        if not re.fullmatch(r"[0-9a-f]{64}", self.schema_fingerprint):
            raise ValueError("candidate schema fingerprint is invalid")
        if self.offers and not self.normalization_complete:
            raise ValueError("rendered offers require complete normalization")


class PublicHtmlBusAdapter(SourceAdapter):
    """Base for provider-specific public-page adapters."""

    source_slug: ClassVar[str]
    public_hosts: ClassVar[tuple[str, ...]]
    provider_names: ClassVar[tuple[str, ...]]
    default_search_url_template: ClassVar[str | None] = None
    parser_version: ClassVar[str] = "public-html-v1"
    rendered_cards_authoritative: ClassVar[bool] = False
    consent_labels: ClassVar[tuple[str, ...]] = (
        "Accept all",
        "Accept cookies",
        "Allow all",
    )

    def __init__(
        self,
        spec: SourceSpec,
        *,
        loader: PublicPageLoader | None = None,
        clock: Callable[[], datetime] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if spec.slug != self.source_slug:
            raise ValueError(
                f"provider class {type(self).__name__} handles {self.source_slug}, "
                f"not {spec.slug}"
            )
        self._spec = spec
        self._loader = loader or PlaywrightPublicPageLoader()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._environ = os.environ if environ is None else environ
        self._closed = False
        enabled = spec.catalog_enabled
        self._metadata = AdapterMetadata(
            source_slug=spec.slug,
            display_name=spec.name,
            modes=spec.modes,
            strategy=(
                ExtractionStrategy.PLAYWRIGHT
                if enabled
                else ExtractionStrategy.DISABLED
            ),
            intended_strategy=spec.intended_strategy,
            connector_version=f"{spec.config_version}-public-html-v1",
            parser_version=self.parser_version,
            enabled=enabled,
            disabled_reason=None if enabled else spec.disabled_reason,
            # Keep catalog identity exact; the implementation itself is more
            # restrictive and only accepts ordinary public pages.
            access_basis=spec.access_basis,
        )

    @property
    def _env_prefix(self) -> str:
        return f"CROWL_{self.source_slug.upper()}"

    def metadata(self) -> AdapterMetadata:
        return self._metadata

    async def discover_coverage(self) -> CoverageDiscovery:
        self._ensure_open()
        observed_at = self._clock()
        if not self._metadata.enabled:
            return CoverageDiscovery(
                source_slug=self.source_slug,
                supported_terminal_pairs=(),
                sellable_through=None,
                observed_at=observed_at,
                diagnostic_code="SOURCE_DISABLED",
            )
        raw = self._environ.get(f"{self._env_prefix}_COVERAGE_JSON", "").strip()
        if not raw:
            return CoverageDiscovery(
                source_slug=self.source_slug,
                supported_terminal_pairs=(),
                sellable_through=None,
                observed_at=observed_at,
                diagnostic_code=f"{self.source_slug.upper()}_COVERAGE_UNCONFIGURED",
            )
        try:
            pairs, through = _parse_coverage(json.loads(raw))
        except (json.JSONDecodeError, PublicPageSchemaError):
            return CoverageDiscovery(
                source_slug=self.source_slug,
                supported_terminal_pairs=(),
                sellable_through=None,
                observed_at=observed_at,
                diagnostic_code=f"{self.source_slug.upper()}_COVERAGE_INVALID",
            )
        return CoverageDiscovery(
            source_slug=self.source_slug,
            supported_terminal_pairs=pairs,
            sellable_through=through,
            observed_at=observed_at,
            diagnostic_code=f"{self.source_slug.upper()}_COVERAGE_OK",
        )

    async def canary(
        self, requests: Sequence[SearchRequest]
    ) -> tuple[ConnectorResult, ...]:
        return tuple([await self.search(request) for request in requests])

    async def search(self, request: SearchRequest) -> ConnectorResult:
        self._ensure_open()
        observed_at = self._clock()
        if not self._metadata.enabled:
            return _empty_result(
                SearchOutcome.SKIPPED_DISABLED,
                "SOURCE_DISABLED",
                self.parser_version,
                observed_at,
            )
        try:
            searches, terminal_aliases = self._search_pages(request)
        except PublicPageConfigurationError:
            return ConnectorResult.failure(
                outcome=SearchOutcome.CONFIGURATION_ERROR,
                diagnostic_code=f"{self.source_slug.upper()}_PUBLIC_PAGE_CONFIG_INVALID",
                parser_version=self.parser_version,
                observed_at=observed_at,
                retryable=False,
            )

        pages: list[tuple[Direction, RenderedPage]] = []
        try:
            async with asyncio.timeout(SEARCH_DEADLINE_SECONDS):
                for search in searches:
                    page = await self._loader.load(
                        search.url,
                        allowed_hosts=self.public_hosts,
                        consent_labels=self.consent_labels,
                    )
                    failure = _classify_page_failure(page)
                    if failure is not None:
                        raise failure
                    pages.append((search.direction, page))
        except TimeoutError:
            return ConnectorResult.failure(
                outcome=SearchOutcome.TIMEOUT,
                diagnostic_code=f"{self.source_slug.upper()}_SEARCH_DEADLINE",
                parser_version=self.parser_version,
                observed_at=observed_at,
            )
        except PublicPageLoadError as error:
            return ConnectorResult.failure(
                outcome=error.outcome,
                diagnostic_code=f"{self.source_slug.upper()}_{error.diagnostic_suffix}",
                parser_version=self.parser_version,
                observed_at=observed_at,
                retryable=error.outcome
                in {
                    SearchOutcome.TIMEOUT,
                    SearchOutcome.RATE_LIMITED,
                    SearchOutcome.UPSTREAM_ERROR,
                },
            )

        try:
            outbound, returns, saw_explicit_empty = _parse_pages(
                pages,
                source_slug=self.source_slug,
                provider_names=self.provider_names,
                booking_hosts=self._spec.booking_hosts,
                terminal_aliases=terminal_aliases,
                request=request,
            )
            offers = _normalize_offers(
                outbound,
                returns,
                source_slug=self.source_slug,
                display_name=self._spec.name,
                request=request,
                observed_at=observed_at,
            )
            candidate_inspection = self._inspect_rendered_candidates(
                pages,
                request=request,
                observed_at=observed_at,
            )
            if self.rendered_cards_authoritative:
                if candidate_inspection is None:
                    if offers:
                        raise PublicPageSchemaError(
                            "structured fares are not authoritative for this provider"
                        )
                    offers = ()
                else:
                    offers = candidate_inspection.offers
            elif candidate_inspection is not None and candidate_inspection.offers:
                if offers:
                    raise PublicPageSchemaError(
                        "rendered and structured fares are ambiguous"
                    )
                offers = candidate_inspection.offers
        except (PublicPageSchemaError, ValueError, InvalidOperation):
            return ConnectorResult.failure(
                outcome=SearchOutcome.PARSE_SCHEMA_ERROR,
                diagnostic_code=f"{self.source_slug.upper()}_PUBLIC_MARKUP_INVALID",
                parser_version=self.parser_version,
                observed_at=observed_at,
                retryable=False,
            )

        page_fingerprint = hashlib.sha256(
            "\n".join(page.html for _, page in pages).encode("utf-8")
        ).hexdigest()
        if offers:
            outcome = SearchOutcome.SUCCESS_RESULTS
            suffix = "RESULTS_OK"
        elif (
            candidate_inspection is not None
            and candidate_inspection.normalization_complete
        ):
            outcome = SearchOutcome.SUCCESS_EMPTY
            suffix = "NO_ELIGIBLE_DISPLAYED_FARES"
        elif candidate_inspection is not None:
            # The DOM cards prove that extraction reached journey data, but the
            # provider-specific contract could not safely normalize them.
            return ConnectorResult(
                outcome=SearchOutcome.PARSE_SCHEMA_ERROR,
                retryable=False,
                diagnostic_code=(
                    f"{self.source_slug.upper()}_"
                    f"{candidate_inspection.diagnostic_suffix}"
                ),
                expected_count=len(searches),
                completed_count=len(searches),
                offers=(),
                parser_version=self.parser_version,
                observed_at=observed_at,
                response_fingerprint=page_fingerprint,
                schema_fingerprint=candidate_inspection.schema_fingerprint,
            )
        elif saw_explicit_empty:
            outcome = SearchOutcome.NO_SERVICE
            suffix = "NO_SERVICE"
        else:
            # A visually valid page with no recognized exact fares is schema
            # drift, not an honest zero result and not a teaser price.
            return ConnectorResult.failure(
                outcome=SearchOutcome.PARSE_SCHEMA_ERROR,
                diagnostic_code=f"{self.source_slug.upper()}_NO_EXACT_FARES",
                parser_version=self.parser_version,
                observed_at=observed_at,
                retryable=False,
            )
        return ConnectorResult(
            outcome=outcome,
            retryable=False,
            diagnostic_code=f"{self.source_slug.upper()}_{suffix}",
            expected_count=len(searches),
            completed_count=len(searches),
            offers=offers,
            parser_version=self.parser_version,
            observed_at=observed_at,
            response_fingerprint=page_fingerprint,
            schema_fingerprint=(
                candidate_inspection.schema_fingerprint
                if candidate_inspection is not None
                else _SCHEMA_FINGERPRINT
            ),
        )

    def _inspect_rendered_candidates(
        self,
        pages: Sequence[tuple[Direction, RenderedPage]],
        *,
        request: SearchRequest,
        observed_at: datetime,
    ) -> RenderedCandidateInspection | None:
        """Return provider-specific rendered-card evidence or normalized offers."""

        return None

    def _search_pages(
        self, request: SearchRequest
    ) -> tuple[tuple[_SearchPage, ...], Mapping[str, str]]:
        template = self._environ.get(
            f"{self._env_prefix}_PUBLIC_SEARCH_URL_TEMPLATE",
            self.default_search_url_template or "",
        ).strip()
        raw_aliases = self._environ.get(
            f"{self._env_prefix}_TERMINAL_ALIASES_JSON", ""
        ).strip()
        if not template or not raw_aliases:
            raise PublicPageConfigurationError
        try:
            aliases_value = json.loads(raw_aliases)
        except json.JSONDecodeError as error:
            raise PublicPageConfigurationError from error
        if not isinstance(aliases_value, dict) or not aliases_value:
            raise PublicPageConfigurationError
        aliases: dict[str, str] = {}
        for key, value in aliases_value.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise PublicPageConfigurationError
            aliases[_clean_text(key, "terminal alias", maximum=120)] = _clean_text(
                value, "provider terminal code", maximum=200
            )

        origin = _first_alias(request.origin_terminal_ids, aliases)
        destination = _first_alias(request.destination_terminal_ids, aliases)
        primary_direction = (
            Direction.RETURN
            if request.search_kind is SearchKind.ONE_WAY
            and request.direction is Direction.RETURN
            else Direction.OUTBOUND
        )
        primary_origin = (
            destination if primary_direction is Direction.RETURN else origin
        )
        primary_destination = (
            origin if primary_direction is Direction.RETURN else destination
        )
        searches: list[_SearchPage] = []
        for travel_date in _window_dates(request.outbound_window):
            searches.append(
                _SearchPage(
                    primary_direction,
                    _format_public_url(
                        template,
                        origin=primary_origin,
                        destination=primary_destination,
                        travel_date=travel_date,
                        direction=primary_direction,
                        allowed_hosts=self.public_hosts,
                    ),
                )
            )
        if request.search_kind is SearchKind.ROUND_TRIP:
            assert request.return_window is not None
            for travel_date in _window_dates(request.return_window):
                searches.append(
                    _SearchPage(
                        Direction.RETURN,
                        _format_public_url(
                            template,
                            origin=destination,
                            destination=origin,
                            travel_date=travel_date,
                            direction=Direction.RETURN,
                            allowed_hosts=self.public_hosts,
                        ),
                    )
                )
        if not searches or len(searches) > MAX_SEARCH_PAGES:
            raise PublicPageConfigurationError
        return tuple(searches), aliases

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"{self.source_slug} adapter is closed")

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._loader.close()


class _JsonLdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._collecting = False
        self._chunks: list[str] = []
        self.documents: list[object] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {key.lower(): value for key, value in attrs}
        if (attributes.get("type") or "").lower() == "application/ld+json":
            self._collecting = True
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or not self._collecting:
            return
        self._collecting = False
        raw = "".join(self._chunks).strip()
        if not raw:
            return
        try:
            self.documents.append(json.loads(raw))
        except json.JSONDecodeError as error:
            raise PublicPageSchemaError("invalid JSON-LD") from error


class _VisibleBodyTextCollector(HTMLParser):
    """Collect rendered body text while ignoring scripts and hidden nodes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool, bool]] = []
        self._chunks: list[str] = []
        self._characters = 0

    @property
    def text(self) -> str:
        return " ".join(" ".join(self._chunks).split())

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        normalized_tag = tag.casefold()
        attributes = {name.casefold(): value or "" for name, value in attrs}
        parent_in_body = self._stack[-1][1] if self._stack else False
        parent_hidden = self._stack[-1][2] if self._stack else False
        in_body = parent_in_body or normalized_tag == "body"
        compact_style = re.sub(r"\s+", "", attributes.get("style", "").casefold())
        hidden = (
            parent_hidden
            or normalized_tag in _NON_VISIBLE_TEXT_TAGS
            or "hidden" in attributes
            or attributes.get("aria-hidden", "").casefold() == "true"
            or "display:none" in compact_style
            or "visibility:hidden" in compact_style
        )
        self._stack.append((normalized_tag, in_body, hidden))
        if normalized_tag in _HTML_VOID_ELEMENTS:
            self._stack.pop()

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _HTML_VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        matching_index = next(
            (
                index
                for index in range(len(self._stack) - 1, -1, -1)
                if self._stack[index][0] == normalized_tag
            ),
            None,
        )
        if matching_index is not None:
            del self._stack[matching_index:]

    def handle_data(self, data: str) -> None:
        if (
            not self._stack
            or not self._stack[-1][1]
            or self._stack[-1][2]
            or self._characters >= 1_000_000
        ):
            return
        remaining = 1_000_000 - self._characters
        chunk = data[:remaining]
        self._chunks.append(chunk)
        self._characters += len(chunk)


def _visible_body_text(html: str) -> str:
    collector = _VisibleBodyTextCollector()
    collector.feed(html)
    collector.close()
    return collector.text


def _parse_pages(
    pages: Sequence[tuple[Direction, RenderedPage]],
    *,
    source_slug: str,
    provider_names: tuple[str, ...],
    booking_hosts: tuple[str, ...],
    terminal_aliases: Mapping[str, str],
    request: SearchRequest,
) -> tuple[tuple[_ParsedOneWay, ...], tuple[_ParsedOneWay, ...], bool]:
    outbound: dict[str, _ParsedOneWay] = {}
    returns: dict[str, _ParsedOneWay] = {}
    saw_empty = False
    reverse_aliases = {value: key for key, value in terminal_aliases.items()}
    if len(reverse_aliases) != len(terminal_aliases):
        raise PublicPageSchemaError("terminal aliases must be one-to-one")
    for page_direction, page in pages:
        if _NO_RESULTS.search(page.html):
            saw_empty = True
        collector = _JsonLdCollector()
        collector.feed(page.html)
        trip_nodes: list[Mapping[str, Any]] = []
        for document in collector.documents:
            trip_nodes.extend(_find_bus_trips(document))
        if len(trip_nodes) > MAX_TRIPS_PER_SEARCH:
            raise PublicPageSchemaError("too many trips in public markup")
        for node in trip_nodes:
            # Marketing route pages often publish a BusTrip without an exact
            # date or Offer.  It is intentionally ignored, never normalized.
            if not all(
                key in node
                for key in (
                    "departureTime",
                    "arrivalTime",
                    "departureBusStop",
                    "arrivalBusStop",
                    "offers",
                )
            ):
                continue
            parsed = _parse_bus_trip(
                node,
                provider_names=provider_names,
                booking_hosts=booking_hosts,
                reverse_aliases=reverse_aliases,
            )
            _validate_one_way(parsed, page_direction, request)
            target = outbound if page_direction is Direction.OUTBOUND else returns
            existing = target.get(parsed.provider_offer_id)
            if existing is not None and existing != parsed:
                raise PublicPageSchemaError(
                    "provider offer ID identifies conflicting bus trips"
                )
            target[parsed.provider_offer_id] = parsed
    return tuple(outbound.values()), tuple(returns.values()), saw_empty


def _find_bus_trips(value: object) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(value, dict):
        item_type = value.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if "BusTrip" in types:
            found.append(value)
        for child in value.values():
            found.extend(_find_bus_trips(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_bus_trips(child))
    return found


def _parse_bus_trip(
    node: Mapping[str, Any],
    *,
    provider_names: tuple[str, ...],
    booking_hosts: tuple[str, ...],
    reverse_aliases: Mapping[str, str],
) -> _ParsedOneWay:
    provider = node.get("provider")
    provider_name = provider.get("name") if isinstance(provider, dict) else provider
    normalized_names = {name.casefold() for name in provider_names}
    if (
        not isinstance(provider_name, str)
        or provider_name.strip().casefold() not in normalized_names
    ):
        raise PublicPageSchemaError("unexpected trip provider")
    identifier = _identifier(node.get("identifier"), "trip identifier")
    departure = _bus_stop(node["departureBusStop"], reverse_aliases)
    arrival = _bus_stop(node["arrivalBusStop"], reverse_aliases)
    departs_at = _timestamp(node["departureTime"], "departureTime")
    arrives_at = _timestamp(node["arrivalTime"], "arrivalTime")
    if arrives_at <= departs_at:
        raise PublicPageSchemaError("trip arrival must follow departure")
    offer_value = node["offers"]
    if isinstance(offer_value, list):
        exact_offers = [item for item in offer_value if _has_type(item, "Offer")]
        if len(exact_offers) != 1:
            raise PublicPageSchemaError("trip must expose one exact offer")
        offer = exact_offers[0]
    elif _has_type(offer_value, "Offer"):
        offer = offer_value
    else:
        # AggregateOffer / lowPrice is a teaser, not an exact bookable fare.
        raise PublicPageSchemaError("trip offer is not exact")
    assert isinstance(offer, dict)
    availability = str(offer.get("availability", ""))
    if not availability.endswith("/InStock") and availability != "InStock":
        raise PublicPageSchemaError("trip is not explicitly bookable")
    currency = _clean_text(offer.get("priceCurrency"), "priceCurrency", maximum=3)
    amount_minor = _money_minor(offer.get("price"), currency)
    booking_url = _https_url(offer.get("url"), "offer.url", allowed_hosts=booking_hosts)
    fare_name = _clean_text(offer.get("name", "Standard"), "offer.name", maximum=120)
    customer_type = _clean_text(
        offer.get("eligibleCustomerType"),
        "eligibleCustomerType",
        maximum=80,
    )
    if customer_type.casefold() not in {"adult", "one adult", "adult passenger"}:
        raise PublicPageSchemaError("fare eligibility is not an adult fare")
    if _eligible_quantity(offer.get("eligibleQuantity")) != 1:
        raise PublicPageSchemaError("fare is not explicitly for one passenger")
    if offer.get("requiresPaidMembership") is not False:
        raise PublicPageSchemaError(
            "fare does not explicitly exclude paid entitlements"
        )
    price_spec = offer.get("priceSpecification")
    if not isinstance(price_spec, dict) or not _has_type(
        price_spec, "UnitPriceSpecification"
    ):
        raise PublicPageSchemaError("exact price specification is missing")
    if price_spec.get("valueAddedTaxIncluded") is not True:
        raise PublicPageSchemaError("tax inclusion is not confirmed")
    specification_currency = _clean_text(
        price_spec.get("priceCurrency"),
        "priceSpecification.priceCurrency",
        maximum=3,
    )
    specification_minor = _money_minor(price_spec.get("price"), specification_currency)
    if specification_currency != currency or specification_minor != amount_minor:
        raise PublicPageSchemaError(
            "offer price conflicts with its exact specification"
        )
    description = " ".join(
        str(value)
        for value in (offer.get("description"), price_spec.get("description"))
        if isinstance(value, str)
    )
    if _FEE_EXCLUDED.search(description) or not _FEE_INCLUDED.search(description):
        raise PublicPageSchemaError("mandatory fee inclusion is not confirmed")
    service_number = node.get("busNumber") or node.get("serviceNumber")
    if service_number is not None:
        service_number = _clean_text(service_number, "service number", maximum=40)
    segment = Segment(
        sequence=1,
        mode=TransportMode.BUS,
        operator=provider_name.strip(),
        service_number=service_number,
        departure_terminal_id=departure[0],
        departure_terminal_name=departure[1],
        arrival_terminal_id=arrival[0],
        arrival_terminal_name=arrival[1],
        departs_at=departs_at,
        arrives_at=arrives_at,
        protected_connection=True,
    )
    return _ParsedOneWay(
        provider_offer_id=identifier,
        amount_minor=amount_minor,
        currency=currency,
        booking_url=booking_url,
        fare_name=fare_name,
        segments=(segment,),
    )


def _normalize_offers(
    outbound: tuple[_ParsedOneWay, ...],
    returns: tuple[_ParsedOneWay, ...],
    *,
    source_slug: str,
    display_name: str,
    request: SearchRequest,
    observed_at: datetime,
) -> tuple[NormalizedOffer, ...]:
    if request.search_kind is SearchKind.ROUND_TRIP:
        if not outbound or not returns:
            return ()
        compatible = [
            (out, back)
            for out in outbound
            for back in returns
            if out.currency == back.currency
            and back.segments[0].departs_at - out.segments[-1].arrives_at
            >= timedelta(hours=12)
        ]
        if not compatible:
            return ()
        out, back = min(
            compatible, key=lambda pair: pair[0].amount_minor + pair[1].amount_minor
        )
        components = (
            _component(out, source_slug, display_name, request),
            _component(back, source_slug, display_name, request),
        )
        digest = hashlib.sha256(
            f"{out.provider_offer_id}:{back.provider_offer_id}".encode()
        ).hexdigest()[:24]
        return (
            NormalizedOffer(
                provider_offer_id=f"public-{source_slug}-{digest}",
                source_slug=source_slug,
                destination_city_id=request.destination_city_id,
                pricing_semantics=PricingSemantics.COMPOSABLE,
                price_completeness=PriceCompleteness.COMPLETE,
                total_amount_minor=out.amount_minor + back.amount_minor,
                currency=out.currency,
                mandatory_fees_included=True,
                passenger_age=request.passenger.age,
                passenger_student=request.passenger.student,
                passenger_eligibility_confirmed=True,
                bookable=True,
                is_teaser=False,
                observed_at=observed_at,
                outbound_segments=out.segments,
                return_segments=back.segments,
                booking_components=components,
                included_baggage=request.baggage,
            ),
        )
    candidates = returns if request.direction is Direction.RETURN else outbound
    if not candidates:
        return ()
    best = min(candidates, key=lambda item: item.amount_minor)
    return (
        NormalizedOffer(
            provider_offer_id=best.provider_offer_id,
            source_slug=source_slug,
            destination_city_id=request.destination_city_id,
            pricing_semantics=PricingSemantics.ONE_WAY,
            price_completeness=PriceCompleteness.COMPLETE,
            total_amount_minor=best.amount_minor,
            currency=best.currency,
            mandatory_fees_included=True,
            passenger_age=request.passenger.age,
            passenger_student=request.passenger.student,
            passenger_eligibility_confirmed=True,
            bookable=True,
            is_teaser=False,
            observed_at=observed_at,
            outbound_segments=best.segments,
            booking_components=(_component(best, source_slug, display_name, request),),
            included_baggage=request.baggage,
        ),
    )


def _component(
    fare: _ParsedOneWay,
    source_slug: str,
    display_name: str,
    request: SearchRequest,
) -> BookingComponent:
    return BookingComponent(
        seller=display_name,
        source_slug=source_slug,
        amount_minor=fare.amount_minor,
        currency=fare.currency,
        booking_url=fare.booking_url,
        fare_name=fare.fare_name,
        mandatory_fees_included=True,
        passenger_age=request.passenger.age,
        passenger_student=request.passenger.student,
        passenger_eligibility_confirmed=True,
        bookable=True,
        is_teaser=False,
        student_fare_applied=False,
    )


def _validate_one_way(
    fare: _ParsedOneWay, direction: Direction, request: SearchRequest
) -> None:
    segments = fare.segments
    if direction is Direction.OUTBOUND:
        starts = request.origin_terminal_ids
        ends = request.destination_terminal_ids
        window = request.outbound_window
    else:
        starts = request.destination_terminal_ids
        ends = request.origin_terminal_ids
        window = request.return_window or request.outbound_window
    if segments[0].departure_terminal_id not in starts:
        raise PublicPageSchemaError("unexpected departure terminal")
    if segments[-1].arrival_terminal_id not in ends:
        raise PublicPageSchemaError("unexpected arrival terminal")
    if not window.starts_at <= segments[0].departs_at <= window.ends_at:
        raise PublicPageSchemaError("departure is outside requested window")
    if direction is Direction.RETURN and segments[-1].arrives_at > window.ends_at:
        raise PublicPageSchemaError("return misses the Paris deadline")
    elapsed = segments[-1].arrives_at - segments[0].departs_at
    if elapsed > timedelta(minutes=request.maximum_direction_minutes):
        raise PublicPageSchemaError("trip exceeds maximum duration")
    if fare.currency != request.currency:
        raise PublicPageSchemaError("unexpected currency")


def _classify_page_failure(page: RenderedPage) -> PublicPageLoadError | None:
    if page.status == 429:
        return PublicPageLoadError(SearchOutcome.RATE_LIMITED, "RATE_LIMITED")
    if page.status in (401, 403):
        return PublicPageLoadError(SearchOutcome.BLOCKED, "PAGE_BLOCKED")
    if page.status >= 500 or page.status == 0:
        return PublicPageLoadError(SearchOutcome.UPSTREAM_ERROR, "UPSTREAM_ERROR")
    if not 200 <= page.status < 300:
        return PublicPageLoadError(SearchOutcome.UPSTREAM_ERROR, "HTTP_ERROR")
    if "html" not in page.content_type.lower():
        return PublicPageLoadError(
            SearchOutcome.PARSE_SCHEMA_ERROR, "CONTENT_TYPE_INVALID"
        )
    # Rendered application bundles routinely contain words such as "captcha"
    # even when the visitor was not challenged.  Classify only visible body
    # evidence; scripts, templates, hidden nodes, and styles are excluded.
    visible_text = _visible_body_text(page.html)
    if _CAPTCHA.search(visible_text):
        return PublicPageLoadError(SearchOutcome.CAPTCHA_CHALLENGE, "CAPTCHA_CHALLENGE")
    if _BLOCKED.search(visible_text):
        return PublicPageLoadError(SearchOutcome.BLOCKED, "PAGE_BLOCKED")
    if _MAINTENANCE.search(visible_text):
        return PublicPageLoadError(SearchOutcome.UPSTREAM_ERROR, "PAGE_MAINTENANCE")
    return None


def _parse_coverage(payload: object) -> tuple[tuple[tuple[str, str], ...], date]:
    if not isinstance(payload, dict) or set(payload) != {
        "supported_terminal_pairs",
        "sellable_through",
    }:
        raise PublicPageSchemaError("invalid coverage configuration")
    raw_pairs = payload["supported_terminal_pairs"]
    if not isinstance(raw_pairs, list) or not raw_pairs or len(raw_pairs) > 10_000:
        raise PublicPageSchemaError("invalid coverage pairs")
    pairs: list[tuple[str, str]] = []
    for pair in raw_pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise PublicPageSchemaError("invalid coverage pair")
        pairs.append(
            (
                _clean_text(pair[0], "coverage origin", maximum=120),
                _clean_text(pair[1], "coverage destination", maximum=120),
            )
        )
    if len(pairs) != len(set(pairs)):
        raise PublicPageSchemaError("duplicate coverage pair")
    try:
        through = date.fromisoformat(
            _clean_text(payload["sellable_through"], "sellable through", maximum=10)
        )
    except ValueError as error:
        raise PublicPageSchemaError("invalid coverage horizon") from error
    return tuple(sorted(pairs)), through


def _first_alias(values: tuple[str, ...], aliases: Mapping[str, str]) -> str:
    for value in values:
        if value in aliases:
            return aliases[value]
    raise PublicPageConfigurationError


def _window_dates(window: Any) -> tuple[date, ...]:
    timezone = ZoneInfo(window.timezone)
    first = window.starts_at.astimezone(timezone).date()
    last = window.ends_at.astimezone(timezone).date()
    return tuple(
        first + timedelta(days=offset) for offset in range((last - first).days + 1)
    )


def _format_public_url(
    template: str,
    *,
    origin: str,
    destination: str,
    travel_date: date,
    direction: Direction,
    allowed_hosts: tuple[str, ...],
) -> str:
    try:
        url = template.format(
            origin=quote(origin, safe=""),
            destination=quote(destination, safe=""),
            date=travel_date.isoformat(),
            date_dot=travel_date.strftime("%d.%m.%Y"),
            direction=direction.value,
        )
    except (KeyError, ValueError) as error:
        raise PublicPageConfigurationError from error
    if not _url_host_allowed(url, allowed_hosts):
        raise PublicPageConfigurationError
    return url


def _url_host_allowed(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        return False
    host = parsed.hostname.rstrip(".").lower()
    path = parsed.path.rstrip("/") or "/"
    if (
        host.startswith("api.")
        or ".api." in host
        or _API_PATH.search(path)
        or path.lower().endswith(".json")
    ):
        return False
    return any(
        host == allowed.rstrip(".").lower()
        or host.endswith(f".{allowed.rstrip('.').lower()}")
        for allowed in allowed_hosts
    )


def _bus_stop(value: object, reverse_aliases: Mapping[str, str]) -> tuple[str, str]:
    if not isinstance(value, dict) or not _has_type(value, "BusStop"):
        raise PublicPageSchemaError("trip stop is not a BusStop")
    provider_id = _identifier(value.get("identifier"), "stop identifier")
    try:
        terminal_id = reverse_aliases[provider_id]
    except KeyError as error:
        raise PublicPageSchemaError("unmapped provider stop") from error
    name = _clean_text(value.get("name"), "stop name", maximum=200)
    return terminal_id, name


def _has_type(value: object, expected: str) -> bool:
    if not isinstance(value, dict):
        return False
    item_type = value.get("@type")
    return item_type == expected or (
        isinstance(item_type, list) and expected in item_type
    )


def _identifier(value: object, field: str) -> str:
    if isinstance(value, dict):
        value = value.get("value")
    return _clean_text(value, field, maximum=300)


def _eligible_quantity(value: object) -> int:
    if isinstance(value, bool):
        raise PublicPageSchemaError("eligibleQuantity must be numeric")
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise PublicPageSchemaError("eligibleQuantity must be an integer")


def _money_minor(value: object, currency: str) -> int:
    if currency != "EUR":
        raise PublicPageSchemaError("Crowl public bus pages must be priced in EUR")
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PublicPageSchemaError("offer price is invalid")
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as error:
        raise PublicPageSchemaError("offer price is invalid") from error
    minor = int(amount * 100)
    if minor <= 0:
        raise PublicPageSchemaError("offer price must be positive")
    return minor


def _timestamp(value: object, field: str) -> datetime:
    text = _clean_text(value, field, maximum=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise PublicPageSchemaError(f"{field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublicPageSchemaError(f"{field} lacks timezone")
    return parsed


def _https_url(value: object, field: str, *, allowed_hosts: tuple[str, ...]) -> str:
    url = _clean_text(value, field, maximum=2_000)
    if not _url_host_allowed(url, allowed_hosts):
        raise PublicPageSchemaError(f"{field} host is invalid")
    return url


def _clean_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise PublicPageSchemaError(f"{field} must be text")
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > maximum
        or _CONTROL_CHARACTERS.search(normalized)
    ):
        raise PublicPageSchemaError(f"{field} is invalid")
    return normalized


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
