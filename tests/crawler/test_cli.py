from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from crowl_core import Base, create_database_engine, create_session_factory
from crowl_core.catalog import seed_development_data
from crowl_core.models import City, CrawlRun, ExtractorRun, Source
from crowl_crawler.adapters.registry import AdapterRegistry, build_default_registry
from crowl_crawler.cli import _production_run, main


ROOT = Path(__file__).resolve().parents[2]


class CliTests(unittest.TestCase):
    def test_sources_json_lists_exactly_the_requested_sources(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(("sources", "--json"))
        rows = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(rows), 14)
        self.assertTrue(all(row["enabled"] is False for row in rows))

    def test_fixture_batch_command_prints_success_report(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(("fixture-batch", "--date", "2027-01-01"))
        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["offer_count"], 4)
        self.assertGreater(report["health_event_count"], 0)

    def test_fixture_publish_requires_database_url(self) -> None:
        errors = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(errors):
            exit_code = main(("fixture-publish", "--date", "2027-01-01"))
        self.assertEqual(exit_code, 2)
        self.assertIn("requires DATABASE_URL", errors.getvalue())

    def test_production_run_requires_database_url(self) -> None:
        errors = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(errors):
            exit_code = main(("run", "--date", "2027-01-01"))
        self.assertEqual(exit_code, 2)
        self.assertIn("run requires DATABASE_URL", errors.getvalue())

    def test_production_run_publishes_disabled_state_without_fixture_fares(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "crowl.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            engine = create_database_engine(database_url)
            Base.metadata.create_all(engine)
            sessions = create_session_factory(engine)
            with sessions() as session:
                seed_development_data(
                    session,
                    data_dir=ROOT / "data",
                    as_of=date(2027, 1, 1),
                    include_fixture_fares=True,
                )
                for city in session.scalars(select(City)).all():
                    city.active = city.id == "lisbon-pt"
                session.commit()
            engine.dispose()

            default_registry = build_default_registry(include_fixture=False)
            registry = AdapterRegistry((default_registry.get("ryanair"),))
            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "DATABASE_URL": database_url,
                        "CROWL_BATCH_DEADLINE_HOURS": "1",
                    },
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                exit_code = asyncio.run(
                    _production_run(
                        scheduled_for=date(2027, 1, 1),
                        pretty=False,
                        registry=registry,
                        fx_syncer=None,
                    )
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["sources"][0]["state"], "disabled")
            self.assertEqual(payload["sources"][0]["expected_tasks"], 0)
            self.assertEqual(payload["publication"]["snapshots_created"], 0)
            self.assertEqual(payload["fx_sync"]["status"], "skipped")
            self.assertNotIn("executions", payload["sources"][0])

            engine = create_database_engine(database_url)
            sessions = create_session_factory(engine)
            with sessions() as session:
                extractor = session.scalar(
                    select(ExtractorRun)
                    .join(CrawlRun, ExtractorRun.crawl_run_id == CrawlRun.id)
                    .where(
                        ExtractorRun.source_id == "ryanair",
                        CrawlRun.is_synthetic.is_(False),
                    )
                )
                self.assertIsNotNone(extractor)
                assert extractor is not None
                self.assertEqual(extractor.status.value, "disabled")
                self.assertEqual(extractor.attempted_searches, 0)
                self.assertEqual(
                    session.get(Source, "ryanair").strategy,
                    "http_structured",
                )
                same_day_runs = session.scalars(
                    select(CrawlRun).where(
                        CrawlRun.scheduled_for == date(2027, 1, 1)
                    )
                ).all()
                self.assertEqual(len(same_day_runs), 2)
                self.assertEqual(
                    {item.is_synthetic for item in same_day_runs},
                    {False, True},
                )
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
