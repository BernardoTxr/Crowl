from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[2]


def test_initial_migration_upgrades_to_expected_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "packages" / "crowl-core")
    environment["MIGRATION_DATABASE_URL"] = f"sqlite:///{database_path}"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "migrations" / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        version_num = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
        assert version_num == "0006_flixbus_fare_policy"
        # Alembic creates this column as VARCHAR(32) on PostgreSQL.
        assert len(version_num) <= 32
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        booking_columns = {
            item["name"]: item for item in inspector.get_columns("snapshot_bookings")
        }
        booking_checks = {
            item["name"] for item in inspector.get_check_constraints("snapshot_bookings")
        }
        crawl_columns = {item["name"] for item in inspector.get_columns("crawl_runs")}
        snapshot_columns = {
            item["name"] for item in inspector.get_columns("fare_snapshots")
        }
        duration_columns = {
            item["name"]
            for item in inspector.get_columns("duration_histograms_daily")
        }
    assert {
        "cities",
        "terminals",
        "crawl_runs",
        "extractor_runs",
        "fare_snapshots",
        "fare_snapshot_revisions",
        "fare_cell_observations",
        "snapshot_bookings",
        "snapshot_legs",
    } <= tables
    assert {
        "mandatory_fees_included",
        "price_completeness",
        "passenger_age",
        "passenger_student",
        "passenger_eligibility_confirmed",
        "bookable_at_observation",
        "is_teaser",
    } <= booking_columns.keys()
    assert "is_synthetic" in crawl_columns
    assert "is_synthetic" in snapshot_columns
    assert "is_synthetic" in duration_columns
    assert all(
        booking_columns[name]["nullable"] is False
        for name in (
            "mandatory_fees_included",
            "price_completeness",
            "passenger_age",
            "passenger_student",
            "passenger_eligibility_confirmed",
            "bookable_at_observation",
            "is_teaser",
        )
    )
    assert {
        "ck_snapshot_bookings_booking_price_policy",
        "ck_snapshot_bookings_booking_passenger_age_22",
        "ck_snapshot_bookings_booking_passenger_eligibility_confirmed",
        "ck_snapshot_bookings_booking_bookable_at_observation",
        "ck_snapshot_bookings_booking_not_teaser",
        "ck_snapshot_bookings_booking_student_fare_eligible",
    } <= booking_checks

    drift = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "migrations" / "alembic.ini"),
            "check",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert drift.returncode == 0, drift.stderr or drift.stdout
    assert "No new upgrade operations detected" in drift.stdout

    invalid_revision = text(
        """
        INSERT INTO fare_snapshot_revisions (
            fare_snapshot_id, revision_number, crawl_run_id, rule_version_id,
            profile_id, mode_bucket, total_price_eur_cents, availability,
            execution_coverage, source_scope_ratio, observed_at, published_at,
            transfer_count
        ) VALUES (
            1, 1, 1, 1, 1, 'flight', NULL, 'available', 'complete', 1.0,
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0
        )
        """
    )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(invalid_revision)


def test_initial_migration_compiles_for_postgresql_with_deferred_revision_fk() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "packages" / "crowl-core")
    environment["MIGRATION_DATABASE_URL"] = "postgresql://user:pass@localhost/crowl"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "migrations" / "alembic.ini"),
            "upgrade",
            "head",
            "--sql",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    sql = result.stdout
    revision_table = sql.index("CREATE TABLE fare_snapshot_revisions")
    current_pointer = sql.index("ADD CONSTRAINT fk_fare_snapshots_current_revision")
    assert revision_table < current_pointer
    assert "JSONB" in sql
    assert "CREATE CONSTRAINT TRIGGER crowl_snapshot_current_revision_guard" in sql
    assert "CREATE CONSTRAINT TRIGGER crowl_revision_dimensions_guard" in sql
    assert "CREATE CONSTRAINT TRIGGER crowl_leg_booking_revision_guard" in sql
    assert "CREATE CONSTRAINT TRIGGER crowl_revision_booking_total_guard" in sql
    assert "CREATE CONSTRAINT TRIGGER crowl_observation_trusted_revision_guard" in sql
    assert "trusted fare revision must match observation dimensions" in sql
    assert (
        "ADD COLUMN mandatory_fees_included BOOLEAN DEFAULT true NOT NULL"
        in sql
    )
    assert "ALTER COLUMN mandatory_fees_included DROP DEFAULT" in sql
    assert "ADD COLUMN passenger_age INTEGER DEFAULT 22 NOT NULL" in sql
    assert "ALTER COLUMN passenger_age DROP DEFAULT" in sql
    assert "ck_snapshot_bookings_booking_mandatory_fees_complete" in sql
    assert "ck_snapshot_bookings_booking_student_fare_eligible" in sql
    assert "ADD COLUMN price_completeness VARCHAR(9) DEFAULT 'complete' NOT NULL" in sql
    assert "ALTER COLUMN price_completeness DROP DEFAULT" in sql
    assert "ck_snapshot_bookings_booking_price_policy" in sql
