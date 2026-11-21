"""Fail-closed public-page building blocks for airline connectors.

The airline modules intentionally do not contain hidden service URLs. A
connector becomes operational only when the source catalog enables it and an
ordinary public booking-page URL on an allowlisted provider host has been
configured. This keeps importing the registry and running the default catalog
completely network-free.

Provider modules own their public booking-page query and embedded-data schema.
This module owns the shared safety boundary: finite requests, bounded
responses, typed outcomes, terminal normalization, business-window filtering,
and diagnostics that can never contain a response body, URL, credential, or
exception message.  It does not implement an API or partner-feed client.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
from abc import abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..base import SourceAdapter
from ..catalog import SourceSpec, source_catalog_path
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
    RETRYABLE_SEARCH_OUTCOMES,
    SearchKind,
    SearchOutcome,
    SearchRequest,
    Segment,
    TransportMode,
)
from ._html_text import visible_html_text


DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_SALE_HORIZON_DAYS = 210
MAXIMUM_DATES_PER_DIRECTION = 3
MINIMUM_DESTINATION_STAY = timedelta(hours=12)
SEARCH_DEADLINE_SECONDS = 120
_CAPTCHA_MARKERS = (
    "captcha",
    "verify you are human",
    "verify that you are human",
    "confirm you are human",
)
_BLOCKED_MARKERS = (
    "access denied",
    "request blocked",
    "temporarily blocked",
    "you have been blocked",
    "forbidden",
)
_MAINTENANCE_MARKERS = (
    "under maintenance",
    "temporarily unavailable",
    "service unavailable",
)
_CONSENT_MARKERS = (
    "cookie consent required",
    "consent-required",
    "please accept cookies to continue",
    "privacy choices before continuing",
)
_CONSENT_LABELS = (
    "Accept all",
    "Accept cookies",
    "Allow all",
    "Tout accepter",
    "Accepter tout",
    "Aceptar todo",
    "Accetta tutto",
    "Alle akzeptieren",
    "Zaakceptuj wszystkie",
    "Aceitar tudo",
)


class FlightPayloadError(ValueError):
    """A provider response cannot be normalized without guessing."""


class FlightConfigurationError(ValueError):
    """A live connector setting violates Crowl's fail-closed policy."""


class FlightResponseTooLarge(RuntimeError):
    """The upstream response exceeded the configured byte budget."""


@dataclass(frozen=True, slots=True)
class FlightHttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if self.method != "GET":
            raise FlightConfigurationError(
                "public flight pages must be requested with GET"
            )
        if self.timeout_seconds <= 0:
            raise FlightConfigurationError("flight HTTP timeout must be positive")
        if self.max_response_bytes <= 0:
            raise FlightConfigurationError("flight response budget must be positive")
        if self.body is not None:
            raise FlightConfigurationError(
                "public flight page requests cannot contain a body"
            )


@dataclass(frozen=True, slots=True)
class FlightHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str


class FlightHttpClient(Protocol):
    async def send(self, request: FlightHttpRequest) -> FlightHttpResponse: ...

    async def close(self) -> None: ...


class _NoRedirects(HTTPRedirectHandler):
    """Do not follow redirects before the adapter can validate their host."""

    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ):
        return None


class UrllibFlightHttpClient:
    """Small dependency-free async wrapper around a bounded urllib request."""

    def __init__(self) -> None:
        self._closed = False
        self._opener = build_opener(_NoRedirects())

    async def send(self, request: FlightHttpRequest) -> FlightHttpResponse:
        if self._closed:
            raise RuntimeError("flight HTTP client is closed")
        return await asyncio.to_thread(self._send_sync, request)

    def _send_sync(self, request: FlightHttpRequest) -> FlightHttpResponse:
        outgoing = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            response = self._opener.open(  # noqa: S310 - URL is provider-allowlisted
                outgoing,
                timeout=request.timeout_seconds,
            )
        except HTTPError as error:
            body = _read_bounded(error, request.max_response_bytes)
            return FlightHttpResponse(
                status=error.code,
                headers={key.lower(): value for key, value in error.headers.items()},
                body=body,
                final_url=error.geturl(),
            )
        with response:
            body = _read_bounded(response, request.max_response_bytes)
            return FlightHttpResponse(
                status=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=body,
                final_url=response.geturl(),
            )

    async def close(self) -> None:
        self._closed = True


class PlaywrightFlightHtmlClient:
    """Render one ordinary public page without login or challenge handling.

    Playwright is imported lazily so disabled sources stay dependency- and
    network-free. It may accept a normal visible cookie-consent button. It
    never solves CAPTCHAs, injects cookies, logs in, or intercepts private API
    calls. Provider pages that cannot render without those actions fail closed.
    """

    def __init__(self) -> None:
        self._closed = False
        self._playwright = None
        self._browser = None

    async def send(self, request: FlightHttpRequest) -> FlightHttpResponse:
        if self._closed:
            raise RuntimeError("flight HTML renderer is closed")
        if request.method != "GET" or request.body is not None:
            raise FlightConfigurationError("page rendering only supports GET")
        try:
            from playwright.async_api import (
                Error as PlaywrightError,
                TimeoutError as PlaywrightTimeoutError,
                async_playwright,
            )
        except ImportError as error:
            raise FlightConfigurationError(
                "Playwright is required for this public booking page"
            ) from error
        try:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
            assert self._browser is not None
            context = await self._browser.new_context(
                locale="en-GB",
                timezone_id="Europe/Paris",
                service_workers="block",
                user_agent="Crowl/0.1 public-fare-monitor",
                extra_http_headers={
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() != "user-agent"
                },
            )
            page = await context.new_page()
            try:
                response = await page.goto(
                    request.url,
                    wait_until="domcontentloaded",
                    timeout=int(request.timeout_seconds * 1_000),
                )
                for label in _CONSENT_LABELS:
                    button = page.get_by_role(
                        "button",
                        name=re.compile(rf"^{re.escape(label)}$", re.IGNORECASE),
                    ).first
                    try:
                        if await button.is_visible(timeout=150):
                            await button.click(timeout=1_500)
                            break
                    except PlaywrightTimeoutError:
                        continue
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except PlaywrightTimeoutError:
                    # Analytics and telemetry can keep otherwise complete
                    # public pages busy indefinitely.
                    pass
                body = (await page.content()).encode("utf-8")
                if len(body) > request.max_response_bytes:
                    raise FlightResponseTooLarge
                return FlightHttpResponse(
                    status=response.status if response is not None else 599,
                    headers=(
                        await response.all_headers() if response is not None else {}
                    ),
                    body=body,
                    final_url=page.url,
                )
            finally:
                await context.close()
        except PlaywrightTimeoutError as error:
            raise TimeoutError from error
        except PlaywrightError as error:
            raise URLError("public page rendering failed") from error

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()


def _read_bounded(response: Any, maximum_bytes: int) -> bytes:
    payload = response.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise FlightResponseTooLarge
    return payload


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    terminal_id: str
    name: str
    timezone: str
    iata: str


class FlightTerminalDirectory:
    def __init__(self, terminals: Sequence[TerminalInfo]) -> None:
        self._by_id = {terminal.terminal_id: terminal for terminal in terminals}
        self._by_iata = {terminal.iata: terminal for terminal in terminals}
        if len(self._by_id) != len(terminals) or len(self._by_iata) != len(terminals):
            raise FlightConfigurationError(
                "airport terminal identifiers must be unique"
            )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "FlightTerminalDirectory":
        catalog_path = (
            Path(path)
            if path is not None
            else source_catalog_path().with_name("terminals.json")
        )
        try:
            payload = json.loads(catalog_path.read_text(encoding="utf-8"))
            entries = payload["terminals"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise FlightConfigurationError("terminal catalog is unavailable") from error
        if not isinstance(entries, list):
            raise FlightConfigurationError(
                "terminal catalog terminals must be an array"
            )

        terminals: list[TerminalInfo] = []
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("kind") != "airport":
                continue
            codes = entry.get("codes")
            iata = codes.get("iata") if isinstance(codes, Mapping) else None
            if not isinstance(iata, str) or len(iata) != 3 or not iata.isalpha():
                continue
            try:
                terminal = TerminalInfo(
                    terminal_id=_required_text(entry, "id"),
                    name=_required_text(entry, "name"),
                    timezone=_required_text(entry, "timezone"),
                    iata=iata.upper(),
                )
                ZoneInfo(terminal.timezone)
            except (FlightPayloadError, ZoneInfoNotFoundError) as error:
                raise FlightConfigurationError(
                    "terminal catalog contains an invalid airport"
                ) from error
            terminals.append(terminal)
        if not terminals:
            raise FlightConfigurationError("terminal catalog has no IATA airports")
        return cls(tuple(terminals))

    def by_id(self, terminal_id: str) -> TerminalInfo | None:
        return self._by_id.get(terminal_id)

    def by_iata(self, iata: str) -> TerminalInfo:
        try:
            return self._by_iata[iata.upper()]
        except KeyError as error:
            raise FlightPayloadError("provider returned an unknown airport") from error


@dataclass(frozen=True, slots=True)
class RawFlightSegment:
    departure_iata: str
    arrival_iata: str
    departs_at: str
    arrives_at: str
    service_number: str | None
    operator: str
    protected_connection: bool = True


@dataclass(frozen=True, slots=True)
class RawFlightOffer:
    provider_offer_id: str
    amount_minor: int
    currency: str
    booking_url: str
    fare_name: str
    outbound_segments: tuple[RawFlightSegment, ...]
    passenger_quantity: int
    paid_membership_required: bool
    return_segments: tuple[RawFlightSegment, ...] = ()
    mandatory_fees_included: bool = True
    passenger_eligibility_confirmed: bool = True
    bookable: bool = True
    is_teaser: bool = False
    included_baggage: str = "smallest_included_personal_item"


@dataclass(frozen=True, slots=True)
class ParsedFlightResponse:
    offers: tuple[RawFlightOffer, ...]
    empty_outcome: SearchOutcome = SearchOutcome.SUCCESS_EMPTY
    schema_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.empty_outcome not in {
            SearchOutcome.SUCCESS_EMPTY,
            SearchOutcome.NOT_ON_SALE,
            SearchOutcome.NO_SERVICE,
            SearchOutcome.SOLD_OUT,
        }:
            raise ValueError("flight parser empty outcome is invalid")


@dataclass(frozen=True, slots=True)
class FlightSearchContext:
    request: SearchRequest
    origin: TerminalInfo
    destination: TerminalInfo
    selected_outbound_date: date
    selected_return_date: date | None

    @property
    def query_origin(self) -> TerminalInfo:
        if (
            self.request.search_kind is SearchKind.ONE_WAY
            and self.request.direction is Direction.RETURN
        ):
            return self.destination
        return self.origin

    @property
    def query_destination(self) -> TerminalInfo:
        if (
            self.request.search_kind is SearchKind.ONE_WAY
            and self.request.direction is Direction.RETURN
        ):
            return self.origin
        return self.destination


@dataclass(frozen=True, slots=True)
class _PairResult:
    outcome: SearchOutcome
    diagnostic_code: str
    offers: tuple[NormalizedOffer, ...]
    response_fingerprint: str | None
    schema_fingerprint: str | None


class ConfiguredFlightAdapter(SourceAdapter):
    """Base for a public booking page that is inert until explicitly enabled."""

    source_slug: str
    operator_name: str
    connector_version = "flight-http-v1"
    parser_version = "flight-parser-v1"
    runtime_strategy = ExtractionStrategy.HTTP_STRUCTURED
    public_page_hosts: tuple[str, ...] = ()
    sale_horizon_days = DEFAULT_SALE_HORIZON_DAYS
    search_timeout_seconds = SEARCH_DEADLINE_SECONDS + 5
    maximum_concurrent_searches = 1

    def __init__(
        self,
        spec: SourceSpec,
        *,
        http_client: FlightHttpClient | None = None,
        clock: Callable[[], datetime] | None = None,
        terminal_catalog_path: str | Path | None = None,
        public_page_url: str | None = None,
        supported_terminal_pairs: Sequence[tuple[str, str]] | None = None,
    ) -> None:
        if spec.slug != self.source_slug:
            raise ValueError(
                f"provider class {type(self).__name__} handles {self.source_slug}, "
                f"not {spec.slug}"
            )
        self._spec = spec
        self._clock = clock or (lambda: datetime.now(UTC))
        self._terminal_directory = FlightTerminalDirectory.load(terminal_catalog_path)
        self._public_page_url = (
            public_page_url.strip()
            if public_page_url is not None
            else os.environ.get(self._public_page_url_variable, "").strip()
        )
        self._supported_pairs = (
            tuple(supported_terminal_pairs)
            if supported_terminal_pairs is not None
            else _route_environment(self._route_pairs_variable)
        )
        self._closed = False
        self._owns_http_client = http_client is None
        self._http_client = http_client

        configured = bool(self._public_page_url)
        if self._public_page_url:
            validate_provider_url(self._public_page_url, self._allowed_hosts)
        enabled = spec.catalog_enabled and configured
        if enabled:
            self._metadata = AdapterMetadata(
                source_slug=spec.slug,
                display_name=spec.name,
                modes=spec.modes,
                strategy=self.runtime_strategy,
                intended_strategy=spec.intended_strategy,
                connector_version=self.connector_version,
                parser_version=self.parser_version,
                enabled=True,
                disabled_reason=None,
                access_basis=spec.access_basis,
                sale_horizon_days=self.sale_horizon_days,
            )
        else:
            if not spec.catalog_enabled:
                reason = spec.disabled_reason
            else:
                reason = "The provider public booking page is not configured."
            self._metadata = AdapterMetadata(
                source_slug=spec.slug,
                display_name=spec.name,
                modes=spec.modes,
                strategy=ExtractionStrategy.DISABLED,
                intended_strategy=spec.intended_strategy,
                connector_version=self.connector_version,
                parser_version=self.parser_version,
                enabled=False,
                disabled_reason=reason,
                access_basis=spec.access_basis,
                sale_horizon_days=self.sale_horizon_days,
            )

    @property
    def _public_page_url_variable(self) -> str:
        return f"CROWL_{self.source_slug.upper()}_PUBLIC_PAGE_URL"

    @property
    def _route_pairs_variable(self) -> str:
        return f"CROWL_{self.source_slug.upper()}_PUBLIC_ROUTE_PAIRS"

    @property
    def _allowed_hosts(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys((*self.public_page_hosts, *self._spec.booking_hosts))
        )

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

        valid_pairs: list[tuple[str, str]] = []
        for origin_id, destination_id in self._supported_pairs:
            origin = self._terminal_directory.by_id(origin_id)
            destination = self._terminal_directory.by_id(destination_id)
            if origin is None or destination is None:
                raise FlightConfigurationError(
                    "configured flight coverage contains an unknown airport terminal"
                )
            valid_pairs.append((origin_id, destination_id))
        diagnostic = (
            f"{self.source_slug.upper()}_COVERAGE_OK"
            if valid_pairs
            else f"{self.source_slug.upper()}_COVERAGE_UNCONFIGURED"
        )
        return CoverageDiscovery(
            source_slug=self.source_slug,
            supported_terminal_pairs=tuple(sorted(set(valid_pairs))),
            sellable_through=observed_at.date()
            + timedelta(days=self.sale_horizon_days),
            observed_at=observed_at,
            diagnostic_code=diagnostic,
        )

    async def canary(
        self, requests: Sequence[SearchRequest]
    ) -> tuple[ConnectorResult, ...]:
        return tuple([await self.search(request) for request in requests])

    async def search(self, request: SearchRequest) -> ConnectorResult:
        self._ensure_open()
        observed_at = self._clock()
        if not self._metadata.enabled:
            return ConnectorResult(
                outcome=SearchOutcome.SKIPPED_DISABLED,
                retryable=False,
                diagnostic_code="SOURCE_DISABLED",
                expected_count=1,
                completed_count=1,
                offers=(),
                parser_version=self.parser_version,
                observed_at=observed_at,
            )

        try:
            contexts = self._search_contexts(request)
        except FlightConfigurationError:
            return ConnectorResult(
                outcome=SearchOutcome.CONFIGURATION_ERROR,
                retryable=False,
                diagnostic_code=f"{self.source_slug.upper()}_CONFIGURATION_ERROR",
                expected_count=1,
                completed_count=1,
                offers=(),
                parser_version=self.parser_version,
                observed_at=observed_at,
            )
        if not contexts:
            return ConnectorResult(
                outcome=SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE,
                retryable=False,
                diagnostic_code=f"{self.source_slug.upper()}_ROUTE_UNSUPPORTED",
                expected_count=1,
                completed_count=1,
                offers=(),
                parser_version=self.parser_version,
                observed_at=observed_at,
            )

        client = self._http_client
        if client is None:
            client = (
                PlaywrightFlightHtmlClient()
                if self.runtime_strategy is ExtractionStrategy.PLAYWRIGHT
                else UrllibFlightHttpClient()
            )
            self._http_client = client

        results: list[_PairResult] = []
        try:
            async with asyncio.timeout(SEARCH_DEADLINE_SECONDS):
                for context in contexts:
                    pair_result = await self._search_pair(client, context, observed_at)
                    results.append(pair_result)
                    if pair_result.outcome in {
                        SearchOutcome.BLOCKED,
                        SearchOutcome.CAPTCHA_CHALLENGE,
                        SearchOutcome.RATE_LIMITED,
                    }:
                        return ConnectorResult(
                            outcome=pair_result.outcome,
                            retryable=(
                                pair_result.outcome in RETRYABLE_SEARCH_OUTCOMES
                            ),
                            diagnostic_code=pair_result.diagnostic_code,
                            expected_count=len(contexts),
                            completed_count=len(results),
                            offers=(),
                            parser_version=self.parser_version,
                            observed_at=observed_at,
                            response_fingerprint=pair_result.response_fingerprint,
                            schema_fingerprint=pair_result.schema_fingerprint,
                        )
        except TimeoutError:
            return ConnectorResult(
                outcome=SearchOutcome.TIMEOUT,
                retryable=True,
                diagnostic_code=f"{self.source_slug.upper()}_SEARCH_DEADLINE",
                expected_count=len(contexts),
                completed_count=len(results),
                offers=(),
                parser_version=self.parser_version,
                observed_at=observed_at,
            )
        return self._combine_pair_results(results, observed_at)

    async def _search_pair(
        self,
        client: FlightHttpClient,
        context: FlightSearchContext,
        observed_at: datetime,
    ) -> _PairResult:
        try:
            assert self._public_page_url
            outgoing = self.build_request(self._public_page_url, context)
            validate_provider_url(outgoing.url, self._allowed_hosts)
            response = await client.send(outgoing)
            validate_provider_url(response.final_url, self._allowed_hosts)
        except (asyncio.TimeoutError, TimeoutError, socket.timeout):
            return self._pair_failure(SearchOutcome.TIMEOUT, "TIMEOUT")
        except FlightResponseTooLarge:
            return self._pair_failure(
                SearchOutcome.PARSE_SCHEMA_ERROR,
                "RESPONSE_TOO_LARGE",
            )
        except (URLError, OSError):
            return self._pair_failure(SearchOutcome.UPSTREAM_ERROR, "NETWORK_ERROR")
        except FlightConfigurationError:
            return self._pair_failure(
                SearchOutcome.CONFIGURATION_ERROR,
                "CONFIGURATION_ERROR",
            )

        response_fingerprint = hashlib.sha256(response.body).hexdigest()
        if response.status in {401, 403}:
            return self._pair_failure(SearchOutcome.BLOCKED, "ACCESS_BLOCKED")
        if response.status == 429:
            return self._pair_failure(SearchOutcome.RATE_LIMITED, "RATE_LIMITED")
        if response.status in {408, 504}:
            return self._pair_failure(SearchOutcome.TIMEOUT, "UPSTREAM_TIMEOUT")
        content_type = response.headers.get("content-type", "").lower()
        is_html = not content_type or (
            content_type.startswith("text/html")
            or content_type.startswith("application/xhtml+xml")
        )
        # A challenge can be returned with a 200 or a non-success status such
        # as 405. Page bundles and hidden templates are not challenge evidence.
        visible_text = (
            visible_html_text(
                response.body.decode("utf-8", errors="replace"), body_only=False
            ).casefold()
            if is_html
            else ""
        )
        if any(marker in visible_text for marker in _CAPTCHA_MARKERS):
            return self._pair_failure(
                SearchOutcome.CAPTCHA_CHALLENGE,
                "CAPTCHA_CHALLENGE",
            )
        if response.status < 200 or response.status >= 300:
            return self._pair_failure(SearchOutcome.UPSTREAM_ERROR, "HTTP_ERROR")
        if not is_html:
            return self._pair_failure(
                SearchOutcome.PARSE_SCHEMA_ERROR,
                "NON_HTML_RESPONSE",
            )
        if any(marker in visible_text for marker in _BLOCKED_MARKERS):
            return self._pair_failure(SearchOutcome.BLOCKED, "ACCESS_BLOCKED")
        if any(marker in visible_text for marker in _MAINTENANCE_MARKERS):
            return self._pair_failure(
                SearchOutcome.UPSTREAM_ERROR,
                "MAINTENANCE",
            )
        if not response.body.strip():
            return self._pair_failure(
                SearchOutcome.PARSE_SCHEMA_ERROR,
                "EMPTY_RESPONSE",
            )

        try:
            parsed = self.parse_response(response.body, context)
            normalized = tuple(
                self._normalize_offer(raw, context, observed_at)
                for raw in parsed.offers
            )
        except (
            FlightPayloadError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ):
            if any(marker in visible_text for marker in _CONSENT_MARKERS):
                return self._pair_failure(
                    SearchOutcome.BLOCKED,
                    "CONSENT_REQUIRED",
                )
            return self._pair_failure(
                SearchOutcome.PARSE_SCHEMA_ERROR,
                "SCHEMA_MISMATCH",
            )
        schema_fingerprint = parsed.schema_fingerprint or _schema_fingerprint(response.body)
        if not normalized:
            return _PairResult(
                outcome=parsed.empty_outcome,
                diagnostic_code=(
                    f"{self.source_slug.upper()}_{parsed.empty_outcome.value.upper()}"
                ),
                offers=(),
                response_fingerprint=response_fingerprint,
                schema_fingerprint=schema_fingerprint,
            )
        eligible = tuple(
            offer
            for offer in normalized
            if _offer_matches_request(offer, context.request)
        )
        if not eligible:
            return _PairResult(
                outcome=SearchOutcome.SUCCESS_EMPTY,
                diagnostic_code=f"{self.source_slug.upper()}_RESULTS_FILTERED",
                offers=(),
                response_fingerprint=response_fingerprint,
                schema_fingerprint=schema_fingerprint,
            )
        return _PairResult(
            outcome=SearchOutcome.SUCCESS_RESULTS,
            diagnostic_code=f"{self.source_slug.upper()}_RESULTS_OK",
            offers=eligible,
            response_fingerprint=response_fingerprint,
            schema_fingerprint=schema_fingerprint,
        )

    def _pair_failure(self, outcome: SearchOutcome, suffix: str) -> _PairResult:
        return _PairResult(
            outcome=outcome,
            diagnostic_code=f"{self.source_slug.upper()}_{suffix}",
            offers=(),
            response_fingerprint=None,
            schema_fingerprint=None,
        )

    def _combine_pair_results(
        self,
        results: Sequence[_PairResult],
        observed_at: datetime,
    ) -> ConnectorResult:
        deduplicated: dict[
            tuple[
                str,
                tuple[tuple[str, str, datetime, datetime], ...],
                tuple[tuple[str, str, datetime, datetime], ...],
            ],
            NormalizedOffer,
        ] = {}
        for result in results:
            for offer in result.offers:
                key = (
                    offer.provider_offer_id,
                    tuple(
                        (
                            segment.departure_terminal_id,
                            segment.arrival_terminal_id,
                            segment.departs_at,
                            segment.arrives_at,
                        )
                        for segment in offer.outbound_segments
                    ),
                    tuple(
                        (
                            segment.departure_terminal_id,
                            segment.arrival_terminal_id,
                            segment.departs_at,
                            segment.arrives_at,
                        )
                        for segment in offer.return_segments
                    ),
                )
                existing = deduplicated.get(key)
                if (
                    existing is None
                    or offer.total_amount_minor < existing.total_amount_minor
                ):
                    deduplicated[key] = offer
        offers = tuple(
            sorted(
                deduplicated.values(),
                key=lambda offer: (
                    offer.total_amount_minor,
                    offer.outbound_segments[0].departs_at,
                    offer.provider_offer_id,
                ),
            )
        )
        response_fingerprint = _combine_fingerprints(
            result.response_fingerprint for result in results
        )
        schema_fingerprint = _combine_fingerprints(
            result.schema_fingerprint for result in results
        )
        if offers:
            complete = all(
                result.outcome
                in {
                    SearchOutcome.SUCCESS_RESULTS,
                    SearchOutcome.SUCCESS_EMPTY,
                    SearchOutcome.NOT_ON_SALE,
                    SearchOutcome.NO_SERVICE,
                    SearchOutcome.SOLD_OUT,
                }
                for result in results
            )
            outcome = (
                SearchOutcome.SUCCESS_RESULTS
                if complete
                else SearchOutcome.PARTIAL_SUCCESS
            )
            diagnostic = (
                f"{self.source_slug.upper()}_RESULTS_OK"
                if complete
                else f"{self.source_slug.upper()}_PARTIAL_RESULTS"
            )
        else:
            failures = [
                result
                for result in results
                if result.outcome
                not in {
                    SearchOutcome.SUCCESS_EMPTY,
                    SearchOutcome.NOT_ON_SALE,
                    SearchOutcome.NO_SERVICE,
                    SearchOutcome.SOLD_OUT,
                }
            ]
            selected = failures[0] if failures else results[0]
            outcome = selected.outcome
            diagnostic = selected.diagnostic_code
        return ConnectorResult(
            outcome=outcome,
            retryable=outcome in RETRYABLE_SEARCH_OUTCOMES,
            diagnostic_code=diagnostic,
            expected_count=len(results),
            completed_count=len(results),
            offers=offers,
            parser_version=self.parser_version,
            observed_at=observed_at,
            response_fingerprint=response_fingerprint,
            schema_fingerprint=schema_fingerprint,
        )

    def _search_contexts(
        self, request: SearchRequest
    ) -> tuple[FlightSearchContext, ...]:
        configured_pairs = frozenset(self._supported_pairs)
        contexts: list[FlightSearchContext] = []
        for origin_id in request.origin_terminal_ids:
            origin = self._terminal_directory.by_id(origin_id)
            if origin is None:
                continue
            for destination_id in request.destination_terminal_ids:
                if (origin_id, destination_id) not in configured_pairs:
                    continue
                destination = self._terminal_directory.by_id(destination_id)
                if destination is None:
                    continue
                provisional = FlightSearchContext(
                    request=request,
                    origin=origin,
                    destination=destination,
                    selected_outbound_date=request.weekend_start,
                    selected_return_date=None,
                )
                outbound_dates = _window_dates(
                    request.outbound_window.starts_at,
                    request.outbound_window.ends_at,
                    provisional.query_origin,
                )
                if request.search_kind is SearchKind.ONE_WAY:
                    contexts.extend(
                        FlightSearchContext(
                            request=request,
                            origin=origin,
                            destination=destination,
                            selected_outbound_date=outbound_date,
                            selected_return_date=None,
                        )
                        for outbound_date in outbound_dates
                    )
                    continue

                assert request.return_window is not None
                return_dates = _window_dates(
                    request.return_window.starts_at,
                    request.return_window.ends_at,
                    provisional.query_destination,
                )
                contexts.extend(
                    FlightSearchContext(
                        request=request,
                        origin=origin,
                        destination=destination,
                        selected_outbound_date=outbound_date,
                        selected_return_date=return_date,
                    )
                    for outbound_date in outbound_dates
                    for return_date in return_dates
                    if return_date >= outbound_date
                )
        return tuple(contexts)

    def _normalize_offer(
        self,
        raw: RawFlightOffer,
        context: FlightSearchContext,
        observed_at: datetime,
    ) -> NormalizedOffer:
        if raw.amount_minor <= 0:
            raise FlightPayloadError("flight price must be positive")
        if raw.currency != context.request.currency:
            raise FlightPayloadError("flight price currency does not match request")
        if raw.passenger_quantity != context.request.passenger.adults:
            raise FlightPayloadError("flight fare is not for exactly one passenger")
        if raw.paid_membership_required:
            raise FlightPayloadError("flight fare requires an unconfigured entitlement")
        validate_provider_url(raw.booking_url, self._spec.booking_hosts)
        outbound = self._normalize_segments(raw.outbound_segments)
        inbound = (
            self._normalize_segments(raw.return_segments) if raw.return_segments else ()
        )
        if context.request.search_kind is SearchKind.ROUND_TRIP and not inbound:
            raise FlightPayloadError("round-trip response has no return itinerary")
        if context.request.search_kind is SearchKind.ONE_WAY and inbound:
            raise FlightPayloadError(
                "one-way response unexpectedly has a return itinerary"
            )

        component = BookingComponent(
            seller=self._spec.name,
            source_slug=self.source_slug,
            amount_minor=raw.amount_minor,
            currency=raw.currency,
            booking_url=raw.booking_url,
            fare_name=raw.fare_name,
            mandatory_fees_included=raw.mandatory_fees_included,
            passenger_age=context.request.passenger.age,
            passenger_student=context.request.passenger.student,
            passenger_eligibility_confirmed=raw.passenger_eligibility_confirmed,
            bookable=raw.bookable,
            is_teaser=raw.is_teaser,
            student_fare_applied=False,
        )
        return NormalizedOffer(
            provider_offer_id=raw.provider_offer_id,
            source_slug=self.source_slug,
            destination_city_id=context.request.destination_city_id,
            pricing_semantics=(
                PricingSemantics.ROUND_TRIP_ONLY
                if inbound
                else PricingSemantics.ONE_WAY
            ),
            price_completeness=(
                PriceCompleteness.COMPLETE
                if raw.mandatory_fees_included
                else PriceCompleteness.UNKNOWN
            ),
            total_amount_minor=raw.amount_minor,
            currency=raw.currency,
            mandatory_fees_included=raw.mandatory_fees_included,
            passenger_age=context.request.passenger.age,
            passenger_student=context.request.passenger.student,
            passenger_eligibility_confirmed=raw.passenger_eligibility_confirmed,
            bookable=raw.bookable,
            is_teaser=raw.is_teaser,
            observed_at=observed_at,
            outbound_segments=outbound,
            return_segments=inbound,
            booking_components=(component,),
            included_baggage=raw.included_baggage,
        )

    def _normalize_segments(
        self, raw_segments: Sequence[RawFlightSegment]
    ) -> tuple[Segment, ...]:
        segments: list[Segment] = []
        for index, raw in enumerate(raw_segments, start=1):
            departure = self._terminal_directory.by_iata(raw.departure_iata)
            arrival = self._terminal_directory.by_iata(raw.arrival_iata)
            segment = Segment(
                sequence=index,
                mode=TransportMode.FLIGHT,
                operator=raw.operator,
                service_number=raw.service_number,
                departure_terminal_id=departure.terminal_id,
                departure_terminal_name=departure.name,
                arrival_terminal_id=arrival.terminal_id,
                arrival_terminal_name=arrival.name,
                departs_at=parse_provider_datetime(raw.departs_at, departure.timezone),
                arrives_at=parse_provider_datetime(raw.arrives_at, arrival.timezone),
                protected_connection=raw.protected_connection,
            )
            if segments and (
                segments[-1].arrival_terminal_id != segment.departure_terminal_id
                or segments[-1].arrives_at > segment.departs_at
            ):
                raise FlightPayloadError("flight segments are not contiguous")
            segments.append(segment)
        if not segments:
            raise FlightPayloadError("flight itinerary has no segments")
        return tuple(segments)

    @abstractmethod
    def build_request(
        self, public_page_url: str, context: FlightSearchContext
    ) -> FlightHttpRequest:
        raise NotImplementedError

    @abstractmethod
    def parse_response(
        self, body: bytes, context: FlightSearchContext
    ) -> ParsedFlightResponse:
        raise NotImplementedError

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"{self.source_slug} adapter is closed")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.close()


class _EmbeddedJsonCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._capturing = False
        self._chunks: list[str] = []
        self.documents: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {key.lower(): (value or "") for key, value in attrs}
        script_type = attributes.get("type", "").lower()
        script_id = attributes.get("id", "").lower()
        if script_type in {"application/json", "application/ld+json"} or script_id in {
            "__next_data__",
            "__nuxt_data__",
        }:
            self._capturing = True
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self.documents.append("".join(self._chunks))
            self._capturing = False
            self._chunks = []


def embedded_json_documents(body: bytes) -> tuple[Mapping[str, Any], ...]:
    """Parse literal JSON script elements without evaluating page JavaScript."""

    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FlightPayloadError("provider page is not UTF-8 HTML") from error
    parser = _EmbeddedJsonCollector()
    parser.feed(html)
    documents: list[Mapping[str, Any]] = []
    for source in parser.documents:
        try:
            payload = json.loads(source)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            documents.append(payload)
        elif isinstance(payload, list):
            documents.append({"items": payload})
    if not documents:
        raise FlightPayloadError("provider page has no literal embedded JSON")
    return tuple(documents)


def mapping_with_keys(
    documents: Sequence[Mapping[str, Any]], required_keys: Sequence[str]
) -> Mapping[str, Any]:
    """Find a provider-specific state object inside common SSR wrappers."""

    wanted = frozenset(required_keys)
    for document in documents:
        for candidate in walk_mappings(document):
            if wanted.issubset(candidate):
                return candidate
    raise FlightPayloadError("provider embedded state does not match its schema")


def walk_mappings(value: object):  # type: ignore[no-untyped-def]
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from walk_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_mappings(nested)


def parse_provider_datetime(value: str, timezone_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FlightPayloadError("provider datetime is missing")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise FlightPayloadError("provider datetime is invalid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed


def decimal_minor_units(value: object) -> int:
    if isinstance(value, bool):
        raise FlightPayloadError("provider price is invalid")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise FlightPayloadError("provider price is invalid") from error
    if not decimal.is_finite() or decimal <= 0:
        raise FlightPayloadError("provider price must be positive")
    return int((decimal * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def required_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FlightPayloadError(f"{label} must be an object")
    return value


def required_sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise FlightPayloadError(f"{label} must be an array")
    return value


def required_text(mapping: Mapping[str, Any], key: str) -> str:
    return _required_text(mapping, key)


def required_bool(mapping: Mapping[str, Any], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise FlightPayloadError(f"{key} must be a boolean")
    return value


def required_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FlightPayloadError(f"{key} must be an integer")
    return value


def optional_text(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise FlightPayloadError(f"{key} must be text")
    return value.strip()


def query_request(
    public_page_url: str,
    parameters: Mapping[str, object],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> FlightHttpRequest:
    return FlightHttpRequest(
        method="GET",
        url=url_with_query(public_page_url, parameters),
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Crowl/0.1 (+private fare monitor)",
        },
        timeout_seconds=timeout_seconds,
    )


def url_with_query(base_url: str, parameters: Mapping[str, object]) -> str:
    parsed = urlsplit(base_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(
        (key, str(value)) for key, value in parameters.items() if value is not None
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def local_date(value: datetime, terminal: TerminalInfo) -> date:
    return value.astimezone(ZoneInfo(terminal.timezone)).date()


def _window_dates(
    starts_at: datetime, ends_at: datetime, terminal: TerminalInfo
) -> tuple[date, ...]:
    first = local_date(starts_at, terminal)
    last = local_date(ends_at, terminal)
    count = (last - first).days + 1
    if count < 1 or count > MAXIMUM_DATES_PER_DIRECTION:
        raise FlightConfigurationError(
            "flight public-page search window must cover one to three dates"
        )
    return tuple(first + timedelta(days=offset) for offset in range(count))


def validate_provider_url(url: str, allowed_hosts: Sequence[str]) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise FlightConfigurationError("provider URL is invalid") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise FlightConfigurationError("provider URL must be a plain HTTPS URL")
    hostname = parsed.hostname.rstrip(".").lower()
    allowed = tuple(host.rstrip(".").lower() for host in allowed_hosts)
    if not any(hostname == host or hostname.endswith(f".{host}") for host in allowed):
        raise FlightConfigurationError("provider URL host is not allowlisted")
    host_labels = frozenset(hostname.split("."))
    path_labels = frozenset(item.lower() for item in parsed.path.split("/") if item)
    if (
        host_labels & {"api", "graphql", "rest"}
        or path_labels & {"api", "graphql", "rest"}
        or parsed.path.lower().endswith(".json")
    ):
        raise FlightConfigurationError("provider URL must be a public HTML page")
    forbidden_query_names = {
        "apikey",
        "api_key",
        "access_token",
        "auth",
        "authorization",
        "password",
        "sessiontoken",
    }
    if {
        key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    } & forbidden_query_names:
        raise FlightConfigurationError("provider URL cannot contain credentials")


def _required_text(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FlightPayloadError(f"{key} must be non-empty text")
    return value.strip()


def _route_environment(variable: str) -> tuple[tuple[str, str], ...]:
    raw = os.environ.get(variable, "").strip()
    if not raw:
        return ()
    pairs: list[tuple[str, str]] = []
    for item in raw.split(","):
        values = tuple(part.strip() for part in item.split(":"))
        if len(values) != 2 or not all(values):
            raise FlightConfigurationError(
                f"{variable} must contain origin:destination pairs"
            )
        pairs.append((values[0], values[1]))
    return tuple(pairs)


def _offer_matches_request(offer: NormalizedOffer, request: SearchRequest) -> bool:
    if (
        not offer.mandatory_fees_included
        or not offer.passenger_eligibility_confirmed
        or not offer.bookable
        or offer.is_teaser
    ):
        return False
    outbound = offer.outbound_segments
    return_direction = (
        request.search_kind is SearchKind.ONE_WAY
        and request.direction is Direction.RETURN
    )
    allowed_departures = (
        request.destination_terminal_ids
        if return_direction
        else request.origin_terminal_ids
    )
    allowed_arrivals = (
        request.origin_terminal_ids
        if return_direction
        else request.destination_terminal_ids
    )
    if (
        outbound[0].departure_terminal_id not in allowed_departures
        or outbound[-1].arrival_terminal_id not in allowed_arrivals
    ):
        return False
    outbound_elapsed = outbound[-1].arrives_at.astimezone(UTC) - outbound[
        0
    ].departs_at.astimezone(UTC)
    if outbound_elapsed > timedelta(minutes=request.maximum_direction_minutes):
        return False
    if (
        outbound[0].departs_at < request.outbound_window.starts_at
        or outbound[-1].arrives_at > request.outbound_window.ends_at
    ):
        return False
    if request.search_kind is SearchKind.ROUND_TRIP:
        if request.return_window is None or not offer.return_segments:
            return False
        inbound = offer.return_segments
        if (
            inbound[0].departure_terminal_id not in request.destination_terminal_ids
            or inbound[-1].arrival_terminal_id not in request.origin_terminal_ids
        ):
            return False
        inbound_elapsed = inbound[-1].arrives_at.astimezone(UTC) - inbound[
            0
        ].departs_at.astimezone(UTC)
        if inbound_elapsed > timedelta(minutes=request.maximum_direction_minutes):
            return False
        if (
            inbound[0].departs_at < request.return_window.starts_at
            or inbound[-1].arrives_at > request.return_window.ends_at
        ):
            return False
        destination_stay = inbound[0].departs_at.astimezone(UTC) - outbound[
            -1
        ].arrives_at.astimezone(UTC)
        if destination_stay < MINIMUM_DESTINATION_STAY:
            return False
    return True


def _schema_fingerprint(body: bytes) -> str | None:
    try:
        payload: object = embedded_json_documents(body)
    except FlightPayloadError:
        return None

    def shape(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: shape(value[key]) for key in sorted(value)}
        if isinstance(value, (list, tuple)):
            return [shape(value[0])] if value else []
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        return "string"

    encoded = json.dumps(shape(payload), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _combine_fingerprints(values: Sequence[str | None] | Any) -> str | None:
    present = sorted(value for value in values if value)
    if not present:
        return None
    return hashlib.sha256(":".join(present).encode()).hexdigest()
