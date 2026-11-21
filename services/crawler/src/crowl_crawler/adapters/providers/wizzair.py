"""Wizz Air rendered public booking-page extractor."""

from __future__ import annotations

import hashlib
from itertools import product

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


class WizzAirAdapter(ConfiguredFlightAdapter):
    source_slug = "wizzair"
    operator_name = "Wizz Air"
    connector_version = "wizzair-public-page-v1"
    parser_version = "wizzair-embedded-flight-lists-v1"
    runtime_strategy = ExtractionStrategy.PLAYWRIGHT
    public_page_hosts = ("wizzair.com",)

    def build_request(
        self, public_page_url: str, context: FlightSearchContext
    ) -> FlightHttpRequest:
        return query_request(
            public_page_url,
            {
                "origin": context.query_origin.iata,
                "destination": context.query_destination.iata,
                "departureDate": context.selected_outbound_date.isoformat(),
                "returnDate": (
                    context.selected_return_date.isoformat()
                    if context.selected_return_date is not None
                    else None
                ),
                "adultCount": 1,
                "childCount": 0,
                "infantCount": 0,
            },
        )

    def parse_response(
        self, body: bytes, context: FlightSearchContext
    ) -> ParsedFlightResponse:
        state = mapping_with_keys(embedded_json_documents(body), ("outboundFlights",))
        outbound_values = required_sequence(state["outboundFlights"], "outboundFlights")
        if not outbound_values:
            return ParsedFlightResponse((), _empty_outcome(state))
        outbound = tuple(_priced_flight(value) for value in outbound_values)
        if context.request.search_kind is SearchKind.ONE_WAY:
            return ParsedFlightResponse(
                tuple(_one_way_offer(item, state) for item in outbound)
            )

        inbound_values = required_sequence(state.get("returnFlights"), "returnFlights")
        if not inbound_values:
            return ParsedFlightResponse((), _empty_outcome(state))
        inbound = tuple(_priced_flight(value) for value in inbound_values)
        offers = tuple(
            _round_trip_offer(outbound_item, inbound_item, state)
            for outbound_item, inbound_item in product(outbound, inbound)
        )
        return ParsedFlightResponse(offers)


def _priced_flight(value: object) -> tuple[RawFlightSegment, int, str, str]:
    flight = required_mapping(value, "flight")
    price = required_mapping(flight.get("price"), "price")
    segment = RawFlightSegment(
        departure_iata=required_text(flight, "departureStation").upper(),
        arrival_iata=required_text(flight, "arrivalStation").upper(),
        departs_at=required_text(flight, "departureDateTime"),
        arrives_at=required_text(flight, "arrivalDateTime"),
        service_number=required_text(flight, "flightNumber"),
        operator="Wizz Air",
    )
    return (
        segment,
        decimal_minor_units(price.get("amount")),
        required_text(price, "currencyCode").upper(),
        required_text(flight, "flightId"),
    )


def _one_way_offer(
    item: tuple[RawFlightSegment, int, str, str], state
) -> RawFlightOffer:  # type: ignore[no-untyped-def]
    segment, amount, currency, identifier = item
    return RawFlightOffer(
        provider_offer_id=identifier,
        amount_minor=amount,
        currency=currency,
        booking_url=required_text(state, "bookingUrl"),
        fare_name=required_text(state, "fareName"),
        outbound_segments=(segment,),
        passenger_quantity=required_int(state, "eligibleQuantity"),
        paid_membership_required=required_bool(state, "requiresPaidMembership"),
        mandatory_fees_included=required_bool(state, "mandatoryFeesIncluded"),
        passenger_eligibility_confirmed=required_bool(state, "adultEligible"),
        bookable=required_bool(state, "bookable"),
        is_teaser=required_bool(state, "isTeaser"),
    )


def _round_trip_offer(  # type: ignore[no-untyped-def]
    outbound, inbound, state
) -> RawFlightOffer:
    out_segment, out_amount, out_currency, out_id = outbound
    in_segment, in_amount, in_currency, in_id = inbound
    if out_currency != in_currency:
        raise FlightPayloadError("Wizz Air directions use different currencies")
    identifier = hashlib.sha256(f"{out_id}:{in_id}".encode()).hexdigest()[:24]
    return RawFlightOffer(
        provider_offer_id=f"wizzair-{identifier}",
        amount_minor=out_amount + in_amount,
        currency=out_currency,
        booking_url=required_text(state, "bookingUrl"),
        fare_name=required_text(state, "fareName"),
        outbound_segments=(out_segment,),
        passenger_quantity=required_int(state, "eligibleQuantity"),
        paid_membership_required=required_bool(state, "requiresPaidMembership"),
        return_segments=(in_segment,),
        mandatory_fees_included=required_bool(state, "mandatoryFeesIncluded"),
        passenger_eligibility_confirmed=required_bool(state, "adultEligible"),
        bookable=required_bool(state, "bookable"),
        is_teaser=required_bool(state, "isTeaser"),
    )


def _empty_outcome(state) -> SearchOutcome:  # type: ignore[no-untyped-def]
    outcome = {
        "NOT_ON_SALE": SearchOutcome.NOT_ON_SALE,
        "NO_SERVICE": SearchOutcome.NO_SERVICE,
        "SOLD_OUT": SearchOutcome.SOLD_OUT,
        "NO_RESULTS": SearchOutcome.SUCCESS_EMPTY,
    }.get(state.get("status"))
    if outcome is None:
        raise FlightPayloadError("empty flight state is not explicitly final")
    return outcome


ADAPTER_CLASS = WizzAirAdapter
