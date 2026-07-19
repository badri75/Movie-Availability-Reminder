from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

from monitor import (
    BookingResult,
    BookingCheckError,
    ConfigurationError,
    MonitorConfig,
    Showtime,
    TicketClass,
    already_notified,
    build_notification_message,
    filter_booking_result,
    load_config,
    run_monitor,
)


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


class MonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = Path(__file__).with_name(".runtime")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.runtime_dir / "config.test.json"
        self.state_path = self.runtime_dir / "state.test.json"
        self.config = MonitorConfig(
            movie_name="The Odyssey",
            city="Chennai",
            theatre_name="PVR: Palazzo, The Nexus Vijaya Mall",
            date=date(2026, 7, 22),
            formats="IMAX",
            bookmyshow_retry_delay_seconds=90,
        )
        self.available = BookingResult(
            available=True,
            theatre_name=self.config.theatre_name,
            showtimes=(
                Showtime(
                    time="10:30 AM",
                    format="IMAX",
                    availability="AVAILABLE",
                    ticket_classes=(
                        TicketClass(
                            class_name="ELITE",
                            price="508.34",
                            availability="AVAILABLE",
                        ),
                    ),
                ),
                Showtime(
                    time="07:30 PM",
                    format="IMAX",
                    availability="FAST FILLING",
                    ticket_classes=(
                        TicketClass(
                            class_name="ELITE",
                            price="508.34",
                            availability="FAST FILLING",
                        ),
                    ),
                ),
                Showtime(
                    time="09:00 PM",
                    format="2D",
                    availability="AVAILABLE",
                    ticket_classes=(
                        TicketClass(
                            class_name="PRIME",
                            price="190.00",
                            availability="AVAILABLE",
                        ),
                    ),
                ),
            ),
            booking_url="https://in.bookmyshow.com/example",
            event_id="ET00480917",
        )

    def tearDown(self) -> None:
        self.config_path.unlink(missing_ok=True)
        self.state_path.unlink(missing_ok=True)
        self.state_path.with_suffix(self.state_path.suffix + ".tmp").unlink(missing_ok=True)

    def test_config_requires_exact_five_fields(self) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "movie_name": "The Odyssey",
                    "city": "Chennai",
                    "theatre_name": self.config.theatre_name,
                    "date": "2026-07-22",
                    "formats": "IMAX",
                    "bookmyshow_retry_delay_seconds": 90,
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(load_config(self.config_path), self.config)

        self.config_path.write_text(
            json.dumps(
                {
                    "movie_name": "The Odyssey",
                    "city": "Chennai",
                    "theatre_name": self.config.theatre_name,
                    "date": "2026-07-22",
                    "formats": "IMAX",
                    "bookmyshow_retry_delay_seconds": 90,
                    "token": "must-not-be-here",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigurationError):
            load_config(self.config_path)

    def test_positive_result_must_be_confirmed_before_notification(self) -> None:
        results = iter([self.available, self.available])
        notifier = FakeNotifier()
        outcome = run_monitor(
            self.config,
            dry_run=False,
            confirmation_delay=0,
            ignore_state=False,
            state_path=self.state_path,
            checker=lambda _: next(results),
            notifier_factory=lambda: notifier,
        )
        self.assertTrue(outcome.notified)
        self.assertEqual(len(notifier.messages), 1)
        self.assertTrue(already_notified(self.config, self.state_path))

    def test_failed_confirmation_does_not_notify(self) -> None:
        unavailable = BookingResult(available=False, theatre_name=self.config.theatre_name)
        results = iter([self.available, unavailable])
        notifier = FakeNotifier()
        outcome = run_monitor(
            self.config,
            dry_run=False,
            confirmation_delay=0,
            ignore_state=False,
            state_path=self.state_path,
            checker=lambda _: next(results),
            notifier_factory=lambda: notifier,
        )
        self.assertEqual(outcome.status, "not_confirmed")
        self.assertEqual(notifier.messages, [])

    def test_dry_run_never_creates_notifier_or_state(self) -> None:
        results = iter([self.available, self.available])
        outcome = run_monitor(
            self.config,
            dry_run=True,
            confirmation_delay=0,
            ignore_state=False,
            state_path=self.state_path,
            checker=lambda _: next(results),
            notifier_factory=lambda: self.fail("notifier must not be created"),
        )
        self.assertEqual(outcome.status, "available_dry_run")
        self.assertFalse(self.state_path.exists())

    def test_notification_contains_target_and_booking_link(self) -> None:
        message = build_notification_message(self.config, self.available)
        self.assertIn("The Odyssey bookings are open", message)
        self.assertIn(self.config.theatre_name, message)
        self.assertIn("Wednesday, 22 July 2026", message)
        self.assertIn("10:30 AM (IMAX) - AVAILABLE", message)
        self.assertIn("ELITE | 508.34 | FAST FILLING", message)
        self.assertNotIn("₹", message)
        self.assertIn(self.available.booking_url, message)

    def test_filter_booking_result_keeps_requested_format_only(self) -> None:
        filtered = filter_booking_result(self.config, self.available)
        self.assertTrue(filtered.available)
        self.assertEqual(
            [showtime.format for showtime in filtered.showtimes],
            ["IMAX", "IMAX"],
        )

    def test_semicolon_format_filter_keeps_multiple_formats(self) -> None:
        config = MonitorConfig(
            movie_name=self.config.movie_name,
            city=self.config.city,
            theatre_name=self.config.theatre_name,
            date=self.config.date,
            formats="2D;IMAX",
            bookmyshow_retry_delay_seconds=90,
        )
        filtered = filter_booking_result(config, self.available)
        self.assertEqual(
            [showtime.format for showtime in filtered.showtimes],
            ["IMAX", "IMAX", "2D"],
        )

    def test_empty_format_filter_keeps_everything(self) -> None:
        config = MonitorConfig(
            movie_name=self.config.movie_name,
            city=self.config.city,
            theatre_name=self.config.theatre_name,
            date=self.config.date,
            formats="",
            bookmyshow_retry_delay_seconds=90,
        )
        self.assertEqual(filter_booking_result(config, self.available), self.available)

    def test_bookmyshow_errors_are_retried_once_before_failing_run(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def checker(_: MonitorConfig) -> BookingResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BookingCheckError("temporary BookMyShow failure")
            return self.available

        outcome = run_monitor(
            self.config,
            dry_run=True,
            confirmation_delay=0,
            ignore_state=False,
            state_path=self.state_path,
            checker=checker,
            sleep=sleeps.append,
        )

        self.assertEqual(outcome.status, "available_dry_run")
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [90])


if __name__ == "__main__":
    unittest.main()
