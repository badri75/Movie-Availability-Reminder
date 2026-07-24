"""Fetch and extract BookMyShow schedules through ScraperAPI."""

from __future__ import annotations

import html as html_module
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com"
INITIAL_STATE_MARKER = "window.__INITIAL_STATE__ = "


class ScraperAPIError(RuntimeError):
    """Raised when ScraperAPI or the returned BookMyShow page cannot be used."""


@dataclass(frozen=True)
class ScrapedTicketClass:
    class_name: str
    price: str
    availability: str


@dataclass(frozen=True)
class ScrapedShowtime:
    time: str
    format: str
    availability: str
    ticket_classes: tuple[ScrapedTicketClass, ...] = ()


@dataclass(frozen=True)
class ScrapedBooking:
    theatre_name: str
    showtimes: tuple[ScrapedShowtime, ...]
    booking_url: str
    event_ids: tuple[str, ...] = ()


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _enabled(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


class ScraperAPIClient:
    """Small ScraperAPI client configured for low-credit BookMyShow checks."""

    def __init__(self, api_key: str, *, timeout_seconds: float = 90.0) -> None:
        if not api_key.strip():
            raise ScraperAPIError("SCRAPERAPI_API_KEY is empty.")
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls, *, timeout_seconds: float = 90.0) -> "ScraperAPIClient":
        api_key = os.environ.get("SCRAPERAPI_API_KEY", "").strip()
        if not api_key:
            raise ScraperAPIError(
                "SCRAPERAPI_API_KEY is not set. Add it as an environment variable or GitHub secret."
            )
        return cls(api_key, timeout_seconds=timeout_seconds)

    def fetch(self, target_url: str) -> str:
        parameters = {
            'api_key': self.api_key,
            'render': 'true' if _enabled('SCRAPERAPI_RENDER') else 'false',
            'url': target_url,
        }
        request_url = f"{SCRAPERAPI_ENDPOINT}?{urlencode(parameters)}"
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "BMS-Booking-Monitor/1.0",
        }

        request = Request(request_url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", response.getcode())
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            raise ScraperAPIError(f"ScraperAPI returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError) as exc:
            detail = str(exc.reason if isinstance(exc, URLError) else exc).strip()
            raise ScraperAPIError(
                f"ScraperAPI could not be reached: {detail or type(exc).__name__}."
            ) from None

        if status != 200:
            raise ScraperAPIError(f"ScraperAPI returned HTTP {status}.")
        text = body.decode(charset, errors="replace")
        if not text.strip():
            raise ScraperAPIError("ScraperAPI returned an empty BookMyShow response.")
        return text


def extract_initial_state(html: str) -> Any:
    """Extract BookMyShow's JSON state without executing page JavaScript."""
    search_from = 0
    decoder = json.JSONDecoder()
    while True:
        marker_index = html.find(INITIAL_STATE_MARKER, search_from)
        if marker_index < 0:
            break
        object_index = html.find("{", marker_index + len(INITIAL_STATE_MARKER))
        if object_index < 0:
            break
        try:
            state, _ = decoder.raw_decode(html[object_index:])
            return state
        except json.JSONDecodeError:
            search_from = object_index + 1
    raise ScraperAPIError("BookMyShow initial-state data was not present in the returned page.")


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def discover_scheduled_theatre(
    state: Any,
    theatre_url: str,
    page_html: str = "",
) -> str:
    """Find the theatre name that corresponds to the URL's venue code."""
    match = re.search(
        r"/buytickets/([A-Za-z0-9]+)/(?:\d{8})/?$",
        urlparse(theatre_url).path,
    )
    if not match:
        raise ScraperAPIError("The configured theatre URL has no venue code.")
    target_code = match.group(1).casefold()

    for value in _walk(state):
        name = value.get("VenueName") or value.get("venueName")
        code = value.get("VenueCode") or value.get("venueCode")
        if str(code or "").strip().casefold() == target_code and str(name or "").strip():
            return str(name).strip()

    title_match = re.search(
        r"<title[^>]*>(.*?)</title>",
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if title_match:
        title = html_module.unescape(title_match.group(1))
        theatre_name = re.split(r"\s+\|\s+", title, maxsplit=1)[0].strip()
        if theatre_name:
            return theatre_name

    raise ScraperAPIError(
        "BookMyShow did not provide the theatre name for the configured URL."
    )


def _status_text(value: dict[str, Any]) -> str:
    for key in (
        "AvailStatusText",
        "AvailabilityText",
        "availabilityText",
        "StatusText",
        "statusText",
    ):
        text = str(value.get(key) or "").strip()
        if text:
            return text
    raw_status = value.get("AvailStatus", value.get("availStatus", ""))
    code = str(raw_status).strip()
    if code == "2":
        return "FAST FILLING"
    if code == "1":
        return "AVAILABLE"
    if code in {"0", "3"}:
        return "SOLD OUT"
    if code and not code.isdigit():
        return code
    return "UNKNOWN"


def _price_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("₹", "").replace("â‚¹", "")
    return re.sub(r"\s+", " ", text).strip()


def extract_movie_schedule(
    state: Any,
    movie_name: str,
    theatre_name: str,
    booking_url: str,
) -> ScrapedBooking:
    target = _normalize(movie_name)
    child_events: list[dict[str, Any]] = []
    for value in _walk(state):
        title = value.get("EventTitle") or value.get("eventTitle")
        children = value.get("ChildEvents") or value.get("childEvents")
        if _normalize(title) == target and isinstance(children, list):
            child_events.extend(child for child in children if isinstance(child, dict))

    events: dict[str, dict[str, Any]] = {}
    for event in child_events:
        event_id = str(event.get("EventCode") or event.get("eventCode") or "").strip()
        if event_id:
            events[event_id] = event

    showtimes: list[ScrapedShowtime] = []
    for event in events.values():
        event_format = event.get("EventDimension") or event.get("eventDimension") or ""
        raw_showtimes = event.get("ShowTimes") or event.get("showTimes") or []
        for showtime in raw_showtimes:
            if not isinstance(showtime, dict):
                continue
            time_text = str(showtime.get("ShowTime") or showtime.get("showTime") or "").strip()
            if not time_text:
                continue
            raw_categories = showtime.get("Categories") or showtime.get("categories") or []
            ticket_classes = tuple(
                ScrapedTicketClass(
                    class_name=str(
                        category.get("PriceDesc")
                        or category.get("priceDesc")
                        or category.get("PriceCode")
                        or category.get("priceCode")
                        or "Unknown"
                    ).strip(),
                    price=_price_text(category.get("CurPrice", category.get("curPrice"))),
                    availability=_status_text(category),
                )
                for category in raw_categories
                if isinstance(category, dict)
            )
            showtimes.append(
                ScrapedShowtime(
                    time=time_text,
                    format=str(
                        showtime.get("Attributes")
                        or showtime.get("attributes")
                        or event_format
                    ).strip(),
                    availability=_status_text(showtime),
                    ticket_classes=ticket_classes,
                )
            )

    return ScrapedBooking(
        theatre_name=theatre_name,
        showtimes=tuple(dict.fromkeys(showtimes)),
        booking_url=booking_url,
        event_ids=tuple(events),
    )


def poll_bookmyshow(
    movie_name: str,
    theatre_url: str,
    *,
    timeout_seconds: float = 90.0,
) -> ScrapedBooking:
    """Extract a movie from one configured, dated theatre page via ScraperAPI."""
    client = ScraperAPIClient.from_environment(timeout_seconds=timeout_seconds)
    schedule_html = client.fetch(theatre_url)
    schedule_state = extract_initial_state(schedule_html)
    theatre_name = discover_scheduled_theatre(schedule_state, theatre_url, schedule_html)
    return extract_movie_schedule(
        schedule_state,
        movie_name,
        theatre_name,
        theatre_url,
    )
