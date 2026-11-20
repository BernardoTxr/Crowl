from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from ..schemas import HealthResponse


router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthResponse)
def live() -> HealthResponse:
    return HealthResponse(status="ok", database="not_checked")


@router.get("/health/ready", response_model=HealthResponse)
def ready(request: Request, response: Response) -> HealthResponse:
    startup_error = getattr(request.app.state, "startup_error", None)
    try:
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            required = {
                "cities",
                "crawl_runs",
                "duration_histograms_daily",
                "fare_snapshots",
                "fare_snapshot_revisions",
                "snapshot_bookings",
                "travel_windows",
            }
            missing = required - tables
            if missing:
                response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                return HealthResponse(
                    status="not_ready",
                    database="reachable",
                    detail="missing required tables: " + ", ".join(sorted(missing)),
                )
            required_columns = {
                "crawl_runs": {"is_synthetic"},
                "fare_snapshots": {"is_synthetic"},
                "duration_histograms_daily": {"is_synthetic"},
                "snapshot_bookings": {
                    "mandatory_fees_included",
                    "passenger_age",
                    "passenger_student",
                    "passenger_eligibility_confirmed",
                    "bookable_at_observation",
                    "is_teaser",
                },
            }
            missing_columns = {
                table: names
                - {column["name"] for column in inspector.get_columns(table)}
                for table, names in required_columns.items()
                if table in tables
            }
            missing_columns = {
                table: names for table, names in missing_columns.items() if names
            }
            if missing_columns:
                response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                detail = "; ".join(
                    f"{table}: {', '.join(sorted(names))}"
                    for table, names in sorted(missing_columns.items())
                )
                return HealthResponse(
                    status="not_ready",
                    database="reachable",
                    schema="create_all",
                    detail="missing required columns: " + detail,
                )
            schema = "create_all"
            if "alembic_version" in tables:
                revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
                schema = str(revision)
            if startup_error:
                response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                return HealthResponse(
                    status="not_ready",
                    database="reachable",
                    schema=schema,
                    detail="database initialization failed",
                )
            if "alembic_version" in tables:
                expected = request.app.state.settings.expected_schema_revision
                if revision != expected:
                    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                    return HealthResponse(
                        status="not_ready",
                        database="reachable",
                        schema=schema,
                        detail=f"schema revision mismatch; expected {expected}",
                    )
            return HealthResponse(status="ok", database="reachable", schema=schema)
    except SQLAlchemyError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        detail = "database connection failed"
        if startup_error:
            detail = "database initialization failed"
        return HealthResponse(status="not_ready", database="unreachable", detail=detail)
