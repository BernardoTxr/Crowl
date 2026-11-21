"""Small, dependency-free settings layer for shared code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CoreSettings:
    database_url: str = "sqlite:///./crowl.db"
    home_timezone: str = "Europe/Paris"
    freshness_hours: int = 36
    schema_revision: str = "0006_flixbus_fare_policy"
    repository_root: Path = Path(__file__).resolve().parents[3]

    @classmethod
    def from_env(cls) -> "CoreSettings":
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///./crowl.db"),
            freshness_hours=int(os.getenv("CROWL_FRESHNESS_HOURS", "36")),
            schema_revision=os.getenv(
                "CROWL_SCHEMA_REVISION", "0006_flixbus_fare_policy"
            ),
        )
