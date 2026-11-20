"""strengthen fare snapshot invariants

Revision ID: 0002_snapshot_invariants
Revises: 0001_initial
Create Date: 2026-11-20
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0002_snapshot_invariants"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


POSTGRES_INVARIANTS = """
CREATE FUNCTION crowl_enforce_snapshot_current_revision()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    revision_snapshot_id integer;
BEGIN
    IF NEW.current_revision_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT fare_snapshot_id INTO revision_snapshot_id
    FROM fare_snapshot_revisions
    WHERE id = NEW.current_revision_id;

    IF revision_snapshot_id IS NULL OR revision_snapshot_id <> NEW.id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'fare snapshot current revision must belong to the same snapshot';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER crowl_snapshot_current_revision_guard
AFTER INSERT OR UPDATE ON fare_snapshots
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION crowl_enforce_snapshot_current_revision();

CREATE FUNCTION crowl_enforce_revision_dimensions()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    snapshot_profile_id integer;
    snapshot_mode text;
    run_profile_id integer;
    run_rule_version_id integer;
BEGIN
    SELECT profile_id, mode_bucket::text
      INTO snapshot_profile_id, snapshot_mode
      FROM fare_snapshots
     WHERE id = NEW.fare_snapshot_id;

    SELECT profile_id, rule_version_id
      INTO run_profile_id, run_rule_version_id
      FROM crawl_runs
     WHERE id = NEW.crawl_run_id;

    IF snapshot_profile_id IS NULL
       OR NEW.profile_id <> snapshot_profile_id
       OR NEW.mode_bucket::text <> snapshot_mode
       OR run_profile_id IS NULL
       OR NEW.profile_id <> run_profile_id
       OR NEW.rule_version_id <> run_rule_version_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'fare revision dimensions must match its snapshot and crawl run';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER crowl_revision_dimensions_guard
AFTER INSERT OR UPDATE ON fare_snapshot_revisions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION crowl_enforce_revision_dimensions();

CREATE FUNCTION crowl_enforce_leg_booking_revision()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    booking_revision_id integer;
BEGIN
    IF NEW.booking_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT revision_id INTO booking_revision_id
      FROM snapshot_bookings
     WHERE id = NEW.booking_id;

    IF booking_revision_id IS NULL OR booking_revision_id <> NEW.revision_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'snapshot leg booking must belong to the same revision';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER crowl_leg_booking_revision_guard
AFTER INSERT OR UPDATE ON snapshot_legs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION crowl_enforce_leg_booking_revision();

CREATE FUNCTION crowl_enforce_revision_booking_total()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_revision_id integer;
    expected_total integer;
    booking_count integer;
    booking_total bigint;
BEGIN
    IF TG_TABLE_NAME = 'fare_snapshot_revisions' THEN
        target_revision_id := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target_revision_id := OLD.revision_id;
    ELSE
        target_revision_id := NEW.revision_id;
    END IF;

    SELECT total_price_eur_cents INTO expected_total
      FROM fare_snapshot_revisions
     WHERE id = target_revision_id AND availability = 'available';

    IF expected_total IS NULL THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    SELECT count(*), COALESCE(sum(eur_amount_cents), 0)
      INTO booking_count, booking_total
      FROM snapshot_bookings
     WHERE revision_id = target_revision_id;

    IF booking_count = 0 OR booking_total <> expected_total THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'available fare revision booking components must sum to its total price';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER crowl_revision_booking_total_guard
AFTER INSERT OR UPDATE ON fare_snapshot_revisions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION crowl_enforce_revision_booking_total();

CREATE CONSTRAINT TRIGGER crowl_booking_revision_total_guard
AFTER INSERT OR UPDATE OR DELETE ON snapshot_bookings
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION crowl_enforce_revision_booking_total();
"""


POSTGRES_DROP_INVARIANTS = """
DROP TRIGGER IF EXISTS crowl_booking_revision_total_guard ON snapshot_bookings;
DROP TRIGGER IF EXISTS crowl_revision_booking_total_guard ON fare_snapshot_revisions;
DROP FUNCTION IF EXISTS crowl_enforce_revision_booking_total();
DROP TRIGGER IF EXISTS crowl_leg_booking_revision_guard ON snapshot_legs;
DROP FUNCTION IF EXISTS crowl_enforce_leg_booking_revision();
DROP TRIGGER IF EXISTS crowl_revision_dimensions_guard ON fare_snapshot_revisions;
DROP FUNCTION IF EXISTS crowl_enforce_revision_dimensions();
DROP TRIGGER IF EXISTS crowl_snapshot_current_revision_guard ON fare_snapshots;
DROP FUNCTION IF EXISTS crowl_enforce_snapshot_current_revision();
"""


def upgrade() -> None:
    with op.batch_alter_table("fare_snapshot_revisions") as batch_op:
        batch_op.create_check_constraint(
            "ck_fare_snapshot_revisions_revision_number_positive",
            "revision_number > 0",
        )
        batch_op.create_check_constraint(
            "ck_fare_snapshot_revisions_availability_price_consistent",
            "(availability = 'available' AND total_price_eur_cents IS NOT NULL) "
            "OR (availability <> 'available' AND total_price_eur_cents IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_fare_snapshot_revisions_outbound_duration_nonnegative",
            "outbound_duration_minutes IS NULL OR outbound_duration_minutes >= 0",
        )
        batch_op.create_check_constraint(
            "ck_fare_snapshot_revisions_return_duration_nonnegative",
            "return_duration_minutes IS NULL OR return_duration_minutes >= 0",
        )
        batch_op.create_check_constraint(
            "ck_fare_snapshot_revisions_stay_duration_nonnegative",
            "stay_duration_minutes IS NULL OR stay_duration_minutes >= 0",
        )
        batch_op.create_check_constraint(
            "ck_fare_snapshot_revisions_transfer_count_nonnegative",
            "transfer_count >= 0",
        )

    with op.batch_alter_table("snapshot_bookings") as batch_op:
        batch_op.create_check_constraint(
            "ck_snapshot_bookings_booking_component_order_nonnegative",
            "component_order >= 0",
        )

    with op.batch_alter_table("snapshot_legs") as batch_op:
        batch_op.create_check_constraint(
            "ck_snapshot_legs_leg_segment_order_nonnegative",
            "segment_order >= 0",
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(POSTGRES_INVARIANTS)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(POSTGRES_DROP_INVARIANTS)

    with op.batch_alter_table("snapshot_legs") as batch_op:
        batch_op.drop_constraint(
            "ck_snapshot_legs_leg_segment_order_nonnegative", type_="check"
        )

    with op.batch_alter_table("snapshot_bookings") as batch_op:
        batch_op.drop_constraint(
            "ck_snapshot_bookings_booking_component_order_nonnegative", type_="check"
        )

    with op.batch_alter_table("fare_snapshot_revisions") as batch_op:
        for name in (
            "ck_fare_snapshot_revisions_transfer_count_nonnegative",
            "ck_fare_snapshot_revisions_stay_duration_nonnegative",
            "ck_fare_snapshot_revisions_return_duration_nonnegative",
            "ck_fare_snapshot_revisions_outbound_duration_nonnegative",
            "ck_fare_snapshot_revisions_availability_price_consistent",
            "ck_fare_snapshot_revisions_revision_number_positive",
        ):
            batch_op.drop_constraint(name, type_="check")
