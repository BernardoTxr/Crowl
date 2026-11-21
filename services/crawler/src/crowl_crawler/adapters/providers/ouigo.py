from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from ...models import (
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
    TimeWindow,
    TransportMode,
)
from ...trust import sanitize_booking_url
from ..catalog import SourceSpec
from ._ouigo_mcp import OuigoMcpClient, OuigoMcpError
from .rail_html import PublicHtmlRailAdapter, RailHtmlConfigurationError, _window_dates


_STATIONS = {
    "par-gare-de-lyon": ("87686006", "Paris Gare de Lyon"),
    "lyon-part-dieu": ("87723197", "Lyon Part Dieu"),
    "par-gare-du-nord": ("87271007", "Paris Nord"),
    "brussels-midi": ("88140010", "Bruxelles-Midi"),
}
_VERIFIED_ROUTES = {
    ("par-gare-de-lyon", "lyon-part-dieu"),
    ("par-gare-du-nord", "brussels-midi"),
}
_SERVICE_ID = re.compile(
    r"0:RDA\|(?P<number>\d{1,8})\|(?:OUIGO|OUIGO_LB)\|"
    r"(?P<date>\d{4}-\d{2}-\d{2})\|(?P<departure>[\d:T-]+)\|"
    r"(?P<arrival>[\d:T-]+);(?P<origin>\d{8});(?P<destination>\d{8})"
)
_BOOKING_PARAMETERS = {
    "origin", "destination", "outboundDate", "animalCount", "nbBabies", "target", "serviceId",
}
_SEARCH_DEADLINE_SECONDS = 90
_PARIS = ZoneInfo("Europe/Paris")


class _FareClient(Protocol):
    async def search(
        self, *, origin: str, destination: str, departure_date: str
    ) -> Mapping[str, Any]: ...


class OuigoAdapter(PublicHtmlRailAdapter):
    source_slug = "ouigo"
    operator_name = "OUIGO"
    empty_markers = ("aucun train", "aucun trajet", "no trains found")

    def __init__(
        self, spec: SourceSpec, *, mcp_client: _FareClient | None = None, **kwargs: Any
    ) -> None:
        super().__init__(spec, **kwargs)
        self._mcp = None
        if spec.intended_strategy is ExtractionStrategy.HTTP_STRUCTURED:
            self._mcp = mcp_client or OuigoMcpClient()
            self.search_timeout_seconds = _SEARCH_DEADLINE_SECONDS + 5
            self._metadata = replace(
                self._metadata,
                connector_version=f"{spec.config_version}-official-mcp-v1",
                parser_version="ouigo-mcp-v1",
            )

    async def discover_coverage(self) -> CoverageDiscovery:
        coverage = await super().discover_coverage()
        if self._mcp is None or not self.metadata().enabled:
            return coverage
        if not set(coverage.supported_terminal_pairs) <= _VERIFIED_ROUTES:
            raise RailHtmlConfigurationError("OUIGO MCP coverage includes an unverified route")
        return replace(coverage, diagnostic_code="OUIGO_MCP_COVERAGE_OK")

    async def search(self, request: SearchRequest) -> ConnectorResult:
        client = self._mcp
        if client is None or not self.metadata().enabled:
            return await super().search(request)
        self._ensure_open()
        observed_at = self._observed_at()
        parser_version = self.metadata().parser_version
        completed = 0
        expected = 1
        try:
            outbound_id, destination_id = _route(request)
            inbound = (
                request.search_kind is SearchKind.ONE_WAY
                and request.direction is Direction.RETURN
            )
            outbound_route = (
                (destination_id, outbound_id)
                if inbound else (outbound_id, destination_id)
            )
            searches = [(*outbound_route, request.outbound_window)]
            if request.search_kind is SearchKind.ROUND_TRIP:
                if request.return_window is None:
                    raise RailHtmlConfigurationError("OUIGO round trip is missing a return window")
                searches.append((destination_id, outbound_id, request.return_window))
            searches_by_date = [
                (direction_index, origin, destination, window, day)
                for direction_index, (origin, destination, window) in enumerate(searches)
                for day in _window_dates(window)
            ]
            expected = len(searches_by_date)
        except RailHtmlConfigurationError:
            return ConnectorResult.failure(
                outcome=SearchOutcome.CONFIGURATION_ERROR,
                diagnostic_code="OUIGO_MCP_CONFIG",
                parser_version=parser_version,
                observed_at=observed_at,
                retryable=False,
            )

        outbound: list[NormalizedOffer] = []
        returning: list[NormalizedOffer] = []
        rejected = False
        fingerprint = hashlib.sha256()
        try:
            async with asyncio.timeout(_SEARCH_DEADLINE_SECONDS):
                for direction_index, origin, destination, window, day in searches_by_date:
                    payload = await client.search(
                        origin=_STATIONS[origin][0],
                        destination=_STATIONS[destination][0],
                        departure_date=day.isoformat(),
                    )
                    matches, invalid_rows = self._parse_page(
                        payload, request, origin, destination, window, day, observed_at
                    )
                    rejected |= invalid_rows
                    (returning if direction_index else outbound).extend(matches)
                    fingerprint.update(json.dumps(payload, sort_keys=True).encode())
                    completed += 1
        except (OuigoMcpError, TimeoutError) as error:
            outcome = error.outcome if isinstance(error, OuigoMcpError) else SearchOutcome.TIMEOUT
            code = error.code if isinstance(error, OuigoMcpError) else "OUIGO_MCP_DEADLINE"
            return replace(
                ConnectorResult.failure(
                    outcome=outcome, diagnostic_code=code,
                    parser_version=parser_version, observed_at=observed_at,
                ),
                expected_count=expected,
                completed_count=completed,
            )

        offers = tuple(outbound)
        if request.search_kind is SearchKind.ROUND_TRIP:
            offers = _best_round_trip(outbound, returning)
        if rejected and not offers:
            return replace(
                ConnectorResult.failure(
                    outcome=SearchOutcome.PARSE_SCHEMA_ERROR,
                    diagnostic_code="OUIGO_MCP_SCHEMA",
                    parser_version=parser_version,
                    observed_at=observed_at,
                    retryable=False,
                ),
                expected_count=expected,
                completed_count=completed,
            )
        outcome = (
            SearchOutcome.PARTIAL_SUCCESS if rejected
            else SearchOutcome.SUCCESS_RESULTS if offers
            else SearchOutcome.SUCCESS_EMPTY
        )
        return ConnectorResult(
            outcome=outcome,
            retryable=False,
            diagnostic_code={
                SearchOutcome.PARTIAL_SUCCESS: "OUIGO_MCP_PARTIAL",
                SearchOutcome.SUCCESS_RESULTS: "OUIGO_MCP_RESULTS",
                SearchOutcome.SUCCESS_EMPTY: "OUIGO_MCP_EMPTY",
            }[outcome],
            expected_count=expected,
            completed_count=completed,
            offers=offers,
            parser_version=parser_version,
            observed_at=observed_at,
            response_fingerprint=fingerprint.hexdigest(),
        )

    def _parse_page(
        self,
        payload: Mapping[str, Any],
        request: SearchRequest,
        origin_id: str,
        destination_id: str,
        window: TimeWindow,
        day: date,
        observed_at: datetime,
    ) -> tuple[list[NormalizedOffer], bool]:
        context = payload.get("searchContext")
        rows = payload.get("journeys")
        # Empty searches echo UIC codes. Searches with trains currently echo
        # the provider's exact station names.
        if (
            not isinstance(context, dict)
            or context.get("origin") not in _STATIONS[origin_id]
            or context.get("destination") not in _STATIONS[destination_id]
            or context.get("date") != day.isoformat()
            or type(context.get("passengerCount")) is not int
            or context["passengerCount"] != 1
            or not isinstance(rows, list)
            or len(rows) > 100
        ):
            raise OuigoMcpError(SearchOutcome.PARSE_SCHEMA_ERROR, "OUIGO_MCP_SEARCH_CONTEXT")
        unique: dict[str, NormalizedOffer] = {}
        invalid = False
        for raw in rows:
            try:
                offer = self._direct_offer(raw, request, origin_id, destination_id, day, observed_at)
            except (InvalidOperation, TypeError, ValueError):
                invalid = True
                continue
            segment = offer.outbound_segments[0]
            departs_at = segment.departs_at.astimezone(UTC)
            arrives_at = segment.arrives_at.astimezone(UTC)
            if not (
                window.starts_at.astimezone(UTC) <= departs_at
                and arrives_at <= window.ends_at.astimezone(UTC)
                and arrives_at - departs_at
                <= timedelta(minutes=request.maximum_direction_minutes)
            ):
                continue
            old = unique.setdefault(offer.provider_offer_id, offer)
            if old != offer:
                raise OuigoMcpError(SearchOutcome.PARSE_SCHEMA_ERROR, "OUIGO_MCP_FARE_CONFLICT")
        return list(unique.values()), invalid

    def _direct_offer(
        self, raw: object, request: SearchRequest, origin_id: str, destination_id: str,
        day: date, observed_at: datetime,
    ) -> NormalizedOffer:
        origin_code, origin_name = _STATIONS[origin_id]
        destination_code, destination_name = _STATIONS[destination_id]
        if (
            not isinstance(raw, dict)
            or raw.get("origin") != origin_name
            or raw.get("destination") != destination_name
            or raw.get("serviceModality") not in {"STANDARD_RAIL", "HIGHSPEED_RAIL"}
        ):
            raise ValueError("OUIGO journey does not match a verified rail route")
        departure = _provider_time(raw.get("departureDateTime"))
        arrival = _provider_time(raw.get("arrivalDateTime"))
        if departure.date() != day:
            raise ValueError("OUIGO trip date does not match the search")
        deep_link = raw.get("deepLink")
        if not isinstance(deep_link, str):
            raise ValueError("OUIGO journey has no exact booking link")
        booking_url = sanitize_booking_url(deep_link, self._spec.booking_hosts)
        parsed = urlsplit(booking_url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        service = _SERVICE_ID.fullmatch(params.get("serviceId", [""])[0])
        if (
            parsed.netloc != "ventes.ouigo.com"
            or parsed.path.rstrip("/") != "/fr-FR/orientation/deep-link"
            or not set(params) <= _BOOKING_PARAMETERS
            or params.get("target") != ["routes"]
            or params.get("origin") != [origin_code]
            or params.get("destination") != [destination_code]
            or params.get("outboundDate") != [day.isoformat()]
            or params.get("animalCount", ["0"]) != ["0"]
            or params.get("nbBabies", ["0"]) != ["0"]
            or len(params.get("serviceId", [])) != 1
            or service is None
            or service["origin"] != origin_code
            or service["destination"] != destination_code
            or service["date"] != day.isoformat()
            or service["departure"] != departure.strftime("%Y-%m-%dT%H:%M")
            or service["arrival"] != arrival.strftime("%Y-%m-%dT%H:%M")
        ):
            raise ValueError("OUIGO link does not identify the exact direct train")
        price = raw.get("price")
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            raise ValueError("OUIGO journey has no exact numeric total")
        amount_minor = Decimal(str(price)) * 100
        if not amount_minor.is_finite() or not 0 < amount_minor <= 2_147_483_647 or amount_minor % 1:
            raise ValueError("OUIGO journey price is invalid")
        # The MCP contract declares journey_search prices as totals for the
        # entire selected group. OUIGO's fare conditions include taxes and
        # small luggage in the published base fare; extras are optional.
        booking = BookingComponent(
            seller=self._spec.name, source_slug=self.source_slug, amount_minor=int(amount_minor),
            currency="EUR", booking_url=booking_url, fare_name="OUIGO adult",
            mandatory_fees_included=True, passenger_age=request.passenger.age,
            passenger_student=request.passenger.student, passenger_eligibility_confirmed=True,
            bookable=True, is_teaser=False,
        )
        segment = Segment(
            sequence=1, mode=TransportMode.TRAIN, operator=self.operator_name,
            service_number=service["number"], departure_terminal_id=origin_id,
            departure_terminal_name=self._terminal_names.get(origin_id, origin_name),
            arrival_terminal_id=destination_id,
            arrival_terminal_name=self._terminal_names.get(destination_id, destination_name),
            departs_at=departure, arrives_at=arrival,
        )
        return NormalizedOffer(
            provider_offer_id=_offer_id(service[0]), source_slug=self.source_slug,
            destination_city_id=request.destination_city_id, pricing_semantics=PricingSemantics.ONE_WAY,
            price_completeness=PriceCompleteness.COMPLETE, total_amount_minor=booking.amount_minor,
            currency="EUR", mandatory_fees_included=True, passenger_age=request.passenger.age,
            passenger_student=request.passenger.student, passenger_eligibility_confirmed=True,
            bookable=True, is_teaser=False, observed_at=observed_at, outbound_segments=(segment,),
            booking_components=(booking,), included_baggage=request.baggage,
        )


def _route(request: SearchRequest) -> tuple[str, str]:
    if (
        len(request.origin_terminal_ids) != 1
        or len(request.destination_terminal_ids) != 1
        or request.passenger.age < 12
        or request.currency != "EUR"
        or request.baggage != "smallest_included_personal_item"
    ):
        raise RailHtmlConfigurationError("OUIGO requires an exact route and one ordinary adult in EUR")
    pair = (request.origin_terminal_ids[0], request.destination_terminal_ids[0])
    if pair not in _VERIFIED_ROUTES:
        raise RailHtmlConfigurationError("OUIGO MCP route is unverified")
    return pair


def _provider_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("OUIGO trip time is missing")
    aware = datetime.fromisoformat(value)
    if aware.tzinfo is None or aware.astimezone(_PARIS).utcoffset() != aware.utcoffset():
        raise ValueError("OUIGO trip has an invalid time offset")
    return aware


def _offer_id(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _best_round_trip(
    outbound: list[NormalizedOffer], returning: list[NormalizedOffer]
) -> tuple[NormalizedOffer, ...]:
    best = min(
        (
            (going, back)
            for going in outbound
            for back in returning
            if back.outbound_segments[0].departs_at.astimezone(UTC)
            - going.outbound_segments[0].arrives_at.astimezone(UTC) >= timedelta(hours=12)
        ),
        key=lambda pair: (
            pair[0].total_amount_minor + pair[1].total_amount_minor,
            pair[0].provider_offer_id,
            pair[1].provider_offer_id,
        ),
        default=None,
    )
    if best is None:
        return ()
    going, back = best
    return (replace(
        going,
        provider_offer_id=_offer_id(going.provider_offer_id, back.provider_offer_id),
        pricing_semantics=PricingSemantics.COMPOSABLE,
        total_amount_minor=going.total_amount_minor + back.total_amount_minor,
        return_segments=back.outbound_segments,
        booking_components=going.booking_components + back.booking_components,
    ),)


ADAPTER_CLASS = OuigoAdapter
