#!/usr/bin/env python3
"""Validate Crowl's version-controlled catalog and source configuration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jsonschema
import yaml


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WEB_PUBLIC = ROOT / "services" / "web" / "public"
EXPECTED_SOURCES = {
    "ryanair",
    "easyjet",
    "transavia",
    "wizzair",
    "vueling",
    "volotea",
    "flixbus",
    "blablacar_bus",
    "itabus",
    "alsa",
    "ouigo",
    "sncf_connect",
    "trenitalia_france",
    "eurostar",
}


class ValidationError(RuntimeError):
    pass


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def unique(items: list[str], label: str) -> None:
    repeated = sorted({item for item in items if items.count(item) > 1})
    if repeated:
        raise ValidationError(f"duplicate {label}: {', '.join(repeated)}")


def valid_timezone(value: str, label: str) -> None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError(f"unknown timezone for {label}: {value}") from exc


def validate_schema(payload: Any, schema_name: str) -> None:
    schema = load_json(DATA / schema_name)
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    validator.validate(payload)


def local_public_asset(value: str, label: str, *, suffixes: set[str]) -> Path:
    if not value.startswith("/") or value.startswith("//"):
        raise ValidationError(f"{label} must be a root-relative local asset path")
    relative = Path(value.removeprefix("/"))
    if ".." in relative.parts:
        raise ValidationError(f"{label} escapes the public asset directory")
    path = (WEB_PUBLIC / relative).resolve()
    public = WEB_PUBLIC.resolve()
    if path != public and public not in path.parents:
        raise ValidationError(f"{label} escapes the public asset directory")
    if path.suffix.lower() not in suffixes:
        raise ValidationError(f"{label} has an unexpected file type: {path.suffix}")
    if not path.is_file() or path.stat().st_size < 1_024:
        raise ValidationError(f"{label} is missing or empty: {value}")
    return path


def validate_image_signature(path: Path, label: str) -> None:
    header = path.read_bytes()[:24]
    suffix = path.suffix.lower()
    valid = {
        ".jpg": header.startswith(b"\xff\xd8\xff"),
        ".jpeg": header.startswith(b"\xff\xd8\xff"),
        ".webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
        ".avif": len(header) >= 12 and header[4:12] in {b"ftypavif", b"ftypavis"},
    }.get(suffix, False)
    if not valid:
        raise ValidationError(f"{label} does not contain a valid {suffix} image")


def validate_production_photo(city_id: str, photo: dict[str, Any]) -> None:
    if photo["curationStatus"] != "curated":
        raise ValidationError(f"{city_id} still has an uncurated photo")
    required_text = ("creator", "sourceUrl", "license", "attribution")
    if not all(isinstance(photo.get(field), str) and photo[field].strip() for field in required_text):
        raise ValidationError(f"{city_id} is missing photo provenance or attribution")
    source = urlparse(photo["sourceUrl"])
    if source.scheme != "https" or not source.netloc:
        raise ValidationError(f"{city_id} photo sourceUrl must be an absolute HTTPS URL")
    if "placeholder" in photo["alt"].casefold() or "placeholder" in photo["asset"].casefold():
        raise ValidationError(f"{city_id} still uses a placeholder photo")

    main = local_public_asset(photo["asset"], f"{city_id}.photo.asset", suffixes={".webp"})
    original = local_public_asset(
        photo["originalAsset"],
        f"{city_id}.photo.originalAsset",
        suffixes={".jpg", ".jpeg"},
    )
    derivatives = photo["optimizedAssets"]
    paths: list[Path] = [main, original]
    for format_name, expected_suffix in (("avif", ".avif"), ("webp", ".webp")):
        for index, asset in enumerate(derivatives[format_name]):
            paths.append(
                local_public_asset(
                    asset,
                    f"{city_id}.photo.optimizedAssets.{format_name}[{index}]",
                    suffixes={expected_suffix},
                )
            )
    if photo["asset"] not in derivatives["webp"]:
        raise ValidationError(f"{city_id} primary photo asset is not one of its WebP derivatives")
    if not any("-640." in asset for values in derivatives.values() for asset in values):
        raise ValidationError(f"{city_id} is missing a small responsive photo derivative")
    if not any("-1280." in asset for values in derivatives.values() for asset in values):
        raise ValidationError(f"{city_id} is missing a large responsive photo derivative")
    for path in paths:
        validate_image_signature(path, city_id)


def validate_city_catalog(production: bool) -> dict[str, Any]:
    catalog = load_json(DATA / "cities.mock.json")
    validate_schema(catalog, "cities.schema.json")

    category_ids = [item["id"] for item in catalog["categoryDefinitions"]]
    attribute_ids = [item["id"] for item in catalog["attributeDefinitions"]]
    unique(category_ids, "category definitions")
    unique(attribute_ids, "attribute definitions")
    categories = set(category_ids)
    attributes = set(attribute_ids)
    cities = catalog["cities"]
    unique([city["id"] for city in cities], "city IDs")
    unique([city["slug"] for city in cities], "city slugs")
    valid_timezone(catalog["origin"]["timezone"], "origin")

    schengen = set(load_json(DATA / "schengen.json")["countryCodes"])
    for city in cities:
        valid_timezone(city["timezone"], city["id"])
        unknown_categories = set(city["categories"]) - categories
        if unknown_categories:
            raise ValidationError(
                f"{city['id']} uses unknown categories: {sorted(unknown_categories)}"
            )
        actual_attributes = set(city["attributes"])
        if actual_attributes != attributes:
            missing = attributes - actual_attributes
            extra = actual_attributes - attributes
            raise ValidationError(
                f"{city['id']} attribute mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
            )
        if city["country"]["code"] not in schengen:
            raise ValidationError(f"{city['id']} is outside the configured Schengen scope")
        photo = city["photo"]
        if production:
            validate_production_photo(city["id"], photo)

    return catalog


def validate_terminals(catalog: dict[str, Any]) -> None:
    payload = load_json(DATA / "terminals.json")
    validate_schema(payload, "terminals.schema.json")
    terminals = payload["terminals"]
    terminal_ids = [item["id"] for item in terminals]
    unique(terminal_ids, "terminal IDs")
    terminal_set = set(terminal_ids)
    city_set = {city["id"] for city in catalog["cities"]}
    schengen = set(load_json(DATA / "schengen.json")["countryCodes"])

    missing_origins = set(payload["originTerminalIds"]) - terminal_set
    if missing_origins:
        raise ValidationError(f"origin terminal IDs do not exist: {sorted(missing_origins)}")

    for terminal in terminals:
        valid_timezone(terminal["timezone"], terminal["id"])
        if payload["strictSchengenPath"] and terminal["countryCode"] not in schengen:
            raise ValidationError(f"{terminal['id']} is outside strict Schengen scope")

    code_owners: dict[tuple[str, str], str] = {}
    for terminal in terminals:
        for code_type, code in terminal["codes"].items():
            normalized = code.upper()
            key = (code_type, normalized)
            if key in code_owners:
                raise ValidationError(
                    f"duplicate terminal code {code_type}={normalized}: "
                    f"{code_owners[key]} and {terminal['id']}"
                )
            code_owners[key] = terminal["id"]

    seen_links: set[tuple[str, str]] = set()
    for link in payload["cityTerminalLinks"]:
        key = (link["cityId"], link["terminalId"])
        if key in seen_links:
            raise ValidationError(f"duplicate city-terminal link: {key}")
        seen_links.add(key)
        if link["cityId"] not in city_set:
            raise ValidationError(f"unknown city in terminal link: {link['cityId']}")
        if link["terminalId"] not in terminal_set:
            raise ValidationError(f"unknown terminal in city link: {link['terminalId']}")
        terminal = next(item for item in terminals if item["id"] == link["terminalId"])
        relationship = link["relationship"]
        expected_kind = {
            "primary_airport": "airport",
            "nearby_airport": "airport",
            "primary_station": "rail",
            "nearby_station": "rail",
            "primary_bus": "bus",
            "nearby_bus": "bus",
        }.get(relationship)
        if expected_kind and terminal["kind"] != expected_kind:
            raise ValidationError(
                f"{link['terminalId']} is {terminal['kind']} but link is {relationship}"
            )

    linked_cities = {link["cityId"] for link in payload["cityTerminalLinks"]}
    missing_links = city_set - linked_cities
    if missing_links:
        raise ValidationError(f"cities without a terminal link: {sorted(missing_links)}")
    linked_or_origin = {link["terminalId"] for link in payload["cityTerminalLinks"]} | set(
        payload["originTerminalIds"]
    )
    orphaned = terminal_set - linked_or_origin
    if orphaned:
        raise ValidationError(f"terminals neither linked nor allowed as origins: {sorted(orphaned)}")

    seen_transfers: set[tuple[str, str]] = set()
    for transfer in payload.get("terminalTransfers", []):
        edge = (transfer["fromTerminalId"], transfer["toTerminalId"])
        if edge in seen_transfers:
            raise ValidationError(f"duplicate terminal transfer: {edge}")
        seen_transfers.add(edge)
        unknown = set(edge) - terminal_set
        if unknown:
            raise ValidationError(f"unknown terminal in transfer {edge}: {sorted(unknown)}")
        if transfer["costIsComplete"] and transfer["costEurCents"] is None:
            raise ValidationError(f"complete transfer cost is missing for {edge}")


def validate_sources() -> None:
    with (DATA / "sources.yaml").open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    validate_schema(payload, "sources.schema.json")
    sources = payload["sources"]
    source_ids = [item["id"] for item in sources]
    unique(source_ids, "source IDs")
    actual = set(source_ids)
    if actual != EXPECTED_SOURCES:
        raise ValidationError(
            f"source set mismatch; missing={sorted(EXPECTED_SOURCES - actual)}, "
            f"extra={sorted(actual - EXPECTED_SOURCES)}"
        )
    valid_modes = {"flight", "train", "bus"}
    valid_strategies = {"http_structured", "http_html", "playwright", "disabled"}
    for source in sources:
        if not set(source["modes"]) <= valid_modes:
            raise ValidationError(f"invalid mode for {source['id']}")
        if source["strategy"] not in valid_strategies:
            raise ValidationError(f"invalid strategy for {source['id']}")
        if source["enabled"] == source["kill_switch"]:
            raise ValidationError(
                f"{source['id']} enabled and kill_switch must be logical opposites"
            )
        if source["enabled"] != (source["state"] == "enabled"):
            raise ValidationError(f"{source['id']} state disagrees with enabled")
        if not source["enabled"] and source["rate_limit"]["max_concurrency"] != 0:
            raise ValidationError(f"{source['id']} disabled source must have zero concurrency")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--production",
        action="store_true",
        help="also require curated image assets and complete provenance",
    )
    args = parser.parse_args()
    try:
        catalog = validate_city_catalog(args.production)
        validate_terminals(catalog)
        validate_sources()
    except (ValidationError, jsonschema.ValidationError, KeyError, TypeError) as exc:
        print(f"data validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"validated {len(catalog['cities'])} cities, "
        f"{len(load_json(DATA / 'terminals.json')['terminals'])} terminals, "
        f"and {len(EXPECTED_SOURCES)} sources"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
