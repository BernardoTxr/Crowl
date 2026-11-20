from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "packages" / "crowl-core"))
sys.path.insert(0, str(REPOSITORY_ROOT / "services" / "api"))

from app.config import APISettings  # noqa: E402
from app.main import create_app  # noqa: E402
from crowl_core.catalog import seed_development_data  # noqa: E402
from crowl_core.db import Base  # noqa: E402


@pytest.fixture
def seeded_app(tmp_path: Path):
    database_path = tmp_path / "crowl-test.db"
    settings = APISettings(
        database_url=f"sqlite:///{database_path}",
        data_dir=REPOSITORY_ROOT / "data",
        auto_create_schema=True,
        auto_seed=False,
        seed_fixtures=True,
        freshness_hours=24 * 3650,
        duration_history_days=180,
        cors_origins=("http://localhost:5173",),
        expected_schema_revision="0005_synthetic_run_isolation",
    )
    app = create_app(
        settings,
        clock=lambda: datetime(2026, 1, 5, 12, tzinfo=timezone.utc),
    )
    Base.metadata.create_all(app.state.engine)
    with app.state.session_factory() as session:
        seed_development_data(
            session,
            data_dir=settings.data_dir,
            as_of=date(2026, 1, 5),
            include_fixture_fares=True,
        )
    return app


@pytest.fixture
def client(seeded_app):
    with TestClient(seeded_app) as test_client:
        yield test_client
