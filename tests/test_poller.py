from __future__ import annotations

import json
import os
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from poller import (
    ScrapingAntClient,
    ScrapingAntError,
    extract_initial_state,
    extract_movie_schedule,
    poll_bookmyshow,
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
                "VenueName": "INOX: LUXE Phoenix Market City, Velachery",
                "VenueCode": "INPR",
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
        result = extract_movie_schedule(
            parsed,
            "The Odyssey",
            "PVR: Palazzo, The Nexus Vijaya Mall",
            "https://in.bookmyshow.com/example",
        )

        self.assertEqual(result.event_ids, ("ET00480917",))
        self.assertEqual(result.showtimes[0].format, "IMAX")
        self.assertEqual(result.showtimes[0].availability, "Fast Filling")
        self.assertEqual(result.showtimes[0].ticket_classes[0].availability, "Almost full")
        self.assertEqual(result.showtimes[0].ticket_classes[0].price, "508.34")

    def test_missing_initial_state_is_an_error(self) -> None:
        with self.assertRaises(ScrapingAntError):
            extract_initial_state("<html><body>No schedule</body></html>")

    def test_poller_fetches_only_the_configured_theatre_url(self) -> None:
        theatre_url = (
            "https://in.bookmyshow.com/cinemas/CHEN/"
            "inox-luxe-phoenix-market-city-velachery/buytickets/INPR/20260722"
        )
        state = {
            "schedule": {
                "VenueName": "INOX: LUXE Phoenix Market City, Velachery",
                "VenueCode": "INPR",
                "EventTitle": "The Odyssey",
                "ChildEvents": [
                    {
                        "EventCode": "ET00480917",
                        "EventDimension": "IMAX 2D",
                        "ShowTimes": [{"ShowTime": "09:10 AM", "AvailStatus": "1"}],
                    }
                ],
            }
        }
        html = (
            "<html><script>window.__INITIAL_STATE__ = "
            + json.dumps(state)
            + ";</script></html>"
        )

        class FakeClient:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def fetch(self, url: str) -> str:
                self.urls.append(url)
                return html

        client = FakeClient()
        with patch(
            "poller.ScrapingAntClient.from_environment",
            return_value=client,
        ):
            result = poll_bookmyshow(
                "The Odyssey",
                theatre_url,
            )

        self.assertEqual(client.urls, [theatre_url])
        self.assertEqual(result.theatre_name, "INOX: LUXE Phoenix Market City, Velachery")
        self.assertEqual(result.booking_url, theatre_url)
        self.assertEqual(result.event_ids, ("ET00480917",))
        self.assertEqual(result.showtimes[0].format, "IMAX 2D")


if __name__ == "__main__":
    unittest.main()
