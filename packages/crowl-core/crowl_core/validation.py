"""Canonical journey validation independent of any extractor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .enums import Direction, ProtectionState, TransportMode
from .travel_windows import TravelWindowSpec


@dataclass(frozen=True, slots=True)
class Segment:
    origin_terminal_id: str
    destination_terminal_id: str
    departs_at: datetime
    arrives_at: datetime
    mode: TransportMode
    operator: str
    service_number: str | None = None
    protection_state: ProtectionState = ProtectionState.NOT_APPLICABLE


@dataclass(frozen=True, slots=True)
class TransferRule:
    from_terminal_id: str
    to_terminal_id: str
    minimum_minutes: int
    cost_eur_cents: int | None
    cost_is_complete: bool


@dataclass(frozen=True, slots=True)
class Itinerary:
    direction: Direction
    segments: tuple[Segment, ...]

    @property
    def departs_at(self) -> datetime:
        return self.segments[0].departs_at

    @property
    def arrives_at(self) -> datetime:
        return self.segments[-1].arrives_at

    @property
    def elapsed(self) -> timedelta:
        return _elapsed_between(self.departs_at, self.arrives_at)


@dataclass(frozen=True, slots=True)
class TripCandidate:
    outbound: Itinerary
    inbound: Itinerary
    destination_city_id: str
    total_price_eur_cents: int
    price_complete: bool = True
    passenger_age: int = 22
    student_eligible: bool = True
    mandatory_fees_included: bool = True
    smallest_personal_item_included: bool = True
    included_transfer_cost_eur_cents: int = 0
    itinerary_key: str = ""
    displayed_fare_service_fees_may_apply: bool = False

    @property
    def mode_bucket(self) -> TransportMode:
        modes = {segment.mode for route in (self.outbound, self.inbound) for segment in route.segments}
        return next(iter(modes)) if len(modes) == 1 else TransportMode.MIXED


@dataclass(frozen=True, slots=True)
class ValidationContext:
    travel_window: TravelWindowSpec
    allowed_origin_terminal_ids: frozenset[str]
    destination_terminal_ids: frozenset[str]
    schengen_terminal_ids: frozenset[str]
    strict_schengen_path: bool = True
    transfer_rules: tuple[TransferRule, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str


class TripValidationError(ValueError):
    def __init__(self, issues: Iterable[ValidationIssue]):
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.code}: {issue.message}" for issue in self.issues))


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _elapsed_between(start: datetime, end: datetime) -> timedelta:
    """Return real elapsed time, never same-zone wall-clock arithmetic."""

    if not _aware(start) or not _aware(end):
        raise ValueError("elapsed-time arithmetic requires timezone-aware datetimes")
    return end.astimezone(timezone.utc) - start.astimezone(timezone.utc)


def _validate_itinerary(
    itinerary: Itinerary,
    issues: list[ValidationIssue],
    transfer_rules: dict[tuple[str, str], TransferRule],
) -> int:
    if not itinerary.segments:
        issues.append(ValidationIssue("empty_itinerary", f"{itinerary.direction} has no segments"))
        return 0
    previous: Segment | None = None
    visited = {itinerary.segments[0].origin_terminal_id}
    required_transfer_cost = 0
    for index, segment in enumerate(itinerary.segments):
        if not _aware(segment.departs_at) or not _aware(segment.arrives_at):
            issues.append(
                ValidationIssue("naive_datetime", f"segment {index} must have timezone-aware times")
            )
            continue
        if (
            _aware(segment.departs_at)
            and _aware(segment.arrives_at)
            and _elapsed_between(segment.departs_at, segment.arrives_at) <= timedelta(0)
        ):
            issues.append(
                ValidationIssue("invalid_segment_time", f"segment {index} must arrive after departure")
            )
        if previous is not None:
            terminal_changed = previous.destination_terminal_id != segment.origin_terminal_id
            protected = segment.protection_state == ProtectionState.PROTECTED
            rule = transfer_rules.get(
                (previous.destination_terminal_id, segment.origin_terminal_id)
            )
            if terminal_changed or not protected:
                if not protected and rule is None:
                    issues.append(
                        ValidationIssue(
                            "unknown_transfer",
                            f"segment {index} has an unprotected connection without a trusted transfer rule",
                        )
                    )
                elif not protected and rule is not None:
                    if not rule.cost_is_complete or rule.cost_eur_cents is None:
                        issues.append(
                            ValidationIssue(
                                "unknown_transfer_cost",
                                f"segment {index} requires a transfer with unknown complete cost",
                            )
                        )
                    else:
                        required_transfer_cost += rule.cost_eur_cents
                    if _aware(segment.departs_at) and _aware(previous.arrives_at):
                        connection_minutes = int(
                            _elapsed_between(previous.arrives_at, segment.departs_at).total_seconds()
                            // 60
                        )
                        if connection_minutes < rule.minimum_minutes:
                            issues.append(
                                ValidationIssue(
                                    "insufficient_transfer_time",
                                    f"segment {index} does not meet the configured transfer minimum",
                                )
                            )
            if (
                _aware(segment.departs_at)
                and _aware(previous.arrives_at)
                and _elapsed_between(previous.arrives_at, segment.departs_at) < timedelta(0)
            ):
                issues.append(
                    ValidationIssue(
                        "negative_connection", f"segment {index} departs before the prior arrival"
                    )
                )
        if segment.destination_terminal_id in visited:
            issues.append(
                ValidationIssue("cyclic_itinerary", f"segment {index} revisits a prior terminal")
            )
        visited.add(segment.origin_terminal_id)
        visited.add(segment.destination_terminal_id)
        previous = segment
    return required_transfer_cost


def validate_trip(candidate: TripCandidate, context: ValidationContext) -> None:
    """Validate all trip invariants, raising one aggregate error when invalid."""

    issues: list[ValidationIssue] = []
    transfer_rules = {
        (rule.from_terminal_id, rule.to_terminal_id): rule for rule in context.transfer_rules
    }
    if candidate.outbound.direction != Direction.OUTBOUND:
        issues.append(ValidationIssue("wrong_direction", "outbound itinerary is not outbound"))
    if candidate.inbound.direction != Direction.RETURN:
        issues.append(ValidationIssue("wrong_direction", "return itinerary is not return"))
    required_transfer_cost = _validate_itinerary(candidate.outbound, issues, transfer_rules)
    required_transfer_cost += _validate_itinerary(candidate.inbound, issues, transfer_rules)
    if issues and any(issue.code == "empty_itinerary" for issue in issues):
        raise TripValidationError(issues)

    outbound = candidate.outbound
    inbound = candidate.inbound
    window = context.travel_window

    if outbound.segments[0].origin_terminal_id not in context.allowed_origin_terminal_ids:
        issues.append(ValidationIssue("origin_not_allowed", "outbound must start at an allowed Paris terminal"))
    if inbound.segments[-1].destination_terminal_id not in context.allowed_origin_terminal_ids:
        issues.append(ValidationIssue("return_not_allowed", "return must end at an allowed Paris terminal"))
    if outbound.segments[-1].destination_terminal_id not in context.destination_terminal_ids:
        issues.append(ValidationIssue("wrong_destination", "outbound does not end at the destination city"))
    if inbound.segments[0].origin_terminal_id not in context.destination_terminal_ids:
        issues.append(ValidationIssue("wrong_destination", "return does not start at the destination city"))

    if _aware(outbound.departs_at) and _elapsed_between(window.starts_at, outbound.departs_at) < timedelta(0):
        issues.append(ValidationIssue("too_early", "outbound departs before Friday 19:00 Europe/Paris"))
    if _aware(inbound.arrives_at) and _elapsed_between(inbound.arrives_at, window.ends_at) < timedelta(0):
        issues.append(ValidationIssue("too_late", "return arrives after Monday 07:00 Europe/Paris"))

    maximum = timedelta(minutes=window.maximum_direction_minutes)
    if _aware(outbound.departs_at) and _aware(outbound.arrives_at) and outbound.elapsed > maximum:
        issues.append(ValidationIssue("outbound_too_long", "outbound exceeds 12 hours"))
    if _aware(inbound.departs_at) and _aware(inbound.arrives_at) and inbound.elapsed > maximum:
        issues.append(ValidationIssue("return_too_long", "return exceeds 12 hours"))
    minimum_stay = timedelta(minutes=window.minimum_stay_minutes)
    if _aware(outbound.arrives_at) and _aware(inbound.departs_at):
        if _elapsed_between(outbound.arrives_at, inbound.departs_at) < minimum_stay:
            issues.append(ValidationIssue("stay_too_short", "destination stay is under 12 hours"))

    if context.strict_schengen_path:
        terminal_ids = {
            terminal_id
            for itinerary in (outbound, inbound)
            for segment in itinerary.segments
            for terminal_id in (segment.origin_terminal_id, segment.destination_terminal_id)
        }
        outside = terminal_ids - context.schengen_terminal_ids
        if outside:
            issues.append(
                ValidationIssue(
                    "non_schengen_path", "strict Schengen path contains: " + ", ".join(sorted(outside))
                )
            )

    if candidate.total_price_eur_cents <= 0:
        issues.append(ValidationIssue("invalid_price", "published price must be greater than zero"))
    if (
        not candidate.price_complete or not candidate.mandatory_fees_included
    ) and not candidate.displayed_fare_service_fees_may_apply:
        issues.append(ValidationIssue("incomplete_price", "price must include all mandatory fees"))
    if not candidate.smallest_personal_item_included:
        issues.append(ValidationIssue("baggage_missing", "smallest included personal item is required"))
    if candidate.included_transfer_cost_eur_cents != required_transfer_cost:
        issues.append(
            ValidationIssue(
                "transfer_cost_not_included",
                "the complete required transfer cost must be included in the total",
            )
        )
    if candidate.passenger_age != 22:
        issues.append(ValidationIssue("wrong_passenger", "profile is for one traveler aged 22"))
    if not candidate.student_eligible:
        issues.append(
            ValidationIssue(
                "unconfirmed_passenger_eligibility",
                "fare eligibility for the configured passenger is unconfirmed",
            )
        )

    if issues:
        raise TripValidationError(issues)


def select_best(candidates: Iterable[TripCandidate]) -> TripCandidate:
    """Select a deterministic winner after validation has already succeeded."""

    materialized = list(candidates)
    if not materialized:
        raise ValueError("cannot select a winner from an empty collection")

    def key(candidate: TripCandidate) -> tuple[int, float, int, str]:
        elapsed = candidate.outbound.elapsed + candidate.inbound.elapsed
        transfers = len(candidate.outbound.segments) + len(candidate.inbound.segments) - 2
        return (
            candidate.total_price_eur_cents,
            elapsed.total_seconds(),
            transfers,
            candidate.itinerary_key,
        )

    return min(materialized, key=key)
