from __future__ import annotations

import unittest

from scripts.discover_flixbus_routes import (
    MAX_DURATION_MINUTES,
    RoutePageError,
    parse_route_page,
)


ROUTE_PAGE = """
<html><body>
<script>injectSearchMask("en", "40de8964-8646-11e6-9066-549f350fcb0c",
"40deaa9f-8646-11e6-9066-549f350fcb0c", "");</script>
<h4>Min. trip duration</h4>
<h1 class="city-connection-trip-overview-card-title">2 hr 40 mins</h1>
<div id="second-city-content">
  <div class="flix-accordion" data-stop-id="dcbf8aeb-9603-11e6-9066-549f350fcb0c">
    <h3><span></span><span>Lille Europe</span></h3>
    <a href="https://www.google.com/maps/place/50.639197,3.076202">Map</a>
  </div>
</div>
</body></html>
"""


class FlixBusRouteDiscoveryTests(unittest.TestCase):
    def test_extracts_city_uuid_duration_and_exact_stop_from_public_html(self) -> None:
        route = parse_route_page(ROUTE_PAGE)

        self.assertEqual(
            route["destination_city_id"],
            "40deaa9f-8646-11e6-9066-549f350fcb0c",
        )
        self.assertEqual(route["minimum_duration_minutes"], 160)
        self.assertLess(route["minimum_duration_minutes"], MAX_DURATION_MINUTES)
        self.assertEqual(
            route["stops"],
            (
                {
                    "stop_id": "dcbf8aeb-9603-11e6-9066-549f350fcb0c",
                    "name": "Lille Europe",
                    "coordinates": {"lat": 50.639197, "lon": 3.076202},
                },
            ),
        )

    def test_accepts_reverse_public_page_as_route_identity_evidence(self) -> None:
        reverse = (
            ROUTE_PAGE.replace(
                '"40de8964-8646-11e6-9066-549f350fcb0c",\n'
                '"40deaa9f-8646-11e6-9066-549f350fcb0c"',
                '"40deaa9f-8646-11e6-9066-549f350fcb0c",\n'
                '"40de8964-8646-11e6-9066-549f350fcb0c"',
            )
            .replace('id="second-city-content"', 'id="first-city-content"')
        )

        route = parse_route_page(reverse)

        self.assertEqual(
            route["destination_city_id"],
            "40deaa9f-8646-11e6-9066-549f350fcb0c",
        )
        self.assertEqual(route["public_page_direction"], "city_to_paris")

    def test_rejects_page_without_route_identity(self) -> None:
        with self.assertRaisesRegex(RoutePageError, "route identity"):
            parse_route_page("<html><body>route explorer</body></html>")


if __name__ == "__main__":
    unittest.main()
