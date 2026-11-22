#!/usr/bin/env python3
"""Build Crowl's FlixBus route catalog from ordinary public HTML pages.

The command intentionally does not call a private or public JSON API.  It
paces requests to FlixBus's public Paris-to-city marketing pages, records an
explicit result for every editorial city, and emits only routes whose
advertised minimum duration is within Crowl's 12-hour per-direction rule.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import UTC, date, datetime, timedelta
from html import unescape
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dateutil.relativedelta import relativedelta


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BASE_URL = "https://global.flixbus.com/bus-routes/bus-paris-{slug}"
SOURCE_HOST = "global.flixbus.com"
PARIS_CITY_ID = "40de8964-8646-11e6-9066-549f350fcb0c"
PARIS_TERMINAL_ID = "par-bercy-seine-bus"
PARIS_STOP_NAME = "Paris (Bercy Seine)"
MAX_DURATION_MINUTES = 12 * 60
DEFAULT_DELAY_SECONDS = 5.0
USER_AGENT = "Crowl/1.0 route-catalog builder (ordinary public HTML)"
_SEARCH_MASK = re.compile(
    r'injectSearchMask\(\s*"en"\s*,\s*"(?P<origin>[0-9a-f-]+)"\s*,'
    r'\s*"(?P<destination>[0-9a-f-]+)"',
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"Min\.\s*trip\s*duration.*?"
    r"city-connection-trip-overview-card-title[^>]*>\s*(?P<duration>[^<]+)",
    re.IGNORECASE | re.DOTALL,
)
_ACCORDION = re.compile(
    r'<div\s+class="flix-accordion"\s+data-stop-id="(?P<id>[0-9a-f-]+)"'
    r"(?P<body>.*?)(?=<div\s+class=\"flix-accordion\"|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_TITLE = re.compile(r"<h3\b[^>]*>(?P<title>.*?)</h3>", re.IGNORECASE | re.DOTALL)
_COORDINATES = re.compile(
    r"google\.com/maps/place/(?P<lat>-?[0-9]+(?:\.[0-9]+)?),"
    r"(?P<lon>-?[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_DURATION_PART = re.compile(
    r"(?P<value>[0-9]+)\s*(?P<unit>days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m)\b",
    re.IGNORECASE,
)
_GENERIC_ROUTES_CANONICAL = re.compile(
    r'<link\s+rel="canonical"\s+href="https://global\.flixbus\.com/bus-routes/?"\s*/?>',
    re.IGNORECASE,
)

SLUG_OVERRIDES = {
    "luxembourg-city": ("luxembourg",),
    "freiburg-im-breisgau": ("freiburg",),
}
PRIMARY_STOP_OVERRIDES = {
    # These two exact stops were already verified against live rendered fare
    # cards during the guarded activation; keep that stronger evidence.
    "amsterdam-nl": "Amsterdam Sloterdijk",
    "brussels-be": "Brussels Gare du Midi",
}
VERIFIED_RENDERED_STOP_ALIASES = {
    "Amsterdam Sloterdijk": "ams-sloterdijk-bus",
    "Brussels South (Gare du Midi)": "bru-gare-du-midi-bus",
    PARIS_STOP_NAME: PARIS_TERMINAL_ID,
}


class RoutePageError(RuntimeError):
    """The public page did not expose the minimum route contract Crowl needs."""


def _plain_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")


def _candidate_slugs(city: dict[str, Any]) -> tuple[str, ...]:
    slug = city["slug"]
    candidates = [slug, *SLUG_OVERRIDES.get(slug, ()), _plain_slug(city["name"])]
    return tuple(dict.fromkeys(item for item in candidates if item))


def _clean_html_text(raw: str) -> str:
    return _SPACE.sub(" ", unescape(_TAG.sub(" ", raw))).strip()


def _duration_minutes(raw: str) -> int:
    total = 0
    matches = tuple(_DURATION_PART.finditer(_clean_html_text(raw)))
    if not matches:
        raise RoutePageError("minimum duration is not machine-readable")
    for match in matches:
        value = int(match.group("value"))
        unit = match.group("unit").casefold()
        if unit.startswith("d"):
            total += value * 24 * 60
        elif unit.startswith("h"):
            total += value * 60
        else:
            total += value
    if total <= 0:
        raise RoutePageError("minimum duration must be positive")
    return total


def _section(html: str, section_id: str) -> str:
    marker = re.search(
        rf'<div\b[^>]*\bid="{re.escape(section_id)}"[^>]*>',
        html,
        re.IGNORECASE,
    )
    if marker is None:
        raise RoutePageError(f"missing {section_id}")
    following = html[marker.end() :]
    next_section = re.search(
        r'<div\b[^>]*\bid="(?:first|second)-city-content"[^>]*>',
        following,
        re.IGNORECASE,
    )
    return following[: next_section.start()] if next_section else following


def _stops(section: str) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for match in _ACCORDION.finditer(section):
        body = match.group("body")
        title = _TITLE.search(body)
        if title is None:
            raise RoutePageError("stop is missing its public label")
        coordinates = _COORDINATES.search(body)
        result.append(
            {
                "stop_id": match.group("id").casefold(),
                "name": _clean_html_text(title.group("title")),
                "coordinates": (
                    {
                        "lat": float(coordinates.group("lat")),
                        "lon": float(coordinates.group("lon")),
                    }
                    if coordinates is not None
                    else None
                ),
            }
        )
    if not result:
        raise RoutePageError("destination has no public stop entries")
    stop_ids = [item["stop_id"] for item in result]
    if len(stop_ids) != len(set(stop_ids)):
        raise RoutePageError("destination exposes duplicate stop IDs")
    return tuple(result)


def parse_route_page(html: str) -> dict[str, Any]:
    mask = _SEARCH_MASK.search(html)
    duration = _DURATION.search(html)
    if mask is None or duration is None:
        raise RoutePageError("route identity or minimum duration is missing")
    origin = mask.group("origin").casefold()
    destination = mask.group("destination").casefold()
    if origin == PARIS_CITY_ID:
        city_id = destination
        city_section = "second-city-content"
        public_page_direction = "paris_to_city"
    elif destination == PARIS_CITY_ID:
        city_id = origin
        city_section = "first-city-content"
        public_page_direction = "city_to_paris"
    else:
        raise RoutePageError("route page does not include Paris")
    return {
        "destination_city_id": city_id,
        "minimum_duration_minutes": _duration_minutes(duration.group("duration")),
        "stops": _stops(_section(html, city_section)),
        "public_page_direction": public_page_direction,
    }


def _distance_km(left: dict[str, float], right: dict[str, float]) -> float:
    left_lat = radians(left["lat"])
    right_lat = radians(right["lat"])
    delta_lat = right_lat - left_lat
    delta_lon = radians(right["lon"] - left["lon"])
    a = sin(delta_lat / 2) ** 2 + cos(left_lat) * cos(right_lat) * sin(delta_lon / 2) ** 2
    return 2 * 6371 * asin(min(1, sqrt(a)))


def _select_primary_stop(
    stops: tuple[dict[str, Any], ...], city: dict[str, Any]
) -> dict[str, Any]:
    override = PRIMARY_STOP_OVERRIDES.get(city["id"])
    if override is not None:
        selected = next((item for item in stops if item["name"] == override), None)
        if selected is None:
            raise RoutePageError(
                f"verified primary stop disappeared for {city['id']}: {override}"
            )
        return selected
    # FlixBus publishes its primary city stop first. This is better route
    # evidence than guessing from distance to an editorial city coordinate.
    return stops[0]


def _fetch(url: str, timeout_seconds: float) -> tuple[int, str]:
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise RoutePageError(f"unexpected content type {content_type}")
            return response.status, response.read().decode("utf-8", errors="strict")
    except HTTPError as error:
        if error.code == 404:
            return 404, ""
        raise


class PacedFetcher:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds
        self._next_start = 0.0

    def fetch(self, url: str, timeout_seconds: float) -> tuple[int, str]:
        wait = self._next_start - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_start = time.monotonic() + self._delay_seconds
        return _fetch(url, timeout_seconds)


def _discover_city(
    city: dict[str, Any], *, timeout_seconds: float, cache_dir: Path | None,
    fetcher: PacedFetcher,
) -> dict[str, Any]:
    attempts: list[str] = []
    missing_trip_summary = False
    last_schema_error: str | None = None
    for slug in _candidate_slugs(city):
        candidates = (
            (
                BASE_URL.format(slug=slug),
                cache_dir / f"{city['id']}--{slug}.html" if cache_dir else None,
            ),
            (
                f"https://global.flixbus.com/bus-routes/bus-{slug}-paris",
                cache_dir / f"{city['id']}--{slug}--reverse.html"
                if cache_dir
                else None,
            ),
        )
        for url, cache_path in candidates:
            attempts.append(url)
            try:
                if cache_path is not None and cache_path.is_file():
                    html = cache_path.read_text(encoding="utf-8")
                    status = 200
                else:
                    status, html = fetcher.fetch(url, timeout_seconds)
                    if status == 200 and cache_path is not None:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_text(html, encoding="utf-8")
            except (OSError, UnicodeError, URLError) as error:
                return {
                    "city_id": city["id"],
                    "status": "request_failed",
                    "attempted_urls": attempts,
                    "diagnostic": type(error).__name__,
                }
            if status == 404:
                continue
            try:
                parsed = parse_route_page(html)
            except RoutePageError as error:
                # FlixBus redirects some non-existent SEO route URLs to its
                # route explorer with HTTP 200. Its canonical link is the
                # unambiguous public HTML signal that this is not a route.
                if _GENERIC_ROUTES_CANONICAL.search(html):
                    continue
                if (
                    _SEARCH_MASK.search(html) is not None
                    and _DURATION.search(html) is None
                ):
                    missing_trip_summary = True
                    continue
                last_schema_error = str(error)
                continue
            primary_stop = _select_primary_stop(parsed["stops"], city)
            minimum = parsed["minimum_duration_minutes"]
            return {
                "city_id": city["id"],
                "status": "supported" if minimum <= MAX_DURATION_MINUTES else "duration_exceeds_limit",
                "route_url": url,
                "public_page_direction": parsed["public_page_direction"],
                "provider_city_id": parsed["destination_city_id"],
                "minimum_duration_minutes": minimum,
                "primary_stop": primary_stop,
                "stop_count": len(parsed["stops"]),
            }
    result = {
        "city_id": city["id"],
        "status": (
            "no_current_trip_summary"
            if missing_trip_summary
            else ("schema_error" if last_schema_error else "no_public_route_page")
        ),
        "attempted_urls": attempts,
    }
    if last_schema_error:
        result["diagnostic"] = last_schema_error
    return result


def _existing_bus_terminal(
    terminals: dict[str, Any], city_id: str, stop: dict[str, Any]
) -> str | None:
    terminal_by_id = {item["id"]: item for item in terminals["terminals"]}
    linked = [
        terminal_by_id[link["terminalId"]]
        for link in terminals["cityTerminalLinks"]
        if link["cityId"] == city_id
        and terminal_by_id[link["terminalId"]]["kind"] == "bus"
    ]
    exact = next(
        (item for item in linked if item["name"].casefold() == stop["name"].casefold()),
        None,
    )
    if exact is not None:
        return exact["id"]
    if stop["coordinates"] is None:
        return None
    nearby = [
        item
        for item in linked
        if _distance_km(item["coordinates"], stop["coordinates"]) <= 0.75
    ]
    return nearby[0]["id"] if len(nearby) == 1 else None


def _terminal_id(stop_id: str) -> str:
    return f"flix-{stop_id}"


def build_catalog(
    *,
    cities: list[dict[str, Any]],
    terminals: dict[str, Any],
    discovered: list[dict[str, Any]],
    observed_on: date,
) -> tuple[dict[str, Any], dict[str, Any]]:
    city_by_id = {item["id"]: item for item in cities}
    generated_terminal_ids = {
        item["id"]
        for item in terminals["terminals"]
        if item["id"].startswith("flix-")
        and "flixbus_stop_id" in item.get("codes", {})
    }
    terminals["terminals"] = [
        item
        for item in terminals["terminals"]
        if item["id"] not in generated_terminal_ids
    ]
    terminals["cityTerminalLinks"] = [
        item
        for item in terminals["cityTerminalLinks"]
        if item["terminalId"] not in generated_terminal_ids
    ]
    terminal_ids = {item["id"] for item in terminals["terminals"]}
    links = {(item["cityId"], item["terminalId"]) for item in terminals["cityTerminalLinks"]}
    aliases: dict[str, str] = {PARIS_TERMINAL_ID: PARIS_CITY_ID}
    stop_labels: dict[str, str] = dict(VERIFIED_RENDERED_STOP_ALIASES)
    pairs: list[list[str]] = []
    routes: list[dict[str, Any]] = []

    for result in discovered:
        if result["status"] != "supported":
            continue
        city = city_by_id[result["city_id"]]
        stop = result["primary_stop"]
        terminal_id = _existing_bus_terminal(terminals, city["id"], stop)
        if terminal_id is None:
            terminal_id = _terminal_id(stop["stop_id"])
            if terminal_id not in terminal_ids:
                terminals["terminals"].append(
                    {
                        "id": terminal_id,
                        "name": stop["name"],
                        "kind": "bus",
                        "timezone": city["timezone"],
                        "countryCode": city["country"]["code"],
                        "coordinates": stop["coordinates"] or city["coordinates"],
                        "codes": {"flixbus_stop_id": stop["stop_id"]},
                        "active": True,
                        "schengenEligible": True,
                    }
                )
                terminal_ids.add(terminal_id)
        link = (city["id"], terminal_id)
        if link not in links:
            terminals["cityTerminalLinks"].append(
                {
                    "cityId": city["id"],
                    "terminalId": terminal_id,
                    "relationship": "primary_bus",
                }
            )
            links.add(link)
        aliases[terminal_id] = result["provider_city_id"]
        if stop["name"] in stop_labels and stop_labels[stop["name"]] != terminal_id:
            raise RoutePageError(f"ambiguous public stop label: {stop['name']}")
        stop_labels[stop["name"]] = terminal_id
        pairs.append([PARIS_TERMINAL_ID, terminal_id])
        routes.append(
            {
                "city_id": city["id"],
                "terminal_id": terminal_id,
                "provider_city_id": result["provider_city_id"],
                "public_stop_id": stop["stop_id"],
                "public_stop_name": stop["name"],
                "minimum_duration_minutes": result["minimum_duration_minutes"],
                "route_url": result["route_url"],
            }
        )

    horizon = observed_on + relativedelta(months=6)
    offset = (4 - horizon.weekday()) % 7
    last_friday = horizon + timedelta(days=offset - 7 if offset else 0)
    sellable_through = last_friday + timedelta(days=3)
    route_catalog = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "observed_on": observed_on.isoformat(),
        "provenance": {
            "method": "ordinary_public_html",
            "host": SOURCE_HOST,
            "request_interval_seconds": DEFAULT_DELAY_SECONDS,
            "maximum_direction_minutes": MAX_DURATION_MINUTES,
        },
        "summary": {
            "catalog_cities": len(cities),
            "supported_routes": len(routes),
            "duration_exceeds_limit": sum(
                item["status"] == "duration_exceeds_limit" for item in discovered
            ),
            "no_public_route_page": sum(
                item["status"] == "no_public_route_page" for item in discovered
            ),
            "no_current_trip_summary": sum(
                item["status"] == "no_current_trip_summary" for item in discovered
            ),
            "discovery_failures": sum(
                item["status"] in {"request_failed", "schema_error"}
                for item in discovered
            ),
        },
        "terminal_city_ids": dict(sorted(aliases.items())),
        "stop_labels": dict(sorted(stop_labels.items())),
        "coverage": {
            "supported_terminal_pairs": sorted(pairs),
            "sellable_through": sellable_through.isoformat(),
        },
        "routes": sorted(routes, key=lambda item: item["city_id"]),
        "city_results": sorted(discovered, key=lambda item: item["city_id"]),
    }
    terminals["terminals"].sort(key=lambda item: item["id"])
    return route_catalog, terminals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--terminals-output", type=Path, required=True)
    parser.add_argument("--observed-on", type=date.fromisoformat, default=date.today())
    parser.add_argument("--delay-seconds", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    if args.delay_seconds < DEFAULT_DELAY_SECONDS:
        parser.error(f"--delay-seconds must be at least {DEFAULT_DELAY_SECONDS}")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    city_payload = json.loads((DATA / "crowl-cities-150.json").read_text(encoding="utf-8"))
    terminal_payload = json.loads((DATA / "terminals.json").read_text(encoding="utf-8"))
    cities = city_payload["cities"]
    discovered: list[dict[str, Any]] = []
    fetcher = PacedFetcher(args.delay_seconds)
    for index, city in enumerate(cities, start=1):
        result = _discover_city(
            city,
            timeout_seconds=args.timeout_seconds,
            cache_dir=args.cache_dir,
            fetcher=fetcher,
        )
        discovered.append(result)
        print(
            f"[{index:03d}/{len(cities):03d}] {city['id']}: {result['status']}",
            file=sys.stderr,
            flush=True,
        )

    catalog, updated_terminals = build_catalog(
        cities=cities,
        terminals=terminal_payload,
        discovered=discovered,
        observed_on=args.observed_on,
    )
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.terminals_output.write_text(
        json.dumps(updated_terminals, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(catalog["summary"], sort_keys=True))
    return 0 if catalog["summary"]["discovery_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
