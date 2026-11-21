"""FlixBus ordinary public rendered search-card extractor.

Crowl accepts only direct journeys whose visible cards identify exact stops,
times, and a one-adult EUR amount. The amount is deliberately marked estimated
because a later FlixBus service fee may be added. Connected cards are parsed
for schema monitoring but are never expanded into invented legs or offers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from ...models import (
    BookingComponent,
    ConnectorResult,
    Direction,
    NormalizedOffer,
    PriceCompleteness,
    PricingSemantics,
    SearchKind,
    SearchRequest,
    Segment,
    TransportMode,
)
from ..catalog import SourceSpec
from ._bus_public_html import (
    MAX_TRIPS_PER_SEARCH,
    PublicHtmlBusAdapter,
    PublicPageLoader,
    PublicPageConfigurationError,
    PublicPageSchemaError,
    RenderedCandidateInspection,
    RenderedPage,
)

_CARD_PREFIX = "search-result-item "
_CARD_ID = re.compile(
    r"^search-result-item-(?P<kind>direct|ic):(?P<identifier>[A-Za-z0-9:/-]{1,700})$"
)
_PRICE = re.compile(r"^\s*€\s*(?P<major>[0-9]{1,6})[.,](?P<minor>[0-9]{2})\s*$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_DISPLAYED_FARE_NAME = "Displayed fare"
_SCHEMA_FINGERPRINT = hashlib.sha256(
    b"flixbus-rendered-card-v1:data-e2e:time-datetime:station:priceButton:reserve"
).hexdigest()
_VOID_ELEMENTS = frozenset(
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


class _PacedPublicPageLoader:
    """Serialize public-page starts and keep them at or below 12/minute."""

    def __init__(
        self,
        delegate: PublicPageLoader,
        *,
        minimum_interval_seconds: float,
    ) -> None:
        if not 0 <= minimum_interval_seconds <= 60:
            raise ValueError("FlixBus request interval is out of range")
        self._delegate = delegate
        self._minimum_interval_seconds = minimum_interval_seconds
        self._lock = asyncio.Lock()
        self._last_started_at: float | None = None

    async def load(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        consent_labels: tuple[str, ...],
    ) -> RenderedPage:
        async with self._lock:
            now = time.monotonic()
            if self._last_started_at is not None:
                wait_seconds = (
                    self._minimum_interval_seconds
                    - (now - self._last_started_at)
                )
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
            self._last_started_at = time.monotonic()
            return await self._delegate.load(
                url,
                allowed_hosts=allowed_hosts,
                consent_labels=consent_labels,
            )

    async def close(self) -> None:
        await self._delegate.close()


@dataclass(frozen=True, slots=True)
class FlixBusRenderedCandidate:
    """Structurally complete visible journey-card data."""

    provider_candidate_id: str
    connection_kind: str
    departs_at: datetime
    arrives_at: datetime
    departure_station_name: str
    arrival_station_name: str
    amount_minor: int
    currency: str
    reserve_button_count: int


@dataclass(slots=True)
class _CandidateBuilder:
    provider_candidate_id: str
    connection_kind: str
    departure_markers: int = 0
    arrival_markers: int = 0
    departure_station_markers: int = 0
    arrival_station_markers: int = 0
    departure_datetimes: list[str] = field(default_factory=list)
    arrival_datetimes: list[str] = field(default_factory=list)
    departure_station_text: list[str] = field(default_factory=list)
    arrival_station_text: list[str] = field(default_factory=list)
    price_labels: list[str] = field(default_factory=list)
    reserve_button_count: int = 0


@dataclass(frozen=True, slots=True)
class _Frame:
    tag: str
    capture: str | None
    hidden: bool
    is_card_root: bool = False


class _RenderedCardCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[FlixBusRenderedCandidate] = []
        self._current: _CandidateBuilder | None = None
        self._stack: list[_Frame] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        normalized_tag = tag.casefold()
        attributes = _attributes(attrs)
        data_e2e = attributes.get("data-e2e", "")
        card_identity = _card_identity(data_e2e)
        if card_identity is not None:
            if normalized_tag != "li" or self._current is not None:
                raise PublicPageSchemaError("invalid rendered result-card root")
            self._current = _CandidateBuilder(*card_identity)
            self._stack = [_Frame(normalized_tag, None, False, True)]
            return
        if self._current is None:
            return
        if data_e2e == "search-result-item":
            raise PublicPageSchemaError("rendered result-card identity is missing")

        capture: str | None = None
        if data_e2e == "search-result-departure-time":
            self._current.departure_markers += 1
            capture = "departure_time"
        elif data_e2e == "search-result-arrival-time":
            self._current.arrival_markers += 1
            capture = "arrival_time"
        elif data_e2e == "search-result-departure-station":
            self._current.departure_station_markers += 1
            capture = "departure_station"
        elif data_e2e == "search-result-arrival-station":
            self._current.arrival_station_markers += 1
            capture = "arrival_station"

        hidden = bool(self._stack and self._stack[-1].hidden) or (
            attributes.get("aria-hidden", "").casefold() == "true"
        )
        self._stack.append(_Frame(normalized_tag, capture, hidden))

        if normalized_tag == "time":
            time_capture = self._nearest_capture(
                {"departure_time", "arrival_time"}
            )
            if time_capture is not None:
                raw_datetime = attributes.get("datetime")
                if raw_datetime is None:
                    raise PublicPageSchemaError("rendered journey time is missing")
                target = (
                    self._current.departure_datetimes
                    if time_capture == "departure_time"
                    else self._current.arrival_datetimes
                )
                target.append(raw_datetime)

        if (
            normalized_tag == "button"
            and attributes.get("data-type") == "priceButton"
        ):
            raw_price = attributes.get("aria-label")
            if raw_price is None:
                raise PublicPageSchemaError("rendered journey price is missing")
            self._current.price_labels.append(raw_price)
        if normalized_tag == "button" and data_e2e == "button-reserve-trip":
            self._current.reserve_button_count += 1

        if normalized_tag in _VOID_ELEMENTS:
            self._stack.pop()

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if self._current is not None and tag.casefold() not in _VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._current is None or not self._stack or self._stack[-1].hidden:
            return
        capture = self._nearest_capture({"departure_station", "arrival_station"})
        if capture == "departure_station":
            self._current.departure_station_text.append(data)
        elif capture == "arrival_station":
            self._current.arrival_station_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        normalized_tag = tag.casefold()
        matching_index = next(
            (
                index
                for index in range(len(self._stack) - 1, -1, -1)
                if self._stack[index].tag == normalized_tag
            ),
            None,
        )
        if matching_index is None:
            raise PublicPageSchemaError("unbalanced rendered journey card")
        closing = self._stack[matching_index:]
        del self._stack[matching_index:]
        if any(frame.is_card_root for frame in closing):
            if normalized_tag != "li" or self._stack:
                raise PublicPageSchemaError("invalid rendered journey card boundary")
            self.candidates.append(_finish_candidate(self._current))
            if len(self.candidates) > MAX_TRIPS_PER_SEARCH:
                raise PublicPageSchemaError("too many rendered journey cards")
            self._current = None

    def close(self) -> None:
        super().close()
        if self._current is not None:
            raise PublicPageSchemaError("unterminated rendered journey card")
        identifiers = [item.provider_candidate_id for item in self.candidates]
        if len(identifiers) != len(set(identifiers)):
            raise PublicPageSchemaError("duplicate rendered journey card")

    def _nearest_capture(self, allowed: set[str]) -> str | None:
        return next(
            (
                frame.capture
                for frame in reversed(self._stack)
                if frame.capture in allowed
            ),
            None,
        )


def parse_flixbus_rendered_candidates(
    html: str,
) -> tuple[FlixBusRenderedCandidate, ...]:
    """Parse stable rendered-card fields without inferring connected legs."""

    collector = _RenderedCardCollector()
    collector.feed(html)
    collector.close()
    return tuple(collector.candidates)


class FlixBusAdapter(PublicHtmlBusAdapter):
    source_slug = "flixbus"
    public_hosts = ("flixbus.com",)
    provider_names = ("FlixBus",)
    default_search_url_template = (
        "https://shop.global.flixbus.com/search?departureCity={origin}"
        "&arrivalCity={destination}&rideDate={date_dot}&adult=1"
        "&currency=EUR"
    )
    parser_version = "flixbus-rendered-dom-v3"
    rendered_cards_authoritative = True

    def __init__(
        self,
        spec: SourceSpec,
        *,
        loader: PublicPageLoader | None = None,
        clock: Callable[[], datetime] | None = None,
        environ: Mapping[str, str] | None = None,
        minimum_request_interval_seconds: float = 5.0,
    ) -> None:
        super().__init__(spec, loader=loader, clock=clock, environ=environ)
        self._loader = _PacedPublicPageLoader(
            self._loader,
            minimum_interval_seconds=minimum_request_interval_seconds,
        )
        # The coordinator may schedule many cells concurrently. Waiting for
        # this source lock happens before the base adapter starts its 90-second
        # per-search deadline, so queued cells do not fail as timeouts.
        self._search_lock = asyncio.Lock()

    async def search(self, request: SearchRequest) -> ConnectorResult:
        async with self._search_lock:
            return await super().search(request)

    def _search_pages(self, request: SearchRequest):
        if request.passenger.paid_entitlements:
            raise PublicPageConfigurationError
        searches, aliases = super()._search_pages(request)
        if any(
            not _is_one_adult_eur_search(search.url, request.currency)
            for search in searches
        ):
            raise PublicPageConfigurationError
        return searches, aliases

    def _inspect_rendered_candidates(
        self,
        pages: Sequence[tuple[Direction, RenderedPage]],
        *,
        request: SearchRequest,
        observed_at: datetime,
    ) -> RenderedCandidateInspection | None:
        candidate_count = 0
        eligible_count = 0
        outbound: dict[str, FlixBusRenderedCandidate] = {}
        returns: dict[str, FlixBusRenderedCandidate] = {}
        stop_aliases, stop_configuration = _stop_aliases(
            self._environ.get(f"{self._env_prefix}_STOP_ALIASES_JSON", "")
        )
        for direction, page in pages:
            candidates = parse_flixbus_rendered_candidates(page.html)
            candidate_count += len(candidates)
            for candidate in candidates:
                if not _eligible_for_direction(
                    candidate,
                    direction,
                    request,
                    stop_aliases=stop_aliases,
                ):
                    continue
                eligible_count += 1
                target = outbound if direction is Direction.OUTBOUND else returns
                existing = target.get(candidate.provider_candidate_id)
                if existing is not None and existing != candidate:
                    raise PublicPageSchemaError(
                        "rendered candidate ID identifies conflicting journeys"
                    )
                target[candidate.provider_candidate_id] = candidate
        if candidate_count == 0:
            return None
        if stop_configuration == "missing":
            diagnostic = f"CANDIDATES_{candidate_count}_STOPS_UNCONFIGURED"
            offers: tuple[NormalizedOffer, ...] = ()
            normalization_complete = False
        elif stop_configuration == "invalid":
            diagnostic = f"CANDIDATES_{candidate_count}_STOP_CONFIG_INVALID"
            offers = ()
            normalization_complete = False
        else:
            offers = _normalize_displayed_offers(
                tuple(outbound.values()),
                tuple(returns.values()),
                request=request,
                observed_at=observed_at,
                stop_aliases=stop_aliases,
            )
            diagnostic = f"CANDIDATES_{candidate_count}_ELIGIBLE_{eligible_count}"
            normalization_complete = True
        return RenderedCandidateInspection(
            candidate_count=candidate_count,
            eligible_count=eligible_count,
            diagnostic_suffix=diagnostic,
            schema_fingerprint=_SCHEMA_FINGERPRINT,
            offers=offers,
            normalization_complete=normalization_complete,
        )


def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, value in attrs:
        name = raw_name.casefold()
        if name in result:
            raise PublicPageSchemaError("duplicate rendered-card attribute")
        result[name] = value or ""
    return result


def _card_identity(data_e2e: str) -> tuple[str, str] | None:
    if not data_e2e.startswith(_CARD_PREFIX):
        return None
    match = _CARD_ID.fullmatch(data_e2e[len(_CARD_PREFIX) :])
    if match is None:
        raise PublicPageSchemaError("rendered result-card identity is invalid")
    kind = "direct" if match.group("kind") == "direct" else "connection"
    return match.group("identifier"), kind


def _finish_candidate(builder: _CandidateBuilder) -> FlixBusRenderedCandidate:
    marker_counts = (
        builder.departure_markers,
        builder.arrival_markers,
        builder.departure_station_markers,
        builder.arrival_station_markers,
    )
    if marker_counts != (1, 1, 1, 1):
        raise PublicPageSchemaError("rendered journey markers are ambiguous")
    if len(builder.departure_datetimes) != 1 or len(builder.arrival_datetimes) != 1:
        raise PublicPageSchemaError("rendered journey times are ambiguous")
    if len(builder.price_labels) != 1:
        raise PublicPageSchemaError("rendered journey price is ambiguous")
    if builder.reserve_button_count < 1:
        raise PublicPageSchemaError("rendered journey is not selectable")

    departs_at = _aware_datetime(builder.departure_datetimes[0])
    arrives_at = _aware_datetime(builder.arrival_datetimes[0])
    if arrives_at <= departs_at:
        raise PublicPageSchemaError("rendered journey arrival precedes departure")
    return FlixBusRenderedCandidate(
        provider_candidate_id=builder.provider_candidate_id,
        connection_kind=builder.connection_kind,
        departs_at=departs_at,
        arrives_at=arrives_at,
        departure_station_name=_station_name(
            builder.departure_station_text,
            prefix="Departure stop:",
        ),
        arrival_station_name=_station_name(
            builder.arrival_station_text,
            prefix="Arrival stop:",
        ),
        amount_minor=_price_minor(builder.price_labels[0]),
        currency="EUR",
        reserve_button_count=builder.reserve_button_count,
    )


def _aware_datetime(raw: str) -> datetime:
    normalized = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise PublicPageSchemaError("rendered journey time is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublicPageSchemaError("rendered journey time lacks timezone")
    return parsed


def _station_name(chunks: list[str], *, prefix: str) -> str:
    text = " ".join(" ".join(chunks).split())
    if not text.startswith(prefix):
        raise PublicPageSchemaError("rendered journey station label is invalid")
    name = " ".join(text[len(prefix) :].split())
    if not name or len(name) > 200 or _CONTROL_CHARACTERS.search(name):
        raise PublicPageSchemaError("rendered journey station is invalid")
    return name


def _price_minor(raw: str) -> int:
    match = _PRICE.fullmatch(raw.replace("\N{NO-BREAK SPACE}", " "))
    if match is None:
        raise PublicPageSchemaError("rendered journey price is invalid")
    amount = int(match.group("major")) * 100 + int(match.group("minor"))
    if amount <= 0:
        raise PublicPageSchemaError("rendered journey price is invalid")
    return amount


def _is_one_adult_eur_search(url: str, currency: str) -> bool:
    """Require the public search itself to prove the passenger/currency scope."""

    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    return query.get("adult") == ["1"] and query.get("currency") == [currency]


def _normalize_displayed_offers(
    outbound: tuple[FlixBusRenderedCandidate, ...],
    returns: tuple[FlixBusRenderedCandidate, ...],
    *,
    request: SearchRequest,
    observed_at: datetime,
    stop_aliases: dict[str, str],
) -> tuple[NormalizedOffer, ...]:
    if request.search_kind is SearchKind.ROUND_TRIP:
        compatible = [
            (out, back)
            for out in outbound
            for back in returns
            if out.currency == back.currency
            and back.departs_at - out.arrives_at >= timedelta(hours=12)
        ]
        if not compatible:
            return ()
        out, back = min(
            compatible,
            key=lambda pair: (
                pair[0].amount_minor + pair[1].amount_minor,
                pair[0].departs_at,
                pair[1].departs_at,
                pair[0].provider_candidate_id,
                pair[1].provider_candidate_id,
            ),
        )
        outbound_segment = _segment(out, stop_aliases)
        return_segment = _segment(back, stop_aliases)
        components = (
            _displayed_component(out, request),
            _displayed_component(back, request),
        )
        digest = hashlib.sha256(
            f"{out.provider_candidate_id}:{back.provider_candidate_id}".encode()
        ).hexdigest()[:24]
        return (
            NormalizedOffer(
                provider_offer_id=f"displayed-flixbus-{digest}",
                source_slug="flixbus",
                destination_city_id=request.destination_city_id,
                pricing_semantics=PricingSemantics.COMPOSABLE,
                price_completeness=PriceCompleteness.ESTIMATED,
                total_amount_minor=out.amount_minor + back.amount_minor,
                currency=out.currency,
                mandatory_fees_included=False,
                passenger_age=request.passenger.age,
                passenger_student=request.passenger.student,
                passenger_eligibility_confirmed=True,
                bookable=True,
                is_teaser=False,
                observed_at=observed_at,
                outbound_segments=(outbound_segment,),
                return_segments=(return_segment,),
                booking_components=components,
                included_baggage=request.baggage,
            ),
        )

    candidates = returns if request.direction is Direction.RETURN else outbound
    if not candidates:
        return ()
    best = min(
        candidates,
        key=lambda candidate: (
            candidate.amount_minor,
            candidate.departs_at,
            candidate.arrives_at,
            candidate.provider_candidate_id,
        ),
    )
    digest = hashlib.sha256(best.provider_candidate_id.encode()).hexdigest()[:24]
    return (
        NormalizedOffer(
            provider_offer_id=f"displayed-flixbus-{digest}",
            source_slug="flixbus",
            destination_city_id=request.destination_city_id,
            pricing_semantics=PricingSemantics.ONE_WAY,
            price_completeness=PriceCompleteness.ESTIMATED,
            total_amount_minor=best.amount_minor,
            currency=best.currency,
            mandatory_fees_included=False,
            passenger_age=request.passenger.age,
            passenger_student=request.passenger.student,
            passenger_eligibility_confirmed=True,
            bookable=True,
            is_teaser=False,
            observed_at=observed_at,
            outbound_segments=(_segment(best, stop_aliases),),
            booking_components=(_displayed_component(best, request),),
            included_baggage=request.baggage,
        ),
    )


def _segment(
    candidate: FlixBusRenderedCandidate,
    stop_aliases: dict[str, str],
) -> Segment:
    try:
        departure_terminal_id = stop_aliases[candidate.departure_station_name]
        arrival_terminal_id = stop_aliases[candidate.arrival_station_name]
    except KeyError as error:
        raise PublicPageSchemaError(
            "rendered journey stop is not configured"
        ) from error
    return Segment(
        sequence=1,
        mode=TransportMode.BUS,
        operator="FlixBus",
        service_number=None,
        departure_terminal_id=departure_terminal_id,
        departure_terminal_name=candidate.departure_station_name,
        arrival_terminal_id=arrival_terminal_id,
        arrival_terminal_name=candidate.arrival_station_name,
        departs_at=candidate.departs_at,
        arrives_at=candidate.arrives_at,
        protected_connection=True,
    )


def _displayed_component(
    candidate: FlixBusRenderedCandidate,
    request: SearchRequest,
) -> BookingComponent:
    return BookingComponent(
        seller="FlixBus",
        source_slug="flixbus",
        amount_minor=candidate.amount_minor,
        currency=candidate.currency,
        booking_url=None,
        fare_name=_DISPLAYED_FARE_NAME,
        mandatory_fees_included=False,
        passenger_age=request.passenger.age,
        passenger_student=request.passenger.student,
        passenger_eligibility_confirmed=True,
        bookable=True,
        is_teaser=False,
        student_fare_applied=False,
        price_completeness=PriceCompleteness.ESTIMATED,
    )


def _stop_aliases(raw: str) -> tuple[dict[str, str], str]:
    if not raw.strip():
        return {}, "missing"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "invalid"
    if not isinstance(payload, dict) or not payload or len(payload) > 1_000:
        return {}, "invalid"
    aliases: dict[str, str] = {}
    for provider_name, terminal_id in payload.items():
        if not isinstance(provider_name, str) or not isinstance(terminal_id, str):
            return {}, "invalid"
        normalized_name = " ".join(provider_name.split())
        normalized_terminal = terminal_id.strip()
        if (
            not normalized_name
            or len(normalized_name) > 200
            or _CONTROL_CHARACTERS.search(normalized_name)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", normalized_terminal)
            or normalized_name in aliases
        ):
            return {}, "invalid"
        aliases[normalized_name] = normalized_terminal
    return aliases, "valid"


def _eligible_for_direction(
    candidate: FlixBusRenderedCandidate,
    direction: Direction,
    request: SearchRequest,
    *,
    stop_aliases: dict[str, str],
) -> bool:
    if candidate.connection_kind != "direct":
        return False
    window = (
        request.outbound_window
        if direction is Direction.OUTBOUND
        else (request.return_window or request.outbound_window)
    )
    if not window.starts_at <= candidate.departs_at <= window.ends_at:
        return False
    if direction is Direction.RETURN and candidate.arrives_at > window.ends_at:
        return False
    if candidate.arrives_at - candidate.departs_at > timedelta(
        minutes=request.maximum_direction_minutes
    ):
        return False
    if candidate.currency != request.currency:
        return False
    if direction is Direction.OUTBOUND:
        expected_origins = request.origin_terminal_ids
        expected_destinations = request.destination_terminal_ids
    else:
        expected_origins = request.destination_terminal_ids
        expected_destinations = request.origin_terminal_ids
    return (
        stop_aliases.get(candidate.departure_station_name) in expected_origins
        and stop_aliases.get(candidate.arrival_station_name) in expected_destinations
    )


ADAPTER_CLASS = FlixBusAdapter
