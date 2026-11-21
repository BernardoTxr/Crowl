"""Assess direct FlixBus weekend service against Crowl's city catalog."""

from __future__ import annotations

import argparse
import csv
import io
import json
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PARIS = ZoneInfo("Europe/Paris")
SOURCE = "https://transport.data.gouv.fr/datasets/flixbus-horaires-theoriques-du-reseau-europeen-1"
DAY = timedelta(days=1)
MAX_DURATION = 12 * 60 * 60
MAX_CITY_DISTANCE_KM = 12
MAX_ORIGIN_DISTANCE_KM = 0.4


def rows(feed: ZipFile, name: str):
    with feed.open(f"{name}.txt") as stream:
        yield from csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))


def distance(lat: float, lon: float, stop: dict[str, str]) -> float:
    stop_lat = radians(float(stop["stop_lat"]))
    lat = radians(lat)
    delta_lon = radians(float(stop["stop_lon"]) - lon)
    a = sin((stop_lat - lat) / 2) ** 2 + cos(stop_lat) * cos(lat) * sin(delta_lon / 2) ** 2
    return 2 * 6371 * asin(min(1, sqrt(a)))


def unix(day: date, at: time, timezone=UTC) -> int:
    return int(datetime.combine(day, at, timezone).timestamp())


def seconds(clock: str) -> int:
    hour, minute, second = map(int, clock.split(":"))
    return hour * 3600 + minute * 60 + second


def assess(feed: ZipFile, first: date, last: date) -> list[dict]:
    if first.weekday() != 4 or last.weekday() != 4 or first > last:
        raise ValueError("Choose first and last Fridays in chronological order.")
    cities = json.loads((ROOT / "data/crowl-cities-150.json").read_text(encoding="utf-8"))["cities"]
    terminals = json.loads((ROOT / "data/terminals.json").read_text(encoding="utf-8"))
    origin_terminals = [
        item for item in terminals["terminals"]
        if item["id"] in terminals["originTerminalIds"] and item["kind"] == "bus"
    ]
    routes = {
        row["route_id"] for row in rows(feed, "routes")
        if row["agency_id"] == "FLIXBUS-eu" and row["route_type"] == "3"
    }
    trip_services = {
        row["trip_id"]: row["service_id"] for row in rows(feed, "trips")
        if row["route_id"] in routes
    }
    stops = {
        row["stop_id"]: row for row in rows(feed, "stops")
        if row["stop_lat"] and row["stop_lon"] and row["location_type"] in {"", "0"}
        and "waypoint" not in row["stop_name"].casefold().split()
    }
    origin_ids: dict[str, str] = {}
    for terminal in origin_terminals:
        coords = terminal["coordinates"]
        nearest = min(stops, key=lambda stop: distance(coords["lat"], coords["lon"], stops[stop]))
        if distance(coords["lat"], coords["lon"], stops[nearest]) <= MAX_ORIGIN_DISTANCE_KM:
            origin_ids[nearest] = terminal["id"]
    if not origin_ids:
        raise ValueError("The feed has no stops near Crowl's allowed Paris bus terminals.")

    city_stops: dict[str, dict[str, float]] = defaultdict(dict)
    stop_cities: dict[str, list[str]] = defaultdict(list)
    for city in cities:
        coords = city["coordinates"]
        for stop_id, stop in stops.items():
            km = distance(coords["lat"], coords["lon"], stop)
            if km <= MAX_CITY_DISTANCE_KM:
                city_stops[city["id"]][stop_id] = km
                stop_cities[stop_id].append(city["id"])

    markets: dict[str, list] = defaultdict(list)
    for row in rows(feed, "stop_times"):
        stop_id = row["stop_id"]
        if row["trip_id"] in trip_services and (stop_id in origin_ids or stop_id in stop_cities):
            markets[row["trip_id"]].append(row)

    days = tuple(first - 3 * DAY + i * DAY for i in range((last - first).days + 7))
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    services: dict[str, set[date]] = defaultdict(set)
    for row in rows(feed, "calendar"):
        starts = date.fromisoformat(row["start_date"])
        ends = date.fromisoformat(row["end_date"])
        services[row["service_id"]].update(
            day for day in days if starts <= day <= ends and row[weekdays[day.weekday()]] == "1"
        )
    day_set = set(days)
    for row in rows(feed, "calendar_dates"):
        day = date.fromisoformat(row["date"])
        if day in day_set:
            active = services[row["service_id"]]
            if row["exception_type"] == "1":
                active.add(day)
            elif row["exception_type"] == "2":
                active.discard(day)
    day_starts = {day: unix(day, time.min) for day in days}
    windows = {}
    for index in range((last - first).days // 7 + 1):
        friday = first + timedelta(weeks=index)
        lo = unix(friday, time(19), PARIS)
        hi = unix(friday + timedelta(days=3), time(7), PARIS)
        for offset in range(4):
            windows[friday + offset * DAY] = friday, lo, hi

    bus_stops = set()
    direct_directions: dict[str, set[bool]] = defaultdict(set)
    within_cap: dict[str, set[bool]] = defaultdict(set)
    candidates: dict[tuple[str, str, date], list[int | None]] = defaultdict(lambda: [None, None])
    for trip, timetable in markets.items():
        bus_stops.update(row["stop_id"] for row in timetable)
        origins = [row for row in timetable if row["stop_id"] in origin_ids]
        if not origins or not services[trip_services[trip]]:
            continue
        for other in (row for row in timetable if row["stop_id"] in stop_cities):
            for origin in origins:
                if origin["stop_id"] == other["stop_id"]:
                    continue
                outbound = int(origin["stop_sequence"]) < int(other["stop_sequence"])
                departure, arrival = (origin, other) if outbound else (other, origin)
                if departure["pickup_type"] not in {"", "0"} or arrival["drop_off_type"] not in {"", "0"}:
                    continue
                departs = seconds(departure["departure_time"])
                arrives = seconds(arrival["arrival_time"])
                if arrives <= departs:
                    continue
                for city_id in stop_cities[other["stop_id"]]:
                    direct_directions[city_id].add(outbound)
                    if arrives - departs <= MAX_DURATION:
                        within_cap[city_id].add(outbound)
                if arrives - departs > MAX_DURATION:
                    continue
                for day in services[trip_services[trip]]:
                    start = day_starts[day] + departs
                    end = day_starts[day] + arrives
                    window = windows.get(datetime.fromtimestamp(start, PARIS).date())
                    if not window:
                        continue
                    friday, lo, hi = window
                    if not lo <= start < end <= hi:
                        continue
                    pair = candidates[origin["stop_id"], other["stop_id"], friday]
                    if outbound:
                        pair[0] = end if pair[0] is None else min(pair[0], end)
                    else:
                        pair[1] = start if pair[1] is None else max(pair[1], start)

    qualified: dict[tuple[str, str], list[str]] = defaultdict(list)
    for (origin, destination, friday), (arrive, depart) in candidates.items():
        if arrive is not None and depart is not None and depart - arrive >= MAX_DURATION:
            qualified[origin, destination].append(friday.isoformat())

    results = []
    for city in cities:
        city_id = city["id"]
        nearby = city_stops[city_id]
        matches = [
            (origin, stop_id, weekends)
            for (origin, stop_id), weekends in qualified.items() if stop_id in nearby
        ]
        if matches:
            origin, stop_id, weekends = min(
                matches, key=lambda pair: (-len(pair[2]), nearby[pair[1]], pair)
            )
            stop = stops[stop_id]
            result = {
                "status": "scheduled_weekend",
                "origin_terminal_id": origin_ids[origin],
                "stop_name": stop["stop_name"],
                "stop_id": stop_id,
                "stop_lat": float(stop["stop_lat"]),
                "stop_lon": float(stop["stop_lon"]),
                "distance_km": round(nearby[stop_id], 2),
                "weekends": sorted(weekends),
            }
        elif len(direct_directions[city_id]) == 2:
            result = {
                "status": "no_qualifying_weekend" if len(within_cap[city_id]) == 2 else "over_duration_limit"
            }
        elif nearby.keys() & bus_stops:
            result = {"status": "no_two_way_direct_service"}
        else:
            result = {"status": "no_nearby_flixbus_stop"}
        results.append({"city_id": city_id, "city": city["name"], **result})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gtfs", type=Path)
    parser.add_argument("--first-friday", required=True, type=date.fromisoformat)
    parser.add_argument("--last-friday", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with ZipFile(args.gtfs) as feed:
        results = assess(feed, args.first_friday, args.last_friday)
    payload = {
        "source": SOURCE,
        "licence": "ODbL-1.0",
        "first_friday": args.first_friday.isoformat(),
        "last_friday": args.last_friday.isoformat(),
        "cities": results,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
