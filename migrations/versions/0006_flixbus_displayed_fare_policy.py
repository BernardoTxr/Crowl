"""persist the explicit FlixBus displayed-fare exception

Revision ID: 0006_flixbus_fare_policy
Revises: 0005_synthetic_run_isolation
Create Date: 2026-11-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_flixbus_fare_policy"
down_revision: Union[str, Sequence[str], None] = "0005_synthetic_run_isolation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PRICE_COMPLETENESS = sa.Enum(
    "complete",
    "estimated",
    "unknown",
    name="booking_price_completeness",
    native_enum=False,
)


def upgrade() -> None:
    op.add_column(
        "snapshot_bookings",
        sa.Column(
            "price_completeness",
            PRICE_COMPLETENESS,
            nullable=False,
            server_default="complete",
        ),
    )
    with op.batch_alter_table("snapshot_bookings") as batch_op:
        batch_op.drop_constraint(
            "booking_mandatory_fees_complete",
            type_="check",
        )
        batch_op.alter_column(
            "price_completeness",
            existing_type=PRICE_COMPLETENESS,
            existing_nullable=False,
            existing_server_default="complete",
            server_default=None,
        )
        batch_op.create_check_constraint(
            "booking_price_policy",
            "(price_completeness = 'complete' AND mandatory_fees_included = true) "
            "OR (source_id = 'flixbus' AND price_completeness = 'estimated' "
            "AND mandatory_fees_included = false)",
        )


def downgrade() -> None:
    # Rows using the displayed-fare exception cannot satisfy the older
    # mandatory-fee invariant, so fail instead of silently changing evidence.
    connection = op.get_bind()
    displayed_fare = connection.execute(
        sa.text(
            """
            SELECT id
              FROM snapshot_bookings
             WHERE price_completeness <> 'complete'
                OR mandatory_fees_included = false
             LIMIT 1
            """
        )
    ).first()
    if displayed_fare is not None:
        raise RuntimeError(
            "cannot downgrade while displayed FlixBus fare evidence exists"
        )

    with op.batch_alter_table("snapshot_bookings") as batch_op:
        batch_op.drop_constraint("booking_price_policy", type_="check")
        batch_op.create_check_constraint(
            "booking_mandatory_fees_complete",
            "mandatory_fees_included = true",
        )
        batch_op.drop_column("price_completeness")
