"""Monitor BookMyShow and notify when a configured movie is bookable."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlparse

from notifier import (
    TelegramConfigurationError,
    TelegramDeliveryError,
    TelegramNotifier,
)


CONFIG_KEYS = frozenset(
    {
        "movie_name",
        "theatre_url",
        "formats",
        "bookmyshow_retry_delay_seconds",
    }
)
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_STATE_PATH = Path(__file__).with_name("state.json")
INDIA_TIMEZONE = timezone(timedelta(hours=5, minutes=30), name="IST")


class ConfigurationError(ValueError):
    """Raised when config.json violates the required schema."""


class BookingCheckError(RuntimeError):
    """Raised when BookMyShow cannot be checked reliably."""


@dataclass(frozen=True)
class MonitorConfig:
    movie_name: str
    theatre_url: str
    formats: str
    bookmyshow_retry_delay_seconds: float

    @property
    def date(self) -> date:
        return theatre_url_date(self.theatre_url)

    @property
    def target_key(self) -> str:
        return (
            f"{normalize_text(self.movie_name)}|{normalize_text(self.theatre_url)}|"
            f"{normalize_text(self.formats)}"
        )


@dataclass(frozen=True)
class TicketClass:
    class_name: str
    price: str
    availability: str


@dataclass(frozen=True)
class Showtime:
    time: str
    format: str = ""
    availability: str = "Unknown"
    ticket_classes: tuple[TicketClass, ...] = ()


@dataclass(frozen=True)
class BookingResult:
    available: bool
    theatre_name: str
    showtimes: tuple[Showtime, ...] = ()
    booking_url: str = ""
    event_id: str = ""


@dataclass(frozen=True)
class RunOutcome:
    status: str
    available: bool
    notified: bool
    result: BookingResult | None = None


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return normalized


def validate_theatre_url(value: str) -> str:
    """Validate a direct, dated BookMyShow theatre booking URL."""
    theatre_url = value.strip()
    parsed = urlparse(theatre_url)
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != "in.bookmyshow.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "theatre_url must be a plain HTTPS URL on in.bookmyshow.com."
        )

    match = re.fullmatch(
        r"/cinemas/[A-Za-z0-9-]+/[A-Za-z0-9-]+/buytickets/"
        r"[A-Za-z0-9]+/(\d{8})/?",
        parsed.path,
    )
    if not match:
        raise ConfigurationError(
            "theatre_url must be a dated BookMyShow /cinemas/.../buytickets/... URL."
        )
    try:
        datetime.strptime(match.group(1), "%Y%m%d")
    except ValueError:
        raise ConfigurationError("theatre_url contains an invalid date.") from None
    return theatre_url


def theatre_url_date(theatre_url: str) -> date:
    """Return the date encoded in a validated theatre booking URL."""
    match = re.search(r"/(\d{8})/?$", urlparse(theatre_url).path)
    if not match:
        raise ConfigurationError("theatre_url does not contain a booking date.")
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def theatre_url_venue_code(theatre_url: str) -> str:
    """Return the venue code encoded in a validated theatre booking URL."""
    match = re.search(r"/buytickets/([A-Za-z0-9]+)/(?:\d{8})/?$", urlparse(theatre_url).path)
    if not match:
        raise ConfigurationError("theatre_url does not contain a venue code.")
    return match.group(1)


def format_filters(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(";") if part.strip())


def showtime_matches_formats(showtime: Showtime, filters: Sequence[str]) -> bool:
    if not filters:
        return True
    normalized_format = normalize_text(showtime.format)
    return any(normalize_text(filter_value) in normalized_format for filter_value in filters)


def filter_booking_result(config: MonitorConfig, result: BookingResult) -> BookingResult:
    filters = format_filters(config.formats)
    if not filters or not result.available:
        return result

    showtimes = tuple(
        showtime for showtime in result.showtimes if showtime_matches_formats(showtime, filters)
    )
    return BookingResult(
        available=bool(showtimes),
        theatre_name=result.theatre_name,
        showtimes=showtimes,
        booking_url=result.booking_url,
        event_id=result.event_id,
    )


def check_bookmyshow_with_retry(
    config: MonitorConfig,
    checker: Callable[[MonitorConfig], BookingResult],
    sleep: Callable[[float], None],
) -> BookingResult:
    try:
        return checker(config)
    except BookingCheckError:
        delay = config.bookmyshow_retry_delay_seconds
        if delay > 0:
            sleep(delay)
        return checker(config)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> MonitorConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigurationError(f"Configuration file was not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Configuration is not valid JSON: {exc.msg}") from None

    if not isinstance(payload, dict):
        raise ConfigurationError("Configuration must be a JSON object.")
    keys = frozenset(payload)
    if keys != CONFIG_KEYS:
        missing = sorted(CONFIG_KEYS - keys)
        extra = sorted(keys - CONFIG_KEYS)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected: {', '.join(extra)}")
        raise ConfigurationError(
            "config.json must contain exactly movie_name, theatre_url, formats, "
            "and bookmyshow_retry_delay_seconds"
            + (f" ({'; '.join(details)})" if details else "")
            + "."
        )

    movie_name = payload.get("movie_name")
    theatre_url = payload.get("theatre_url")
    formats = payload.get("formats")
    retry_delay = payload.get("bookmyshow_retry_delay_seconds")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (movie_name, theatre_url)
    ):
        raise ConfigurationError(
            "movie_name and theatre_url must be non-empty strings."
        )
    if not isinstance(formats, str):
        raise ConfigurationError("formats must be a string; use an empty string for all formats.")
    if isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float)):
        raise ConfigurationError("bookmyshow_retry_delay_seconds must be a number.")
    if retry_delay < 0:
        raise ConfigurationError("bookmyshow_retry_delay_seconds cannot be negative.")

    validated_theatre_url = validate_theatre_url(theatre_url)

    return MonitorConfig(
        movie_name=movie_name.strip(),
        theatre_url=validated_theatre_url,
        formats=formats.strip(),
        bookmyshow_retry_delay_seconds=float(retry_delay),
    )


EXTRACT_MOVIE_FROM_THEATRE_SCRIPT = """
({ movieName, venueCode }) => {
  const normalize = (value) => String(value || "")
    .normalize("NFKC")
    .replace(/\\s+/g, " ")
    .trim()
    .toLocaleLowerCase("en-IN");
  const statusFromCode = (value) => {
    if (String(value) === "2") return "FAST FILLING";
    if (String(value) === "1") return "AVAILABLE";
    if (["0", "3"].includes(String(value))) return "SOLD OUT";
    return "UNKNOWN";
  };
  const marker = "window.__INITIAL_STATE__ = ";
  const script = Array.from(document.scripts)
    .map((item) => item.textContent || "")
    .find((text) => text.includes(marker) && text.includes(movieName));
  if (!script) return null;

  const markerIndex = script.indexOf(marker);
  const openIndex = script.indexOf("{", markerIndex + marker.length);
  if (openIndex < 0) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  let closeIndex = -1;
  for (let index = openIndex; index < script.length; index += 1) {
    const character = script[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (character === "\\\\") escaped = true;
      else if (character === '"') inString = false;
    } else if (character === '"') inString = true;
    else if (character === "{") depth += 1;
    else if (character === "}" && --depth === 0) {
      closeIndex = index;
      break;
    }
  }
  if (closeIndex < 0) return null;

  let state;
  try {
    state = JSON.parse(script.slice(openIndex, closeIndex + 1));
  } catch (_) {
    return null;
  }

  const target = normalize(movieName);
  const childEvents = [];
  let theatreName = "";
  const seen = new Set();
  const visit = (value) => {
    if (!value || typeof value !== "object" || seen.has(value)) return;
    seen.add(value);
    const candidateVenueCode = String(value.VenueCode || value.venueCode || "").trim();
    const candidateVenueName = String(value.VenueName || value.venueName || "").trim();
    if (
      !theatreName &&
      candidateVenueName &&
      candidateVenueCode.toLocaleLowerCase("en-IN") ===
        String(venueCode).toLocaleLowerCase("en-IN")
    ) {
      theatreName = candidateVenueName;
    }
    if (normalize(value.EventTitle) === target && Array.isArray(value.ChildEvents)) {
      childEvents.push(...value.ChildEvents);
    }
    Object.values(value).forEach(visit);
  };
  visit(state);

  const uniqueEvents = new Map();
  for (const event of childEvents) {
    const eventId = String(event.EventCode || "").trim();
    if (eventId) uniqueEvents.set(eventId, event);
  }

  const showtimes = [];
  for (const event of uniqueEvents.values()) {
    for (const showtime of event.ShowTimes || []) {
      const classes = (showtime.Categories || []).map((category) => ({
        className: String(category.PriceDesc || category.PriceCode || "Unknown").trim(),
        price: String(category.CurPrice || "").replace(/₹/g, "").trim(),
        availability: statusFromCode(category.AvailStatus),
      }));
      showtimes.push({
        time: String(showtime.ShowTime || "").trim(),
        format: String(showtime.Attributes || event.EventDimension || "").trim(),
        availability: statusFromCode(showtime.AvailStatus),
        ticketClasses: classes,
      });
    }
  }

  return {
    theatreName,
    eventIds: Array.from(uniqueEvents.keys()),
    showtimes,
  };
}
"""


def _check_bookings_with_playwright(
    config: MonitorConfig,
    *,
    headless: bool = True,
    timeout_ms: int = 20_000,
) -> BookingResult:
    """Check the configured movie from the configured theatre's dated schedule."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BookingCheckError(
            "Playwright is not installed. Run: pip install -r requirements.txt"
        ) from None

    compact_date = config.date.strftime("%Y%m%d")
    debug_enabled = os.environ.get("BMS_DEBUG", "").strip() == "1"

    try:
        with sync_playwright() as playwright:
            browser_channel = os.environ.get("BMS_BROWSER_CHANNEL", "").strip() or None
            browser = playwright.chromium.launch(
                headless=headless,
                channel=browser_channel,
            )
            context = browser.new_context(
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
            )

            def block_heavy_assets(route: object) -> None:
                resource_type = route.request.resource_type
                if resource_type in {"image", "media", "font"}:
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", block_heavy_assets)
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            try:
                schedule_url = config.theatre_url
                venue_code = theatre_url_venue_code(schedule_url)
                response = page.goto(
                    schedule_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                if response is not None and response.status in {403, 429}:
                    raise BookingCheckError(
                        f"Theatre schedule was blocked by BookMyShow (HTTP {response.status})."
                    )
                page.wait_for_selector("main", state="attached", timeout=timeout_ms)

                if compact_date not in page.url:
                    return BookingResult(
                        available=False,
                        theatre_name="",
                    )

                extracted = page.evaluate(
                    EXTRACT_MOVIE_FROM_THEATRE_SCRIPT,
                    {
                        "movieName": config.movie_name,
                        "venueCode": venue_code,
                    },
                )
                if debug_enabled:
                    print(
                        json.dumps(
                            {
                                "schedule_url": schedule_url,
                                "loaded_url": page.url,
                                "event_ids": extracted.get("eventIds", []) if extracted else [],
                            }
                        ),
                        file=sys.stderr,
                    )

                if not extracted:
                    raise BookingCheckError(
                        "BookMyShow returned an unrecognized theatre schedule."
                    )
                theatre_name = str(extracted.get("theatreName") or "").strip()
                if not theatre_name:
                    theatre_name = re.split(r"\s+\|\s+", page.title(), maxsplit=1)[0].strip()
                if not theatre_name:
                    raise BookingCheckError(
                        "BookMyShow did not provide the theatre name for the configured URL."
                    )
                if not extracted.get("showtimes"):
                    return BookingResult(
                        available=False,
                        theatre_name=theatre_name,
                    )

                showtimes = tuple(
                    Showtime(
                        time=str(value.get("time") or "").strip(),
                        format=str(value.get("format") or "").strip(),
                        availability=str(value.get("availability") or "UNKNOWN").strip(),
                        ticket_classes=tuple(
                            TicketClass(
                                class_name=str(
                                    ticket_class.get("className") or "Unknown"
                                ).strip(),
                                price=str(ticket_class.get("price") or "").strip(),
                                availability=str(
                                    ticket_class.get("availability") or "UNKNOWN"
                                ).strip(),
                            )
                            for ticket_class in value.get("ticketClasses", [])
                            if isinstance(ticket_class, dict)
                        ),
                    )
                    for value in extracted["showtimes"]
                    if isinstance(value, dict) and str(value.get("time") or "").strip()
                )
                return BookingResult(
                    available=bool(showtimes),
                    theatre_name=theatre_name,
                    showtimes=tuple(dict.fromkeys(showtimes)),
                    booking_url=schedule_url,
                    event_id=",".join(
                        str(value) for value in extracted.get("eventIds", [])
                    ),
                )
            finally:
                page.close()
                browser.close()
    except Exception as exc:
        if isinstance(exc, BookingCheckError):
            raise
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else "no details"
        raise BookingCheckError(
            f"Browser check failed: {type(exc).__name__}: {first_line[:300]}"
        ) from None


def _check_bookings_with_scrapingant(
    config: MonitorConfig,
    *,
    timeout_ms: int = 20_000,
) -> BookingResult:
    try:
        from poller import ScrapingAntError, poll_bookmyshow
    except ImportError as exc:
        raise BookingCheckError(f"ScrapingAnt poller could not be loaded: {exc}.") from None

    try:
        scraped = poll_bookmyshow(
            config.movie_name,
            config.theatre_url,
            timeout_seconds=max(90.0, timeout_ms / 1000),
        )
    except ScrapingAntError as exc:
        raise BookingCheckError(f"ScrapingAnt check failed: {exc}") from None

    showtimes = tuple(
        Showtime(
            time=value.time,
            format=value.format,
            availability=value.availability,
            ticket_classes=tuple(
                TicketClass(
                    class_name=ticket_class.class_name,
                    price=ticket_class.price,
                    availability=ticket_class.availability,
                )
                for ticket_class in value.ticket_classes
            ),
        )
        for value in scraped.showtimes
    )
    return BookingResult(
        available=bool(showtimes),
        theatre_name=scraped.theatre_name,
        showtimes=showtimes,
        booking_url=scraped.booking_url,
        event_id=",".join(scraped.event_ids),
    )


def check_bookings(
    config: MonitorConfig,
    *,
    headless: bool = True,
    timeout_ms: int = 20_000,
) -> BookingResult:
    """Use ScrapingAnt when configured; otherwise use the local Playwright browser."""
    if os.environ.get("SCRAPINGANT_API_KEY", "").strip():
        return _check_bookings_with_scrapingant(config, timeout_ms=timeout_ms)
    return _check_bookings_with_playwright(
        config,
        headless=headless,
        timeout_ms=timeout_ms,
    )


def build_notification_message(config: MonitorConfig, result: BookingResult) -> str:
    date_label = config.date.strftime("%A, %d %B %Y")
    showtime_lines: list[str] = []
    for showtime in result.showtimes:
        format_label = f" ({showtime.format})" if showtime.format else ""
        showtime_lines.append(
            f"- {showtime.time}{format_label} - {showtime.availability}"
        )
        for ticket_class in showtime.ticket_classes:
            price_label = f" | {ticket_class.price}" if ticket_class.price else ""
            showtime_lines.append(
                f"  {ticket_class.class_name}{price_label} | {ticket_class.availability}"
            )
    showtimes = "\n".join(showtime_lines) or "No showtime details returned"
    detected_at = datetime.now(INDIA_TIMEZONE).strftime("%d %b %Y, %I:%M %p IST")
    return (
        f"\U0001F3AC {config.movie_name} bookings are open!\n\n"
        f"Theatre: {result.theatre_name}\n"
        f"Date: {date_label}\n"
        f"Showtimes:\n{showtimes}\n\n"
        f"Book now: {result.booking_url}\n"
        f"Detected: {detected_at}"
    )


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(config: MonitorConfig, result: BookingResult, path: Path = DEFAULT_STATE_PATH) -> None:
    payload = {
        "target_key": config.target_key,
        "notified": True,
        "notified_at": datetime.now(timezone.utc).isoformat(),
        "booking": asdict(result),
    }
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def already_notified(config: MonitorConfig, path: Path = DEFAULT_STATE_PATH) -> bool:
    state = load_state(path)
    return state.get("notified") is True and state.get("target_key") == config.target_key


def run_monitor(
    config: MonitorConfig,
    *,
    dry_run: bool,
    confirmation_delay: float,
    ignore_state: bool,
    state_path: Path,
    checker: Callable[[MonitorConfig], BookingResult] = check_bookings,
    notifier_factory: Callable[[], TelegramNotifier] = TelegramNotifier.from_environment,
    sleep: Callable[[float], None] = time.sleep,
) -> RunOutcome:
    if not ignore_state and already_notified(config, state_path):
        return RunOutcome(status="already_notified", available=True, notified=False)

    first_result = filter_booking_result(
        config,
        check_bookmyshow_with_retry(config, checker, sleep),
    )
    if not first_result.available:
        return RunOutcome(status="not_available", available=False, notified=False, result=first_result)

    if confirmation_delay > 0:
        sleep(confirmation_delay)
    confirmed_result = filter_booking_result(
        config,
        check_bookmyshow_with_retry(config, checker, sleep),
    )
    if not confirmed_result.available:
        return RunOutcome(
            status="not_confirmed",
            available=False,
            notified=False,
            result=confirmed_result,
        )

    if dry_run:
        return RunOutcome(
            status="available_dry_run",
            available=True,
            notified=False,
            result=confirmed_result,
        )

    notifier = notifier_factory()
    notifier.send(build_notification_message(config, confirmed_result))
    save_state(config, confirmed_result, state_path)
    return RunOutcome(
        status="notified",
        available=True,
        notified=True,
        result=confirmed_result,
    )


def write_github_output(path: Path, outcome: RunOutcome) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"available={str(outcome.available).lower()}\n")
        output.write(f"notified={str(outcome.notified).lower()}\n")
        output.write(f"status={outcome.status}\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check twice and print the result without contacting Telegram.",
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="Send a Telegram connectivity test without checking BookMyShow.",
    )
    parser.add_argument(
        "--confirmation-delay",
        type=float,
        default=30.0,
        help="Seconds between the initial positive result and confirmation (default: 30).",
    )
    parser.add_argument("--ignore-state", action="store_true")
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Append available/notified/status outputs to this GitHub Actions output file.",
    )
    parser.add_argument(
        "--soft-fail-bookmyshow-errors",
        action="store_true",
        help="Print BookMyShow check errors as JSON and exit 0 after retries.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        if args.test_notification:
            TelegramNotifier.from_environment().send(
                f"\u2705 {config.movie_name} booking monitor Telegram test succeeded."
            )
            print("Telegram test notification sent.")
            return 0

        checker = lambda value: check_bookings(value)
        outcome = run_monitor(
            config,
            dry_run=args.dry_run,
            confirmation_delay=max(0.0, args.confirmation_delay),
            ignore_state=args.ignore_state,
            state_path=args.state,
            checker=checker,
        )
        if args.github_output:
            write_github_output(args.github_output, outcome)

        print(
            json.dumps(
                {
                    "status": outcome.status,
                    "available": outcome.available,
                    "notified": outcome.notified,
                    "showtimes": (
                        [asdict(showtime) for showtime in outcome.result.showtimes]
                        if outcome.result
                        else []
                    ),
                    "booking_url": outcome.result.booking_url if outcome.result else "",
                },
                indent=2,
            )
        )
        return 0
    except (ConfigurationError, TelegramConfigurationError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except BookingCheckError as exc:
        if args.soft_fail_bookmyshow_errors:
            outcome = RunOutcome(status="bookmyshow_error", available=False, notified=False)
            if args.github_output:
                write_github_output(args.github_output, outcome)
            print(
                json.dumps(
                    {
                        "status": outcome.status,
                        "available": outcome.available,
                        "notified": outcome.notified,
                        "error": str(exc),
                    },
                    indent=2,
                )
            )
            return 0
        print(f"Booking check error: {exc}", file=sys.stderr)
        return 3
    except TelegramDeliveryError as exc:
        print(f"Notification error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
