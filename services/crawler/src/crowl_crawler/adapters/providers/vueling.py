"""Vueling rendered public booking-page extractor."""

from __future__ import annotations

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


class VuelingAdapter(ConfiguredFlightAdapter):
    source_slug = "vueling"
    operator_name = "Vueling"
    connector_version = "vueling-public-page-v1"
    parser_version = "vueling-embedded-journeys-v1"
    runtime_strategy = ExtractionStrategy.PLAYWRIGHT
    public_page_hosts = ("vueling.com",)

    def build_request(
        self, public_page_url: str, context: FlightSearchContext
    ) -> FlightHttpRequest:
        request = context.request
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
                "adults": 1,
                "currency": request.currency,
            },
        )

    def parse_response(
        self, body: bytes, context: FlightSearchContext
    ) -> ParsedFlightResponse:
        state = mapping_with_keys(embedded_json_documents(body), ("journeys",))
        journeys = required_sequence(state["journeys"], "journeys")
        if not journeys:
            return ParsedFlightResponse((), _empty_outcome(state))
        offers: list[RawFlightOffer] = []
        for item in journeys:
            journey = required_mapping(item, "journey")
            price = required_mapping(journey.get("totalPrice"), "totalPrice")
            fare = required_mapping(journey.get("fareFamily"), "fareFamily")
            inbound: tuple[RawFlightSegment, ...] = ()
            if context.request.search_kind is SearchKind.ROUND_TRIP:
                inbound = _segments(journey.get("return"))
            offers.append(
                RawFlightOffer(
                    provider_offer_id=required_text(journey, "journeyId"),
                    amount_minor=decimal_minor_units(price.get("amount")),
                    currency=required_text(price, "currency").upper(),
                    booking_url=required_text(journey, "bookingUrl"),
                    fare_name=required_text(fare, "name"),
                    outbound_segments=_segments(journey.get("outbound")),
                    passenger_quantity=required_int(fare, "eligibleQuantity"),
                    paid_membership_required=required_bool(
                        fare, "requiresPaidMembership"
                    ),
                    return_segments=inbound,
                    mandatory_fees_included=required_bool(
                        price, "includesMandatoryFees"
                    ),
                    passenger_eligibility_confirmed=required_bool(
                        fare, "adultEligible"
                    ),
                    bookable=required_bool(fare, "bookable"),
                    is_teaser=required_bool(fare, "isTeaser"),
                )
            )
        return ParsedFlightResponse(tuple(offers))


def _segments(value: object) -> tuple[RawFlightSegment, ...]:
    itinerary = required_mapping(value, "itinerary")
    result: list[RawFlightSegment] = []
    for item in required_sequence(itinerary.get("segments"), "segments"):
        segment = required_mapping(item, "segment")
        result.append(
            RawFlightSegment(
                departure_iata=required_text(segment, "departureAirport").upper(),
                arrival_iata=required_text(segment, "arrivalAirport").upper(),
                departs_at=required_text(segment, "departureDateTime"),
                arrives_at=required_text(segment, "arrivalDateTime"),
                service_number=required_text(segment, "flightNumber"),
                operator="Vueling",
                protected_connection=segment.get("protected", True) is True,
            )
        )
    return tuple(result)


def _empty_outcome(state) -> SearchOutcome:  # type: ignore[no-untyped-def]
    outcome = {
        "not_on_sale": SearchOutcome.NOT_ON_SALE,
        "no_service": SearchOutcome.NO_SERVICE,
        "sold_out": SearchOutcome.SOLD_OUT,
        "no_results": SearchOutcome.SUCCESS_EMPTY,
    }.get(state.get("availability"))
    if outcome is None:
        raise FlightPayloadError("empty journey state is not explicitly final")
    return outcome


ADAPTER_CLASS = VuelingAdapter
