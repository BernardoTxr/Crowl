"""Ryanair public booking-page extractor.

Only literal JSON embedded in the rendered public page is parsed. The adapter
does not call private/undocumented web APIs and never handles login or
challenge pages.
"""

from __future__ import annotations

import hashlib

from ...models import ExtractionStrategy, SearchKind, SearchOutcome
from .flight_http import (
    ConfiguredFlightAdapter,
    FlightHttpRequest,
    FlightPayloadError,
    FlightSearchContext,
    ParsedFlightResponse,
    RawFlightOffer,
    RawFlightSegment,
    decimal_minor_units,
    embedded_json_documents,
    mapping_with_keys,
    query_request,
    required_bool,
    required_int,
    required_mapping,
    required_sequence,
    required_text,
)


class RyanairAdapter(ConfiguredFlightAdapter):
    source_slug = "ryanair"
    operator_name = "Ryanair"
    connector_version = "ryanair-public-page-v1"
    parser_version = "ryanair-embedded-fares-v1"
    runtime_strategy = ExtractionStrategy.PLAYWRIGHT
    public_page_hosts = ("ryanair.com",)

    def build_request(
        self, public_page_url: str, context: FlightSearchContext
    ) -> FlightHttpRequest:
        return query_request(
            public_page_url,
            {
                "adults": 1,
                "teens": 0,
                "children": 0,
                "infants": 0,
                "dateOut": context.selected_outbound_date.isoformat(),
                "dateIn": (
                    context.selected_return_date.isoformat()
                    if context.selected_return_date is not None
                    else None
                ),
                "originIata": context.query_origin.iata,
                "destinationIata": context.query_destination.iata,
            },
        )

    def parse_response(
        self, body: bytes, context: FlightSearchContext
    ) -> ParsedFlightResponse:
        state = mapping_with_keys(embedded_json_documents(body), ("fares",))
        fares = required_sequence(state["fares"], "fares")
        if not fares:
            return ParsedFlightResponse(offers=(), empty_outcome=_empty_outcome(state))

        offers: list[RawFlightOffer] = []
        for item in fares:
            fare = required_mapping(item, "fare")
            outbound_mapping = required_mapping(fare.get("outbound"), "outbound")
            outbound = _flight(outbound_mapping)
            inbound_mapping = None
            inbound: RawFlightSegment | None = None
            if context.request.search_kind is SearchKind.ROUND_TRIP:
                inbound_mapping = required_mapping(fare.get("inbound"), "inbound")
                inbound = _flight(inbound_mapping)
            summary = required_mapping(fare.get("summary"), "summary")
            price = required_mapping(summary.get("price"), "summary.price")
            identity = required_text(outbound_mapping, "flightKey")
            if inbound_mapping is not None:
                identity += ":" + required_text(inbound_mapping, "flightKey")
            offers.append(
                RawFlightOffer(
                    provider_offer_id="ryanair-"
                    + hashlib.sha256(identity.encode()).hexdigest()[:24],
                    amount_minor=decimal_minor_units(price.get("value")),
                    currency=required_text(price, "currencyCode").upper(),
                    booking_url=required_text(fare, "bookingUrl"),
                    fare_name=required_text(fare, "fareName"),
                    outbound_segments=(outbound,),
                    passenger_quantity=required_int(fare, "eligibleQuantity"),
                    paid_membership_required=required_bool(
                        fare, "requiresPaidMembership"
                    ),
                    return_segments=(inbound,) if inbound is not None else (),
                    mandatory_fees_included=required_bool(
                        fare, "mandatoryFeesIncluded"
                    ),
                    passenger_eligibility_confirmed=required_bool(
                        fare, "adultEligible"
                    ),
                    bookable=required_bool(fare, "bookable"),
                    is_teaser=required_bool(fare, "isTeaser"),
                )
            )
        return ParsedFlightResponse(tuple(offers))


def _flight(flight: object) -> RawFlightSegment:
    value = required_mapping(flight, "flight")
    departure = required_mapping(value.get("departureAirport"), "departureAirport")
    arrival = required_mapping(value.get("arrivalAirport"), "arrivalAirport")
    return RawFlightSegment(
        departure_iata=required_text(departure, "iataCode").upper(),
        arrival_iata=required_text(arrival, "iataCode").upper(),
        departs_at=required_text(value, "departureDate"),
        arrives_at=required_text(value, "arrivalDate"),
        service_number=required_text(value, "flightNumber"),
        operator="Ryanair",
    )


def _empty_outcome(state) -> SearchOutcome:  # type: ignore[no-untyped-def]
    if state.get("notOnSale") is True:
        return SearchOutcome.NOT_ON_SALE
    if state.get("soldOut") is True:
        return SearchOutcome.SOLD_OUT
    if state.get("routeAvailable") is False:
        return SearchOutcome.NO_SERVICE
    if state.get("noResults") is True:
        return SearchOutcome.SUCCESS_EMPTY
    raise FlightPayloadError("empty fare state is not explicitly final")


ADAPTER_CLASS = RyanairAdapter
