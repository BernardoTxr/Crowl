from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from crowl_core import Base, create_database_engine, create_session_factory
from crowl_core.models import FxRate
from crowl_crawler.fx import (
    FxSyncError,
    parse_ecb_reference_rates,
    sync_ecb_reference_rates,
)


FETCHED_AT = datetime(2026, 11, 20, 16, 30, tzinfo=UTC)
CSV = b"""KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE
EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2026-11-20,0.83735
EXR.D.CHF.EUR.SP00.A,D,CHF,EUR,SP00,A,2026-11-20,0.9223
EXR.D.JPY.EUR.SP00.A,D,JPY,EUR,SP00,A,2026-11-20,174.35
"""


def test_parser_inverts_ecb_currency_per_euro_quotes() -> None:
    rates = parse_ecb_reference_rates(CSV, fetched_at=FETCHED_AT)

    assert [item.currency for item in rates] == ["CHF", "GBP"]
    assert math.isclose(rates[0].eur_per_unit, 1 / 0.9223)
    assert math.isclose(rates[1].eur_per_unit, 1 / 0.83735)
    assert all(item.published_at == FETCHED_AT for item in rates)


def test_sync_is_atomic_idempotent_and_timestamps_revisions() -> None:
    engine = create_database_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    def downloader(_url: str, _timeout: float) -> bytes:
        return CSV

    first = sync_ecb_reference_rates(
        sessions,
        fetched_at=FETCHED_AT,
        downloader=downloader,
    )
    second = sync_ecb_reference_rates(
        sessions,
        fetched_at=FETCHED_AT,
        downloader=downloader,
    )
    revised_csv = CSV.replace(b"0.83735", b"0.84000")
    revised_at = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)
    third = sync_ecb_reference_rates(
        sessions,
        fetched_at=revised_at,
        downloader=lambda _url, _timeout: revised_csv,
    )

    assert (first.inserted, first.updated, first.unchanged) == (2, 0, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 2)
    assert (third.inserted, third.updated, third.unchanged) == (0, 1, 1)
    with sessions() as session:
        rows = session.scalars(select(FxRate).order_by(FxRate.currency)).all()
    assert [item.currency for item in rows] == ["CHF", "GBP"]
    assert math.isclose(rows[1].eur_per_unit, 1 / 0.84)
    assert rows[1].published_at.replace(tzinfo=UTC) == revised_at
    engine.dispose()


def test_parser_rejects_future_or_malformed_data() -> None:
    future = CSV.replace(b"2026-11-20", b"2026-11-21")
    with pytest.raises(FxSyncError, match="future"):
        parse_ecb_reference_rates(future, fetched_at=FETCHED_AT)
    with pytest.raises(FxSyncError, match="missing required"):
        parse_ecb_reference_rates(b"currency,value\nGBP,1.2\n", fetched_at=FETCHED_AT)
