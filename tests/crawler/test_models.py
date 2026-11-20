from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from crowl_crawler.manifest import DailyManifestBuilder, FIXTURE_DESTINATIONS
from crowl_crawler.models import (
    ConnectorResult,
    SearchOutcome,
    validate_diagnostic_code,
)


class ModelContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = DailyManifestBuilder(
            origin_terminal_ids=("par-cdg", "par-ory"),
            config_version="test-v1",
        )

    def test_request_fingerprint_is_stable_and_ignores_terminal_order(self) -> None:
        manifest = self.builder.build(
            scheduled_for=date(2027, 1, 1),
            source_slugs=("fixture",),
            destinations=(FIXTURE_DESTINATIONS["lisbon-pt"],),
            weekend_fridays=(date(2027, 1, 8),),
        )
        request = manifest.tasks[0].request
        reordered = replace(
            request, origin_terminal_ids=tuple(reversed(request.origin_terminal_ids))
        )
        self.assertEqual(request.fingerprint, reordered.fingerprint)
        self.assertEqual(len(request.fingerprint), 64)

    def test_manifest_identity_is_stable_across_creation_time(self) -> None:
        arguments = dict(
            scheduled_for=date(2027, 1, 1),
            source_slugs=("fixture",),
            destinations=(FIXTURE_DESTINATIONS["lisbon-pt"],),
            weekend_fridays=(date(2027, 1, 8),),
        )
        first = self.builder.build(
            **arguments,
            created_at=datetime(2027, 1, 1, 0, tzinfo=ZoneInfo("UTC")),
        )
        second = self.builder.build(
            **arguments,
            created_at=datetime(2027, 1, 1, 1, tzinfo=ZoneInfo("UTC")),
        )
        self.assertEqual(first.manifest_id, second.manifest_id)

    def test_diagnostic_codes_cannot_contain_urls_or_free_text(self) -> None:
        self.assertEqual(validate_diagnostic_code("REQUEST_TIMEOUT"), "REQUEST_TIMEOUT")
        with self.assertRaises(ValueError):
            validate_diagnostic_code("upstream said https://secret.example/token")

    def test_success_results_requires_an_offer(self) -> None:
        with self.assertRaises(ValueError):
            ConnectorResult(
                outcome=SearchOutcome.SUCCESS_RESULTS,
                retryable=False,
                diagnostic_code="IMPOSSIBLE_EMPTY_SUCCESS",
                expected_count=1,
                completed_count=1,
                offers=(),
                parser_version="test",
                observed_at=datetime.now(ZoneInfo("UTC")),
            )


if __name__ == "__main__":
    unittest.main()
