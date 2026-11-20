"""persist booking trust evidence

Revision ID: 0004_booking_trust_evidence
Revises: 0003_fare_cell_observations
Create Date: 2026-11-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004_booking_trust_evidence"
down_revision: Union[str, Sequence[str], None] = "0003_fare_cell_observations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A temporary server default backfills legacy rows as part of ADD COLUMN.
    # Avoid a separate UPDATE before ALTER TABLE: PostgreSQL can otherwise
    # retain FK trigger events for the updated rows and reject the following
    # ALTER with ObjectInUse. Defaults are removed below so new rows must
    # explicitly persist every trust claim.
    op.add_column(
        "snapshot_bookings",
        sa.Column(
            "mandatory_fees_included",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "snapshot_bookings",
        sa.Column(
            "passenger_age",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("22"),
        ),
    )
    op.add_column(
        "snapshot_bookings",
        sa.Column(
            "passenger_student",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "snapshot_bookings",
        sa.Column(
            "passenger_eligibility_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "snapshot_bookings",
        sa.Column(
            "bookable_at_observation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "snapshot_bookings",
        sa.Column(
            "is_teaser",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    with op.batch_alter_table("snapshot_bookings") as batch_op:
        for column, existing_type, existing_default in (
            ("mandatory_fees_included", sa.Boolean(), sa.true()),
            ("passenger_age", sa.Integer(), sa.text("22")),
            ("passenger_student", sa.Boolean(), sa.true()),
            ("passenger_eligibility_confirmed", sa.Boolean(), sa.true()),
            ("bookable_at_observation", sa.Boolean(), sa.true()),
            ("is_teaser", sa.Boolean(), sa.false()),
        ):
            batch_op.alter_column(
                column,
                existing_type=existing_type,
                existing_nullable=False,
                existing_server_default=existing_default,
                server_default=None,
            )
        batch_op.create_check_constraint(
            "booking_mandatory_fees_complete",
            "mandatory_fees_included = true",
        )
        batch_op.create_check_constraint(
            "booking_passenger_age_22",
            "passenger_age = 22",
        )
        batch_op.create_check_constraint(
            "booking_passenger_eligibility_confirmed",
            "passenger_eligibility_confirmed = true",
        )
        batch_op.create_check_constraint(
            "booking_bookable_at_observation",
            "bookable_at_observation = true",
        )
        batch_op.create_check_constraint(
            "booking_not_teaser",
            "is_teaser = false",
        )
        batch_op.create_check_constraint(
            "booking_student_fare_eligible",
            "student_fare_used = false OR passenger_student = true",
        )


def downgrade() -> None:
    with op.batch_alter_table("snapshot_bookings") as batch_op:
        for name in (
            "booking_student_fare_eligible",
            "booking_not_teaser",
            "booking_bookable_at_observation",
            "booking_passenger_eligibility_confirmed",
            "booking_passenger_age_22",
            "booking_mandatory_fees_complete",
        ):
            batch_op.drop_constraint(name, type_="check")
        for column in (
            "is_teaser",
            "bookable_at_observation",
            "passenger_eligibility_confirmed",
            "passenger_student",
            "passenger_age",
            "mandatory_fees_included",
        ):
            batch_op.drop_column(column)
