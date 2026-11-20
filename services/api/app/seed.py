from __future__ import annotations

from crowl_core.catalog import seed_development_data
from crowl_core.db import Base, create_database_engine, create_session_factory
from crowl_core.models import City  # noqa: F401 - registers model metadata

from .config import APISettings


def main() -> None:
    settings = APISettings.from_env()
    engine = create_database_engine(settings.database_url)
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        report = seed_development_data(
            session,
            data_dir=settings.data_dir,
            include_fixture_fares=settings.seed_fixtures,
        )
    print(
        f"Seeded {report.cities} cities, {report.terminals} terminals, "
        f"{report.sources} sources, {report.travel_windows} weekends and "
        f"{report.fixture_snapshots} fixture snapshots."
    )


if __name__ == "__main__":
    main()
