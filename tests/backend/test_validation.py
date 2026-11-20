from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from crowl_core.enums import Direction, ProtectionState, TransportMode
from crowl_core.travel_windows import TravelWindowSpec
from crowl_core.validation import (
    Itinerary,
    Segment,
    TransferRule,
    TripCandidate,
    TripValidationError,
    ValidationContext,
    select_best,
    validate_trip,
)


@pytest.fixture
def valid_trip() -> tuple[TripCandidate, ValidationContext]:
    window = TravelWindowSpec.for_friday(date(2026, 3, 27))
    outbound = Itinerary(
        direction=Direction.OUTBOUND,
        segments=(
            Segment(
                origin_terminal_id="par-ory",
                destination_terminal_id="vie",
                departs_at=window.starts_at,
                arrives_at=window.starts_at + timedelta(hours=12),
                mode=TransportMode.FLIGHT,
                operator="Example Air",
            ),
        ),
    )
    inbound = Itinerary(
        direction=Direction.RETURN,
        segments=(
            Segment(
                origin_terminal_id="vie",
                destination_terminal_id="massy-tgv",
                departs_at=window.ends_at - timedelta(hours=12),
                arrives_at=window.ends_at,
                mode=TransportMode.TRAIN,
                operator="Example Rail",
            ),
        ),
    )
    candidate = TripCandidate(
        outbound=outbound,
        inbound=inbound,
        destination_city_id="vienna-at",
        total_price_eur_cents=10_000,
        itinerary_key="valid",
    )
    context = ValidationContext(
        travel_window=window,
        allowed_origin_terminal_ids=frozenset({"par-ory", "massy-tgv"}),
        destination_terminal_ids=frozenset({"vie"}),
        schengen_terminal_ids=frozenset({"par-ory", "massy-tgv", "vie"}),
    )
    return candidate, context


def test_exact_business_rule_boundaries_are_valid(valid_trip) -> None:
    candidate, context = valid_trip

    validate_trip(candidate, context)
    assert candidate.mode_bucket == TransportMode.MIXED


@pytest.mark.parametrize(
    ("mutation", "issue_code"),
    [
        ("early", "too_early"),
        ("late", "too_late"),
        ("long", "outbound_too_long"),
        ("short_stay", "stay_too_short"),
        ("zero_price", "invalid_price"),
        ("incomplete_price", "incomplete_price"),
        ("unconfirmed_eligibility", "unconfirmed_passenger_eligibility"),
        ("outside_schengen", "non_schengen_path"),
    ],
)
def test_business_rule_violations_are_explicit(valid_trip, mutation: str, issue_code: str) -> None:
    candidate, context = valid_trip
    if mutation == "early":
        first = replace(
            candidate.outbound.segments[0],
            departs_at=context.travel_window.starts_at - timedelta(microseconds=1),
        )
        candidate = replace(candidate, outbound=replace(candidate.outbound, segments=(first,)))
    elif mutation == "late":
        segment = replace(
            candidate.inbound.segments[0],
            arrives_at=context.travel_window.ends_at + timedelta(microseconds=1),
        )
        candidate = replace(candidate, inbound=replace(candidate.inbound, segments=(segment,)))
    elif mutation == "long":
        segment = replace(
            candidate.outbound.segments[0],
            arrives_at=candidate.outbound.departs_at + timedelta(hours=12, microseconds=1),
        )
        candidate = replace(candidate, outbound=replace(candidate.outbound, segments=(segment,)))
    elif mutation == "short_stay":
        segment = replace(
            candidate.inbound.segments[0],
            departs_at=candidate.outbound.arrives_at + timedelta(hours=11, minutes=59),
            arrives_at=candidate.outbound.arrives_at + timedelta(hours=12, minutes=59),
        )
        candidate = replace(candidate, inbound=replace(candidate.inbound, segments=(segment,)))
    elif mutation == "zero_price":
        candidate = replace(candidate, total_price_eur_cents=0)
    elif mutation == "incomplete_price":
        candidate = replace(candidate, price_complete=False)
    elif mutation == "unconfirmed_eligibility":
        candidate = replace(candidate, student_eligible=False)
    elif mutation == "outside_schengen":
        segment = replace(candidate.outbound.segments[0], destination_terminal_id="lhr")
        candidate = replace(candidate, outbound=replace(candidate.outbound, segments=(segment,)))
        context = replace(context, destination_terminal_ids=frozenset({"lhr"}))

    with pytest.raises(TripValidationError) as captured:
        validate_trip(candidate, context)
    assert issue_code in {issue.code for issue in captured.value.issues}


def test_deterministic_winner_uses_price_duration_transfers_then_key(valid_trip) -> None:
    candidate, _ = valid_trip
    expensive = replace(candidate, total_price_eur_cents=10_001, itinerary_key="a")
    cheap_z = replace(candidate, itinerary_key="z")
    cheap_a = replace(candidate, itinerary_key="a")

    assert select_best([expensive, cheap_z, cheap_a]) is cheap_a


def test_unprotected_terminal_change_requires_time_rule_and_complete_cost(valid_trip) -> None:
    candidate, context = valid_trip
    first = replace(
        candidate.outbound.segments[0],
        destination_terminal_id="cdg",
        arrives_at=context.travel_window.starts_at + timedelta(hours=1),
    )
    second = Segment(
        origin_terminal_id="ory",
        destination_terminal_id="vie",
        departs_at=context.travel_window.starts_at + timedelta(hours=2, minutes=30),
        arrives_at=context.travel_window.starts_at + timedelta(hours=4),
        mode=TransportMode.FLIGHT,
        operator="Example Air",
        protection_state=ProtectionState.UNPROTECTED_TRANSFER,
    )
    candidate = replace(
        candidate,
        outbound=replace(candidate.outbound, segments=(first, second)),
        included_transfer_cost_eur_cents=1_800,
    )
    context = replace(
        context,
        schengen_terminal_ids=context.schengen_terminal_ids | {"cdg", "ory"},
        transfer_rules=(
            TransferRule(
                from_terminal_id="cdg",
                to_terminal_id="ory",
                minimum_minutes=90,
                cost_eur_cents=1_800,
                cost_is_complete=True,
            ),
        ),
    )

    validate_trip(candidate, context)

    with pytest.raises(TripValidationError) as missing_rule:
        validate_trip(candidate, replace(context, transfer_rules=()))
    assert "unknown_transfer" in {issue.code for issue in missing_rule.value.issues}

    with pytest.raises(TripValidationError) as missing_cost:
        validate_trip(replace(candidate, included_transfer_cost_eur_cents=0), context)
    assert "transfer_cost_not_included" in {issue.code for issue in missing_cost.value.issues}


def test_unprotected_same_terminal_connection_still_requires_a_minimum_rule(valid_trip) -> None:
    candidate, context = valid_trip
    first = replace(
        candidate.outbound.segments[0],
        destination_terminal_id="connection-platform",
        arrives_at=context.travel_window.starts_at + timedelta(hours=1),
    )
    second = Segment(
        origin_terminal_id="connection-platform",
        destination_terminal_id="vie",
        departs_at=first.arrives_at + timedelta(minutes=1),
        arrives_at=first.arrives_at + timedelta(hours=2),
        mode=TransportMode.TRAIN,
        operator="Separate Ticket Rail",
        protection_state=ProtectionState.UNPROTECTED_TRANSFER,
    )
    candidate = replace(candidate, outbound=replace(candidate.outbound, segments=(first, second)))
    context = replace(
        context,
        schengen_terminal_ids=context.schengen_terminal_ids | {"connection-platform"},
    )

    with pytest.raises(TripValidationError) as missing_rule:
        validate_trip(candidate, context)
    assert "unknown_transfer" in {issue.code for issue in missing_rule.value.issues}

    rule = TransferRule(
        from_terminal_id="connection-platform",
        to_terminal_id="connection-platform",
        minimum_minutes=30,
        cost_eur_cents=0,
        cost_is_complete=True,
    )
    with pytest.raises(TripValidationError) as too_short:
        validate_trip(candidate, replace(context, transfer_rules=(rule,)))
    assert "insufficient_transfer_time" in {issue.code for issue in too_short.value.issues}

    second = replace(second, departs_at=first.arrives_at + timedelta(minutes=30))
    validate_trip(
        replace(candidate, outbound=replace(candidate.outbound, segments=(first, second))),
        replace(context, transfer_rules=(rule,)),
    )


def test_direction_duration_uses_real_elapsed_time_across_dst() -> None:
    paris = ZoneInfo("Europe/Paris")

    def trip(friday: date, departure: datetime, arrival: datetime) -> tuple[TripCandidate, ValidationContext]:
        window = TravelWindowSpec.for_friday(friday)
        outbound = Itinerary(
            direction=Direction.OUTBOUND,
            segments=(Segment("par-ory", "vie", departure, arrival, TransportMode.FLIGHT, "DST Air"),),
        )
        inbound = Itinerary(
            direction=Direction.RETURN,
            segments=(
                Segment(
                    "vie",
                    "par-ory",
                    datetime.combine(friday + timedelta(days=2), datetime.min.time(), paris).replace(hour=21),
                    datetime.combine(friday + timedelta(days=3), datetime.min.time(), paris).replace(hour=6),
                    TransportMode.FLIGHT,
                    "DST Air",
                ),
            ),
        )
        candidate = TripCandidate(outbound, inbound, "vienna-at", 10_000)
        context = ValidationContext(
            travel_window=window,
            allowed_origin_terminal_ids=frozenset({"par-ory"}),
            destination_terminal_ids=frozenset({"vie"}),
            schengen_terminal_ids=frozenset({"par-ory", "vie"}),
        )
        return candidate, context

    spring_trip, spring_context = trip(
        date(2026, 3, 27),
        datetime(2026, 3, 28, 20, 0, tzinfo=paris),
        datetime(2026, 3, 29, 8, 30, tzinfo=paris),
    )
    assert spring_trip.outbound.elapsed == timedelta(hours=11, minutes=30)
    validate_trip(spring_trip, spring_context)

    autumn_trip, autumn_context = trip(
        date(2026, 10, 23),
        datetime(2026, 10, 24, 20, 0, tzinfo=paris),
        datetime(2026, 10, 25, 7, 30, tzinfo=paris),
    )
    assert autumn_trip.outbound.elapsed == timedelta(hours=12, minutes=30)
    with pytest.raises(TripValidationError) as too_long:
        validate_trip(autumn_trip, autumn_context)
    assert "outbound_too_long" in {issue.code for issue in too_long.value.issues}
