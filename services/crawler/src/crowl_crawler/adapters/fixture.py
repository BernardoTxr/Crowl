from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .base import SourceAdapter
from ..models import (
    AdapterMetadata,
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
    TransportMode,
)


_CITY_MODE: dict[str, TransportMode] = {
    "amsterdam-nl": TransportMode.TRAIN,
    "lisbon-pt": TransportMode.FLIGHT,
    "vienna-at": TransportMode.FLIGHT,
    "reykjavik-is": TransportMode.FLIGHT,
}

_TERMINAL_NAMES: dict[str, str] = {
    "par-cdg": "Paris Charles de Gaulle Airport",
    "par-ory": "Paris Orly Airport",
    "ams-schiphol": "Amsterdam Airport Schiphol",
    "lis-humberto-delgado": "Lisbon Humberto Delgado Airport",
    "vie-schwechat": "Vienna International Airport",
    "kef-keflavik": "Keflavik International Airport",
    "par-gare-du-nord": "Paris Gare du Nord",
    "ams-centraal": "Amsterdam Centraal",
}

_TERMINAL_TIMEZONES: dict[str, str] = {
    "par-cdg": "Europe/Paris",
    "par-ory": "Europe/Paris",
    "ams-schiphol": "Europe/Amsterdam",
    "lis-humberto-delgado": "Europe/Lisbon",
    "vie-schwechat": "Europe/Vienna",
    "kef-keflavik": "Atlantic/Reykjavik",
    "par-gare-du-nord": "Europe/Paris",
    "ams-centraal": "Europe/Amsterdam",
}


class FixtureAdapter(SourceAdapter):
    """Deterministic provider used to exercise normalization and the batch ledger."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._closed = False
        self._metadata = AdapterMetadata(
            source_slug="fixture",
            display_name="Crowl deterministic fixture",
            modes=(TransportMode.FLIGHT, TransportMode.TRAIN, TransportMode.BUS),
            strategy=ExtractionStrategy.FIXTURE,
            intended_strategy=ExtractionStrategy.FIXTURE,
            connector_version="1.0.0",
            parser_version="fixture-v1",
            enabled=True,
            disabled_reason=None,
            access_basis="local deterministic fixture; no network access",
            markets=("schengen", "test"),
            sale_horizon_days=365,
        )

    def metadata(self) -> AdapterMetadata:
        return self._metadata

    async def discover_coverage(self) -> CoverageDiscovery:
        self._ensure_open()
        return CoverageDiscovery(
            source_slug="fixture",
            supported_terminal_pairs=(
                ("par-gare-du-nord", "ams-centraal"),
                ("par-cdg", "lis-humberto-delgado"),
                ("par-cdg", "vie-schwechat"),
                ("par-cdg", "kef-keflavik"),
            ),
            sellable_through=self._clock().date() + timedelta(days=365),
            observed_at=self._clock(),
            diagnostic_code="FIXTURE_COVERAGE_OK",
        )

    async def canary(
        self, requests: Sequence[SearchRequest]
    ) -> tuple[ConnectorResult, ...]:
        return tuple([await self.search(request) for request in requests])

    async def search(self, request: SearchRequest) -> ConnectorResult:
        self._ensure_open()
        observed_at = self._clock()
        mode = _CITY_MODE.get(request.destination_city_id)
        if mode is None:
            return ConnectorResult(
                outcome=SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE,
                retryable=False,
                diagnostic_code="FIXTURE_ROUTE_UNSUPPORTED",
                expected_count=1,
                completed_count=1,
                offers=(),
                parser_version=self._metadata.parser_version,
                observed_at=observed_at,
            )

        outbound_origin, return_origin, destination = self._select_terminals(
            request, mode
        )
        duration = {
            ("amsterdam-nl", TransportMode.TRAIN): timedelta(hours=3, minutes=24),
            ("lisbon-pt", TransportMode.FLIGHT): timedelta(hours=2, minutes=35),
            ("vienna-at", TransportMode.FLIGHT): timedelta(hours=1, minutes=55),
            ("reykjavik-is", TransportMode.FLIGHT): timedelta(hours=3, minutes=40),
        }.get((request.destination_city_id, mode), timedelta(hours=2))

        outbound_departure = request.outbound_window.starts_at + timedelta(hours=2)
        outbound_arrival = (outbound_departure.astimezone(UTC) + duration).astimezone(
            _terminal_timezone(destination)
        )
        outbound = Segment(
            sequence=1,
            mode=mode,
            operator="Crowl Fixture Rail"
            if mode is TransportMode.TRAIN
            else "Crowl Fixture Air",
            service_number="CF100",
            departure_terminal_id=outbound_origin,
            departure_terminal_name=_terminal_name(outbound_origin),
            arrival_terminal_id=destination,
            arrival_terminal_name=_terminal_name(destination),
            departs_at=outbound_departure,
            arrives_at=outbound_arrival,
        )

        return_segments: tuple[Segment, ...] = ()
        if request.search_kind is SearchKind.ROUND_TRIP:
            assert request.return_window is not None
            return_arrival = request.return_window.ends_at - timedelta(minutes=45)
            return_departure = (return_arrival.astimezone(UTC) - duration).astimezone(
                _terminal_timezone(destination)
            )
            return_segments = (
                Segment(
                    sequence=1,
                    mode=mode,
                    operator=(
                        "Crowl Fixture Rail"
                        if mode is TransportMode.TRAIN
                        else "Crowl Fixture Air"
                    ),
                    service_number="CF101",
                    departure_terminal_id=destination,
                    departure_terminal_name=_terminal_name(destination),
                    arrival_terminal_id=return_origin,
                    arrival_terminal_name=_terminal_name(return_origin),
                    departs_at=return_departure,
                    arrives_at=return_arrival,
                ),
            )
        elif request.direction is Direction.RETURN:
            # For a directional return request the supplied window describes the
            # actual direction; expose it as the outbound tuple for composition.
            return_destination = return_origin
            directional_departure = (
                request.outbound_window.starts_at + timedelta(hours=1)
            ).astimezone(_terminal_timezone(destination))
            directional_arrival = (
                directional_departure.astimezone(UTC) + duration
            ).astimezone(_terminal_timezone(return_destination))
            outbound = Segment(
                sequence=1,
                mode=mode,
                operator=outbound.operator,
                service_number="CF101",
                departure_terminal_id=destination,
                departure_terminal_name=_terminal_name(destination),
                arrival_terminal_id=return_destination,
                arrival_terminal_name=_terminal_name(return_destination),
                departs_at=directional_departure,
                arrives_at=directional_arrival,
            )

        digest = hashlib.sha256(
            f"{request.destination_city_id}:{request.weekend_start.isoformat()}".encode()
        ).hexdigest()
        amount_minor = 5_900 + int(digest[:4], 16) % 16_000
        offer_id = f"fixture-{request.fingerprint[:16]}"
        booking_url = f"https://example.invalid/crowl-fixture/{offer_id}"
        component = BookingComponent(
            seller="Crowl deterministic fixture",
            source_slug="fixture",
            amount_minor=amount_minor,
            currency="EUR",
            booking_url=booking_url,
            fare_name="Fixture Light",
            mandatory_fees_included=True,
            passenger_age=request.passenger.age,
            passenger_student=request.passenger.student,
            passenger_eligibility_confirmed=True,
            bookable=True,
            is_teaser=False,
            student_fare_applied=(mode is TransportMode.TRAIN),
        )
        offer = NormalizedOffer(
            provider_offer_id=offer_id,
            source_slug="fixture",
            destination_city_id=request.destination_city_id,
            pricing_semantics=(
                PricingSemantics.ROUND_TRIP_ONLY
                if return_segments
                else PricingSemantics.ONE_WAY
            ),
            price_completeness=PriceCompleteness.COMPLETE,
            total_amount_minor=amount_minor,
            currency="EUR",
            mandatory_fees_included=True,
            passenger_age=request.passenger.age,
            passenger_student=request.passenger.student,
            passenger_eligibility_confirmed=True,
            bookable=True,
            is_teaser=False,
            observed_at=observed_at,
            outbound_segments=(outbound,),
            return_segments=return_segments,
            booking_components=(component,),
            expires_at=observed_at + timedelta(minutes=20),
        )
        response_fingerprint = hashlib.sha256(
            f"fixture-v1:{request.fingerprint}:{amount_minor}".encode()
        ).hexdigest()
        schema_fingerprint = hashlib.sha256(
            b"fixture-v1:provider_offer_id,total,segments,booking_url"
        ).hexdigest()
        return ConnectorResult(
            outcome=SearchOutcome.SUCCESS_RESULTS,
            retryable=False,
            diagnostic_code="FIXTURE_RESULTS_OK",
            expected_count=1,
            completed_count=1,
            offers=(offer,),
            parser_version=self._metadata.parser_version,
            observed_at=observed_at,
            response_fingerprint=response_fingerprint,
            schema_fingerprint=schema_fingerprint,
        )

    def _select_terminals(
        self, request: SearchRequest, mode: TransportMode
    ) -> tuple[str, str, str]:
        if mode is TransportMode.TRAIN:
            outbound_origin = _preferred(
                request.origin_terminal_ids, "par-gare-du-nord"
            )
        else:
            outbound_origin = _preferred(request.origin_terminal_ids, "par-cdg")
        return_origin = _preferred(request.origin_terminal_ids, "par-ory")
        if mode is TransportMode.TRAIN:
            return_origin = outbound_origin
        destination = request.destination_terminal_ids[0]
        return outbound_origin, return_origin, destination

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("fixture adapter is closed")

    async def close(self) -> None:
        self._closed = True


def _preferred(values: tuple[str, ...], preferred: str) -> str:
    return preferred if preferred in values else values[0]


def _terminal_name(terminal_id: str) -> str:
    return _TERMINAL_NAMES.get(terminal_id, terminal_id.replace("-", " ").title())


def _terminal_timezone(terminal_id: str) -> ZoneInfo:
    return ZoneInfo(_TERMINAL_TIMEZONES.get(terminal_id, "Europe/Paris"))
