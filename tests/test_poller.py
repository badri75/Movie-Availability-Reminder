from __future__ import annotations

import json
import os
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from poller import (
    ScrapingAntClient,
    ScrapingAntError,
    discover_theatre,
    extract_initial_state,
    extract_movie_schedule,
)


class PollerTests(unittest.TestCase):
    def test_scrapingant_client_uses_free_tier_friendly_defaults(self) -> None:
        captured_urls: list[str] = []

        class FakeHeaders:
            @staticmethod
            def get_content_charset() -> str:
                return "utf-8"

        class FakeResponse:
            status = 200
            headers = FakeHeaders()

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            @staticmethod
            def getcode() -> int:
                return 200

            @staticmethod
            def read() -> bytes:
                return b"<html>BookMyShow</html>"

        def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
            self.assertEqual(timeout, 90)
            captured_urls.append(request.full_url)
            return FakeResponse()

        with patch.dict(os.environ, {"SCRAPINGANT_API_KEY": "test-key"}, clear=True):
            with patch("poller.urlopen", side_effect=fake_urlopen):
                result = ScrapingAntClient.from_environment().fetch(
                    "https://in.bookmyshow.com/chennai/cinemas"
                )

        self.assertEqual(result, "<html>BookMyShow</html>")
        query = parse_qs(urlparse(captured_urls[0]).query)
        self.assertEqual(query["x-api-key"], ["test-key"])
        self.assertEqual(query["browser"], ["false"])
        self.assertEqual(query["proxy_type"], ["datacenter"])
        self.assertEqual(query["proxy_country"], ["IN"])

    def test_extracts_theatre_movie_showtime_and_original_status_text(self) -> None:
        state = {
            "catalog": {
                "VenueName": "PVR: Palazzo, The Nexus Vijaya Mall",
                "VenueCode": "PVPL",
            },
            "schedule": {
                "EventTitle": "The Odyssey",
                "ChildEvents": [
                    {
                        "EventCode": "ET00480917",
                        "EventDimension": "IMAX",
                        "ShowTimes": [
                            {
                                "ShowTime": "10:30 AM",
                                "AvailStatusText": "Fast Filling",
                                "Categories": [
                                    {
                                        "PriceDesc": "ELITE",
                                        "CurPrice": "₹ 508.34",
                                        "AvailabilityText": "Almost full",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        }
        html = (
            "<html><script>window.__INITIAL_STATE__ = "
            + json.dumps(state)
            + ";</script></html>"
        )

        parsed = extract_initial_state(html)
        theatre_name, venue_code = discover_theatre(
            parsed, "PVR: Palazzo, The Nexus Vijaya Mall"
        )
        result = extract_movie_schedule(
            parsed,
            "The Odyssey",
            theatre_name,
            "https://in.bookmyshow.com/example",
        )

        self.assertEqual(venue_code, "PVPL")
        self.assertEqual(result.event_ids, ("ET00480917",))
        self.assertEqual(result.showtimes[0].availability, "Fast Filling")
        self.assertEqual(result.showtimes[0].ticket_classes[0].availability, "Almost full")
        self.assertEqual(result.showtimes[0].ticket_classes[0].price, "508.34")

    def test_missing_initial_state_is_an_error(self) -> None:
        with self.assertRaises(ScrapingAntError):
            extract_initial_state("<html><body>No schedule</body></html>")


if __name__ == "__main__":
    unittest.main()
