"""easyJet rendered public booking-page extractor."""

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


class EasyJetAdapter(ConfiguredFlightAdapter):
    source_slug = "easyjet"
    operator_name = "easyJet"
    connector_version = "easyjet-public-page-v1"
    parser_version = "easyjet-embedded-journeys-v1"
    runtime_strategy = ExtractionStrategy.PLAYWRIGHT
    public_page_hosts = ("easyjet.com",)

    def build_request(
        self, public_page_url: str, context: FlightSearchContext
    ) -> FlightHttpRequest:
        return query_request(
            public_page_url,
            {
                "origin": context.query_origin.iata,
                "destination": context.query_destination.iata,
                "outbound": context.selected_outbound_date.isoformat(),
                "inbound": (
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
        state = mapping_with_keys(embedded_json_documents(body), ("journeys",))
        journeys = required_sequence(state["journeys"], "journeys")
        if not journeys:
            return ParsedFlightResponse((), _empty_outcome(state))
        offers: list[RawFlightOffer] = []
        for item in journeys:
            journey = required_mapping(item, "journey")
            price = required_mapping(journey.get("price"), "price")
            fare = required_mapping(journey.get("fare"), "fare")
            inbound: tuple[RawFlightSegment, ...] = ()
            if context.request.search_kind is SearchKind.ROUND_TRIP:
                inbound = _segments(journey.get("return"))
            offers.append(
                RawFlightOffer(
                    provider_offer_id=required_text(journey, "id"),
                    amount_minor=decimal_minor_units(price.get("total")),
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
                        price, "includesTaxesAndMandatoryFees"
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
    segments: list[RawFlightSegment] = []
    for item in required_sequence(itinerary.get("segments"), "segments"):
        segment = required_mapping(item, "segment")
        departure = required_mapping(segment.get("departure"), "departure")
        arrival = required_mapping(segment.get("arrival"), "arrival")
        segments.append(
            RawFlightSegment(
                departure_iata=required_text(departure, "airportCode").upper(),
                arrival_iata=required_text(arrival, "airportCode").upper(),
                departs_at=required_text(departure, "dateTime"),
                arrives_at=required_text(arrival, "dateTime"),
                service_number=required_text(segment, "flightNumber"),
                operator="easyJet",
                protected_connection=segment.get("protected", True) is True,
            )
        )
    return tuple(segments)


def _empty_outcome(state) -> SearchOutcome:  # type: ignore[no-untyped-def]
    outcome = {
        "NOT_ON_SALE": SearchOutcome.NOT_ON_SALE,
        "NO_SERVICE": SearchOutcome.NO_SERVICE,
        "SOLD_OUT": SearchOutcome.SOLD_OUT,
        "NO_RESULTS": SearchOutcome.SUCCESS_EMPTY,
    }.get(state.get("status"))
    if outcome is None:
        raise FlightPayloadError("empty journey state is not explicitly final")
    return outcome


ADAPTER_CLASS = EasyJetAdapter
