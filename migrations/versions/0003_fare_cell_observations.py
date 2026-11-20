"""add durable per-batch fare cell observations

Revision ID: 0003_fare_cell_observations
Revises: 0002_snapshot_invariants
Create Date: 2026-11-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0003_fare_cell_observations"
down_revision: Union[str, Sequence[str], None] = "0002_snapshot_invariants"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


POSTGRES_OBSERVATION_INVARIANT = """
CREATE FUNCTION crowl_enforce_observation_trusted_revision()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    revision_profile_id integer;
    revision_rule_version_id integer;
    revision_mode text;
    revision_availability text;
    snapshot_city_id text;
    snapshot_window_id integer;
    run_profile_id integer;
    run_rule_version_id integer;
BEGIN
    SELECT profile_id, rule_version_id
      INTO run_profile_id, run_rule_version_id
      FROM crawl_runs
     WHERE id = NEW.crawl_run_id;

    IF run_profile_id IS NULL
       OR NEW.profile_id <> run_profile_id
       OR NEW.rule_version_id <> run_rule_version_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'fare observation dimensions must match its crawl run';
    END IF;

    IF NEW.trusted_revision_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT revision.profile_id,
           revision.rule_version_id,
           revision.mode_bucket::text,
           revision.availability::text,
           snapshot.destination_city_id,
           snapshot.travel_window_id
      INTO revision_profile_id,
           revision_rule_version_id,
           revision_mode,
           revision_availability,
           snapshot_city_id,
           snapshot_window_id
      FROM fare_snapshot_revisions AS revision
      JOIN fare_snapshots AS snapshot
        ON snapshot.id = revision.fare_snapshot_id
     WHERE revision.id = NEW.trusted_revision_id;

    IF revision_profile_id IS NULL
       OR revision_availability <> 'available'
       OR NEW.profile_id <> revision_profile_id
       OR NEW.rule_version_id <> revision_rule_version_id
       OR NEW.mode_bucket::text <> revision_mode
       OR NEW.destination_city_id <> snapshot_city_id
       OR NEW.travel_window_id <> snapshot_window_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'trusted fare revision must match observation dimensions';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER crowl_observation_trusted_revision_guard
AFTER INSERT OR UPDATE ON fare_cell_observations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION crowl_enforce_observation_trusted_revision();
"""


POSTGRES_DROP_OBSERVATION_INVARIANT = """
DROP TRIGGER IF EXISTS crowl_observation_trusted_revision_guard
    ON fare_cell_observations;
DROP FUNCTION IF EXISTS crowl_enforce_observation_trusted_revision();
"""


def upgrade() -> None:
    op.create_table(
        "fare_cell_observations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("crawl_run_id", sa.Integer(), nullable=False),
        sa.Column("rule_version_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("destination_city_id", sa.String(length=96), nullable=False),
        sa.Column("travel_window_id", sa.Integer(), nullable=False),
        sa.Column(
            "mode_bucket",
            sa.Enum(
                "flight",
                "train",
                "bus",
                "mixed",
                name="observation_transport_mode",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "availability",
            sa.Enum(
                "available",
                "no_valid_option",
                "not_on_sale",
                "sold_out",
                "unknown_due_failure",
                name="observation_availability",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "execution_coverage",
            sa.Enum(
                "complete",
                "partial",
                "failed",
                "not_applicable",
                name="observation_execution_coverage",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("source_scope_ratio", sa.Float(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trusted_revision_id", sa.Integer(), nullable=True),
        sa.Column("diagnostic", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_scope_ratio >= 0 AND source_scope_ratio <= 1",
            name=op.f(
                "ck_fare_cell_observations_observation_source_scope_ratio_range"
            ),
        ),
        sa.CheckConstraint(
            "execution_coverage <> 'not_applicable'",
            name=op.f("ck_fare_cell_observations_observation_coverage_applicable"),
        ),
        sa.CheckConstraint(
            "availability <> 'available' OR trusted_revision_id IS NOT NULL",
            name=op.f(
                "ck_fare_cell_observations_available_observation_has_trusted_fare"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["crawl_run_id"],
            ["crawl_runs.id"],
            name=op.f("fk_fare_cell_observations_crawl_run_id_crawl_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id"],
            ["rule_versions.id"],
            name=op.f("fk_fare_cell_observations_rule_version_id_rule_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["search_profiles.id"],
            name=op.f("fk_fare_cell_observations_profile_id_search_profiles"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_city_id"],
            ["cities.id"],
            name=op.f("fk_fare_cell_observations_destination_city_id_cities"),
        ),
        sa.ForeignKeyConstraint(
            ["travel_window_id"],
            ["travel_windows.id"],
            name=op.f("fk_fare_cell_observations_travel_window_id_travel_windows"),
        ),
        sa.ForeignKeyConstraint(
            ["trusted_revision_id"],
            ["fare_snapshot_revisions.id"],
            name=op.f(
                "fk_fare_cell_observations_trusted_revision_id_fare_snapshot_revisions"
            ),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fare_cell_observations")),
        sa.UniqueConstraint(
            "crawl_run_id",
            "profile_id",
            "destination_city_id",
            "travel_window_id",
            "mode_bucket",
            name="uq_fare_cell_observations_batch_cell",
        ),
    )
    op.create_index(
        "ix_fare_cell_observations_current_lookup",
        "fare_cell_observations",
        [
            "profile_id",
            "destination_city_id",
            "travel_window_id",
            "mode_bucket",
            "observed_at",
        ],
        unique=False,
    )
    op.create_index(
        "ix_fare_cell_observations_crawl_run",
        "fare_cell_observations",
        ["crawl_run_id"],
        unique=False,
    )

    # Preserve the visible state of pre-overlay databases. Future batches add
    # explicit rows even when no fare is found.
    op.execute(
        sa.text(
            """
            INSERT INTO fare_cell_observations (
                crawl_run_id,
                rule_version_id,
                profile_id,
                destination_city_id,
                travel_window_id,
                mode_bucket,
                availability,
                execution_coverage,
                source_scope_ratio,
                observed_at,
                trusted_revision_id,
                diagnostic,
                created_at,
                updated_at
            )
            SELECT
                revision.crawl_run_id,
                revision.rule_version_id,
                snapshot.profile_id,
                snapshot.destination_city_id,
                snapshot.travel_window_id,
                snapshot.mode_bucket,
                revision.availability,
                revision.execution_coverage,
                revision.source_scope_ratio,
                revision.observed_at,
                revision.id,
                'MIGRATED_TRUSTED_FARE',
                revision.published_at,
                revision.published_at
            FROM fare_snapshots AS snapshot
            JOIN fare_snapshot_revisions AS revision
              ON revision.id = snapshot.current_revision_id
            """
        )
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(POSTGRES_OBSERVATION_INVARIANT)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(POSTGRES_DROP_OBSERVATION_INVARIANT)
    op.drop_index(
        "ix_fare_cell_observations_crawl_run",
        table_name="fare_cell_observations",
    )
    op.drop_index(
        "ix_fare_cell_observations_current_lookup",
        table_name="fare_cell_observations",
    )
    op.drop_table("fare_cell_observations")
