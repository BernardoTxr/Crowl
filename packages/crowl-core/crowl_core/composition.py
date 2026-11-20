"""Bounded, source-agnostic composition of independent one-way offers.

Crawler publishers normalize provider results into :class:`OneWayCandidate`
instances containing the canonical :class:`~crowl_core.validation.Itinerary`.
They then call :func:`compose_round_trips` with the same
:class:`~crowl_core.validation.ValidationContext` used by publication. The
composer never imports an adapter and never relaxes canonical trip validation.
"""

from __future__ import annotations

import hashlib
import heapq
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import inf
from typing import Iterable, Iterator, Mapping

from .enums import Direction, TransportMode
from .validation import (
    Itinerary,
    TripCandidate,
    TripValidationError,
    ValidationContext,
    validate_trip,
)


MODE_ORDER = (
    TransportMode.FLIGHT,
    TransportMode.TRAIN,
    TransportMode.BUS,
    TransportMode.MIXED,
)


@dataclass(frozen=True, slots=True)
class OneWayCandidate:
    """A complete, independently bookable normalized directional offer."""

    offer_key: str
    source_id: str
    itinerary: Itinerary
    price_eur_cents: int
    observed_at: datetime
    price_complete: bool = True
    mandatory_fees_included: bool = True
    smallest_personal_item_included: bool = True
    passenger_age: int = 22
    student_eligible: bool = True
    included_transfer_cost_eur_cents: int = 0
    direct_operator_booking: bool = False


@dataclass(frozen=True, slots=True)
class CompositionLimits:
    """Hard bounds that keep route-calendar composition predictable."""

    max_offers_per_mode_and_direction: int = 100
    max_pair_evaluations: int = 20_000
    max_results_per_bucket: int = 5
    max_observation_skew: timedelta = timedelta(hours=2)

    def __post_init__(self) -> None:
        if self.max_offers_per_mode_and_direction < 1:
            raise ValueError("max_offers_per_mode_and_direction must be positive")
        if self.max_pair_evaluations < 1:
            raise ValueError("max_pair_evaluations must be positive")
        if self.max_results_per_bucket < 1:
            raise ValueError("max_results_per_bucket must be positive")
        if self.max_observation_skew < timedelta(0):
            raise ValueError("max_observation_skew cannot be negative")


@dataclass(frozen=True, slots=True)
class ComposedRoundTrip:
    """A valid round trip with the two source offers retained for publication."""

    candidate: TripCandidate
    outbound: OneWayCandidate
    inbound: OneWayCandidate

    @property
    def itinerary_key(self) -> str:
        return self.candidate.itinerary_key

    @property
    def mode_bucket(self) -> TransportMode:
        return self.candidate.mode_bucket

    @property
    def total_price_eur_cents(self) -> int:
        return self.candidate.total_price_eur_cents


@dataclass(frozen=True, slots=True)
class CompositionResult:
    """Ranked winners plus bounded-work diagnostics."""

    by_mode: Mapping[TransportMode, tuple[ComposedRoundTrip, ...]]
    overall: tuple[ComposedRoundTrip, ...]
    evaluated_pairs: int
    rejected_pairs: int
    possible_pairs_after_pruning: int
    truncated: bool

    def winner(self, mode: TransportMode | None = None) -> ComposedRoundTrip | None:
        candidates = self.overall if mode is None else self.by_mode.get(mode, ())
        return candidates[0] if candidates else None


def itinerary_identity(itinerary: Itinerary) -> str:
    """Hash schedule identity, excluding seller and price by design."""

    fields = [itinerary.direction.value]
    for index, segment in enumerate(itinerary.segments):
        fields.extend(
            (
                str(index),
                segment.origin_terminal_id,
                segment.destination_terminal_id,
                _datetime_identity(segment.departs_at),
                _datetime_identity(segment.arrives_at),
                segment.mode.value,
                segment.operator,
                segment.service_number or "",
            )
        )
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def compose_round_trips(
    outbound: Iterable[OneWayCandidate],
    inbound: Iterable[OneWayCandidate],
    *,
    destination_city_id: str,
    validation_context: ValidationContext,
    limits: CompositionLimits | None = None,
) -> CompositionResult:
    """Compose and rank valid round trips without Cartesian-product blow-up.

    Identical directional itineraries are deduplicated to their cheapest
    eligible seller first. Each mode-pair frontier is then traversed in
    non-decreasing component-price order with a global evaluation cap and fair
    round-robin allocation across the four result buckets.
    """

    bounds = limits or CompositionLimits()
    outbound_groups, outbound_pruned = _prepare_direction(
        outbound, Direction.OUTBOUND, bounds.max_offers_per_mode_and_direction
    )
    inbound_groups, inbound_pruned = _prepare_direction(
        inbound, Direction.RETURN, bounds.max_offers_per_mode_and_direction
    )
    combinations = _mode_combinations(outbound_groups, inbound_groups)
    possible_pairs = sum(
        len(outbound_groups[out_mode]) * len(inbound_groups[in_mode])
        for pairs in combinations.values()
        for out_mode, in_mode in pairs
    )
    iterators: dict[
        TransportMode, Iterator[tuple[OneWayCandidate, OneWayCandidate]]
    ] = {
        bucket: _iter_bucket_pairs(
            outbound_groups,
            inbound_groups,
            pairs,
        )
        for bucket, pairs in combinations.items()
        if pairs
    }
    winners: dict[TransportMode, dict[str, ComposedRoundTrip]] = {
        mode: {} for mode in MODE_ORDER
    }
    evaluated = 0
    rejected = 0
    active = [(mode, iterators[mode]) for mode in MODE_ORDER if mode in iterators]

    while active and evaluated < bounds.max_pair_evaluations:
        next_active: list[
            tuple[TransportMode, Iterator[tuple[OneWayCandidate, OneWayCandidate]]]
        ] = []
        for bucket, iterator in active:
            if evaluated >= bounds.max_pair_evaluations:
                next_active.append((bucket, iterator))
                continue
            try:
                outbound_offer, inbound_offer = next(iterator)
            except StopIteration:
                continue
            next_active.append((bucket, iterator))
            evaluated += 1
            composed = _compose_pair(
                outbound_offer,
                inbound_offer,
                destination_city_id=destination_city_id,
                validation_context=validation_context,
                maximum_observation_skew=bounds.max_observation_skew,
            )
            if composed is None:
                rejected += 1
                continue
            prior = winners[bucket].get(composed.itinerary_key)
            if prior is None or _round_trip_rank(composed) < _round_trip_rank(prior):
                winners[bucket][composed.itinerary_key] = composed
        active = next_active

    ranked: dict[TransportMode, tuple[ComposedRoundTrip, ...]] = {}
    for mode in MODE_ORDER:
        ranked[mode] = tuple(
            sorted(winners[mode].values(), key=_round_trip_rank)[
                : bounds.max_results_per_bucket
            ]
        )
    all_ranked = sorted(
        (candidate for mode in MODE_ORDER for candidate in ranked[mode]),
        key=_round_trip_rank,
    )
    overall = tuple(all_ranked[: bounds.max_results_per_bucket])
    return CompositionResult(
        by_mode=ranked,
        overall=overall,
        evaluated_pairs=evaluated,
        rejected_pairs=rejected,
        possible_pairs_after_pruning=possible_pairs,
        truncated=outbound_pruned
        or inbound_pruned
        or evaluated < possible_pairs,
    )


def _prepare_direction(
    candidates: Iterable[OneWayCandidate],
    expected_direction: Direction,
    maximum_per_mode: int,
) -> tuple[dict[TransportMode, list[OneWayCandidate]], bool]:
    deduplicated: dict[str, OneWayCandidate] = {}
    for candidate in candidates:
        if not _eligible_one_way(candidate, expected_direction):
            continue
        identity = itinerary_identity(candidate.itinerary)
        prior = deduplicated.get(identity)
        if prior is None or _one_way_rank(candidate) < _one_way_rank(prior):
            deduplicated[identity] = candidate

    groups: dict[TransportMode, list[OneWayCandidate]] = {mode: [] for mode in MODE_ORDER}
    for candidate in deduplicated.values():
        groups[_direction_mode(candidate.itinerary)].append(candidate)
    pruned = False
    for mode in MODE_ORDER:
        groups[mode].sort(key=_one_way_rank)
        if len(groups[mode]) > maximum_per_mode:
            groups[mode] = groups[mode][:maximum_per_mode]
            pruned = True
    return groups, pruned


def _eligible_one_way(candidate: OneWayCandidate, expected_direction: Direction) -> bool:
    if candidate.itinerary.direction != expected_direction or not candidate.itinerary.segments:
        return False
    if not candidate.price_complete or not candidate.mandatory_fees_included:
        return False
    if not candidate.smallest_personal_item_included or candidate.price_eur_cents <= 0:
        return False
    if candidate.included_transfer_cost_eur_cents < 0:
        return False
    if candidate.included_transfer_cost_eur_cents > candidate.price_eur_cents:
        return False
    return _aware(candidate.observed_at)


def _mode_combinations(
    outbound: Mapping[TransportMode, list[OneWayCandidate]],
    inbound: Mapping[TransportMode, list[OneWayCandidate]],
) -> dict[TransportMode, list[tuple[TransportMode, TransportMode]]]:
    combinations: dict[TransportMode, list[tuple[TransportMode, TransportMode]]] = {
        mode: [] for mode in MODE_ORDER
    }
    for outbound_mode in MODE_ORDER:
        if not outbound[outbound_mode]:
            continue
        for inbound_mode in MODE_ORDER:
            if not inbound[inbound_mode]:
                continue
            if outbound_mode == inbound_mode and outbound_mode != TransportMode.MIXED:
                bucket = outbound_mode
            else:
                bucket = TransportMode.MIXED
            combinations[bucket].append((outbound_mode, inbound_mode))
    return combinations


def _iter_bucket_pairs(
    outbound: Mapping[TransportMode, list[OneWayCandidate]],
    inbound: Mapping[TransportMode, list[OneWayCandidate]],
    combinations: list[tuple[TransportMode, TransportMode]],
) -> Iterator[tuple[OneWayCandidate, OneWayCandidate]]:
    heap: list[tuple[int, str, str, int, int, int]] = []
    visited: set[tuple[int, int, int]] = set()

    def push(combination_index: int, outbound_index: int, inbound_index: int) -> None:
        marker = (combination_index, outbound_index, inbound_index)
        if marker in visited:
            return
        outbound_mode, inbound_mode = combinations[combination_index]
        outbound_items = outbound[outbound_mode]
        inbound_items = inbound[inbound_mode]
        if outbound_index >= len(outbound_items) or inbound_index >= len(inbound_items):
            return
        visited.add(marker)
        outbound_offer = outbound_items[outbound_index]
        inbound_offer = inbound_items[inbound_index]
        heapq.heappush(
            heap,
            (
                outbound_offer.price_eur_cents + inbound_offer.price_eur_cents,
                outbound_offer.offer_key,
                inbound_offer.offer_key,
                combination_index,
                outbound_index,
                inbound_index,
            ),
        )

    for combination_index in range(len(combinations)):
        push(combination_index, 0, 0)
    while heap:
        _, _, _, combination_index, outbound_index, inbound_index = heapq.heappop(heap)
        outbound_mode, inbound_mode = combinations[combination_index]
        yield outbound[outbound_mode][outbound_index], inbound[inbound_mode][inbound_index]
        push(combination_index, outbound_index + 1, inbound_index)
        push(combination_index, outbound_index, inbound_index + 1)


def _compose_pair(
    outbound: OneWayCandidate,
    inbound: OneWayCandidate,
    *,
    destination_city_id: str,
    validation_context: ValidationContext,
    maximum_observation_skew: timedelta,
) -> ComposedRoundTrip | None:
    if outbound.passenger_age != inbound.passenger_age:
        return None
    if abs(_utc(outbound.observed_at) - _utc(inbound.observed_at)) > maximum_observation_skew:
        return None
    identity = hashlib.sha256(
        (
            itinerary_identity(outbound.itinerary)
            + "\x1e"
            + itinerary_identity(inbound.itinerary)
        ).encode("ascii")
    ).hexdigest()
    candidate = TripCandidate(
        outbound=outbound.itinerary,
        inbound=inbound.itinerary,
        destination_city_id=destination_city_id,
        total_price_eur_cents=outbound.price_eur_cents + inbound.price_eur_cents,
        price_complete=outbound.price_complete and inbound.price_complete,
        passenger_age=outbound.passenger_age,
        student_eligible=outbound.student_eligible and inbound.student_eligible,
        mandatory_fees_included=(
            outbound.mandatory_fees_included and inbound.mandatory_fees_included
        ),
        smallest_personal_item_included=(
            outbound.smallest_personal_item_included
            and inbound.smallest_personal_item_included
        ),
        included_transfer_cost_eur_cents=(
            outbound.included_transfer_cost_eur_cents
            + inbound.included_transfer_cost_eur_cents
        ),
        itinerary_key=identity,
    )
    try:
        validate_trip(candidate, validation_context)
    except (TripValidationError, TypeError, OverflowError):
        return None
    return ComposedRoundTrip(candidate=candidate, outbound=outbound, inbound=inbound)


def _round_trip_rank(candidate: ComposedRoundTrip) -> tuple[int, bool, float, int, str, str, str]:
    elapsed = candidate.candidate.outbound.elapsed + candidate.candidate.inbound.elapsed
    transfers = (
        len(candidate.candidate.outbound.segments)
        + len(candidate.candidate.inbound.segments)
        - 2
    )
    direct_operator = (
        candidate.outbound.direct_operator_booking
        and candidate.inbound.direct_operator_booking
    )
    return (
        candidate.total_price_eur_cents,
        not direct_operator,
        elapsed.total_seconds(),
        transfers,
        candidate.itinerary_key,
        candidate.outbound.source_id,
        candidate.inbound.source_id,
    )


def _one_way_rank(candidate: OneWayCandidate) -> tuple[int, bool, float, int, str, str]:
    return (
        candidate.price_eur_cents,
        not candidate.direct_operator_booking,
        _safe_elapsed_seconds(candidate.itinerary),
        max(0, len(candidate.itinerary.segments) - 1),
        candidate.source_id,
        candidate.offer_key,
    )


def _direction_mode(itinerary: Itinerary) -> TransportMode:
    modes = {segment.mode for segment in itinerary.segments}
    if len(modes) == 1:
        mode = next(iter(modes))
        return mode if mode != TransportMode.MIXED else TransportMode.MIXED
    return TransportMode.MIXED


def _safe_elapsed_seconds(itinerary: Itinerary) -> float:
    try:
        return itinerary.elapsed.total_seconds()
    except (IndexError, TypeError, OverflowError):
        return inf


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _datetime_identity(value: datetime) -> str:
    return _utc(value).isoformat() if _aware(value) else "naive:" + value.isoformat()
