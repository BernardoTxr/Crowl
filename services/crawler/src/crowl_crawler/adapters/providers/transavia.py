"""Transavia rendered public booking-page extractor."""

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


class TransaviaAdapter(ConfiguredFlightAdapter):
    source_slug = "transavia"
    operator_name = "Transavia"
    connector_version = "transavia-public-page-v1"
    parser_version = "transavia-embedded-offers-v1"
    runtime_strategy = ExtractionStrategy.PLAYWRIGHT
    public_page_hosts = ("transavia.com",)

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
                "adults": 1,
            },
        )

    def parse_response(
        self, body: bytes, context: FlightSearchContext
    ) -> ParsedFlightResponse:
        state = mapping_with_keys(embedded_json_documents(body), ("offers",))
        items = required_sequence(state["offers"], "offers")
        if not items:
            return ParsedFlightResponse((), _empty_outcome(state))
        offers: list[RawFlightOffer] = []
        for item in items:
            offer = required_mapping(item, "offer")
            price = required_mapping(offer.get("totalPrice"), "totalPrice")
            fare = required_mapping(offer.get("fare"), "fare")
            inbound: tuple[RawFlightSegment, ...] = ()
            if context.request.search_kind is SearchKind.ROUND_TRIP:
                inbound = _flights(offer.get("inbound"))
            offers.append(
                RawFlightOffer(
                    provider_offer_id=required_text(offer, "offerId"),
                    amount_minor=decimal_minor_units(price.get("amount")),
                    currency=required_text(price, "currency").upper(),
                    booking_url=required_text(offer, "bookingUrl"),
                    fare_name=required_text(fare, "name"),
                    outbound_segments=_flights(offer.get("outbound")),
                    passenger_quantity=required_int(fare, "eligibleQuantity"),
                    paid_membership_required=required_bool(
                        fare, "requiresPaidMembership"
                    ),
                    return_segments=inbound,
                    mandatory_fees_included=required_bool(
                        price, "includesTaxesAndFees"
                    ),
                    passenger_eligibility_confirmed=required_bool(
                        fare, "adultEligible"
                    ),
                    bookable=required_bool(fare, "bookable"),
                    is_teaser=required_bool(fare, "isTeaser"),
                )
            )
        return ParsedFlightResponse(tuple(offers))


def _flights(value: object) -> tuple[RawFlightSegment, ...]:
    itinerary = required_mapping(value, "itinerary")
    result: list[RawFlightSegment] = []
    for item in required_sequence(itinerary.get("flights"), "flights"):
        flight = required_mapping(item, "flight")
        result.append(
            RawFlightSegment(
                departure_iata=required_text(flight, "origin").upper(),
                arrival_iata=required_text(flight, "destination").upper(),
                departs_at=required_text(flight, "departureDateTime"),
                arrives_at=required_text(flight, "arrivalDateTime"),
                service_number=required_text(flight, "flightNumber"),
                operator="Transavia",
                protected_connection=flight.get("protected", True) is True,
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
        raise FlightPayloadError("empty offer state is not explicitly final")
    return outcome


ADAPTER_CLASS = TransaviaAdapter
