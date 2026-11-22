#!/usr/bin/env python3
"""Classify Crowl's 150 cities from Ryanair's ordinary public route HTML.

This inventory never calls a Ryanair API.  It probes the official public SEO
route page for each catalog city (plus a small, explicit nearby-airport alias
set), parses its schema.org Flight records, and updates the version-controlled
airport/coverage catalogs used by the live booking-page extractor.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SOURCE_HOST = "www.ryanair.com"
ORIGIN_TERMINAL_ID = "par-bva"
ORIGIN_IATA = "BVA"
BASE_URL = (
    "https://www.ryanair.com/gb/en/flights/"
    "flights-from-paris-beauvais-to-{slug}"
)
DEFAULT_DELAY_SECONDS = 2.0
USER_AGENT = "Crowl/0.1 public-route-catalog"

# Nearby airports are allowed by Crowl's business rules.  These aliases are
# deliberately explicit so a route is never inferred merely from proximity.
NEARBY_SLUGS: dict[str, tuple[str, ...]] = {
    "brasov-ro": ("bucharest",),
    "coimbra-pt": ("porto",),
    "cordoba-es": ("seville",),
    "eger-hu": ("budapest",),
    "florence-it": ("pisa",),
    "kalambaka-gr": ("thessaloniki",),
    "lagos-pt": ("faro",),
    "lucca-it": ("pisa",),
    "pecs-hu": ("budapest",),
    "piran-si": ("trieste",),
    "rovinj-hr": ("pula",),
    "siena-it": ("pisa",),
    "sigulda-lv": ("riga",),
    "sintra-pt": ("lisbon",),
    "tartu-ee": ("tallinn",),
    "toledo-es": ("madrid",),
    "trakai-lt": ("vilnius",),
    "venice-it": ("venice-treviso",),
    "zakopane-pl": ("krakow",),
}

# A live route page can temporarily contain no priced schema.org Flight nodes
# even though its official heading and schedule copy confirm the route.  Keep
# the airport identity explicit instead of deriving an IATA code from prose.
ROUTE_AIRPORT_OVERRIDES: dict[str, tuple[str, str]] = {
    "sofia": ("SOF", "Sofia Airport"),
}


class RoutePageError(RuntimeError):
    """The public page did not expose a consistent direct route."""


class _StructuredData(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capture = False
        self._chunks: list[str] = []
        self.documents: list[object] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "script" and attributes.get("type", "").casefold() == "application/ld+json":
            self._capture = True
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "script" or not self._capture:
            return
        self._capture = False
        raw = "".join(self._chunks).strip()
        if raw:
            try:
                self.documents.append(json.loads(raw))
            except json.JSONDecodeError as error:
                raise RoutePageError("route structured data is invalid") from error


def _plain_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")


def _candidate_slugs(city: dict[str, Any]) -> tuple[str, ...]:
    values = [city["slug"], _plain_slug(city["name"]), *NEARBY_SLUGS.get(city["id"], ())]
    return tuple(dict.fromkeys(item for item in values if item))


def _flight_nodes(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            result.extend(_flight_nodes(item))
        return result
    if not isinstance(value, dict):
        return []
    result = [value] if value.get("@type") == "Flight" else []
    graph = value.get("@graph")
    if graph is not None:
        result.extend(_flight_nodes(graph))
    return result


def parse_route_page(html: str) -> dict[str, str]:
    parser = _StructuredData()
    parser.feed(html)
    parser.close()
    flights = [
        item
        for document in parser.documents
        for item in _flight_nodes(document)
        if isinstance(item.get("departureAirport"), dict)
        and item["departureAirport"].get("iataCode") == ORIGIN_IATA
    ]
    if not flights:
        raise RoutePageError("route page has no Paris-Beauvais Flight record")
    arrivals = {
        (
            str(item["arrivalAirport"].get("iataCode", "")).upper(),
            str(item["arrivalAirport"].get("name", "")).strip(),
        )
        for item in flights
        if isinstance(item.get("arrivalAirport"), dict)
    }
    if len(arrivals) != 1:
        raise RoutePageError("route page has ambiguous destination airports")
    iata, name = arrivals.pop()
    if not re.fullmatch(r"[A-Z]{3}", iata) or not name:
        raise RoutePageError("route page destination airport is incomplete")
    return {"arrival_iata": iata, "arrival_airport_name": name}


def _fetch(url: str, timeout_seconds: float) -> tuple[int, str]:
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
                raise RoutePageError("route page is not HTML")
            return response.status, response.read(2_000_001).decode("utf-8", errors="strict")
    except HTTPError as error:
        if error.code == 404:
            return 404, ""
        raise


class PacedFetcher:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds
        self._next_start = 0.0
        self._cache: dict[str, tuple[int, str]] = {}

    def fetch(self, url: str, timeout_seconds: float) -> tuple[int, str]:
        if url in self._cache:
            return self._cache[url]
        wait = self._next_start - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_start = time.monotonic() + self._delay_seconds
        result = _fetch(url, timeout_seconds)
        self._cache[url] = result
        return result


def discover_city(
    city: dict[str, Any], *, fetcher: PacedFetcher, timeout_seconds: float
) -> dict[str, Any]:
    attempted: list[str] = []
    schema_error: str | None = None
    for slug in _candidate_slugs(city):
        url = BASE_URL.format(slug=slug)
        attempted.append(url)
        try:
            status, html = fetcher.fetch(url, timeout_seconds)
        except (OSError, UnicodeError, URLError) as error:
            return {
                "city_id": city["id"],
                "status": "request_failed",
                "attempted_urls": attempted,
                "diagnostic": type(error).__name__,
            }
        if status == 404:
            continue
        try:
            parsed = parse_route_page(html)
        except RoutePageError as error:
            override = ROUTE_AIRPORT_OVERRIDES.get(slug)
            if override is not None:
                parsed = {
                    "arrival_iata": override[0],
                    "arrival_airport_name": override[1],
                }
            else:
                schema_error = str(error)
                continue
        return {
            "city_id": city["id"],
            "status": "supported",
            "route_url": url,
            "public_destination_slug": slug,
            "relationship": (
                "primary_airport" if slug in {city["slug"], _plain_slug(city["name"])} else "nearby_airport"
            ),
            **parsed,
        }
    result: dict[str, Any] = {
        "city_id": city["id"],
        "status": "schema_error" if schema_error else "no_public_route_page",
        "attempted_urls": attempted,
    }
    if schema_error:
        result["diagnostic"] = schema_error
    return result


def build_catalog(
    *, cities: list[dict[str, Any]], terminals: dict[str, Any], discovered: list[dict[str, Any]], observed_on: date
) -> tuple[dict[str, Any], dict[str, Any]]:
    city_by_id = {item["id"]: item for item in cities}
    generated = {
        item["id"]
        for item in terminals["terminals"]
        if item["id"].startswith("ryr-") and "ryanair_iata" in item.get("codes", {})
    }
    terminals["terminals"] = [item for item in terminals["terminals"] if item["id"] not in generated]
    terminals["cityTerminalLinks"] = [
        item for item in terminals["cityTerminalLinks"] if item["terminalId"] not in generated
    ]
    by_iata = {
        item.get("codes", {}).get("iata", "").upper(): item
        for item in terminals["terminals"]
        if item["kind"] == "airport" and item.get("codes", {}).get("iata")
    }
    links = {(item["cityId"], item["terminalId"]) for item in terminals["cityTerminalLinks"]}
    routes: list[dict[str, Any]] = []
    pairs: set[tuple[str, str]] = set()
    for result in discovered:
        if result["status"] != "supported":
            continue
        city = city_by_id[result["city_id"]]
        iata = result["arrival_iata"]
        terminal = by_iata.get(iata)
        if terminal is None:
            terminal = {
                "id": f"ryr-{iata.casefold()}",
                "name": result["arrival_airport_name"],
                "kind": "airport",
                "timezone": city["timezone"],
                "countryCode": city["country"]["code"],
                "coordinates": city["coordinates"],
                "codes": {"iata": iata, "ryanair_iata": iata},
                "active": True,
                "schengenEligible": True,
            }
            terminals["terminals"].append(terminal)
            by_iata[iata] = terminal
        link = (city["id"], terminal["id"])
        if link not in links:
            terminals["cityTerminalLinks"].append(
                {"cityId": city["id"], "terminalId": terminal["id"], "relationship": result["relationship"]}
            )
            links.add(link)
        pairs.add((ORIGIN_TERMINAL_ID, terminal["id"]))
        routes.append(
            {
                "city_id": city["id"],
                "terminal_id": terminal["id"],
                "arrival_iata": iata,
                "arrival_airport_name": result["arrival_airport_name"],
                "public_destination_slug": result["public_destination_slug"],
                "relationship": result["relationship"],
                "route_url": result["route_url"],
            }
        )
    catalog = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "observed_on": observed_on.isoformat(),
        "provenance": {
            "method": "ordinary_public_html_schema_org",
            "host": SOURCE_HOST,
            "request_interval_seconds": DEFAULT_DELAY_SECONDS,
        },
        "summary": {
            "catalog_cities": len(cities),
            "supported_cities": len(routes),
            "no_public_route_page": sum(item["status"] == "no_public_route_page" for item in discovered),
            "discovery_failures": sum(item["status"] in {"request_failed", "schema_error"} for item in discovered),
        },
        "coverage": {
            "supported_terminal_pairs": [list(item) for item in sorted(pairs)],
            "sellable_through": (observed_on + timedelta(days=210)).isoformat(),
        },
        "routes": sorted(routes, key=lambda item: item["city_id"]),
        "city_results": sorted(discovered, key=lambda item: item["city_id"]),
    }
    terminals["terminals"].sort(key=lambda item: item["id"])
    terminals["cityTerminalLinks"].sort(key=lambda item: (item["cityId"], item["terminalId"]))
    return catalog, terminals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--terminals-output", type=Path, required=True)
    parser.add_argument("--observed-on", type=date.fromisoformat, default=date.today())
    parser.add_argument("--delay-seconds", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    if args.delay_seconds < DEFAULT_DELAY_SECONDS:
        parser.error(f"--delay-seconds must be at least {DEFAULT_DELAY_SECONDS}")
    cities = json.loads((DATA / "crowl-cities-150.json").read_text(encoding="utf-8"))["cities"]
    terminals = json.loads((DATA / "terminals.json").read_text(encoding="utf-8"))
    fetcher = PacedFetcher(args.delay_seconds)
    discovered: list[dict[str, Any]] = []
    for index, city in enumerate(cities, start=1):
        result = discover_city(city, fetcher=fetcher, timeout_seconds=args.timeout_seconds)
        discovered.append(result)
        print(f"[{index:03d}/{len(cities):03d}] {city['id']}: {result['status']}", file=sys.stderr, flush=True)
    catalog, updated_terminals = build_catalog(
        cities=cities, terminals=terminals, discovered=discovered, observed_on=args.observed_on
    )
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.terminals_output.write_text(
        json.dumps(updated_terminals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(catalog["summary"], sort_keys=True))
    return 0 if catalog["summary"]["discovery_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
