from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_integer(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True, slots=True)
class APISettings:
    database_url: str
    data_dir: Path
    auto_create_schema: bool
    auto_seed: bool
    seed_fixtures: bool
    freshness_hours: int
    cors_origins: tuple[str, ...]
    expected_schema_revision: str
    duration_history_days: int = 180

    @classmethod
    def from_env(cls) -> "APISettings":
        database_url = os.getenv("DATABASE_URL", "sqlite:///./crowl.db")
        is_sqlite = database_url.startswith("sqlite")
        default_data_dir = Path(__file__).resolve().parents[3] / "data"
        return cls(
            database_url=database_url,
            data_dir=Path(os.getenv("CROWL_DATA_DIR", str(default_data_dir))),
            auto_create_schema=_boolean("CROWL_AUTO_CREATE_SCHEMA", is_sqlite),
            auto_seed=_boolean("CROWL_AUTO_SEED", is_sqlite),
            seed_fixtures=_boolean("CROWL_SEED_FIXTURES", is_sqlite),
            freshness_hours=int(os.getenv("CROWL_FRESHNESS_HOURS", "36")),
            duration_history_days=_positive_integer("CROWL_DURATION_HISTORY_DAYS", 180),
            cors_origins=tuple(
                origin.strip()
                for origin in os.getenv(
                    "CROWL_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
                ).split(",")
                if origin.strip()
            ),
            expected_schema_revision=os.getenv(
                "CROWL_SCHEMA_REVISION", "0005_synthetic_run_isolation"
            ),
        )
