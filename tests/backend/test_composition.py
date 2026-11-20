from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from crowl_core.composition import (
    CompositionLimits,
    OneWayCandidate,
    compose_round_trips,
    itinerary_identity,
)
from crowl_core.enums import Direction, ProtectionState, TransportMode
from crowl_core.travel_windows import TravelWindowSpec
from crowl_core.validation import Itinerary, Segment, TransferRule, ValidationContext


OBSERVED_AT = datetime(2026, 3, 20, 12, tzinfo=timezone.utc)


def _context() -> ValidationContext:
    window = TravelWindowSpec.for_friday(date(2026, 3, 27))
    return ValidationContext(
        travel_window=window,
        allowed_origin_terminal_ids=frozenset(
            {"par-ory", "par-cdg", "par-gare-du-nord", "massy-tgv"}
        ),
        destination_terminal_ids=frozenset({"vie"}),
        schengen_terminal_ids=frozenset(
            {"par-ory", "par-cdg", "par-gare-du-nord", "massy-tgv", "vie"}
        ),
    )


def _offer(
    direction: Direction,
    mode: TransportMode,
    price: int,
    *,
    source: str,
    offer_key: str,
    origin: str | None = None,
    destination: str | None = None,
    departure_offset: timedelta | None = None,
    duration: timedelta = timedelta(hours=2),
    observed_at: datetime = OBSERVED_AT,
    direct_operator_booking: bool = False,
) -> OneWayCandidate:
    window = _context().travel_window
    if direction == Direction.OUTBOUND:
        departs_at = window.starts_at + (departure_offset or timedelta(hours=1))
        origin = origin or "par-ory"
        destination = destination or "vie"
    else:
        departs_at = window.ends_at - (departure_offset or timedelta(hours=4))
        origin = origin or "vie"
        destination = destination or "par-ory"
    return OneWayCandidate(
        offer_key=offer_key,
        source_id=source,
        itinerary=Itinerary(
            direction=direction,
            segments=(
                Segment(
                    origin_terminal_id=origin,
                    destination_terminal_id=destination,
                    departs_at=departs_at,
                    arrives_at=departs_at + duration,
                    mode=mode,
                    operator=f"{mode.value} operator",
                    service_number=offer_key,
                ),
            ),
        ),
        price_eur_cents=price,
        observed_at=observed_at,
        direct_operator_booking=direct_operator_booking,
    )


def _compose(outbound, inbound, *, context=None, limits=None):
    return compose_round_trips(
        outbound,
        inbound,
        destination_city_id="vienna-at",
        validation_context=context or _context(),
        limits=limits,
    )


def test_composes_different_sources_modes_and_paris_return_terminal() -> None:
    outbound = _offer(
        Direction.OUTBOUND,
        TransportMode.FLIGHT,
        4_500,
        source="flight_source",
        offer_key="out-flight",
        origin="par-ory",
    )
    inbound = _offer(
        Direction.RETURN,
        TransportMode.TRAIN,
        3_500,
        source="rail_source",
        offer_key="return-train",
        destination="massy-tgv",
    )

    result = _compose([outbound], [inbound])
    winner = result.winner()

    assert winner is not None
    assert winner.mode_bucket == TransportMode.MIXED
    assert winner.total_price_eur_cents == 8_000
    assert winner.outbound.source_id == "flight_source"
    assert winner.inbound.source_id == "rail_source"
    assert winner.candidate.outbound.segments[0].origin_terminal_id == "par-ory"
    assert winner.candidate.inbound.segments[-1].destination_terminal_id == "massy-tgv"


def test_returns_cheapest_candidates_for_each_mode_and_overall() -> None:
    outbound = [
        _offer(Direction.OUTBOUND, mode, price, source=f"{mode}-out", offer_key=f"{mode}-out")
        for mode, price in (
            (TransportMode.FLIGHT, 3_000),
            (TransportMode.TRAIN, 4_000),
            (TransportMode.BUS, 5_000),
        )
    ]
    inbound = [
        _offer(Direction.RETURN, mode, price, source=f"{mode}-in", offer_key=f"{mode}-in")
        for mode, price in (
            (TransportMode.FLIGHT, 3_100),
            (TransportMode.TRAIN, 4_100),
            (TransportMode.BUS, 5_100),
        )
    ]

    result = _compose(outbound, inbound)

    assert result.winner(TransportMode.FLIGHT).total_price_eur_cents == 6_100
    assert result.winner(TransportMode.TRAIN).total_price_eur_cents == 8_100
    assert result.winner(TransportMode.BUS).total_price_eur_cents == 10_100
    assert result.winner(TransportMode.MIXED).total_price_eur_cents == 7_100
    assert result.winner().mode_bucket == TransportMode.FLIGHT
    assert result.winner().total_price_eur_cents == 6_100


def test_directional_identity_dedup_chooses_cheapest_seller_deterministically() -> None:
    expensive = _offer(
        Direction.OUTBOUND,
        TransportMode.FLIGHT,
        8_000,
        source="reseller",
        offer_key="same-service",
    )
    cheap = replace(
        expensive,
        offer_key="same-service-cheap",
        source_id="operator",
        price_eur_cents=5_000,
        direct_operator_booking=True,
    )
    inbound = _offer(
        Direction.RETURN,
        TransportMode.FLIGHT,
        4_000,
        source="operator",
        offer_key="return",
    )

    assert itinerary_identity(expensive.itinerary) == itinerary_identity(cheap.itinerary)
    result = _compose([expensive, cheap], [inbound])

    assert len(result.by_mode[TransportMode.FLIGHT]) == 1
    assert result.winner().outbound.source_id == "operator"
    assert result.winner().total_price_eur_cents == 9_000


def test_canonical_time_schengen_and_price_rules_reject_invalid_pairs() -> None:
    valid_return = _offer(
        Direction.RETURN,
        TransportMode.FLIGHT,
        4_000,
        source="return",
        offer_key="return",
    )
    valid_outbound = _offer(
        Direction.OUTBOUND,
        TransportMode.FLIGHT,
        4_000,
        source="outbound",
        offer_key="valid",
    )
    window = _context().travel_window

    too_early = replace(
        valid_outbound,
        offer_key="early",
        itinerary=replace(
            valid_outbound.itinerary,
            segments=(
                replace(
                    valid_outbound.itinerary.segments[0],
                    departs_at=window.starts_at - timedelta(seconds=1),
                    arrives_at=window.starts_at + timedelta(hours=1),
                    service_number="early",
                ),
            ),
        ),
    )
    non_schengen_segment = replace(
        valid_outbound.itinerary.segments[0],
        destination_terminal_id="lhr",
        service_number="outside",
    )
    back_to_destination = Segment(
        origin_terminal_id="lhr",
        destination_terminal_id="vie",
        departs_at=non_schengen_segment.arrives_at + timedelta(minutes=30),
        arrives_at=non_schengen_segment.arrives_at + timedelta(hours=2),
        mode=TransportMode.FLIGHT,
        operator="flight operator",
        service_number="outside-2",
    )
    outside = replace(
        valid_outbound,
        offer_key="outside",
        itinerary=replace(
            valid_outbound.itinerary,
            segments=(non_schengen_segment, back_to_destination),
        ),
    )
    incomplete = replace(valid_outbound, offer_key="incomplete", price_complete=False)

    for invalid in (too_early, outside, incomplete):
        result = _compose([invalid], [valid_return])
        assert result.winner() is None


def test_observation_skew_and_minimum_stay_are_enforced() -> None:
    outbound = _offer(
        Direction.OUTBOUND,
        TransportMode.FLIGHT,
        4_000,
        source="outbound",
        offer_key="outbound",
    )
    stale_return = _offer(
        Direction.RETURN,
        TransportMode.FLIGHT,
        4_000,
        source="return",
        offer_key="stale-return",
        observed_at=OBSERVED_AT + timedelta(hours=2, seconds=1),
    )
    assert _compose([outbound], [stale_return]).winner() is None

    short_stay_departure = outbound.itinerary.arrives_at + timedelta(hours=11, minutes=59)
    short_stay = _offer(
        Direction.RETURN,
        TransportMode.FLIGHT,
        4_000,
        source="return",
        offer_key="short-stay",
    )
    short_stay = replace(
        short_stay,
        itinerary=replace(
            short_stay.itinerary,
            segments=(
                replace(
                    short_stay.itinerary.segments[0],
                    departs_at=short_stay_departure,
                    arrives_at=short_stay_departure + timedelta(hours=2),
                ),
            ),
        ),
    )
    assert _compose([outbound], [short_stay]).winner() is None


def test_unprotected_terminal_transfer_requires_rule_time_and_complete_cost() -> None:
    context = _context()
    window = context.travel_window
    first = Segment(
        origin_terminal_id="par-ory",
        destination_terminal_id="par-cdg",
        departs_at=window.starts_at,
        arrives_at=window.starts_at + timedelta(hours=1),
        mode=TransportMode.BUS,
        operator="transfer feeder",
        service_number="feeder",
    )
    second = Segment(
        origin_terminal_id="par-ory",
        destination_terminal_id="vie",
        departs_at=window.starts_at + timedelta(hours=2, minutes=30),
        arrives_at=window.starts_at + timedelta(hours=4),
        mode=TransportMode.FLIGHT,
        operator="flight operator",
        service_number="flight",
        protection_state=ProtectionState.UNPROTECTED_TRANSFER,
    )
    outbound = OneWayCandidate(
        offer_key="terminal-change",
        source_id="two-sellers",
        itinerary=Itinerary(Direction.OUTBOUND, (first, second)),
        price_eur_cents=6_800,
        observed_at=OBSERVED_AT,
        included_transfer_cost_eur_cents=1_800,
    )
    inbound = _offer(
        Direction.RETURN,
        TransportMode.FLIGHT,
        4_000,
        source="return",
        offer_key="return",
    )

    assert _compose([outbound], [inbound], context=context).winner() is None

    transfer_context = replace(
        context,
        transfer_rules=(
            TransferRule("par-cdg", "par-ory", 90, 1_800, True),
        ),
    )
    winner = _compose([outbound], [inbound], context=transfer_context).winner()
    assert winner is not None
    assert winner.mode_bucket == TransportMode.MIXED
    assert winner.total_price_eur_cents == 10_800


def test_best_first_pairing_is_deterministic_and_hard_capped() -> None:
    outbound = [
        _offer(
            Direction.OUTBOUND,
            TransportMode.FLIGHT,
            2_000 + index * 10,
            source="outbound",
            offer_key=f"out-{index}",
            departure_offset=timedelta(hours=1, minutes=index),
        )
        for index in range(10)
    ]
    inbound = [
        _offer(
            Direction.RETURN,
            TransportMode.FLIGHT,
            2_000 + index * 10,
            source="return",
            offer_key=f"in-{index}",
            departure_offset=timedelta(hours=4, minutes=index),
        )
        for index in range(10)
    ]
    limits = CompositionLimits(max_pair_evaluations=7, max_results_per_bucket=3)

    first = _compose(outbound, inbound, limits=limits)
    second = _compose(reversed(outbound), reversed(inbound), limits=limits)

    assert first.evaluated_pairs == 7
    assert first.possible_pairs_after_pruning == 100
    assert first.truncated is True
    assert [item.itinerary_key for item in first.overall] == [
        item.itinerary_key for item in second.overall
    ]
    assert first.winner().total_price_eur_cents == 4_000


def test_directional_pruning_preserves_each_mode_and_marks_truncation() -> None:
    outbound = [
        _offer(
            Direction.OUTBOUND,
            mode,
            3_000 + index,
            source="out",
            offer_key=f"{mode}-out-{index}",
            departure_offset=timedelta(hours=1, minutes=index),
        )
        for mode in (TransportMode.FLIGHT, TransportMode.TRAIN, TransportMode.BUS)
        for index in range(4)
    ]
    inbound = [
        _offer(
            Direction.RETURN,
            mode,
            3_000,
            source="in",
            offer_key=f"{mode}-in",
        )
        for mode in (TransportMode.FLIGHT, TransportMode.TRAIN, TransportMode.BUS)
    ]
    result = _compose(
        outbound,
        inbound,
        limits=CompositionLimits(
            max_offers_per_mode_and_direction=2,
            max_pair_evaluations=100,
            max_results_per_bucket=2,
        ),
    )

    assert result.truncated is True
    assert all(result.winner(mode) is not None for mode in (
        TransportMode.FLIGHT,
        TransportMode.TRAIN,
        TransportMode.BUS,
        TransportMode.MIXED,
    ))
