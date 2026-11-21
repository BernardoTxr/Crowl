from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from urllib.parse import urlencode

import pytest

from crowl_crawler.adapters.catalog import SourceSpec
from crowl_crawler.adapters.providers._ouigo_mcp import OuigoMcpError, _decode_response
from crowl_crawler.adapters.providers.ouigo import OuigoAdapter
from crowl_crawler.manifest import DailyManifestBuilder, DestinationTarget
from crowl_crawler.models import (
    Direction,
    ExtractionStrategy,
    PriceCompleteness,
    PricingSemantics,
    SearchOutcome,
    TransportMode,
)
from crowl_crawler.trust import sanitize_booking_url


PARIS = "87271007"
BRUSSELS = "88140010"
BOOKING_PATH = "https://ventes.ouigo.com/fr-FR/orientation/deep-link/"
OBSERVED_AT = datetime(2026, 11, 21, 12, tzinfo=UTC)


class FakeMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.pages: dict[tuple[str, str, str], dict] = {}

    async def search(self, *, origin: str, destination: str, departure_date: str) -> dict:
        query = (origin, destination, departure_date)
        self.calls.append(query)
        return self.pages.get(query, _page(*query))


def _adapter(client: FakeMcp, *, enabled: bool = True) -> OuigoAdapter:
    spec = SourceSpec(
        slug="ouigo",
        name="OUIGO",
        modes=(TransportMode.TRAIN,),
        intended_strategy=ExtractionStrategy.HTTP_STRUCTURED,
        state="enabled" if enabled else "disabled",
        configured_enabled=enabled,
        kill_switch=not enabled,
        config_version="test",
        disabled_reason="Needs authorization.",
        access_basis="official OUIGO MCP",
        booking_hosts=("ouigo.com",),
    )
    return OuigoAdapter(spec, mcp_client=client, clock=lambda: OBSERVED_AT)


def _requests():
    manifest = DailyManifestBuilder(origin_terminal_ids=("par-gare-du-nord",)).build(
        scheduled_for=date(2026, 11, 21),
        source_slugs=("ouigo",),
        destinations=(DestinationTarget("brussels-be", ("brussels-midi",)),),
        weekend_fridays=(date(2026, 12, 4),),
        include_one_way_tasks=True,
    )
    return tuple(task.request for task in manifest.tasks)


def _page(
    origin: str,
    destination: str,
    day: str,
    *rows: tuple[str, str, str, int],
) -> dict:
    names = {PARIS: "Paris Nord", BRUSSELS: "Bruxelles-Midi"}
    return {
        "searchContext": {
            "origin": names[origin],
            "destination": names[destination],
            "date": day,
            "passengerCount": 1,
        },
        "journeys": [
            {
                "origin": names[origin],
                "destination": names[destination],
                "departureDateTime": f"{day}T{departure}:00+01:00",
                "arrivalDateTime": f"{day}T{arrival}:00+01:00",
                "price": price,
                "deepLink": BOOKING_PATH + "?" + urlencode({
                    "origin": origin,
                    "destination": destination,
                    "outboundDate": day,
                    "target": "routes",
                    "passengers[0][type]": "A",
                    "utm_source": "test",
                    "serviceId": f"0:RDA|{number}|OUIGO_LB|{day}|{day}T{departure}|"
                    f"{day}T{arrival};{origin};{destination}",
                }),
                "serviceModality": "STANDARD_RAIL",
            }
            for number, departure, arrival, price in rows
        ],
    }


def test_ouigo_combines_exact_adult_fares_within_the_weekend() -> None:
    mcp = FakeMcp()
    mcp.pages[(PARIS, BRUSSELS, "2026-12-04")] = _page(
        PARIS, BRUSSELS, "2026-12-04", ("63", "08:16", "11:21", 35),
        ("69", "19:13", "22:21", 45),
    )
    mcp.pages[(BRUSSELS, PARIS, "2026-12-05")] = _page(
        BRUSSELS, PARIS, "2026-12-05", ("50", "07:38", "10:39", 10),
    )
    mcp.pages[(BRUSSELS, PARIS, "2026-12-06")] = _page(
        BRUSSELS, PARIS, "2026-12-06", ("50", "07:38", "10:39", 22),
    )

    reply = asyncio.run(_adapter(mcp).search(_requests()[0]))

    assert reply.outcome is SearchOutcome.SUCCESS_RESULTS
    assert reply.completed_count == reply.expected_count == len(mcp.calls) == 6
    offer, = reply.offers
    assert offer.total_amount_minor == 6700
    assert offer.currency == "EUR"
    assert offer.price_completeness is PriceCompleteness.COMPLETE
    assert offer.mandatory_fees_included
    assert offer.pricing_semantics is PricingSemantics.COMPOSABLE
    assert offer.outbound_segments[0].service_number == "69"
    assert offer.return_segments[0].departure_terminal_id == "brussels-midi"
    for booking in offer.booking_components:
        assert booking.booking_url == sanitize_booking_url(booking.booking_url, ("ouigo.com",))
        assert "serviceId" in booking.booking_url
        assert "utm_source" not in booking.booking_url


def test_ouigo_rejects_wrong_echo_and_wrong_service_identity() -> None:
    request = next(request for request in _requests() if request.direction is Direction.OUTBOUND)
    key = (PARIS, BRUSSELS, "2026-12-04")
    mcp = FakeMcp()
    mcp.pages[key] = _page(*key, ("69", "19:13", "22:21", 45))
    mcp.pages[key]["journeys"][0]["deepLink"] = mcp.pages[key]["journeys"][0]["deepLink"].replace(
        "19%3A13", "19%3A14"
    )
    invalid_fare = asyncio.run(_adapter(mcp).search(request))
    assert invalid_fare.outcome is SearchOutcome.PARSE_SCHEMA_ERROR
    assert not invalid_fare.offers

    mcp.pages[key]["searchContext"]["passengerCount"] = 2
    wrong_search = asyncio.run(_adapter(mcp).search(request))
    assert wrong_search.diagnostic_code == "OUIGO_MCP_SEARCH_CONTEXT"
    assert wrong_search.completed_count == 0


def test_ouigo_searches_exact_return_stations_for_one_way_requests() -> None:
    request = next(request for request in _requests() if request.direction is Direction.RETURN)
    mcp = FakeMcp()
    mcp.pages[(BRUSSELS, PARIS, "2026-12-06")] = _page(
        BRUSSELS, PARIS, "2026-12-06", ("50", "07:38", "10:39", 22),
    )
    mcp.pages[(BRUSSELS, PARIS, "2026-12-07")] = _page(
        BRUSSELS, PARIS, "2026-12-07", ("50", "05:45", "08:45", 10),
    )

    reply = asyncio.run(_adapter(mcp).search(request))

    assert reply.completed_count == reply.expected_count == len(mcp.calls) == 3
    assert all((origin, destination) == (BRUSSELS, PARIS) for origin, destination, _ in mcp.calls)
    offer, = reply.offers
    assert offer.pricing_semantics is PricingSemantics.ONE_WAY
    assert offer.total_amount_minor == 2200
    assert offer.outbound_segments[0].departure_terminal_id == "brussels-midi"


def test_ouigo_accepts_an_explicit_empty_page_echoing_station_codes() -> None:
    request = next(request for request in _requests() if request.direction is Direction.OUTBOUND)
    mcp = FakeMcp()
    key = (PARIS, BRUSSELS, "2026-12-04")
    mcp.pages[key] = _page(*key)
    mcp.pages[key]["searchContext"].update(origin=PARIS, destination=BRUSSELS)

    reply = asyncio.run(_adapter(mcp).search(request))

    assert reply.outcome is SearchOutcome.SUCCESS_EMPTY
    assert reply.completed_count == reply.expected_count == 3


def test_ouigo_skips_disabled_sources_and_invalid_routes_before_network() -> None:
    mcp = FakeMcp()
    request = _requests()[0]
    disabled = asyncio.run(_adapter(mcp, enabled=False).search(request))
    assert disabled.outcome is SearchOutcome.SKIPPED_DISABLED

    unsupported = asyncio.run(
        _adapter(mcp).search(replace(request, destination_terminal_ids=("ams-centraal",)))
    )
    assert unsupported.outcome is SearchOutcome.CONFIGURATION_ERROR
    assert not mcp.calls


def test_ouigo_decodes_official_json_rpc_and_sse_result_envelopes() -> None:
    result = {"result": {"structuredContent": {"journeys": []}}, "jsonrpc": "2.0", "id": 2}
    raw = json.dumps(result)
    assert _decode_response(raw.encode(), "application/json", 2) == result["result"]
    event = f"event: message\r\ndata: {raw}\r\n\r\n"
    assert _decode_response(event.encode(), "text/event-stream", 2) == result["result"]
    with pytest.raises(OuigoMcpError):
        _decode_response(event.encode(), "text/event-stream", 3)
