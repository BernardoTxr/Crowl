from __future__ import annotations

import pytest

from app.config import APISettings


def test_fixture_seed_defaults_off_and_requires_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("CROWL_SEED_FIXTURES", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./test.db")
    assert APISettings.from_env().seed_fixtures is False

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@postgres/crowl")
    assert APISettings.from_env().seed_fixtures is False

    monkeypatch.setenv("CROWL_SEED_FIXTURES", "true")
    assert APISettings.from_env().seed_fixtures is True


def test_duration_history_horizon_defaults_and_must_be_positive(monkeypatch) -> None:
    monkeypatch.delenv("CROWL_DURATION_HISTORY_DAYS", raising=False)
    assert APISettings.from_env().duration_history_days == 180

    monkeypatch.setenv("CROWL_DURATION_HISTORY_DAYS", "45")
    assert APISettings.from_env().duration_history_days == 45

    monkeypatch.setenv("CROWL_DURATION_HISTORY_DAYS", "0")
    with pytest.raises(ValueError, match="must be greater than zero"):
        APISettings.from_env()
