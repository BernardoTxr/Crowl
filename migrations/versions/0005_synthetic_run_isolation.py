"""isolate synthetic fixture runs from operational daily runs

Revision ID: 0005_synthetic_run_isolation
Revises: 0004_booking_trust_evidence
Create Date: 2026-11-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005_synthetic_run_isolation"
down_revision: Union[str, Sequence[str], None] = "0004_booking_trust_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_crawl_runs_scheduled_for", table_name="crawl_runs")
    op.add_column(
        "crawl_runs",
        sa.Column(
            "is_synthetic",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.create_unique_constraint(
            "uq_crawl_runs_scheduled_for_synthetic",
            ["scheduled_for", "is_synthetic"],
        )
    op.create_index(
        "ix_crawl_runs_scheduled_for",
        "crawl_runs",
        ["scheduled_for"],
        unique=False,
    )
    op.execute(
        sa.text(
            """
            UPDATE crawl_runs
               SET is_synthetic = true
             WHERE release_version = 'development-fixture'
                OR manifest_version = 'fixture-v1'
                OR diagnostic LIKE 'Synthetic%'
            """
        )
    )

    op.add_column(
        "fare_snapshots",
        sa.Column(
            "is_synthetic",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    with op.batch_alter_table("fare_snapshots") as batch_op:
        batch_op.drop_constraint("uq_fare_snapshots_daily_cell", type_="unique")
        batch_op.create_unique_constraint(
            "uq_fare_snapshots_daily_cell_provenance",
            [
                "observed_on",
                "profile_id",
                "destination_city_id",
                "travel_window_id",
                "mode_bucket",
                "is_synthetic",
            ],
        )
    op.execute(
        sa.text(
            """
            UPDATE fare_snapshots
               SET is_synthetic = true
             WHERE current_revision_id IN (
                   SELECT revisions.id
                     FROM fare_snapshot_revisions AS revisions
                     JOIN crawl_runs AS runs
                       ON runs.id = revisions.crawl_run_id
                    WHERE runs.is_synthetic = true
             )
            """
        )
    )

    op.add_column(
        "duration_histograms_daily",
        sa.Column(
            "is_synthetic",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    with op.batch_alter_table("duration_histograms_daily") as batch_op:
        batch_op.drop_constraint(
            "pk_duration_histograms_daily",
            type_="primary",
        )
        batch_op.create_primary_key(
            "pk_duration_histograms_daily",
            ["observed_on", "city_id", "mode", "is_synthetic"],
        )
    op.execute(
        sa.text(
            """
            UPDATE duration_histograms_daily AS histograms
               SET is_synthetic = true
             WHERE EXISTS (
                   SELECT 1
                     FROM crawl_runs AS runs
                    WHERE runs.scheduled_for = histograms.observed_on
                      AND runs.is_synthetic = true
             )
            """
        )
    )


def downgrade() -> None:
    # A database that contains both an operational and a synthetic run for the
    # same day cannot be represented by the older schema without data loss.
    connection = op.get_bind()
    collision = connection.execute(
        sa.text(
            """
            SELECT scheduled_for
              FROM crawl_runs
             GROUP BY scheduled_for
            HAVING COUNT(*) > 1
             LIMIT 1
            """
        )
    ).first()
    if collision is not None:
        raise RuntimeError(
            "cannot downgrade while operational and synthetic runs share a date"
        )

    snapshot_collision = connection.execute(
        sa.text(
            """
            SELECT observed_on, profile_id, destination_city_id,
                   travel_window_id, mode_bucket
              FROM fare_snapshots
             GROUP BY observed_on, profile_id, destination_city_id,
                      travel_window_id, mode_bucket
            HAVING COUNT(*) > 1
             LIMIT 1
            """
        )
    ).first()
    if snapshot_collision is not None:
        raise RuntimeError(
            "cannot downgrade while operational and synthetic snapshots share a cell"
        )

    histogram_collision = connection.execute(
        sa.text(
            """
            SELECT observed_on, city_id, mode
              FROM duration_histograms_daily
             GROUP BY observed_on, city_id, mode
            HAVING COUNT(*) > 1
             LIMIT 1
            """
        )
    ).first()
    if histogram_collision is not None:
        raise RuntimeError(
            "cannot downgrade while operational and synthetic duration rows share a cell"
        )

    with op.batch_alter_table("duration_histograms_daily") as batch_op:
        batch_op.drop_constraint(
            "pk_duration_histograms_daily",
            type_="primary",
        )
        batch_op.create_primary_key(
            "pk_duration_histograms_daily",
            ["observed_on", "city_id", "mode"],
        )
        batch_op.drop_column("is_synthetic")

    with op.batch_alter_table("fare_snapshots") as batch_op:
        batch_op.drop_constraint(
            "uq_fare_snapshots_daily_cell_provenance",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_fare_snapshots_daily_cell",
            [
                "observed_on",
                "profile_id",
                "destination_city_id",
                "travel_window_id",
                "mode_bucket",
            ],
        )
        batch_op.drop_column("is_synthetic")

    op.drop_index("ix_crawl_runs_scheduled_for", table_name="crawl_runs")
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.drop_constraint(
            "uq_crawl_runs_scheduled_for_synthetic",
            type_="unique",
        )
        batch_op.drop_column("is_synthetic")
    op.create_index(
        "ix_crawl_runs_scheduled_for",
        "crawl_runs",
        ["scheduled_for"],
        unique=True,
    )
