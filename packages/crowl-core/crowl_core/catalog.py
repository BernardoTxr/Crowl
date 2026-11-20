"""Validated, idempotent import of version-controlled catalog data."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .enums import (
    Availability,
    BatchStatus,
    Direction,
    ExecutionCoverage,
    ExtractorRunStatus,
    LinkRelationship,
    PricingSemantics,
    ProtectionState,
    TerminalKind,
    TransportMode,
)
from .models import (
    City,
    CityPhoto,
    CityTerminalLink,
    CrawlRun,
    DurationHistogramDaily,
    ExtractorRun,
    FareCellObservation,
    FareSnapshot,
    FareSnapshotRevision,
    OriginTerminal,
    RuleVersion,
    SearchProfile,
    SnapshotBooking,
    SnapshotLeg,
    Source,
    Terminal,
    TerminalTransfer,
    TravelWindow,
)
from .travel_windows import generate_travel_windows


SCHENGEN_COUNTRY_CODES = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "CH",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HR",
        "HU",
        "IS",
        "IT",
        "LI",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "NO",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }
)


SOURCE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {"slug": "ryanair", "name": "Ryanair", "modes": ["flight"], "strategy": "http_structured"},
    {"slug": "easyjet", "name": "easyJet", "modes": ["flight"], "strategy": "http_structured"},
    {"slug": "transavia", "name": "Transavia", "modes": ["flight"], "strategy": "http_structured"},
    {"slug": "wizzair", "name": "Wizz Air", "modes": ["flight"], "strategy": "http_structured"},
    {"slug": "vueling", "name": "Vueling", "modes": ["flight"], "strategy": "http_structured"},
    {"slug": "volotea", "name": "Volotea", "modes": ["flight"], "strategy": "disabled"},
    {"slug": "flixbus", "name": "FlixBus", "modes": ["bus"], "strategy": "http_structured"},
    {"slug": "blablacar_bus", "name": "BlaBlaCar Bus", "modes": ["bus"], "strategy": "disabled"},
    {"slug": "itabus", "name": "Itabus", "modes": ["bus"], "strategy": "disabled"},
    {"slug": "alsa", "name": "Alsa", "modes": ["bus"], "strategy": "disabled"},
    {"slug": "ouigo", "name": "OUIGO", "modes": ["train"], "strategy": "disabled"},
    {"slug": "sncf_connect", "name": "SNCF Connect", "modes": ["train"], "strategy": "disabled"},
    {"slug": "trenitalia_france", "name": "Trenitalia France", "modes": ["train"], "strategy": "http_structured"},
    {"slug": "eurostar", "name": "Eurostar", "modes": ["train"], "strategy": "disabled"},
)


DEFAULT_RULE_CONSTRAINTS: dict[str, Any] = {
    "weekend_only": True,
    "friday_start": "19:00",
    "monday_deadline": "07:00",
    "minimum_stay_minutes": 720,
    "maximum_direction_minutes": 720,
    "duration_measurement": "terminal_to_terminal_per_direction",
    "strict_schengen_path": True,
    "home_timezone": "Europe/Paris",
    "lookahead_months": 6,
    "one_destination_per_trip": True,
    "round_trip_required": True,
    "connections_allowed": True,
    "mixed_modes_allowed": True,
    "nearby_destination_terminals_allowed": True,
    "public_holidays_special_treatment": False,
    "mandatory_fees_included": True,
    "destination_transfer_excluded": True,
    "origin_transfer_excluded": True,
}


_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_SOURCE_STATES = {
    "enabled",
    "disabled",
    "disabled_needs_authorization",
    "disabled_needs_credentials",
}
_SOURCE_STRATEGIES = {"http_structured", "http_html", "playwright", "disabled"}


@dataclass(frozen=True, slots=True)
class SeedReport:
    cities: int
    terminals: int
    sources: int
    travel_windows: int
    fixture_snapshots: int = 0


class CatalogValidationError(ValueError):
    pass


def _require(mapping: dict[str, Any], key: str, location: str) -> Any:
    if key not in mapping:
        raise CatalogValidationError(f"{location}.{key} is required")
    return mapping[key]


def _check_timezone(value: str, location: str) -> None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise CatalogValidationError(f"{location} is not an IANA timezone: {value}") from exc


def _load_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogValidationError(f"cannot load {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogValidationError(f"{label} must be a JSON object")
    return payload


def validate_schengen_catalog(payload: dict[str, Any]) -> None:
    if payload.get("schemaVersion") != 1:
        raise CatalogValidationError("Schengen schemaVersion must equal 1")
    as_of = _require(payload, "asOf", "schengen")
    try:
        date.fromisoformat(as_of)
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError("schengen.asOf must be an ISO date") from exc
    source = _require(payload, "source", "schengen")
    if not isinstance(source, str) or not source.strip():
        raise CatalogValidationError("schengen.source must be a non-empty provenance URL")
    country_codes = _require(payload, "countryCodes", "schengen")
    if not isinstance(country_codes, list) or not country_codes:
        raise CatalogValidationError("schengen.countryCodes must be a non-empty array")
    if len(country_codes) != len(set(country_codes)):
        raise CatalogValidationError("schengen.countryCodes must be unique")
    invalid = [
        code
        for code in country_codes
        if not isinstance(code, str) or len(code) != 2 or code.upper() != code
    ]
    if invalid:
        raise CatalogValidationError(f"schengen.countryCodes contains invalid values: {invalid}")


def load_schengen_catalog(path: str | Path) -> dict[str, Any]:
    payload = _load_json_object(path, label="Schengen catalog")
    validate_schengen_catalog(payload)
    return payload


def validate_city_catalog(
    payload: dict[str, Any],
    *,
    schengen_country_codes: Iterable[str] = SCHENGEN_COUNTRY_CODES,
) -> None:
    """Apply semantic checks that JSON Schema alone cannot express."""

    configured_schengen_codes = frozenset(schengen_country_codes)
    if payload.get("schemaVersion") != 1:
        raise CatalogValidationError("schemaVersion must equal 1")
    origin = _require(payload, "origin", "catalog")
    _check_timezone(_require(origin, "timezone", "origin"), "origin.timezone")
    origin_coordinates = _require(origin, "coordinates", "origin")
    origin_latitude = float(_require(origin_coordinates, "lat", "origin.coordinates"))
    origin_longitude = float(_require(origin_coordinates, "lon", "origin.coordinates"))
    if not -90 <= origin_latitude <= 90 or not -180 <= origin_longitude <= 180:
        raise CatalogValidationError("origin coordinates are outside valid ranges")

    category_definitions = _require(payload, "categoryDefinitions", "catalog")
    attribute_definitions = _require(payload, "attributeDefinitions", "catalog")
    categories = {definition["id"] for definition in category_definitions}
    attributes = {definition["id"]: definition for definition in attribute_definitions}
    if len(categories) != len(category_definitions):
        raise CatalogValidationError("category definition IDs must be unique")
    if len(attributes) != len(attribute_definitions):
        raise CatalogValidationError("attribute definition IDs must be unique")

    ids: set[str] = set()
    slugs: set[str] = set()
    cities = _require(payload, "cities", "catalog")
    if not isinstance(cities, list) or not cities:
        raise CatalogValidationError("catalog.cities must be a non-empty array")
    for index, city in enumerate(cities):
        location = f"cities[{index}]"
        city_id = _require(city, "id", location)
        slug = _require(city, "slug", location)
        if city_id in ids:
            raise CatalogValidationError(f"duplicate city id: {city_id}")
        if slug in slugs:
            raise CatalogValidationError(f"duplicate city slug: {slug}")
        ids.add(city_id)
        slugs.add(slug)

        country_code = _require(_require(city, "country", location), "code", f"{location}.country")
        if country_code not in configured_schengen_codes:
            raise CatalogValidationError(f"{location} is outside configured Schengen scope: {country_code}")
        _check_timezone(_require(city, "timezone", location), f"{location}.timezone")
        coordinates = _require(city, "coordinates", location)
        latitude = float(_require(coordinates, "lat", f"{location}.coordinates"))
        longitude = float(_require(coordinates, "lon", f"{location}.coordinates"))
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise CatalogValidationError(f"{location} coordinates are outside valid ranges")

        unknown_categories = set(city.get("categories", [])) - categories
        if unknown_categories:
            raise CatalogValidationError(
                f"{location} references unknown categories: {sorted(unknown_categories)}"
            )
        city_attributes = _require(city, "attributes", location)
        if set(city_attributes) != set(attributes):
            missing = set(attributes) - set(city_attributes)
            extra = set(city_attributes) - set(attributes)
            raise CatalogValidationError(
                f"{location} attribute coverage mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
            )
        for attribute_id, value in city_attributes.items():
            definition = attributes[attribute_id]
            if not definition["min"] <= value <= definition["max"]:
                raise CatalogValidationError(
                    f"{location}.attributes.{attribute_id} is outside configured range"
                )


def load_city_catalog(
    path: str | Path,
    *,
    schengen_country_codes: Iterable[str] = SCHENGEN_COUNTRY_CODES,
) -> dict[str, Any]:
    payload = _load_json_object(path, label="city catalog")
    validate_city_catalog(payload, schengen_country_codes=schengen_country_codes)
    return payload


def validate_terminal_catalog(
    payload: dict[str, Any],
    *,
    city_ids: Iterable[str],
    schengen_country_codes: Iterable[str],
) -> None:
    if payload.get("schemaVersion") != 1:
        raise CatalogValidationError("terminal schemaVersion must equal 1")
    if payload.get("strictSchengenPath") is not True:
        raise CatalogValidationError("terminal.strictSchengenPath must be true")

    configured_city_ids = frozenset(city_ids)
    configured_schengen_codes = frozenset(schengen_country_codes)
    terminals = _require(payload, "terminals", "terminal")
    if not isinstance(terminals, list) or not terminals:
        raise CatalogValidationError("terminal.terminals must be a non-empty array")
    terminal_ids: set[str] = set()
    active_terminal_ids: set[str] = set()
    eligible_terminal_ids: set[str] = set()
    for index, item in enumerate(terminals):
        location = f"terminals[{index}]"
        terminal_id = _require(item, "id", location)
        if terminal_id in terminal_ids:
            raise CatalogValidationError(f"duplicate terminal id: {terminal_id}")
        terminal_ids.add(terminal_id)
        if bool(item.get("active", True)):
            active_terminal_ids.add(terminal_id)
        timezone_name = _require(item, "timezone", location)
        _check_timezone(timezone_name, f"{location}.timezone")
        coordinates = _require(item, "coordinates", location)
        latitude = float(_require(coordinates, "lat", f"{location}.coordinates"))
        longitude = float(_require(coordinates, "lon", f"{location}.coordinates"))
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise CatalogValidationError(f"{location} coordinates are outside valid ranges")
        country = item.get("country", {})
        country_code = item.get("countryCode", country.get("code"))
        if country_code not in configured_schengen_codes:
            raise CatalogValidationError(
                f"{location} is outside configured Schengen scope: {country_code}"
            )
        if bool(item.get("schengenEligible", True)):
            eligible_terminal_ids.add(terminal_id)
        kind_value = item.get("kind", item.get("type"))
        if kind_value == "station":
            kind_value = "rail"
        try:
            TerminalKind(kind_value)
        except ValueError as exc:
            raise CatalogValidationError(f"{location}.kind is invalid: {kind_value}") from exc

    origin_terminal_ids = _require(payload, "originTerminalIds", "terminal")
    if not isinstance(origin_terminal_ids, list) or not origin_terminal_ids:
        raise CatalogValidationError("terminal.originTerminalIds must be a non-empty array")
    if len(origin_terminal_ids) != len(set(origin_terminal_ids)):
        raise CatalogValidationError("terminal.originTerminalIds must be unique")
    unknown_origins = set(origin_terminal_ids) - terminal_ids
    if unknown_origins:
        raise CatalogValidationError(
            f"terminal.originTerminalIds references unknown terminals: {sorted(unknown_origins)}"
        )
    inactive_origins = set(origin_terminal_ids) - active_terminal_ids
    if inactive_origins:
        raise CatalogValidationError(
            f"terminal.originTerminalIds references inactive terminals: {sorted(inactive_origins)}"
        )
    ineligible_origins = set(origin_terminal_ids) - eligible_terminal_ids
    if ineligible_origins:
        raise CatalogValidationError(
            "terminal.originTerminalIds references non-Schengen terminals: "
            f"{sorted(ineligible_origins)}"
        )

    link_pairs: set[tuple[str, str]] = set()
    for index, item in enumerate(payload.get("cityTerminalLinks", [])):
        location = f"cityTerminalLinks[{index}]"
        city_id = _require(item, "cityId", location)
        terminal_id = _require(item, "terminalId", location)
        pair = (city_id, terminal_id)
        if pair in link_pairs:
            raise CatalogValidationError(f"duplicate city-terminal link: {pair}")
        link_pairs.add(pair)
        if city_id not in configured_city_ids:
            raise CatalogValidationError(f"{location} references unknown city: {city_id}")
        if terminal_id not in terminal_ids:
            raise CatalogValidationError(f"{location} references unknown terminal: {terminal_id}")
        try:
            LinkRelationship(item.get("relationship", "primary"))
        except ValueError as exc:
            raise CatalogValidationError(f"{location}.relationship is invalid") from exc

    transfer_pairs: set[tuple[str, str]] = set()
    for index, item in enumerate(payload.get("terminalTransfers", [])):
        location = f"terminalTransfers[{index}]"
        from_terminal_id = _require(item, "fromTerminalId", location)
        to_terminal_id = _require(item, "toTerminalId", location)
        pair = (from_terminal_id, to_terminal_id)
        if pair in transfer_pairs:
            raise CatalogValidationError(f"duplicate terminal transfer: {pair}")
        transfer_pairs.add(pair)
        unknown = set(pair) - terminal_ids
        if unknown:
            raise CatalogValidationError(
                f"{location} references unknown terminals: {sorted(unknown)}"
            )
        minimum_minutes = _require(item, "minimumMinutes", location)
        if not isinstance(minimum_minutes, int) or minimum_minutes < 0:
            raise CatalogValidationError(f"{location}.minimumMinutes must be non-negative")
        cost_is_complete = bool(item.get("costIsComplete", False))
        cost = item.get("costEurCents")
        if cost is not None and (not isinstance(cost, int) or cost < 0):
            raise CatalogValidationError(f"{location}.costEurCents must be non-negative")
        if cost_is_complete and cost is None:
            raise CatalogValidationError(
                f"{location}.costEurCents is required when costIsComplete is true"
            )


def load_terminal_catalog(
    path: str | Path,
    *,
    city_ids: Iterable[str],
    schengen_country_codes: Iterable[str],
) -> dict[str, Any]:
    payload = _load_json_object(path, label="terminal catalog")
    validate_terminal_catalog(
        payload,
        city_ids=city_ids,
        schengen_country_codes=schengen_country_codes,
    )
    return payload


def validate_source_catalog(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 1:
        raise CatalogValidationError("source schema_version must equal 1")
    sources = _require(payload, "sources", "source")
    if not isinstance(sources, list) or not sources:
        raise CatalogValidationError("source.sources must be a non-empty array")
    seen: set[str] = set()
    for index, item in enumerate(sources):
        location = f"sources[{index}]"
        source_id = _require(item, "id", location)
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise CatalogValidationError(f"{location}.id is invalid")
        if source_id in seen:
            raise CatalogValidationError(f"duplicate source id: {source_id}")
        seen.add(source_id)
        if not isinstance(_require(item, "name", location), str):
            raise CatalogValidationError(f"{location}.name must be a string")
        modes = _require(item, "modes", location)
        if not isinstance(modes, list) or not modes or len(modes) != len(set(modes)):
            raise CatalogValidationError(f"{location}.modes must be a non-empty unique array")
        invalid_modes = set(modes) - {
            TransportMode.FLIGHT.value,
            TransportMode.TRAIN.value,
            TransportMode.BUS.value,
        }
        if invalid_modes:
            raise CatalogValidationError(f"{location}.modes is invalid: {sorted(invalid_modes)}")
        if _require(item, "strategy", location) not in _SOURCE_STRATEGIES:
            raise CatalogValidationError(f"{location}.strategy is invalid")
        if _require(item, "state", location) not in _SOURCE_STATES:
            raise CatalogValidationError(f"{location}.state is invalid")
        for flag in ("enabled", "kill_switch"):
            if flag in item and not isinstance(item[flag], bool):
                raise CatalogValidationError(f"{location}.{flag} must be a boolean")
        booking_hosts = _require(item, "booking_hosts", location)
        if (
            not isinstance(booking_hosts, list)
            or not booking_hosts
            or len(booking_hosts) != len(set(booking_hosts))
            or any(
                not isinstance(host, str)
                or not host
                or host != host.lower()
                or ":" in host
                or "/" in host
                for host in booking_hosts
            )
        ):
            raise CatalogValidationError(
                f"{location}.booking_hosts must be unique lowercase host names"
            )


def load_source_catalog(path: str | Path) -> tuple[dict[str, Any], ...]:
    path = Path(path)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogValidationError(f"cannot load source catalog {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogValidationError("source catalog must be a YAML object")
    validate_source_catalog(payload)
    return tuple(payload["sources"])


def import_city_catalog(
    session: Session,
    payload: dict[str, Any],
    *,
    schengen_country_codes: Iterable[str] = SCHENGEN_COUNTRY_CODES,
) -> int:
    """Upsert catalog cities; absence means inactive, never historical deletion."""

    validate_city_catalog(payload, schengen_country_codes=schengen_country_codes)
    present: set[str] = set()
    for city_data in payload["cities"]:
        city_id = city_data["id"]
        present.add(city_id)
        city = session.get(City, city_id)
        if city is None:
            city = City(id=city_id, slug=city_data["slug"])
            session.add(city)
        country = city_data["country"]
        coordinates = city_data["coordinates"]
        status = city_data["status"]
        city.slug = city_data["slug"]
        city.name = city_data["name"]
        city.country_code = country["code"]
        city.country_name = country["name"]
        city.timezone = city_data["timezone"]
        city.latitude = float(coordinates["lat"])
        city.longitude = float(coordinates["lon"])
        city.visited = bool(status["visited"])
        city.wishlist = bool(status["wishlist"])
        city.categories = list(city_data["categories"])
        city.attributes = dict(city_data["attributes"])
        city.summary = city_data["summary"]
        city.active = True

        photo_data = city_data.get("photo")
        if photo_data:
            for photo in city.photos:
                photo.is_primary = False
            asset_path = photo_data["asset"]
            photo = next((item for item in city.photos if item.asset_path == asset_path), None)
            if photo is None:
                photo = CityPhoto(city=city, asset_path=asset_path, alt_text=photo_data["alt"])
                session.add(photo)
            photo.alt_text = photo_data["alt"]
            photo.creator = photo_data.get("creator")
            photo.source_url = photo_data.get("sourceUrl")
            photo.license = photo_data.get("license")
            photo.attribution = photo_data.get("attribution")
            photo.object_position = photo_data.get("objectPosition", "50% 50%")
            photo.curation_status = photo_data.get("curationStatus", "pending")
            photo.is_primary = True

    for existing in session.scalars(select(City)).all():
        if existing.id not in present:
            existing.active = False
    session.flush()
    return len(present)


def import_terminal_catalog(
    session: Session,
    payload: dict[str, Any],
    *,
    rule_version: RuleVersion | None = None,
    schengen_country_codes: Iterable[str] = SCHENGEN_COUNTRY_CODES,
) -> int:
    """Import the canonical subset required by the API.

    The importer accepts both ``kind`` and the common configuration spelling
    ``type`` so terminal data can evolve independently from the database model.
    Links and aliases are intentionally handled by crawler/catalog tooling later.
    """

    configured_schengen_codes = frozenset(schengen_country_codes)
    present_terminal_ids: set[str] = set()
    for index, item in enumerate(payload.get("terminals", [])):
        location = f"terminals[{index}]"
        terminal_id = _require(item, "id", location)
        present_terminal_ids.add(terminal_id)
        timezone_name = _require(item, "timezone", location)
        _check_timezone(timezone_name, f"{location}.timezone")
        coordinates = _require(item, "coordinates", location)
        country = item.get("country", {})
        country_code = item.get("countryCode", country.get("code"))
        if not country_code:
            raise CatalogValidationError(f"{location}.countryCode is required")
        kind_value = item.get("kind", item.get("type"))
        if kind_value == "station":
            kind_value = "rail"
        terminal = session.get(Terminal, terminal_id)
        if terminal is None:
            terminal = Terminal(id=terminal_id)
            session.add(terminal)
        terminal.name = _require(item, "name", location)
        terminal.kind = TerminalKind(kind_value)
        terminal.country_code = country_code
        terminal.timezone = timezone_name
        terminal.latitude = float(_require(coordinates, "lat", f"{location}.coordinates"))
        terminal.longitude = float(_require(coordinates, "lon", f"{location}.coordinates"))
        terminal.schengen_eligible = bool(
            item.get("schengenEligible", country_code in configured_schengen_codes)
        )
        terminal.active = bool(item.get("active", True))
    session.flush()

    for terminal in session.scalars(select(Terminal)).all():
        if terminal.id not in present_terminal_ids:
            terminal.active = False

    present_links: set[tuple[str, str]] = set()
    for item in payload.get("cityTerminalLinks", []):
        city_id = item["cityId"]
        terminal_id = item["terminalId"]
        present_links.add((city_id, terminal_id))
        link = session.scalar(
            select(CityTerminalLink).where(
                CityTerminalLink.city_id == city_id,
                CityTerminalLink.terminal_id == terminal_id,
            )
        )
        if link is None:
            link = CityTerminalLink(city_id=city_id, terminal_id=terminal_id)
            session.add(link)
        link.relationship_type = LinkRelationship(item.get("relationship", "primary"))

    for link in session.scalars(select(CityTerminalLink)).all():
        if (link.city_id, link.terminal_id) not in present_links:
            session.delete(link)

    present_transfers: set[tuple[str, str]] = set()
    for item in payload.get("terminalTransfers", []):
        pair = (item["fromTerminalId"], item["toTerminalId"])
        present_transfers.add(pair)
        transfer = session.scalar(
            select(TerminalTransfer).where(
                TerminalTransfer.from_terminal_id == pair[0],
                TerminalTransfer.to_terminal_id == pair[1],
            )
        )
        if transfer is None:
            transfer = TerminalTransfer(from_terminal_id=pair[0], to_terminal_id=pair[1])
            session.add(transfer)
        transfer.minimum_minutes = item["minimumMinutes"]
        transfer.cost_eur_cents = item.get("costEurCents")
        transfer.cost_is_complete = bool(item.get("costIsComplete", False))
        transfer.evidence_url = item.get("evidenceUrl")

    for transfer in session.scalars(select(TerminalTransfer)).all():
        if (transfer.from_terminal_id, transfer.to_terminal_id) not in present_transfers:
            session.delete(transfer)

    if rule_version is not None:
        allowed = set(payload.get("originTerminalIds", []))
        stored_allowed = set(
            rule_version.rules.get("catalog", {})
            .get("terminal_catalog", {})
            .get("origin_terminal_ids", [])
        )
        if stored_allowed and stored_allowed != allowed:
            raise CatalogValidationError(
                "current origin terminals do not match the immutable rule identity"
            )

        # New-format rule rows contain enough evidence to repair their own
        # relationship table without mutating the immutable JSON identity.
        for version in session.scalars(select(RuleVersion)).all():
            expected = set(
                version.rules.get("catalog", {})
                .get("terminal_catalog", {})
                .get("origin_terminal_ids", [])
            )
            if version.id == rule_version.id and not expected:
                expected = allowed
            if not expected:
                # A legacy row does not carry enough provenance to reconcile.
                continue
            existing_origins = session.scalars(
                select(OriginTerminal).where(OriginTerminal.rule_version_id == version.id)
            ).all()
            origins_by_terminal = {origin.terminal_id: origin for origin in existing_origins}
            for origin in existing_origins:
                if origin.terminal_id not in expected:
                    session.delete(origin)
            for terminal_id in expected:
                origin = origins_by_terminal.get(terminal_id)
                if origin is None:
                    session.add(
                        OriginTerminal(
                            rule_version_id=version.id,
                            terminal_id=terminal_id,
                            permits_departure=True,
                            permits_return=True,
                        )
                    )
                else:
                    origin.permits_departure = True
                    origin.permits_return = True
    session.flush()
    return len(present_terminal_ids)


def seed_sources(
    session: Session,
    definitions: Sequence[dict[str, Any]] = SOURCE_DEFINITIONS,
) -> int:
    """Reconcile requested sources, with catalog state acting as a kill switch."""

    configured_slugs: set[str] = set()
    for definition in definitions:
        slug = definition.get("slug", definition.get("id"))
        if not isinstance(slug, str):
            raise CatalogValidationError("source definition requires slug or id")
        configured_slugs.add(slug)
        source = session.get(Source, slug)
        if source is None:
            source = Source(
                slug=slug,
                enabled=False,
                connector_version="planned",
                parser_version="planned",
                disabled_reason="Awaiting an authorized live-fare channel",
            )
            session.add(source)
        source.name = definition["name"]
        source.supported_modes = definition["modes"]
        source.strategy = definition["strategy"]
        source.is_requested = True
        requested_enabled = bool(
            definition.get("enabled", definition.get("state") == "enabled")
        )
        configured_enabled = (
            requested_enabled
            and definition.get("state", "disabled") == "enabled"
            and not bool(definition.get("kill_switch", False))
        )
        source.enabled = configured_enabled
        if configured_enabled:
            source.disabled_reason = None
        else:
            prefix = "Catalog kill switch is active. " if definition.get("kill_switch") else ""
            source.disabled_reason = prefix + definition.get(
                "access_note", "Awaiting an authorized live-fare channel"
            )
        if source.connector_version in {None, ""}:
            source.connector_version = "planned"
        if source.parser_version in {None, ""}:
            source.parser_version = "planned"

    for source in session.scalars(select(Source).where(Source.is_requested.is_(True))).all():
        if source.slug not in configured_slugs:
            source.is_requested = False
            source.enabled = False
            source.disabled_reason = "Removed from the requested source catalog"
    session.flush()
    return len(configured_slugs)


def _hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_rule_config(
    *,
    city_payload: dict[str, Any],
    terminal_payload: dict[str, Any],
    schengen_payload: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Build a stable identity from every input that changes trip validity."""

    if city_payload["origin"]["timezone"] != DEFAULT_RULE_CONSTRAINTS["home_timezone"]:
        raise CatalogValidationError(
            "catalog origin timezone does not match the configured rule timezone"
        )
    normalized_schengen = {
        "schema_version": schengen_payload["schemaVersion"],
        "as_of": schengen_payload["asOf"],
        "source": schengen_payload["source"],
        "country_codes": sorted(schengen_payload["countryCodes"]),
    }
    normalized_terminal_catalog = {
        "schema_version": terminal_payload["schemaVersion"],
        "strict_schengen_path": terminal_payload["strictSchengenPath"],
        "origin_terminal_ids": sorted(terminal_payload["originTerminalIds"]),
        "terminals": sorted(terminal_payload["terminals"], key=lambda item: item["id"]),
        "city_terminal_links": sorted(
            terminal_payload.get("cityTerminalLinks", []),
            key=lambda item: (item["cityId"], item["terminalId"]),
        ),
        "terminal_transfers": sorted(
            terminal_payload.get("terminalTransfers", []),
            key=lambda item: (item["fromTerminalId"], item["toTerminalId"]),
        ),
    }
    rule_data = {
        **DEFAULT_RULE_CONSTRAINTS,
        "catalog": {
            "schengen": normalized_schengen,
            "home_origin": city_payload["origin"],
            "terminal_catalog": normalized_terminal_catalog,
        },
    }
    scope_hash = _hash(normalized_schengen)
    scope_version = f"{normalized_schengen['as_of']}-{scope_hash[:12]}"
    return rule_data, scope_version


def ensure_default_profile_and_rules(
    session: Session,
    *,
    rule_data: dict[str, Any] | None = None,
    schengen_scope_version: str | None = None,
) -> tuple[SearchProfile, RuleVersion]:
    profile_data = {
        "passenger_count": 1,
        "passenger_age": 22,
        "student": True,
        "currency": "EUR",
        "baggage": "smallest_included",
        "paid_entitlements": False,
    }
    profile_hash = _hash(profile_data)
    profile = session.scalar(select(SearchProfile).where(SearchProfile.config_hash == profile_hash))
    if profile is None:
        profile = SearchProfile(
            config_hash=profile_hash,
            name="one-adult-age-22-student",
            passenger_age=22,
            student=True,
            currency="EUR",
            baggage="smallest_included",
            entitlements={"paid": [], "passenger_count": 1},
        )
        session.add(profile)

    if rule_data is None:
        if schengen_scope_version is not None:
            raise CatalogValidationError(
                "schengen_scope_version cannot be provided without complete rule_data"
            )
        rule = session.scalar(
            select(RuleVersion)
            .order_by(RuleVersion.updated_at.desc(), RuleVersion.id.desc())
            .limit(1)
        )
        if rule is None:
            raise CatalogValidationError(
                "the versioned rule catalog must be seeded before fares are published"
            )
    else:
        if not schengen_scope_version:
            raise CatalogValidationError(
                "schengen_scope_version is required with complete rule_data"
            )
        rule_hash = _hash(rule_data)
        rule = session.scalar(select(RuleVersion).where(RuleVersion.config_hash == rule_hash))
        if rule is None:
            rule = RuleVersion(
                config_hash=rule_hash,
                version_label=f"catalog-{rule_hash[:12]}",
                schengen_scope_version=schengen_scope_version,
                strict_schengen_path=bool(rule_data["strict_schengen_path"]),
                rules=rule_data,
            )
            session.add(rule)
        elif (
            rule.rules != rule_data
            or rule.schengen_scope_version != schengen_scope_version
            or rule.strict_schengen_path != bool(rule_data["strict_schengen_path"])
        ):
            raise CatalogValidationError(
                "stored immutable rule version does not match its identity"
            )
        else:
            # The semantic record stays immutable; updated_at is the
            # activation marker used by publishers that do not load files.
            rule.updated_at = datetime.now(timezone.utc)
    session.flush()
    return profile, rule


def seed_travel_windows(
    session: Session, as_of: date | datetime | None = None, *, months_ahead: int = 6
) -> int:
    windows = generate_travel_windows(as_of, months_ahead=months_ahead)
    for spec in windows:
        model = session.scalar(select(TravelWindow).where(TravelWindow.friday_date == spec.friday))
        if model is None:
            model = TravelWindow(friday_date=spec.friday)
            session.add(model)
        model.starts_at = spec.starts_at
        model.ends_at = spec.ends_at
        model.timezone = spec.timezone_name
        model.minimum_stay_minutes = spec.minimum_stay_minutes
        model.maximum_direction_minutes = spec.maximum_direction_minutes
    session.flush()
    return len(windows)


def seed_fixture_history(
    session: Session,
    *,
    as_of: date | datetime | None = None,
    profile: SearchProfile | None = None,
    rule: RuleVersion | None = None,
) -> int:
    """Publish a modest, explicitly synthetic development heatmap.

    The non-requested ``fixture`` source is hidden from the 14-source status
    surface and keeps demo observations from being attributed to a real brand.
    Production catalog seeding disables this with ``include_fixture_fares``.
    """

    if profile is None or rule is None:
        profile, rule = ensure_default_profile_and_rules(session)
    source = session.get(Source, "fixture")
    if source is None:
        source = Source(
            slug="fixture",
            name="Development fixture",
            supported_modes=["flight", "train", "bus"],
            strategy="fixture",
            is_requested=False,
            enabled=False,
            disabled_reason="Synthetic development data only",
            connector_version="fixture-v1",
            parser_version="fixture-v1",
        )
        session.add(source)
        session.flush()

    if as_of is None:
        as_of_instant = datetime.now(timezone.utc)
    elif isinstance(as_of, datetime):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("fixture as_of datetime must be timezone-aware")
        as_of_instant = as_of.astimezone(timezone.utc)
    else:
        as_of_instant = datetime.combine(as_of, datetime.min.time(), timezone.utc) + timedelta(hours=12)
    as_of_date = as_of_instant.astimezone(ZoneInfo("Europe/Paris")).date()

    current_fridays = tuple(
        item.friday for item in generate_travel_windows(as_of, months_ahead=6)
    )
    windows = session.scalars(
        select(TravelWindow)
        .where(TravelWindow.friday_date.in_(current_fridays))
        .order_by(TravelWindow.friday_date)
    ).all()
    if len(windows) != len(current_fridays):
        seed_travel_windows(session, as_of)
        windows = session.scalars(
            select(TravelWindow)
            .where(TravelWindow.friday_date.in_(current_fridays))
            .order_by(TravelWindow.friday_date)
        ).all()
    cities = session.scalars(select(City).where(City.active.is_(True)).order_by(City.name)).all()
    if not windows or not cities:
        return 0

    origin_by_kind = {
        TerminalKind.AIRPORT: "par-ory",
        TerminalKind.RAIL: "par-gare-du-nord",
        TerminalKind.BUS: "par-bercy-seine-bus",
    }
    runs: dict[date, tuple[CrawlRun, ExtractorRun]] = {}
    created = 0
    leads = (42, 35, 28, 21, 14, 7)

    for window_index, window in enumerate(windows):
        observation_dates = {
            window.friday_date - timedelta(days=lead)
            for lead in leads
            if window.friday_date - timedelta(days=lead) <= as_of_date
        }
        observation_dates.add(as_of_date)
        for observed_on in sorted(observation_dates):
            observed_at = (
                as_of_instant
                if observed_on == as_of_date
                else datetime.combine(observed_on, datetime.min.time(), timezone.utc)
                + timedelta(hours=12)
            )
            run = session.scalar(
                select(CrawlRun).where(
                    CrawlRun.scheduled_for == observed_on,
                    CrawlRun.is_synthetic.is_(True),
                )
            )
            # One synthetic batch represents one immutable rule/profile pair.
            # If the version-controlled catalog changed, retain the old mock
            # history but do not append new cells under mismatched dimensions.
            # A fresh date (or a deliberate local fixture reset) will build a
            # clean batch with the new dimensions.
            if run is not None and (
                run.rule_version_id != rule.id or run.profile_id != profile.id
            ):
                continue
            if run is None:
                run = CrawlRun(
                    scheduled_for=observed_on,
                    is_synthetic=True,
                    rule_version_id=rule.id,
                    profile_id=profile.id,
                    manifest_version="fixture-v1",
                    release_version="development-fixture",
                    status=BatchStatus.SUCCEEDED,
                    started_at=observed_at,
                    heartbeat_at=observed_at,
                    completed_at=observed_at,
                    published_at=observed_at,
                    diagnostic="Synthetic development fixture; not a live provider observation",
                )
                session.add(run)
                session.flush()
            extractor = session.scalar(
                select(ExtractorRun).where(
                    ExtractorRun.crawl_run_id == run.id,
                    ExtractorRun.source_id == source.slug,
                )
            )
            if extractor is None:
                extractor = ExtractorRun(
                    crawl_run_id=run.id,
                    source_id=source.slug,
                    status=ExtractorRunStatus.SUCCEEDED,
                    started_at=observed_at,
                    last_response_at=observed_at,
                    last_success_at=observed_at,
                    completed_at=observed_at,
                    runtime_ms=250,
                    diagnostic="Synthetic development fixture",
                )
                session.add(extractor)
                session.flush()
            runs[observed_on] = (run, extractor)

            for city_index, city in enumerate(cities):
                link = session.scalar(
                    select(CityTerminalLink)
                    .where(CityTerminalLink.city_id == city.id)
                    .order_by(CityTerminalLink.id)
                    .limit(1)
                )
                if link is None:
                    continue
                destination = session.get(Terminal, link.terminal_id)
                if destination is None:
                    continue
                origin = session.get(Terminal, origin_by_kind[destination.kind])
                if origin is None:
                    continue
                mode = {
                    TerminalKind.AIRPORT: TransportMode.FLIGHT,
                    TerminalKind.RAIL: TransportMode.TRAIN,
                    TerminalKind.BUS: TransportMode.BUS,
                }[destination.kind]
                snapshot = session.scalar(
                    select(FareSnapshot).where(
                        FareSnapshot.observed_on == observed_on,
                        FareSnapshot.profile_id == profile.id,
                        FareSnapshot.destination_city_id == city.id,
                        FareSnapshot.travel_window_id == window.id,
                        FareSnapshot.mode_bucket == mode,
                        FareSnapshot.is_synthetic.is_(True),
                    )
                )
                if snapshot is not None:
                    continue

                snapshot = FareSnapshot(
                    observed_on=observed_on,
                    profile_id=profile.id,
                    destination_city_id=city.id,
                    travel_window_id=window.id,
                    mode_bucket=mode,
                    is_synthetic=True,
                )
                session.add(snapshot)
                session.flush()
                outbound_departure = window.starts_at + timedelta(hours=1)
                outbound_arrival = outbound_departure + timedelta(hours=2)
                return_arrival = window.ends_at - timedelta(hours=1)
                return_departure = return_arrival - timedelta(hours=2)
                lead = (window.friday_date - observed_on).days
                price = 5_900 + city_index * 1_275 + window_index * 350 + max(0, 42 - lead) * 40
                revision = FareSnapshotRevision(
                    fare_snapshot_id=snapshot.id,
                    revision_number=1,
                    crawl_run_id=run.id,
                    rule_version_id=rule.id,
                    profile_id=profile.id,
                    mode_bucket=mode,
                    total_price_eur_cents=price,
                    availability=Availability.AVAILABLE,
                    execution_coverage=ExecutionCoverage.COMPLETE,
                    source_scope_ratio=0.0,
                    observed_at=observed_at,
                    published_at=observed_at,
                    outbound_departure_at=outbound_departure,
                    outbound_arrival_at=outbound_arrival,
                    return_departure_at=return_departure,
                    return_arrival_at=return_arrival,
                    outbound_duration_minutes=120,
                    return_duration_minutes=120,
                    stay_duration_minutes=int(
                        (return_departure - outbound_arrival).total_seconds() // 60
                    ),
                    transfer_count=0,
                    itinerary_key=f"fixture-{city.id}-{window.friday_date}-{observed_on}",
                )
                session.add(revision)
                session.flush()
                booking = SnapshotBooking(
                    revision_id=revision.id,
                    component_order=0,
                    source_id=source.slug,
                    seller="Development fixture",
                    fare_name="Synthetic round trip",
                    pricing_semantics=PricingSemantics.ROUND_TRIP_ONLY,
                    original_amount_minor=price,
                    original_currency="EUR",
                    eur_amount_cents=price,
                    fx_rate=1.0,
                    fx_rate_date=observed_on,
                    student_fare_used=False,
                    mandatory_fees_included=True,
                    passenger_age=22,
                    passenger_student=True,
                    passenger_eligibility_confirmed=True,
                    bookable_at_observation=True,
                    is_teaser=False,
                    booking_url=None,
                )
                session.add(booking)
                session.flush()
                operator = {
                    TransportMode.FLIGHT: "Fixture Air",
                    TransportMode.TRAIN: "Fixture Rail",
                    TransportMode.BUS: "Fixture Coach",
                }[mode]
                session.add_all(
                    [
                        SnapshotLeg(
                            revision_id=revision.id,
                            booking_id=booking.id,
                            direction=Direction.OUTBOUND,
                            segment_order=0,
                            mode=mode,
                            operator=operator,
                            origin_terminal_id=origin.id,
                            destination_terminal_id=destination.id,
                            departs_at=outbound_departure,
                            arrives_at=outbound_arrival,
                            origin_timezone=origin.timezone,
                            destination_timezone=destination.timezone,
                            protection_state=ProtectionState.PROTECTED,
                        ),
                        SnapshotLeg(
                            revision_id=revision.id,
                            booking_id=booking.id,
                            direction=Direction.RETURN,
                            segment_order=0,
                            mode=mode,
                            operator=operator,
                            origin_terminal_id=destination.id,
                            destination_terminal_id=origin.id,
                            departs_at=return_departure,
                            arrives_at=return_arrival,
                            origin_timezone=destination.timezone,
                            destination_timezone=origin.timezone,
                            protection_state=ProtectionState.PROTECTED,
                        ),
                    ]
                )
                snapshot.current_revision_id = revision.id
                session.add(
                    FareCellObservation(
                        crawl_run_id=run.id,
                        rule_version_id=rule.id,
                        profile_id=profile.id,
                        destination_city_id=city.id,
                        travel_window_id=window.id,
                        mode_bucket=mode,
                        availability=Availability.AVAILABLE,
                        execution_coverage=ExecutionCoverage.COMPLETE,
                        source_scope_ratio=0.0,
                        observed_at=observed_at,
                        trusted_revision_id=revision.id,
                        diagnostic="SYNTHETIC_FIXTURE",
                    )
                )
                created += 1

    session.flush()
    for run, extractor in runs.values():
        cell_count = len(
            session.scalars(
                select(FareSnapshotRevision.id).where(FareSnapshotRevision.crawl_run_id == run.id)
            ).all()
        )
        run.expected_cells = cell_count
        run.completed_cells = cell_count
        run.successful_cells = cell_count
        extractor.expected_searches = cell_count
        extractor.attempted_searches = cell_count
        extractor.successful_searches = cell_count
        extractor.result_searches = cell_count
        extractor.offers_found = cell_count
        extractor.physical_requests = cell_count

    for city_index, city in enumerate(cities):
        duration_bases = {
            TransportMode.FLIGHT: 105 + city_index * 5,
            TransportMode.TRAIN: 270 + city_index * 12,
            TransportMode.BUS: 420 + city_index * 15,
        }
        for mode, base in duration_bases.items():
            histogram = session.get(
                DurationHistogramDaily,
                (as_of_date, city.id, mode, True),
            )
            if histogram is None:
                session.add(
                    DurationHistogramDaily(
                        observed_on=as_of_date,
                        city_id=city.id,
                        mode=mode,
                        is_synthetic=True,
                        bins={str(base - 15): 2, str(base): 4, str(base + 15): 3, str(base + 30): 1},
                        sample_count=10,
                    )
                )
    session.flush()
    return created


def seed_development_data(
    session: Session,
    *,
    data_dir: str | Path,
    as_of: date | datetime | None = None,
    include_fixture_fares: bool = False,
) -> SeedReport:
    """Load repository fixtures and foundational immutable records atomically."""

    directory = Path(data_dir)
    schengen_payload = load_schengen_catalog(directory / "schengen.json")
    schengen_country_codes = frozenset(schengen_payload["countryCodes"])
    city_payload = load_city_catalog(
        directory / "cities.mock.json",
        schengen_country_codes=schengen_country_codes,
    )
    terminal_payload = load_terminal_catalog(
        directory / "terminals.json",
        city_ids=(city["id"] for city in city_payload["cities"]),
        schengen_country_codes=schengen_country_codes,
    )
    source_definitions = load_source_catalog(directory / "sources.yaml")
    rule_data, schengen_scope_version = build_rule_config(
        city_payload=city_payload,
        terminal_payload=terminal_payload,
        schengen_payload=schengen_payload,
    )

    city_count = import_city_catalog(
        session,
        city_payload,
        schengen_country_codes=schengen_country_codes,
    )
    profile, rule = ensure_default_profile_and_rules(
        session,
        rule_data=rule_data,
        schengen_scope_version=schengen_scope_version,
    )
    terminal_count = import_terminal_catalog(
        session,
        terminal_payload,
        rule_version=rule,
        schengen_country_codes=schengen_country_codes,
    )
    source_count = seed_sources(session, source_definitions)
    window_count = seed_travel_windows(session, as_of)
    fixture_count = (
        seed_fixture_history(session, as_of=as_of, profile=profile, rule=rule)
        if include_fixture_fares
        else 0
    )
    session.commit()
    return SeedReport(city_count, terminal_count, source_count, window_count, fixture_count)
