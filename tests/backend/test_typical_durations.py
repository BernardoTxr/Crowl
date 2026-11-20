from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import delete

from crowl_core.enums import TransportMode
from crowl_core.models import DurationHistogramDaily


def _mode(payload: dict, mode: str) -> dict:
    return next(item for item in payload["typical_durations"] if item["mode"] == mode)


def test_typical_durations_use_valid_rolling_bins_and_nearest_rank_percentiles(
    client,
    seeded_app,
) -> None:
    # 2026-01-05 is the inclusive 180-day cutoff. Rows before it and rows
    # after the controlled observation date must not influence the result.
    seeded_app.state.clock = lambda: datetime(2026, 7, 4, 12, tzinfo=timezone.utc)
    with seeded_app.state.session_factory() as session:
        session.execute(
            delete(DurationHistogramDaily).where(
                DurationHistogramDaily.city_id == "amsterdam-nl",
                DurationHistogramDaily.mode == TransportMode.FLIGHT,
            )
        )
        session.add_all(
            [
                DurationHistogramDaily(
                    observed_on=date(2026, 1, 4),
                    city_id="amsterdam-nl",
                    mode=TransportMode.FLIGHT,
                    bins={"300": 100},
                    sample_count=100,
                ),
                DurationHistogramDaily(
                    observed_on=date(2026, 1, 5),
                    city_id="amsterdam-nl",
                    mode=TransportMode.FLIGHT,
                    bins={"15": 1, "30": 1, "45": 1, "60": 1, "75": 1},
                    sample_count=999,
                ),
                DurationHistogramDaily(
                    observed_on=date(2026, 7, 4),
                    city_id="amsterdam-nl",
                    mode=TransportMode.FLIGHT,
                    bins={
                        "bad": 50,
                        "-15": 50,
                        "20": 50,
                        "90": -1,
                        "105": True,
                        "120": "2",
                        "135": 0,
                    },
                    sample_count=1_000,
                ),
                DurationHistogramDaily(
                    observed_on=date(2026, 7, 5),
                    city_id="amsterdam-nl",
                    mode=TransportMode.FLIGHT,
                    bins={"600": 100},
                    sample_count=100,
                ),
            ]
        )
        session.commit()

    payload = client.get("/api/v1/cities/amsterdam").json()
    flight = _mode(payload, "flight")
    assert flight == {
        "mode": "flight",
        "state": "available",
        "p25_minutes": 30,
        "median_minutes": 45,
        "p75_minutes": 60,
        "sample_count": 5,
        "is_synthetic": False,
    }
    assert all(
        all(item[field] is not None for field in ("p25_minutes", "median_minutes", "p75_minutes"))
        for item in payload["typical_durations"]
        if item["state"] == "available"
    )

    with seeded_app.state.session_factory() as session:
        boundary = session.get(
            DurationHistogramDaily,
            (date(2026, 1, 5), "amsterdam-nl", TransportMode.FLIGHT, False),
        )
        assert boundary is not None
        boundary.bins = {"15": 1, "30": 1, "45": 1, "60": 1}
        boundary.sample_count = 999
        session.commit()

    flight = _mode(client.get("/api/v1/cities/amsterdam").json(), "flight")
    assert flight == {
        "mode": "flight",
        "state": "not_enough_data",
        "p25_minutes": None,
        "median_minutes": None,
        "p75_minutes": None,
        "sample_count": 4,
        "is_synthetic": False,
    }
