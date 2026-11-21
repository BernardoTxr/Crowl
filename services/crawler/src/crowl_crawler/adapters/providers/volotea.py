"""Volotea rendered public booking-page extractor."""

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


class VoloteaAdapter(ConfiguredFlightAdapter):
    source_slug = "volotea"
    operator_name = "Volotea"
    connector_version = "volotea-public-page-v1"
    parser_version = "volotea-embedded-trips-v1"
    runtime_strategy = ExtractionStrategy.PLAYWRIGHT
    public_page_hosts = ("volotea.com",)

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
        state = mapping_with_keys(embedded_json_documents(body), ("trips",))
        trips = required_sequence(state["trips"], "trips")
        if not trips:
            return ParsedFlightResponse((), _empty_outcome(state))
        offers: list[RawFlightOffer] = []
        for item in trips:
            trip = required_mapping(item, "trip")
            total = required_mapping(trip.get("total"), "total")
            fare = required_mapping(trip.get("fare"), "fare")
            inbound: tuple[RawFlightSegment, ...] = ()
            if context.request.search_kind is SearchKind.ROUND_TRIP:
                inbound = _legs(trip.get("return"))
            offers.append(
                RawFlightOffer(
                    provider_offer_id=required_text(trip, "tripId"),
                    amount_minor=decimal_minor_units(total.get("amount")),
                    currency=required_text(total, "currency").upper(),
                    booking_url=required_text(trip, "bookingUrl"),
                    fare_name=required_text(fare, "name"),
                    outbound_segments=_legs(trip.get("outbound")),
                    passenger_quantity=required_int(fare, "eligibleQuantity"),
                    paid_membership_required=required_bool(
                        fare, "requiresPaidMembership"
                    ),
                    return_segments=inbound,
                    mandatory_fees_included=required_bool(
                        total, "includesMandatoryFees"
                    ),
                    passenger_eligibility_confirmed=required_bool(
                        fare, "adultEligible"
                    ),
                    bookable=required_bool(fare, "bookable"),
                    is_teaser=required_bool(fare, "isTeaser"),
                )
            )
        return ParsedFlightResponse(tuple(offers))


def _legs(value: object) -> tuple[RawFlightSegment, ...]:
    itinerary = required_mapping(value, "itinerary")
    result: list[RawFlightSegment] = []
    for item in required_sequence(itinerary.get("legs"), "legs"):
        leg = required_mapping(item, "leg")
        result.append(
            RawFlightSegment(
                departure_iata=required_text(leg, "from").upper(),
                arrival_iata=required_text(leg, "to").upper(),
                departs_at=required_text(leg, "departure"),
                arrives_at=required_text(leg, "arrival"),
                service_number=required_text(leg, "flightNumber"),
                operator="Volotea",
                protected_connection=leg.get("protected", True) is True,
            )
        )
    return tuple(result)


def _empty_outcome(state) -> SearchOutcome:  # type: ignore[no-untyped-def]
    outcome = {
        "NOT_ON_SALE": SearchOutcome.NOT_ON_SALE,
        "NO_SERVICE": SearchOutcome.NO_SERVICE,
        "SOLD_OUT": SearchOutcome.SOLD_OUT,
        "NO_RESULTS": SearchOutcome.SUCCESS_EMPTY,
    }.get(state.get("status"))
    if outcome is None:
        raise FlightPayloadError("empty trip state is not explicitly final")
    return outcome


ADAPTER_CLASS = VoloteaAdapter
