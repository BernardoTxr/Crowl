"""Exact direct Basic fares on Ryanair's rendered public flight-selection page."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from xml.etree.ElementTree import Element
from zoneinfo import ZoneInfo

from ...models import SearchOutcome
from ._html_text import _HTML_VOID_ELEMENTS, is_hidden
from .flight_http import (
    FlightPayloadError,
    FlightSearchContext,
    ParsedFlightResponse,
    RawFlightOffer,
    RawFlightSegment,
    TerminalInfo,
    decimal_minor_units,
)

_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_DURATION = re.compile(r"(?:(\d{1,2})h)?\s*(?:(\d{1,2})m)?")
_PRICE = re.compile(r"€\s*(\d{1,5}[.,]\d{2})")
_FLIGHT_NUMBER = re.compile(r"FR\s+\d{1,4}")
_NO_FLIGHTS = "Sorry, there are no flights available on this day"
_SCHEMA_FINGERPRINT = hashlib.sha256(
    b"ryanair-public-v2:journey-container:selected-date:flight-list:basic-eur"
).hexdigest()


class _ResultsDom(HTMLParser):
    """Keep only the public trip summary and the two rendered result sections."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("results")
        self._stack: list[tuple[str, Element | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        parent = self._stack[-1][1] if self._stack else self.root
        node = None
        if parent is not None and not is_hidden(tag, attributes):
            if parent is self.root and tag not in {
                "flights-trip-details",
                "journey-container",
            }:
                node = self.root
            else:
                node = Element(tag, attributes)
                parent.append(node)
        if tag not in _HTML_VOID_ELEMENTS:
            self._stack.append((tag, node))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _HTML_VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        node = self._stack[-1][1] if self._stack else None
        if node is None or node is self.root:
            return
        if len(node):
            child = node[-1]
            child.tail = (child.tail or "") + data
        else:
            node.text = (node.text or "") + data


def parse_ryanair_results(
    html: str, context: FlightSearchContext, booking_url: str
) -> ParsedFlightResponse:
    parser = _ResultsDom()
    parser.feed(html)
    parser.close()
    summary = _only(parser.root.findall("flights-trip-details"), "trip details")
    one_way = context.selected_return_date is None
    bottom_bar = _only(_with_class(summary, "details__bottom-bar"), "trip summary")
    trip_type = "One way" if one_way else "Return"
    summary_text = _text(bottom_bar)
    if not summary_text.startswith(trip_type + " ") or summary_text.split()[-1] != "1":
        raise FlightPayloadError("Ryanair did not confirm the one-passenger search")
    outbound = _results(
        parser.root,
        "outbound",
        context.selected_outbound_date,
        context.query_origin,
        context.query_destination,
    )
    inbound = None
    if context.selected_return_date is not None:
        inbound = _results(
            parser.root,
            "inbound",
            context.selected_return_date,
            context.query_destination,
            context.query_origin,
        )
    if not outbound or (not one_way and not inbound):
        return ParsedFlightResponse((), SearchOutcome.NO_SERVICE, _SCHEMA_FINGERPRINT)

    combinations = (
        ((outbound_item, inbound_item) for outbound_item in outbound for inbound_item in inbound)
        if inbound is not None
        else ((outbound_item, None) for outbound_item in outbound)
    )
    offers: list[RawFlightOffer] = []
    for outbound_item, inbound_item in combinations:
        outbound_segment, outbound_price = outbound_item
        return_segments = (inbound_item[0],) if inbound_item is not None else ()
        identity = "|".join(
            f"{segment.service_number}:{segment.departs_at}:{segment.departure_iata}"
            for segment in (outbound_segment, *return_segments)
        )
        offers.append(
            RawFlightOffer(
                provider_offer_id=(
                    "ryanair-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
                ),
                amount_minor=(
                    outbound_price + (inbound_item[1] if inbound_item is not None else 0)
                ),
                currency="EUR",
                booking_url=booking_url,
                fare_name="Basic Fare",
                outbound_segments=(outbound_segment,),
                passenger_quantity=1,
                paid_membership_required=False,
                return_segments=return_segments,
            )
        )
    return ParsedFlightResponse(tuple(offers), schema_fingerprint=_SCHEMA_FINGERPRINT)


def _results(
    root: Element,
    direction: str,
    selected_date: date,
    origin: TerminalInfo,
    destination: TerminalInfo,
) -> tuple[tuple[RawFlightSegment, int], ...]:
    journey = _only(root.findall(f"journey-container[@data-ref='{direction}']"), direction)
    active_date = _only(journey.findall(".//button[@data-selected='true']"), "selected date")
    if (
        active_date.get("data-e2e") != "date-item"
        or active_date.get("data-ref") != selected_date.isoformat()
    ):
        raise FlightPayloadError("Ryanair selected a different flight date")
    listing = _only(journey.findall(".//flight-list"), "flight list")
    cards = listing.findall(".//flight-card-new")
    if not cards and _NO_FLIGHTS in _text(listing):
        return ()
    if not cards:
        raise FlightPayloadError("Ryanair did not show flights or an explicit empty state")
    return tuple(_flight_card(card, direction, selected_date, origin, destination) for card in cards)


def _flight_card(
    card: Element,
    direction: str,
    selected_date: date,
    origin: TerminalInfo,
    destination: TerminalInfo,
) -> tuple[RawFlightSegment, int]:
    if card.get("data-e2e") != f"flight-card--{direction}":
        raise FlightPayloadError("Ryanair flight card direction changed")
    select = _only(card.findall(".//button[@data-ref='regular-price-select']"), "fare selection")
    fare = _only(_with_class(card, "flight-card-summary__fare"), "fare type")
    if (
        "disabled" in select.attrib
        or select.get("aria-disabled") == "true"
        or _text(fare) != "Basic Fare"
    ):
        raise FlightPayloadError("Ryanair did not show a selectable Basic fare")
    price = _only(card.findall(".//flights-price-simple"), "exact fare price")
    value = _PRICE.fullmatch(_text(price))
    if value is None:
        raise FlightPayloadError("Ryanair Basic fare is not an exact EUR amount")

    departure = _flight_time(card, "departure", f"origin-airport__{origin.iata}")
    arrival = _flight_time(card, "arrival", f"destination-airport__{destination.iata}")
    duration_node = _only(card.findall(".//*[@data-ref='flight_duration']"), "flight duration")
    duration = _DURATION.fullmatch(_text(duration_node))
    flight_number = _text(_only(_with_class(card, "card-flight-num__content"), "flight number"))
    if duration is None or not _FLIGHT_NUMBER.fullmatch(flight_number):
        raise FlightPayloadError("Ryanair did not show an exact direct flight")
    hours, minutes = int(duration[1] or 0), int(duration[2] or 0)
    if minutes >= 60:
        raise FlightPayloadError("Ryanair flight duration is invalid")
    elapsed = timedelta(hours=hours, minutes=minutes)
    if elapsed <= timedelta(0):
        raise FlightPayloadError("Ryanair flight duration is missing")
    departs_at = datetime.combine(selected_date, departure, ZoneInfo(origin.timezone))
    arrives_at = (departs_at.astimezone(UTC) + elapsed).astimezone(ZoneInfo(destination.timezone))
    if arrives_at.time() != arrival:
        raise FlightPayloadError("Ryanair flight times and duration disagree")
    return RawFlightSegment(
        departure_iata=origin.iata,
        arrival_iata=destination.iata,
        departs_at=departs_at.isoformat(),
        arrives_at=arrives_at.isoformat(),
        service_number=flight_number,
        operator="Ryanair",
    ), decimal_minor_units(value[1].replace(",", "."))


def _flight_time(card: Element, role: str, airport_ref: str) -> time:
    endpoint = _only(card.findall(f".//*[@data-ref='flight-segment.{role}']"), role)
    _only(endpoint.findall(f".//*[@data-ref='{airport_ref}']"), "airport")
    value = _text(_only(_with_class(endpoint, "flight-info__hour"), "flight time"))
    if not _CLOCK.fullmatch(value):
        raise FlightPayloadError("Ryanair flight time is invalid")
    return time.fromisoformat(value)


def _with_class(node: Element, class_name: str) -> list[Element]:
    return [
        element for element in node.iter()
        if class_name in element.get("class", "").split()
    ]


def _only(elements: list[Element], label: str) -> Element:
    if len(elements) != 1:
        raise FlightPayloadError(f"Ryanair did not show one {label}")
    return elements[0]


def _text(node: Element) -> str:
    return " ".join(" ".join(node.itertext()).split())
