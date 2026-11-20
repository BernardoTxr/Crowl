from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from app.config import APISettings
from app.main import create_app
from crowl_core.enums import (
    Availability,
    BatchStatus,
    ExecutionCoverage,
    ExtractorRunStatus,
)
from crowl_core.models import (
    City,
    CrawlRun,
    ExtractorRun,
    FareCellObservation,
    FareSnapshot,
    FareSnapshotRevision,
    Source,
    TravelWindow,
)
from crowl_core.travel_windows import TravelWindowSpec


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


EXPECTED_SOURCES = {
    "ryanair",
    "easyjet",
    "transavia",
    "wizzair",
    "vueling",
    "volotea",
    "flixbus",
    "blablacar_bus",
    "itabus",
    "alsa",
    "ouigo",
    "sncf_connect",
    "trenitalia_france",
    "eurostar",
}


def test_health_and_catalog_bootstrap(client) -> None:
    assert client.get("/health/live").json()["status"] == "ok"
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ok",
        "service": "crowl-api",
        "database": "reachable",
        "schema": "create_all",
        "detail": None,
    }

    meta = client.get("/api/v1/meta").json()
    assert meta["origin"]["name"] == "Palaiseau / Paris"
    assert meta["passenger"]["age"] == 22
    assert meta["rules"]["minimum_stay_hours"] == 12
    assert meta["rules"]["maximum_direction_hours"] == 12

    cities = client.get("/api/v1/cities")
    assert cities.status_code == 200
    assert cities.json()["total"] >= 8
    assert cities.json()["items"][0]["photo"]["alt"]

    weekends = client.get("/api/v1/weekends").json()["items"]
    assert weekends
    assert weekends[0]["friday"] == "2026-01-09"
    assert weekends[0]["starts_at"].endswith("Z")
    assert weekends[0]["ends_at"].endswith("Z")


def test_weekend_selector_only_returns_the_current_six_month_horizon(
    client,
    seeded_app,
) -> None:
    retained_past = TravelWindowSpec.for_friday(date(2025, 12, 26))
    with seeded_app.state.session_factory() as session, session.begin():
        session.add(
            TravelWindow(
                friday_date=retained_past.friday,
                starts_at=retained_past.starts_at,
                ends_at=retained_past.ends_at,
                timezone=retained_past.timezone_name,
                minimum_stay_minutes=retained_past.minimum_stay_minutes,
                maximum_direction_minutes=retained_past.maximum_direction_minutes,
            )
        )

    weekends = client.get("/api/v1/weekends").json()["items"]
    fridays = [date.fromisoformat(item["friday"]) for item in weekends]

    assert retained_past.friday not in fridays
    assert fridays[0] == date(2026, 1, 9)
    assert fridays[-1] <= date(2026, 7, 5)


def test_startup_reconciles_existing_catalog_rows(tmp_path) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(REPOSITORY_ROOT / "data", data_dir)
    settings = APISettings(
        database_url=f"sqlite:///{tmp_path / 'startup.db'}",
        data_dir=data_dir,
        auto_create_schema=True,
        auto_seed=True,
        seed_fixtures=False,
        freshness_hours=36,
        duration_history_days=180,
        cors_origins=(),
        expected_schema_revision="0005_synthetic_run_isolation",
    )
    app = create_app(settings)
    with TestClient(app) as first_client:
        assert first_client.get("/health/ready").status_code == 200

    with app.state.session_factory() as session:
        city = session.get(City, "amsterdam-nl")
        assert city is not None
        city.name = "Drifted database value"
        session.commit()

    with TestClient(app) as second_client:
        assert second_client.get("/health/ready").status_code == 200
        assert second_client.get("/api/v1/cities/amsterdam").json()["city"]["name"] == "Amsterdam"


def test_readiness_fails_when_catalog_seed_fails(tmp_path) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(REPOSITORY_ROOT / "data", data_dir)
    (data_dir / "schengen.json").write_text("{not-valid-json", encoding="utf-8")
    settings = APISettings(
        database_url=f"sqlite:///{tmp_path / 'failed-seed.db'}",
        data_dir=data_dir,
        auto_create_schema=True,
        auto_seed=True,
        seed_fixtures=False,
        freshness_hours=36,
        duration_history_days=180,
        cors_origins=(),
        expected_schema_revision="0005_synthetic_run_isolation",
    )
    app = create_app(settings)
    with TestClient(app) as failed_client:
        assert failed_client.get("/health/live").status_code == 200
        ready = failed_client.get("/health/ready")

    assert ready.status_code == 503
    assert ready.json() == {
        "status": "not_ready",
        "service": "crowl-api",
        "database": "reachable",
        "schema": "create_all",
        "detail": "database initialization failed",
    }


def test_readiness_rejects_a_stale_create_all_sqlite_schema(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'stale.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for table in (
            "cities",
            "crawl_runs",
            "duration_histograms_daily",
            "fare_snapshots",
            "fare_snapshot_revisions",
            "snapshot_bookings",
            "travel_windows",
        ):
            connection.execute(text(f"CREATE TABLE {table} (id INTEGER)"))
    engine.dispose()

    app = create_app(
        APISettings(
            database_url=database_url,
            data_dir=REPOSITORY_ROOT / "data",
            auto_create_schema=False,
            auto_seed=False,
            seed_fixtures=False,
            freshness_hours=36,
            duration_history_days=180,
            cors_origins=(),
            expected_schema_revision="0005_synthetic_run_isolation",
        )
    )
    with TestClient(app) as stale_client:
        response = stale_client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert "missing required columns" in response.json()["detail"]
    assert "crawl_runs: is_synthetic" in response.json()["detail"]


def test_map_price_returns_every_city_with_price_and_exact_endpoints(client) -> None:
    weekend = client.get("/api/v1/weekends").json()["items"][0]["friday"]
    response = client.get(f"/api/v1/map-prices?weekend={weekend}&mode=all")

    assert response.status_code == 200
    payload = response.json()
    assert payload["price_scale_breakpoints_eur_cents"] == [5000, 10000, 17500, 25000]
    assert len(payload["items"]) >= 8
    assert all(item["availability"] == "available" for item in payload["items"])
    assert all(item["price_eur_cents"] > 0 for item in payload["items"])
    assert all(item["origin_terminal"]["name"].startswith("Paris") for item in payload["items"])
    assert all(item["destination_terminal"]["name"] for item in payload["items"])
    assert all(item["return_origin_terminal"]["name"] for item in payload["items"])
    assert all(item["return_destination_terminal"]["name"].startswith("Paris") for item in payload["items"])
    assert all(item["rule_version_id"] > 0 for item in payload["items"])
    assert all(item["profile_id"] > 0 for item in payload["items"])
    assert all(item["source_scope_ratio"] == 0.0 for item in payload["items"])


def test_map_price_empty_window_is_explicit_failure_never_zero(client) -> None:
    weekends = client.get("/api/v1/weekends").json()["items"]
    empty_weekend = weekends[0]["friday"]
    payload = client.get(
        f"/api/v1/map-prices?weekend={empty_weekend}&mode=mixed"
    ).json()

    assert len(payload["items"]) >= 8
    assert all(item["price_eur_cents"] is None for item in payload["items"])
    assert all(item["availability"] == "unknown_due_failure" for item in payload["items"])
    assert all(item["execution_coverage"] == "failed" for item in payload["items"])
    assert all(item["freshness"] == "never_observed" for item in payload["items"])
    assert all(item["rule_version_id"] is None for item in payload["items"])
    assert all(item["profile_id"] is None for item in payload["items"])


def test_city_history_and_journey_components_are_coherent(client) -> None:
    weekend = client.get("/api/v1/weekends").json()["items"][0]["friday"]
    detail = client.get("/api/v1/cities/amsterdam")
    assert detail.status_code == 200
    assert {item["mode"] for item in detail.json()["typical_durations"]} == {
        "flight",
        "train",
        "bus",
    }
    assert all(
        item["is_synthetic"]
        for item in detail.json()["typical_durations"]
    )

    history = client.get("/api/v1/cities/amsterdam/price-history?mode=all").json()
    assert len(history["cells"]) >= 6
    selected_weekend_cells = [
        cell for cell in history["cells"] if cell["weekend"] == weekend
    ]
    assert selected_weekend_cells[-1]["lead_days"] == 4
    assert history["scale"]["p10_eur_cents"] <= history["scale"]["p90_eur_cents"]

    historical_cell = selected_weekend_cells[0]
    historical = client.get(
        f"/api/v1/cities/amsterdam/journeys?weekend={weekend}"
        f"&mode=all&revision_id={historical_cell['revision_id']}"
    )
    assert historical.status_code == 200
    historical_journey = historical.json()["items"][0]
    assert historical_journey["revision_id"] == historical_cell["revision_id"]
    assert historical_journey["total_price_eur_cents"] == historical_cell["price_eur_cents"]
    assert historical_journey["observed_at"] == historical_cell["observed_at"]
    assert historical_journey["historical"] is True
    assert historical_journey["rule_version_id"] == historical_cell["rule_version_id"]
    assert historical_journey["profile_id"] == historical_cell["profile_id"]

    journeys = client.get(
        f"/api/v1/cities/amsterdam/journeys?weekend={weekend}&mode=all"
    ).json()
    assert journeys["availability"] == "available"
    assert len(journeys["items"]) == 1
    journey = journeys["items"][0]
    assert sum(booking["eur_amount_cents"] for booking in journey["bookings"]) == journey[
        "total_price_eur_cents"
    ]
    assert all(
        booking["mandatory_fees_included"] is True
        and booking["passenger_age"] == 22
        and booking["passenger_student"] is True
        and booking["passenger_eligibility_confirmed"] is True
        and booking["bookable_at_observation"] is True
        and booking["is_teaser"] is False
        for booking in journey["bookings"]
    )
    assert [leg["direction"] for leg in journey["legs"]] == ["outbound", "return"]
    assert journey["legs"][0]["origin_terminal"]["id"] == "par-ory"
    assert journey["legs"][0]["destination_terminal"]["id"] == "ams-schiphol"

    missing_revision = client.get(
        f"/api/v1/cities/amsterdam/journeys?weekend={weekend}&mode=all&revision_id=999999"
    )
    assert missing_revision.status_code == 404


def test_failed_current_overlay_keeps_prior_fare_and_its_original_timestamp(
    client, seeded_app
) -> None:
    weekend = client.get("/api/v1/weekends").json()["items"][0]["friday"]
    window_date = date.fromisoformat(weekend)
    initial_map = client.get(
        f"/api/v1/map-prices?weekend={weekend}&mode=all"
    ).json()
    initial = next(
        item for item in initial_map["items"] if item["city"]["slug"] == "amsterdam"
    )
    assert initial["revision_id"] is not None
    with seeded_app.state.session_factory() as session, session.begin():
        revision = session.get(FareSnapshotRevision, initial["revision_id"])
        assert revision is not None
        baseline_run = session.get(CrawlRun, revision.crawl_run_id)
        snapshot = session.get(FareSnapshot, revision.fare_snapshot_id)
        assert baseline_run is not None and snapshot is not None
        baseline_run.is_synthetic = False
        baseline_run.diagnostic = "TEST_OPERATIONAL_BASELINE"
        snapshot.is_synthetic = False

    initial_map = client.get(
        f"/api/v1/map-prices?weekend={weekend}&mode=all"
    ).json()
    initial = next(
        item for item in initial_map["items"] if item["city"]["slug"] == "amsterdam"
    )
    trustworthy_before_failure = client.get("/api/v1/batches/latest").json()[
        "batch"
    ]["latest_trustworthy_observation_at"]
    assert trustworthy_before_failure is not None
    assert initial["price_eur_cents"] is not None
    assert initial["revision_id"] is not None
    retained_observed_at = initial["observed_at"]

    state_time = datetime(2026, 1, 6, 3, 30, tzinfo=UTC)
    with seeded_app.state.session_factory() as session, session.begin():
        window = session.scalar(
            select(TravelWindow).where(TravelWindow.friday_date == window_date)
        )
        revision = session.get(FareSnapshotRevision, initial["revision_id"])
        assert window is not None and revision is not None
        run = CrawlRun(
            scheduled_for=state_time.date(),
            rule_version_id=revision.rule_version_id,
            profile_id=revision.profile_id,
            manifest_version="failed-overlay-test",
            release_version="test",
            status=BatchStatus.FAILED,
            started_at=state_time,
            heartbeat_at=state_time,
            completed_at=state_time,
            published_at=state_time,
            expected_cells=1,
            completed_cells=0,
            successful_cells=0,
            diagnostic="simulated provider failure",
        )
        session.add(run)
        session.flush()
        observation = FareCellObservation(
            crawl_run_id=run.id,
            rule_version_id=revision.rule_version_id,
            profile_id=revision.profile_id,
            destination_city_id="amsterdam-nl",
            travel_window_id=window.id,
            mode_bucket=revision.mode_bucket,
            availability=Availability.UNKNOWN_DUE_FAILURE,
            execution_coverage=ExecutionCoverage.FAILED,
            source_scope_ratio=1.0,
            observed_at=state_time,
            trusted_revision_id=revision.id,
            diagnostic="UPSTREAM_FAILURE",
        )
        session.add(observation)
        session.flush()
        current_crawl_run_id = run.id

    current_map = client.get(
        f"/api/v1/map-prices?weekend={weekend}&mode=all"
    ).json()
    current = next(
        item for item in current_map["items"] if item["city"]["slug"] == "amsterdam"
    )
    assert current["availability"] == "unknown_due_failure"
    assert current["execution_coverage"] == "failed"
    assert current["price_eur_cents"] == initial["price_eur_cents"]
    assert current["revision_id"] == initial["revision_id"]
    assert current["observed_at"] == retained_observed_at
    assert current["current_observed_at"] == state_time.isoformat().replace(
        "+00:00", "Z"
    )
    assert current["current_crawl_run_id"] == current_crawl_run_id
    assert current["current_diagnostic"] == "UPSTREAM_FAILURE"

    history = client.get(
        "/api/v1/cities/amsterdam/price-history?mode=all"
    ).json()
    failed_cell = next(
        cell
        for cell in history["cells"]
        if cell["weekend"] == weekend
        and cell["observed_on"] == state_time.date().isoformat()
    )
    assert failed_cell["availability"] == "unknown_due_failure"
    assert failed_cell["price_eur_cents"] == initial["price_eur_cents"]
    assert failed_cell["revision_id"] == initial["revision_id"]
    assert failed_cell["observed_at"] == retained_observed_at
    assert failed_cell["current_crawl_run_id"] == current_crawl_run_id

    journeys = client.get(
        f"/api/v1/cities/amsterdam/journeys?weekend={weekend}&mode=all"
    ).json()
    assert journeys["availability"] == "unknown_due_failure"
    assert journeys["execution_coverage"] == "failed"
    assert journeys["observed_at"] == retained_observed_at
    assert journeys["current_crawl_run_id"] == current_crawl_run_id
    assert [item["revision_id"] for item in journeys["items"]] == [
        initial["revision_id"]
    ]
    assert journeys["items"][0]["observed_at"] == retained_observed_at

    latest_batch = client.get("/api/v1/batches/latest").json()["batch"]
    assert latest_batch["id"] == current_crawl_run_id
    assert latest_batch["status"] == "failed"
    assert (
        latest_batch["latest_trustworthy_observation_at"]
        == trustworthy_before_failure
    )


def test_live_price_state_wins_over_cheaper_same_day_synthetic_state(
    client,
    seeded_app,
) -> None:
    weekend = client.get("/api/v1/weekends").json()["items"][0]["friday"]
    seeded_map = client.get(
        f"/api/v1/map-prices?weekend={weekend}&mode=all"
    ).json()
    seeded_item = next(
        item for item in seeded_map["items"]
        if item["city"]["slug"] == "amsterdam"
    )
    assert seeded_item["revision_id"] is not None
    assert seeded_item["is_synthetic"] is True

    with seeded_app.state.session_factory() as session, session.begin():
        synthetic_revision = session.get(
            FareSnapshotRevision,
            seeded_item["revision_id"],
        )
        assert synthetic_revision is not None
        synthetic_snapshot = session.get(
            FareSnapshot,
            synthetic_revision.fare_snapshot_id,
        )
        assert synthetic_snapshot is not None
        observed_at = datetime(2026, 1, 5, 18, tzinfo=UTC)
        run = CrawlRun(
            scheduled_for=date(2026, 1, 5),
            is_synthetic=False,
            rule_version_id=synthetic_revision.rule_version_id,
            profile_id=synthetic_revision.profile_id,
            manifest_version="live-over-synthetic-test",
            release_version="test",
            status=BatchStatus.SUCCEEDED,
            started_at=observed_at,
            heartbeat_at=observed_at,
            completed_at=observed_at,
            published_at=observed_at,
            expected_cells=1,
            completed_cells=1,
            successful_cells=1,
            diagnostic="TEST_LIVE_BASELINE",
        )
        session.add(run)
        session.flush()
        live_snapshot = FareSnapshot(
            observed_on=date(2026, 1, 5),
            profile_id=synthetic_snapshot.profile_id,
            destination_city_id=synthetic_snapshot.destination_city_id,
            travel_window_id=synthetic_snapshot.travel_window_id,
            mode_bucket=synthetic_snapshot.mode_bucket,
            is_synthetic=False,
        )
        session.add(live_snapshot)
        session.flush()
        live_price = synthetic_revision.total_price_eur_cents + 5_000
        live_revision = FareSnapshotRevision(
            fare_snapshot_id=live_snapshot.id,
            revision_number=1,
            crawl_run_id=run.id,
            rule_version_id=synthetic_revision.rule_version_id,
            profile_id=synthetic_revision.profile_id,
            mode_bucket=synthetic_revision.mode_bucket,
            total_price_eur_cents=live_price,
            availability=Availability.AVAILABLE,
            execution_coverage=ExecutionCoverage.COMPLETE,
            source_scope_ratio=1.0,
            observed_at=observed_at,
            published_at=observed_at,
            outbound_departure_at=synthetic_revision.outbound_departure_at,
            outbound_arrival_at=synthetic_revision.outbound_arrival_at,
            return_departure_at=synthetic_revision.return_departure_at,
            return_arrival_at=synthetic_revision.return_arrival_at,
            outbound_duration_minutes=synthetic_revision.outbound_duration_minutes,
            return_duration_minutes=synthetic_revision.return_duration_minutes,
            stay_duration_minutes=synthetic_revision.stay_duration_minutes,
            transfer_count=synthetic_revision.transfer_count,
            itinerary_key="live-over-synthetic-test",
        )
        session.add(live_revision)
        session.flush()
        live_revision_id = live_revision.id
        live_snapshot.current_revision_id = live_revision.id
        session.add(
            FareCellObservation(
                crawl_run_id=run.id,
                rule_version_id=live_revision.rule_version_id,
                profile_id=live_revision.profile_id,
                destination_city_id=live_snapshot.destination_city_id,
                travel_window_id=live_snapshot.travel_window_id,
                mode_bucket=live_snapshot.mode_bucket,
                availability=Availability.AVAILABLE,
                execution_coverage=ExecutionCoverage.COMPLETE,
                source_scope_ratio=1.0,
                observed_at=observed_at,
                trusted_revision_id=live_revision.id,
                diagnostic="CURRENT_FARE_AVAILABLE",
            )
        )

    current_map = client.get(
        f"/api/v1/map-prices?weekend={weekend}&mode=all"
    ).json()
    current = next(
        item for item in current_map["items"]
        if item["city"]["slug"] == "amsterdam"
    )
    assert current["price_eur_cents"] == live_price
    assert current["price_eur_cents"] > seeded_item["price_eur_cents"]
    assert current["source_scope_ratio"] == 1.0
    assert current["is_synthetic"] is False

    history = client.get(
        "/api/v1/cities/amsterdam/price-history?mode=all"
    ).json()
    same_day = next(
        cell for cell in history["cells"]
        if cell["weekend"] == weekend and cell["observed_on"] == "2026-01-05"
    )
    assert same_day["price_eur_cents"] == live_price
    assert same_day["source_scope_ratio"] == 1.0
    assert same_day["is_synthetic"] is False

    journeys = client.get(
        f"/api/v1/cities/amsterdam/journeys?weekend={weekend}&mode=all"
    ).json()
    assert journeys["items"]
    assert [item["total_price_eur_cents"] for item in journeys["items"]] == [
        live_price
    ]
    assert [item["revision_id"] for item in journeys["items"]] == [live_revision_id]
    assert journeys["items"][0]["is_synthetic"] is False


def test_explicit_no_result_overlay_is_rendered_without_a_fare_revision(
    client, seeded_app
) -> None:
    weekend = client.get("/api/v1/weekends").json()["items"][6]["friday"]
    window_date = date.fromisoformat(weekend)
    state_time = datetime(2026, 1, 7, 4, 0, tzinfo=UTC)
    with seeded_app.state.session_factory() as session, session.begin():
        window = session.scalar(
            select(TravelWindow).where(TravelWindow.friday_date == window_date)
        )
        dimensions = session.scalar(select(FareSnapshotRevision))
        assert window is not None and dimensions is not None
        run = CrawlRun(
            scheduled_for=state_time.date(),
            rule_version_id=dimensions.rule_version_id,
            profile_id=dimensions.profile_id,
            manifest_version="sold-out-overlay-test",
            release_version="test",
            status=BatchStatus.SUCCEEDED,
            started_at=state_time,
            heartbeat_at=state_time,
            completed_at=state_time,
            published_at=state_time,
            expected_cells=1,
            completed_cells=1,
            successful_cells=1,
            diagnostic="complete sold-out observation",
        )
        session.add(run)
        session.flush()
        session.add(
            FareCellObservation(
                crawl_run_id=run.id,
                rule_version_id=dimensions.rule_version_id,
                profile_id=dimensions.profile_id,
                destination_city_id="amsterdam-nl",
                travel_window_id=window.id,
                mode_bucket=dimensions.mode_bucket,
                availability=Availability.SOLD_OUT,
                execution_coverage=ExecutionCoverage.COMPLETE,
                source_scope_ratio=1.0,
                observed_at=state_time,
                trusted_revision_id=None,
                diagnostic="SOLD_OUT",
            )
        )
        mode = dimensions.mode_bucket.value
        session.flush()
        run_id = run.id

    map_payload = client.get(
        f"/api/v1/map-prices?weekend={weekend}&mode={mode}"
    ).json()
    item = next(
        row for row in map_payload["items"] if row["city"]["slug"] == "amsterdam"
    )
    assert item["availability"] == "sold_out"
    assert item["execution_coverage"] == "complete"
    assert item["price_eur_cents"] is None
    assert item["observed_at"] is None
    assert item["revision_id"] is None
    assert item["freshness"] == "never_observed"
    assert item["current_crawl_run_id"] == run_id

    history = client.get(
        f"/api/v1/cities/amsterdam/price-history?mode={mode}"
    ).json()
    cell = next(
        row
        for row in history["cells"]
        if row["weekend"] == weekend
        and row["observed_on"] == state_time.date().isoformat()
    )
    assert cell["availability"] == "sold_out"
    assert cell["price_eur_cents"] is None
    assert cell["observed_at"] is None
    assert cell["revision_id"] is None
    assert cell["current_observed_at"] == state_time.isoformat().replace(
        "+00:00", "Z"
    )

    journeys = client.get(
        f"/api/v1/cities/amsterdam/journeys?weekend={weekend}&mode={mode}"
    ).json()
    assert journeys["availability"] == "sold_out"
    assert journeys["execution_coverage"] == "complete"
    assert journeys["observed_at"] is None
    assert journeys["current_crawl_run_id"] == run_id
    assert journeys["items"] == []


def test_status_exposes_all_14_canonical_source_ids_and_batch_counts(client) -> None:
    latest = client.get("/api/v1/batches/latest")
    assert latest.status_code == 200
    assert latest.json()["batch"] is None
    assert latest.json()["state"] == "not_started"

    extractors = client.get("/api/v1/extractors").json()
    assert {item["source"] for item in extractors["items"]} == EXPECTED_SOURCES
    assert len(extractors["items"]) == 14
    assert extractors["requested_sources"] == 14
    assert extractors["enabled_sources"] == 0
    assert extractors["batch_id"] is None
    assert all(item["disabled_reason"] for item in extractors["items"])
    assert all(item["freshness"] == "never_observed" for item in extractors["items"])

    runs = client.get("/api/v1/extractors/transavia/runs")
    assert runs.status_code == 200
    assert runs.json()["items"] == []
    assert client.get("/api/v1/extractors/fixture/runs").status_code == 404


def test_extractor_freshness_uses_last_valid_success_not_current_run_state(
    client,
    seeded_app,
) -> None:
    with seeded_app.state.session_factory() as session, session.begin():
        fixture_batch = session.scalar(
            select(CrawlRun)
            .where(CrawlRun.is_synthetic.is_(True))
            .order_by(CrawlRun.id.desc())
        )
        assert fixture_batch is not None
        batch = CrawlRun(
            scheduled_for=date(2026, 1, 5),
            is_synthetic=False,
            rule_version_id=fixture_batch.rule_version_id,
            profile_id=fixture_batch.profile_id,
            manifest_version="extractor-freshness-test",
            release_version="test",
            status=BatchStatus.FAILED,
            started_at=datetime(2026, 1, 5, 2, tzinfo=UTC),
            heartbeat_at=datetime(2026, 1, 5, 2, tzinfo=UTC),
            completed_at=datetime(2026, 1, 5, 2, tzinfo=UTC),
            published_at=datetime(2026, 1, 5, 2, tzinfo=UTC),
            diagnostic="TEST_OPERATIONAL_BATCH",
        )
        session.add(batch)
        session.flush()
        for source_id in ("easyjet", "ryanair", "transavia"):
            source = session.get(Source, source_id)
            assert source is not None
            source.enabled = True
            source.disabled_reason = None
        session.add_all(
            [
                ExtractorRun(
                    crawl_run_id=batch.id,
                    source_id="easyjet",
                    status=ExtractorRunStatus.FAILED,
                    last_success_at=datetime(2026, 1, 4, 12, tzinfo=UTC),
                    diagnostic="LATEST_ATTEMPT_FAILED",
                ),
                ExtractorRun(
                    crawl_run_id=batch.id,
                    source_id="ryanair",
                    status=ExtractorRunStatus.SUCCEEDED,
                    last_success_at=datetime(2010, 1, 4, 12, tzinfo=UTC),
                ),
            ]
        )

    items = {
        item["source"]: item
        for item in client.get("/api/v1/extractors").json()["items"]
    }
    assert items["easyjet"]["state"] == "failed"
    assert items["easyjet"]["freshness"] == "fresh"
    assert items["ryanair"]["state"] == "succeeded"
    assert items["ryanair"]["freshness"] == "stale"
    assert items["transavia"]["state"] == "pending"
    assert items["transavia"]["freshness"] == "never_observed"


def test_not_found_validation_and_read_only_surface(client) -> None:
    assert client.get("/api/v1/cities/not-a-city").status_code == 404
    assert client.get("/api/v1/map-prices?weekend=2001-01-05&mode=all").status_code == 404
    assert client.get("/api/v1/map-prices?weekend=bad&mode=all").status_code == 422
    assert client.get("/api/v1/map-prices?weekend=2026-01-09&mode=boat").status_code == 422
    assert client.get("/api/v1/extractors/not-a-source/runs").status_code == 404
    assert client.post("/api/v1/cities", json={}).status_code == 405

    openapi = client.get("/openapi.json").json()
    for path, operations in openapi["paths"].items():
        assert not ({"post", "put", "patch", "delete"} & set(operations)), path


def test_cors_allows_configured_frontend_only(client) -> None:
    allowed = client.options(
        "/api/v1/cities",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"

    denied = client.options(
        "/api/v1/cities",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert denied.status_code == 400
