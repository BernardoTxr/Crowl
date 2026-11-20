from __future__ import annotations

from crowl_core.db import create_database_engine, normalize_database_url


def test_railway_postgres_urls_use_installed_psycopg_v3_driver() -> None:
    assert normalize_database_url("postgres://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    assert normalize_database_url("postgresql://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    qualified = "postgresql+psycopg://user:pass@host/db"
    assert normalize_database_url(qualified) == qualified

    engine = create_database_engine("postgresql://user:pass@host/db")
    assert engine.dialect.driver == "psycopg"
    engine.dispose()
