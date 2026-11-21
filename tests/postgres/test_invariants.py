from __future__ import annotations

import os
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


DATABASE_URL = os.getenv("MIGRATION_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL.startswith("postgresql"),
    reason="PostgreSQL invariant tests require MIGRATION_DATABASE_URL",
)


def _seed_valid_revision(connection) -> dict[str, int | str]:
    token = uuid4().hex[:12]
    city_id = f"test-city-{token}"
    source_id = f"test-source-{token}"
    origin_id = f"test-origin-{token}"
    destination_id = f"test-destination-{token}"

    connection.execute(
        text(
            """
            INSERT INTO cities (
                id, slug, name, country_code, country_name, timezone,
                latitude, longitude, visited, wishlist, categories,
                attributes, summary, active, created_at, updated_at
            ) VALUES (
                :city_id, :city_id, 'Test City', 'FR', 'France', 'Europe/Paris',
                48.0, 2.0, false, true, CAST('[]' AS jsonb), CAST('{}' AS jsonb),
                'PostgreSQL invariant fixture', true, now(), now()
            )
            """
        ),
        {"city_id": city_id},
    )
    for terminal_id, name in (
        (origin_id, "Test Paris Origin"),
        (destination_id, "Test Destination"),
    ):
        connection.execute(
            text(
                """
                INSERT INTO terminals (
                    id, name, kind, country_code, timezone, latitude,
                    longitude, schengen_eligible, active, created_at, updated_at
                ) VALUES (
                    :terminal_id, :name, 'rail', 'FR', 'Europe/Paris', 48.0,
                    2.0, true, true, now(), now()
                )
                """
            ),
            {"terminal_id": terminal_id, "name": name},
        )
    connection.execute(
        text(
            """
            INSERT INTO sources (
                slug, name, supported_modes, strategy, is_requested, enabled,
                disabled_reason, connector_version, parser_version,
                created_at, updated_at
            ) VALUES (
                :source_id, 'Test Source', CAST('["train"]' AS jsonb), 'test',
                false, true, NULL, 'test', 'test', now(), now()
            )
            """
        ),
        {"source_id": source_id},
    )
    rule_id = connection.execute(
        text(
            """
            INSERT INTO rule_versions (
                config_hash, version_label, schengen_scope_version,
                strict_schengen_path, rules, created_at, updated_at
            ) VALUES (
                :hash, 'test', 'test', true, CAST('{}' AS jsonb), now(), now()
            ) RETURNING id
            """
        ),
        {"hash": f"rule-{token}"},
    ).scalar_one()
    profile_id = connection.execute(
        text(
            """
            INSERT INTO search_profiles (
                config_hash, name, passenger_age, student, currency, baggage,
                entitlements, created_at, updated_at
            ) VALUES (
                :hash, 'Test', 22, true, 'EUR', 'personal_item',
                CAST('{}' AS jsonb), now(), now()
            ) RETURNING id
            """
        ),
        {"hash": f"profile-{token}"},
    ).scalar_one()
    window_id = connection.execute(
        text(
            """
            INSERT INTO travel_windows (
                friday_date, starts_at, ends_at, timezone,
                minimum_stay_minutes, maximum_direction_minutes,
                created_at, updated_at
            ) VALUES (
                :friday, '2035-01-05 18:00:00+00', '2035-01-08 06:00:00+00',
                'Europe/Paris', 720, 720, now(), now()
            ) RETURNING id
            """
        ),
        {"friday": date(2035, 1, 5)},
    ).scalar_one()
    run_id = connection.execute(
        text(
            """
            INSERT INTO crawl_runs (
                scheduled_for, rule_version_id, profile_id, manifest_version,
                release_version, status, expected_cells, completed_cells,
                successful_cells, created_at, updated_at
            ) VALUES (
                :scheduled_for, :rule_id, :profile_id, 'test', 'test', 'succeeded',
                1, 1, 1, now(), now()
            ) RETURNING id
            """
        ),
        {
            "scheduled_for": date(2034, 12, 1),
            "rule_id": rule_id,
            "profile_id": profile_id,
        },
    ).scalar_one()
    snapshot_id = connection.execute(
        text(
            """
            INSERT INTO fare_snapshots (
                observed_on, profile_id, destination_city_id, travel_window_id,
                mode_bucket, created_at, updated_at
            ) VALUES (
                :observed_on, :profile_id, :city_id, :window_id, 'train',
                now(), now()
            ) RETURNING id
            """
        ),
        {
            "observed_on": date(2034, 12, 1),
            "profile_id": profile_id,
            "city_id": city_id,
            "window_id": window_id,
        },
    ).scalar_one()
    revision_id = connection.execute(
        text(
            """
            INSERT INTO fare_snapshot_revisions (
                fare_snapshot_id, revision_number, crawl_run_id, rule_version_id,
                profile_id, mode_bucket, total_price_eur_cents, availability,
                execution_coverage, source_scope_ratio, observed_at, published_at,
                outbound_duration_minutes, return_duration_minutes,
                stay_duration_minutes, transfer_count, itinerary_key
            ) VALUES (
                :snapshot_id, 1, :run_id, :rule_id, :profile_id, 'train', 1000,
                'available', 'complete', 1.0, now(), now(), 120, 120, 720, 0,
                :itinerary_key
            ) RETURNING id
            """
        ),
        {
            "snapshot_id": snapshot_id,
            "run_id": run_id,
            "rule_id": rule_id,
            "profile_id": profile_id,
            "itinerary_key": f"itinerary-{token}",
        },
    ).scalar_one()
    booking_id = connection.execute(
        text(
            """
            INSERT INTO snapshot_bookings (
                revision_id, component_order, source_id, seller,
                pricing_semantics, original_amount_minor, original_currency,
                eur_amount_cents, fx_rate, fx_rate_date, student_fare_used,
                price_completeness, mandatory_fees_included, passenger_age, passenger_student,
                passenger_eligibility_confirmed, bookable_at_observation,
                is_teaser
            ) VALUES (
                :revision_id, 0, :source_id, 'Test Source', 'round_trip', 1000,
                'EUR', 1000, 1.0, :rate_date, false, 'complete', true, 22, true, true,
                true, false
            ) RETURNING id
            """
        ),
        {
            "revision_id": revision_id,
            "source_id": source_id,
            "rate_date": date(2034, 12, 1),
        },
    ).scalar_one()
    connection.execute(
        text(
            "UPDATE fare_snapshots SET current_revision_id = :revision_id "
            "WHERE id = :snapshot_id"
        ),
        {"revision_id": revision_id, "snapshot_id": snapshot_id},
    )
    return {
        "city_id": city_id,
        "source_id": source_id,
        "origin_id": origin_id,
        "destination_id": destination_id,
        "rule_id": rule_id,
        "profile_id": profile_id,
        "window_id": window_id,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "revision_id": revision_id,
        "booking_id": booking_id,
    }


def _connection():
    engine = create_engine(DATABASE_URL)
    connection = engine.connect()
    transaction = connection.begin()
    return engine, connection, transaction


def test_valid_snapshot_graph_satisfies_deferred_guards() -> None:
    engine, connection, transaction = _connection()
    try:
        _seed_valid_revision(connection)
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_current_revision_cannot_point_to_another_snapshot() -> None:
    engine, connection, transaction = _connection()
    try:
        data = _seed_valid_revision(connection)
        second_snapshot_id = connection.execute(
            text(
                """
                INSERT INTO fare_snapshots (
                    observed_on, profile_id, destination_city_id, travel_window_id,
                    mode_bucket, current_revision_id, created_at, updated_at
                ) VALUES (
                    '2034-12-02', :profile_id, :city_id, :window_id, 'train',
                    :revision_id, now(), now()
                ) RETURNING id
                """
            ),
            data,
        ).scalar_one()
        assert second_snapshot_id != data["snapshot_id"]
        with pytest.raises(IntegrityError, match="same snapshot"):
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_leg_cannot_reference_another_revisions_booking() -> None:
    engine, connection, transaction = _connection()
    try:
        data = _seed_valid_revision(connection)
        second_revision_id = connection.execute(
            text(
                """
                INSERT INTO fare_snapshot_revisions (
                    fare_snapshot_id, revision_number, crawl_run_id,
                    rule_version_id, profile_id, mode_bucket,
                    total_price_eur_cents, availability, execution_coverage,
                    source_scope_ratio, observed_at, published_at,
                    outbound_duration_minutes, return_duration_minutes,
                    stay_duration_minutes, transfer_count, itinerary_key
                ) VALUES (
                    :snapshot_id, 2, :run_id, :rule_id, :profile_id, 'train',
                    1000, 'available', 'complete', 1.0, now(), now(), 120, 120,
                    720, 0, 'second-itinerary'
                ) RETURNING id
                """
            ),
            data,
        ).scalar_one()
        second_booking_id = connection.execute(
            text(
                """
                INSERT INTO snapshot_bookings (
                    revision_id, component_order, source_id, seller,
                    pricing_semantics, original_amount_minor, original_currency,
                    eur_amount_cents, fx_rate, fx_rate_date, student_fare_used,
                    price_completeness, mandatory_fees_included, passenger_age, passenger_student,
                    passenger_eligibility_confirmed, bookable_at_observation,
                    is_teaser
                ) VALUES (
                    :revision_id, 0, :source_id, 'Test Source', 'round_trip',
                    1000, 'EUR', 1000, 1.0, '2034-12-01', false, 'complete', true, 22,
                    true, true, true, false
                ) RETURNING id
                """
            ),
            {**data, "revision_id": second_revision_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO snapshot_legs (
                    revision_id, booking_id, direction, segment_order, mode,
                    operator, origin_terminal_id, destination_terminal_id,
                    departs_at, arrives_at, origin_timezone,
                    destination_timezone, protection_state
                ) VALUES (
                    :revision_id, :booking_id, 'outbound', 0, 'train', 'Test',
                    :origin_id, :destination_id, '2035-01-05 19:00:00+00',
                    '2035-01-05 21:00:00+00', 'Europe/Paris', 'Europe/Paris',
                    'protected'
                )
                """
            ),
            {**data, "booking_id": second_booking_id},
        )
        with pytest.raises(IntegrityError, match="same revision"):
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_available_revision_booking_components_must_sum_to_total() -> None:
    engine, connection, transaction = _connection()
    try:
        data = _seed_valid_revision(connection)
        connection.execute(
            text(
                "UPDATE snapshot_bookings SET eur_amount_cents = 999 "
                "WHERE id = :booking_id"
            ),
            data,
        )
        with pytest.raises(IntegrityError, match="sum to its total price"):
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_flixbus_displayed_fare_policy_is_accepted() -> None:
    engine, connection, transaction = _connection()
    try:
        data = _seed_valid_revision(connection)
        connection.execute(
            text(
                """
                INSERT INTO sources (
                    slug, name, supported_modes, strategy, is_requested, enabled,
                    disabled_reason, connector_version, parser_version,
                    created_at, updated_at
                ) VALUES (
                    'flixbus', 'FlixBus', CAST('["bus"]' AS jsonb), 'playwright',
                    true, true, NULL, 'test', 'test', now(), now()
                ) ON CONFLICT (slug) DO NOTHING
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE snapshot_bookings
                   SET source_id = 'flixbus',
                       price_completeness = 'estimated',
                       mandatory_fees_included = false
                 WHERE id = :booking_id
                """
            ),
            data,
        )
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_non_flixbus_incomplete_fare_policy_is_rejected() -> None:
    engine, connection, transaction = _connection()
    try:
        data = _seed_valid_revision(connection)
        with pytest.raises(IntegrityError, match="booking_price_policy"):
            connection.execute(
                text(
                    """
                    UPDATE snapshot_bookings
                       SET price_completeness = 'estimated',
                           mandatory_fees_included = false
                     WHERE id = :booking_id
                    """
                ),
                data,
            )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
