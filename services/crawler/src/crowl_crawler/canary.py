"""One-route, DB-free execution path for live public-page verification."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .adapters.registry import (
    CanaryAdapterBinding,
    CanaryPolicyError,
    build_canary_adapter,
)
from .adapters.catalog import source_catalog_path
from .manifest import DailyManifestBuilder, DestinationTarget
from .models import (
    ConnectorResult,
    NormalizedOffer,
    SearchOutcome,
    TransportMode,
)


MAX_REPORTED_OFFERS = 20


class CanaryInputError(ValueError):
    """The requested canary is not a bounded, catalog-backed route."""


class CanaryConfigurationError(ValueError):
    """The selected provider lacks a valid public-page crawl recipe."""


class CanaryExecutionError(RuntimeError):
    """A provider escaped the typed connector-result contract."""


async def run_live_canary(
    *,
    source_slug: str,
    origin_terminal_id: str,
    destination_city_id: str,
    destination_terminal_id: str,
    weekend_start: date,
    override_kill_switch_for_canary: bool,
    source_catalog: str | Path | None = None,
) -> dict[str, object]:
    """Execute exactly one round-trip request without opening a database.

    The only mutable external activity is the provider-page GET/render already
    owned by the selected adapter.  No database engine, publisher, health sink,
    file writer, or production registry is constructed on this path.
    """

    if weekend_start.weekday() != 4:
        raise CanaryInputError("live-canary --weekend must be a Friday")

    binding = _build_binding(
        source_slug=source_slug,
        origin_terminal_id=origin_terminal_id,
        destination_terminal_id=destination_terminal_id,
        override_kill_switch_for_canary=override_kill_switch_for_canary,
        source_catalog=source_catalog,
    )
    try:
        _validate_route_scope(
            binding,
            origin_terminal_id=origin_terminal_id,
            destination_city_id=destination_city_id,
            destination_terminal_id=destination_terminal_id,
            source_catalog=source_catalog,
        )
        metadata = binding.adapter.metadata()
        if not metadata.enabled:
            raise CanaryConfigurationError(
                "provider public-page configuration is incomplete"
            )

        manifest = DailyManifestBuilder(
            origin_terminal_ids=(origin_terminal_id,),
            config_version="live-canary-v1",
        ).build(
            scheduled_for=weekend_start,
            source_slugs=(source_slug,),
            destinations=(
                DestinationTarget(
                    destination_city_id,
                    (destination_terminal_id,),
                ),
            ),
            weekend_fridays=(weekend_start,),
            canary_per_source=True,
            include_one_way_tasks=False,
        )
        if len(manifest.tasks) != 1 or not manifest.tasks[0].is_canary:
            raise CanaryExecutionError("bounded canary manifest invariant failed")

        try:
            results = await binding.adapter.canary((manifest.tasks[0].request,))
        except Exception as error:
            # Provider/browser exception messages may contain URLs or tokens.
            # Keep the public failure stable and deliberately discard details.
            raise CanaryExecutionError(
                "provider canary failed outside the typed result contract"
            ) from error
        if len(results) != 1:
            raise CanaryExecutionError("provider returned an invalid canary result count")

        return _report(
            binding,
            origin_terminal_id=origin_terminal_id,
            destination_city_id=destination_city_id,
            destination_terminal_id=destination_terminal_id,
            weekend_start=weekend_start,
            result=results[0],
        )
    finally:
        await binding.adapter.close()


def canary_succeeded(report: dict[str, object]) -> bool:
    result = report.get("result")
    if not isinstance(result, dict):
        return False
    try:
        outcome = SearchOutcome(str(result["outcome"]))
    except (KeyError, ValueError):
        return False
    return outcome in {
        SearchOutcome.SUCCESS_RESULTS,
        SearchOutcome.SUCCESS_EMPTY,
        SearchOutcome.NOT_ON_SALE,
        SearchOutcome.NO_SERVICE,
        SearchOutcome.SOLD_OUT,
        SearchOutcome.PARTIAL_SUCCESS,
    }


def _build_binding(
    *,
    source_slug: str,
    origin_terminal_id: str,
    destination_terminal_id: str,
    override_kill_switch_for_canary: bool,
    source_catalog: str | Path | None,
) -> CanaryAdapterBinding:
    try:
        return build_canary_adapter(
            source_slug,
            override_kill_switch_for_canary=override_kill_switch_for_canary,
            terminal_pair=(origin_terminal_id, destination_terminal_id),
            source_catalog=source_catalog,
        )
    except CanaryPolicyError:
        raise
    except Exception as error:
        # Configuration parsers intentionally raise without making a request.
        # Do not surface values from environment-backed URLs or aliases.
        raise CanaryConfigurationError(
            "provider public-page configuration is invalid"
        ) from error


def _validate_route_scope(
    binding: CanaryAdapterBinding,
    *,
    origin_terminal_id: str,
    destination_city_id: str,
    destination_terminal_id: str,
    source_catalog: str | Path | None,
) -> None:
    catalog_file = source_catalog_path(source_catalog)
    terminal_file = catalog_file.with_name("terminals.json")
    try:
        payload = json.loads(terminal_file.read_text(encoding="utf-8"))
        terminals = payload["terminals"]
        origin_ids = payload["originTerminalIds"]
        links = payload["cityTerminalLinks"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise CanaryInputError("terminal catalog is unavailable") from error
    if not isinstance(terminals, list) or not isinstance(origin_ids, list):
        raise CanaryInputError("terminal catalog has an invalid shape")
    if not isinstance(links, list):
        raise CanaryInputError("city-terminal catalog has an invalid shape")

    terminal_by_id: dict[str, dict[str, Any]] = {}
    for item in terminals:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            terminal_by_id[item["id"]] = item
    origin = terminal_by_id.get(origin_terminal_id)
    destination = terminal_by_id.get(destination_terminal_id)
    if origin is None:
        raise CanaryInputError("origin terminal is not in the terminal catalog")
    if origin_terminal_id not in origin_ids:
        raise CanaryInputError("origin terminal is not an allowed Paris/Massy origin")
    if destination is None:
        raise CanaryInputError("destination terminal is not in the terminal catalog")
    if origin_terminal_id == destination_terminal_id:
        raise CanaryInputError("origin and destination terminals must differ")
    if not any(
        isinstance(link, dict)
        and link.get("cityId") == destination_city_id
        and link.get("terminalId") == destination_terminal_id
        for link in links
    ):
        raise CanaryInputError(
            "destination terminal is not linked to the selected destination city"
        )

    expected_kinds = {
        TransportMode.FLIGHT: {"airport"},
        TransportMode.TRAIN: {"rail", "station"},
        TransportMode.BUS: {"bus"},
    }
    allowed_kinds: set[str] = set()
    for mode in binding.catalog_spec.modes:
        allowed_kinds.update(expected_kinds.get(mode, set()))
    if origin.get("kind") not in allowed_kinds:
        raise CanaryInputError("origin terminal kind does not match the source mode")
    if destination.get("kind") not in allowed_kinds:
        raise CanaryInputError("destination terminal kind does not match the source mode")


def _report(
    binding: CanaryAdapterBinding,
    *,
    origin_terminal_id: str,
    destination_city_id: str,
    destination_terminal_id: str,
    weekend_start: date,
    result: ConnectorResult,
) -> dict[str, object]:
    metadata = binding.adapter.metadata()
    offers = sorted(
        result.offers,
        key=lambda offer: (
            offer.total_amount_minor,
            offer.currency,
            offer.provider_offer_id,
        ),
    )
    rendered_offers = [_offer_payload(offer) for offer in offers[:MAX_REPORTED_OFFERS]]
    return {
        "kind": "live_canary",
        "persistence": "none",
        "source": {
            "slug": metadata.source_slug,
            "strategy": metadata.strategy.value,
            "catalog_enabled": binding.catalog_spec.catalog_enabled,
            "catalog_kill_switch": binding.catalog_spec.kill_switch,
            "catalog_override_used": binding.catalog_override_used,
        },
        "scope": {
            "origin_terminal_id": origin_terminal_id,
            "destination_city_id": destination_city_id,
            "destination_terminal_ids": [destination_terminal_id],
            "weekend_start": weekend_start.isoformat(),
            "search_kind": "round_trip",
            "passengers": 1,
            "passenger_age": 22,
            "passenger_student": True,
            "currency": "EUR",
        },
        "result": {
            "outcome": result.outcome.value,
            "diagnostic_code": result.diagnostic_code,
            "retryable": result.retryable,
            "expected_count": result.expected_count,
            "completed_count": result.completed_count,
            "parser_version": result.parser_version,
            "observed_at": result.observed_at.isoformat(),
            "response_fingerprint": result.response_fingerprint,
            "schema_fingerprint": result.schema_fingerprint,
            "offer_count": len(offers),
            "reported_offer_count": len(rendered_offers),
            "offers_truncated": len(offers) > len(rendered_offers),
            "offers": rendered_offers,
        },
    }


def _offer_payload(offer: NormalizedOffer) -> dict[str, object]:
    return {
        "source_slug": offer.source_slug,
        "destination_city_id": offer.destination_city_id,
        "pricing_semantics": offer.pricing_semantics.value,
        "price_completeness": offer.price_completeness.value,
        "total_amount_minor": offer.total_amount_minor,
        "currency": offer.currency,
        "mandatory_fees_included": offer.mandatory_fees_included,
        "passenger_eligibility_confirmed": offer.passenger_eligibility_confirmed,
        "bookable": offer.bookable,
        "is_teaser": offer.is_teaser,
        "included_baggage": offer.included_baggage,
        "outbound_segments": [_segment_payload(item) for item in offer.outbound_segments],
        "return_segments": [_segment_payload(item) for item in offer.return_segments],
    }


def _segment_payload(segment: Any) -> dict[str, object]:
    return {
        "sequence": segment.sequence,
        "mode": segment.mode.value,
        "operator": segment.operator,
        "service_number": segment.service_number,
        "departure_terminal_id": segment.departure_terminal_id,
        "arrival_terminal_id": segment.arrival_terminal_id,
        "departs_at": segment.departs_at.isoformat(),
        "arrives_at": segment.arrives_at.isoformat(),
        "protected_connection": segment.protected_connection,
    }
