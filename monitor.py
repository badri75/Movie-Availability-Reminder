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
from urllib.parse import quote

from notifier import (
    TelegramConfigurationError,
    TelegramDeliveryError,
    TelegramNotifier,
)


BMS_BASE_URL = "https://in.bookmyshow.com"

CONFIG_KEYS = frozenset(
    {
        "movie_name",
        "city",
        "theatre_name",
        "date",
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
    city: str
    theatre_name: str
    date: date
    formats: str
    bookmyshow_retry_delay_seconds: float

    @property
    def target_key(self) -> str:
        return (
            f"{normalize_text(self.movie_name)}|{normalize_text(self.city)}|"
            f"{normalize_text(self.theatre_name)}|{self.date.isoformat()}|"
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


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    if not value:
        raise ConfigurationError("Value cannot be converted into a BookMyShow URL slug.")
    return value


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
            "config.json must contain exactly movie_name, city, theatre_name, date, "
            "formats, and bookmyshow_retry_delay_seconds"
            + (f" ({'; '.join(details)})" if details else "")
            + "."
        )

    movie_name = payload.get("movie_name")
    city = payload.get("city")
    theatre_name = payload.get("theatre_name")
    date_value = payload.get("date")
    formats = payload.get("formats")
    retry_delay = payload.get("bookmyshow_retry_delay_seconds")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (movie_name, city, theatre_name, date_value)
    ):
        raise ConfigurationError(
            "movie_name, city, theatre_name, and date must be non-empty strings."
        )
    if not isinstance(formats, str):
        raise ConfigurationError("formats must be a string; use an empty string for all formats.")
    if isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float)):
        raise ConfigurationError("bookmyshow_retry_delay_seconds must be a number.")
    if retry_delay < 0:
        raise ConfigurationError("bookmyshow_retry_delay_seconds cannot be negative.")

    try:
        target_date = date.fromisoformat(date_value.strip())
    except ValueError:
        raise ConfigurationError("date must use the YYYY-MM-DD format.") from None

    return MonitorConfig(
        movie_name=movie_name.strip(),
        city=city.strip(),
        theatre_name=theatre_name.strip(),
        date=target_date,
        formats=formats.strip(),
        bookmyshow_retry_delay_seconds=float(retry_delay),
    )


def _booking_urls_for_discovered_variants(
    config: MonitorConfig,
    event_ids: Sequence[str],
) -> list[tuple[str, str]]:
    city_slug = slugify(config.city)
    movie_slug = slugify(config.movie_name)
    compact_date = config.date.strftime("%Y%m%d")
    return [
        (
            event_id,
            f"{BMS_BASE_URL}/movies/{quote(city_slug)}/{quote(movie_slug)}/buytickets/"
            f"{quote(event_id)}/{compact_date}",
        )
        for event_id in event_ids
    ]


DISCOVER_MOVIE_LINK_SCRIPT = """
({ movieName, movieSlug }) => {
  const normalize = (value) => String(value || "")
    .normalize("NFKC")
    .replace(/\\s+/g, " ")
    .trim()
    .toLocaleLowerCase("en-IN");
  const targetName = normalize(movieName);
  const links = Array.from(document.querySelectorAll('a[href*="/movies/"]'));
  const match = links.find((link) => {
    let path = "";
    try {
      path = new URL(link.href, window.location.href).pathname;
    } catch (_) {
      return false;
    }
    if (!path.startsWith("/movies/") || !/\\/ET\\d+(?:\\/|$)/.test(path)) return false;

    const imageTitle = link.querySelector('img[alt]')?.getAttribute("alt") || "";
    const linkTitle = normalize(link.textContent || "");
    const pathParts = path.split("/").filter(Boolean);
    const linkedSlug = pathParts.length >= 4 ? pathParts[pathParts.length - 2] : "";
    return normalize(imageTitle) === targetName ||
      linkedSlug === movieSlug ||
      linkTitle === targetName ||
      linkTitle.startsWith(`${targetName} `);
  });
  return match ? match.href : null;
}
"""


DISCOVER_VARIANTS_SCRIPT = """
({ movieName }) => {
  const normalize = (value) => String(value || "")
    .normalize("NFKC")
    .replace(/\\s+/g, " ")
    .trim()
    .toLocaleLowerCase("en-IN");
  const marker = "window.__INITIAL_STATE__ = ";
  const script = Array.from(document.scripts)
    .map((item) => item.textContent || "")
    .find((text) => text.includes(marker) && text.includes(movieName));
  if (!script) return [];

  const markerIndex = script.indexOf(marker);
  const openIndex = script.indexOf("{", markerIndex + marker.length);
  if (openIndex < 0) return [];

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
    } else if (character === '"') {
      inString = true;
    } else if (character === "{") {
      depth += 1;
    } else if (character === "}" && --depth === 0) {
      closeIndex = index;
      break;
    }
  }
  if (closeIndex < 0) return [];

  let state;
  try {
    state = JSON.parse(script.slice(openIndex, closeIndex + 1));
  } catch (_) {
    return [];
  }

  const variants = [];
  const seen = new Set();
  const visit = (value) => {
    if (!value || typeof value !== "object" || seen.has(value)) return;
    seen.add(value);
    if (
      normalize(value.title) === normalize(movieName) &&
      Array.isArray(value.options)
    ) {
      for (const option of value.options) {
        for (const format of option.formats || []) {
          const eventId = String(format.eventCode || format.refEventCode || "").trim();
          if (/^ET\\d+$/.test(eventId)) {
            variants.push({
              eventId,
              language: String(format.language || option.language || "").trim(),
              format: String(format.dimension || "").trim(),
            });
          }
        }
      }
    }
    if (value.cta?.type === "formatSelector") {
      const data = value.cta.additionalData || {};
      const eventId = String(data.eventCode || data.refEventCode || "").trim();
      if (/^ET\\d+$/.test(eventId)) {
        variants.push({
          eventId,
          language: String(data.language || "").trim(),
          format: String(value.title || value.cta.analytics?.format || "").trim(),
        });
      }
    }
    Object.values(value).forEach(visit);
  };
  visit(state);

  const unique = new Map(variants.map((variant) => [variant.eventId, variant]));
  return Array.from(unique.values());
}
"""


EXTRACT_THEATRE_SCRIPT = """
({ theatreName, compactDate }) => {
  const normalize = (value) => (value || "")
    .normalize("NFKC")
    .replace(/\\s+/g, " ")
    .trim()
    .toLocaleLowerCase("en-IN");
  const target = normalize(theatreName);
  const timePattern = /\\b(?:0?[1-9]|1[0-2]):[0-5][0-9]\\s*(?:AM|PM)\\b/i;

  const statusFromCode = (value) => {
    if (String(value) === "2") return "FAST FILLING";
    if (String(value) === "1") return "AVAILABLE";
    if (["0", "3"].includes(String(value))) return "SOLD OUT";
    return "UNKNOWN";
  };

  const normalizePrice = (value) => String(value || "")
    .replace(/₹/g, "")
    .replace(/\\s+/g, " ")
    .trim();

  const parseInitialState = () => {
    const marker = "window.__INITIAL_STATE__ = ";
    const script = Array.from(document.scripts)
      .map((item) => item.textContent || "")
      .find((text) => text.includes(marker) && text.includes(theatreName));
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
      } else if (character === '"') {
        inString = true;
      } else if (character === "{") {
        depth += 1;
      } else if (character === "}" && --depth === 0) {
        closeIndex = index;
        break;
      }
    }
    if (closeIndex < 0) return null;
    try {
      return JSON.parse(script.slice(openIndex, closeIndex + 1));
    } catch (_) {
      return null;
    }
  };

  const findVenueCard = (root) => {
    if (!root) return null;
    const seen = new Set();
    let result = null;
    const visit = (value) => {
      if (result || !value || typeof value !== "object" || seen.has(value)) return;
      seen.add(value);
      if (
        normalize(value.additionalData?.venueName) === target &&
        Array.isArray(value.showtimes)
      ) {
        result = value;
        return;
      }
      Object.values(value).forEach(visit);
    };
    visit(root);
    return result;
  };

  const ticketRows = (showtime) => {
    const rows = [];
    const seen = new Set();
    const visit = (value) => {
      if (!value || typeof value !== "object" || seen.has(value)) return;
      seen.add(value);
      if (value.title?.text && value.description?.text && value.subtitle?.text) {
        rows.push({
          className: String(value.title.text).trim(),
          price: normalizePrice(value.description.text),
          availability: String(value.subtitle.text).replace(/\\s+/g, " ").trim(),
        });
      }
      Object.values(value).forEach(visit);
    };
    visit(showtime.customGestureCTA);

    if (rows.length === 0) {
      for (const category of showtime.additionalData?.categories || []) {
        rows.push({
          className: String(category.priceDesc || category.priceCode || "Unknown").trim(),
          price: category.curPrice ? String(category.curPrice).trim() : "",
          availability: statusFromCode(category.availStatus),
        });
      }
    }
    const unique = new Map(rows.map((row) => [JSON.stringify(row), row]));
    return Array.from(unique.values());
  };

  const state = parseInitialState();
  const venueCard = findVenueCard(state);
  if (venueCard) {
    const showtimes = venueCard.showtimes.map((showtime) => {
      const classes = ticketRows(showtime);
      const classStatuses = Array.from(
        new Set(classes.map((item) => item.availability).filter(Boolean))
      );
      const availability = classStatuses.join(" / ") ||
        statusFromCode(showtime.additionalData?.availStatus);
      return {
        time: String(showtime.title || showtime.additionalData?.showTime || "").trim(),
        format: String(showtime.screenAttr || showtime.additionalData?.attributes || "").trim(),
        availability,
        ticketClasses: classes,
      };
    }).filter((showtime) => timePattern.test(showtime.time));

    if (showtimes.length > 0) {
      const venueUrl = venueCard.header?.data?.components
        ?.map((component) => component.data?.cta?.additionalData?.redirectionUrl)
        .find(Boolean) || "";
      return {
        theatreName: venueCard.additionalData?.venueName || theatreName,
        theatreUrl: venueUrl,
        showtimes,
      };
    }
  }

  const theatreLinks = Array.from(
    document.querySelectorAll('a[alt][href*="/buytickets/"]')
  ).filter((link) =>
    normalize(link.getAttribute("alt")) === target &&
    (link.href || "").includes(compactDate)
  );

  for (const theatreLink of theatreLinks) {
    let container = theatreLink.parentElement;
    while (container && container !== document.body) {
      const buttons = Array.from(
        container.querySelectorAll('button, [role="button"][aria-label]')
      );
      const showtimeButtons = buttons.filter((button) => {
        const label = button.getAttribute("aria-label") || button.textContent || "";
        return timePattern.test(label);
      });

      if (showtimeButtons.length > 0) {
        const showtimes = showtimeButtons.map((button) => {
          const label = (button.getAttribute("aria-label") || button.textContent || "")
            .replace(/\\s+/g, " ")
            .trim();
          const parts = label.split(",").map((part) => part.trim());
          const disabled = button.disabled || button.getAttribute("aria-disabled") === "true";
          const borderColor = getComputedStyle(button).borderColor;
          return {
            time: parts[0] || label,
            format: parts.slice(1).join(", "),
            availability: disabled
              ? "SOLD OUT"
              : (borderColor.includes("241, 177, 3") ? "FAST FILLING" : "AVAILABLE"),
            ticketClasses: [],
          };
        });
        return {
          theatreName: theatreLink.getAttribute("alt") || theatreName,
          theatreUrl: theatreLink.href,
          showtimes,
        };
      }
      container = container.parentElement;
    }
  }
  return null;
}
"""


DISCOVER_THEATRE_SCRIPT = """
({ theatreName }) => {
  const normalize = (value) => String(value || "")
    .normalize("NFKC")
    .replace(/\\s+/g, " ")
    .trim()
    .toLocaleLowerCase("en-IN");
  const marker = "window.__INITIAL_STATE__ = ";
  const script = Array.from(document.scripts)
    .map((item) => item.textContent || "")
    .find((text) => text.includes(marker) && text.includes(theatreName));
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

  const target = normalize(theatreName);
  const seen = new Set();
  let result = null;
  const visit = (value) => {
    if (result || !value || typeof value !== "object" || seen.has(value)) return;
    seen.add(value);
    if (
      normalize(value.VenueName) === target &&
      /^[A-Za-z0-9]+$/.test(String(value.VenueCode || ""))
    ) {
      result = {
        theatreName: String(value.VenueName).trim(),
        venueCode: String(value.VenueCode).trim(),
      };
      return;
    }
    Object.values(value).forEach(visit);
  };
  visit(state);
  return result;
}
"""


EXTRACT_MOVIE_FROM_THEATRE_SCRIPT = """
({ movieName, theatreName }) => {
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
  const seen = new Set();
  const visit = (value) => {
    if (!value || typeof value !== "object" || seen.has(value)) return;
    seen.add(value);
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


def _check_bookings_by_movie_variants(
    config: MonitorConfig,
    *,
    headless: bool = True,
    timeout_ms: int = 20_000,
    event_ids: Sequence[str] | None = None,
) -> BookingResult:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BookingCheckError(
            "Playwright is not installed. Run: pip install -r requirements.txt"
        ) from None

    compact_date = config.date.strftime("%Y%m%d")
    errors: list[str] = []
    successfully_loaded = 0
    discovered_showtimes: list[Showtime] = []
    matching_urls: list[str] = []
    matching_event_ids: list[str] = []
    matched_theatre_name = config.theatre_name
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

            resolved_event_ids = list(event_ids or ())
            if event_ids is None:
                discovery_page = context.new_page()
                discovery_page.set_default_timeout(timeout_ms)
                try:
                    city_slug = slugify(config.city)
                    movie_slug = slugify(config.movie_name)
                    movie_page_url: str | None = None
                    discovery_urls = (
                        f"{BMS_BASE_URL}/explore/movies-{quote(city_slug)}",
                        f"{BMS_BASE_URL}/explore/upcoming-movies-{quote(city_slug)}",
                    )
                    for discovery_url in discovery_urls:
                        response = discovery_page.goto(
                            discovery_url,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                        if response is not None and response.status in {403, 429}:
                            continue
                        discovery_page.wait_for_selector(
                            "main", state="attached", timeout=timeout_ms
                        )
                        try:
                            discovery_page.wait_for_selector(
                                'a[href*="/movies/"][href*="/ET"]',
                                state="attached",
                                timeout=min(timeout_ms, 10_000),
                            )
                        except PlaywrightTimeoutError:
                            pass
                        movie_page_url = discovery_page.evaluate(
                            DISCOVER_MOVIE_LINK_SCRIPT,
                            {
                                "movieName": config.movie_name,
                                "movieSlug": movie_slug,
                            },
                        )
                        if movie_page_url:
                            break
                        if debug_enabled:
                            diagnostics = discovery_page.evaluate(
                                """
                                () => ({
                                  url: window.location.href,
                                  title: document.title,
                                  movieLinks: Array.from(
                                    document.querySelectorAll('a[href*="/movies/"]')
                                  ).slice(0, 20).map((link) => ({
                                    href: link.href,
                                    imageAlt: link.querySelector('img[alt]')
                                      ?.getAttribute('alt') || '',
                                  })),
                                })
                                """
                            )
                            print(json.dumps({"discovery": diagnostics}), file=sys.stderr)

                    if not movie_page_url:
                        raise BookingCheckError(
                            f"Movie '{config.movie_name}' was not found in BookMyShow "
                            f"listings for {config.city}."
                        )

                    movie_link_match = re.search(
                        r"/movies/(?:[^/]+/)?([^/]+)/(ET\d+)(?:/|$)",
                        movie_page_url,
                    )
                    if not movie_link_match:
                        raise BookingCheckError(
                            "BookMyShow returned an unrecognized movie link."
                        )
                    discovered_slug, primary_event_id = movie_link_match.groups()
                    movie_page_url = (
                        f"{BMS_BASE_URL}/movies/{quote(city_slug)}/"
                        f"{quote(discovered_slug)}/{quote(primary_event_id)}"
                    )
                    variant_discovery_url = (
                        f"{BMS_BASE_URL}/movies/{quote(city_slug)}/"
                        f"{quote(discovered_slug)}/buytickets/{quote(primary_event_id)}/"
                        f"{compact_date}"
                    )

                    response = discovery_page.goto(
                        variant_discovery_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    if response is not None and response.status in {403, 429}:
                        raise BookingCheckError(
                            "Movie-format discovery was blocked by BookMyShow "
                            f"(HTTP {response.status})."
                        )
                    discovery_page.wait_for_selector(
                        "main", state="attached", timeout=timeout_ms
                    )
                    variants = discovery_page.evaluate(
                        DISCOVER_VARIANTS_SCRIPT,
                        {"movieName": config.movie_name},
                    )
                    resolved_event_ids = [
                        str(variant.get("eventId"))
                        for variant in variants
                        if isinstance(variant, dict)
                        and re.fullmatch(r"ET\d+", str(variant.get("eventId") or ""))
                    ]

                    resolved_event_ids.append(primary_event_id)
                    resolved_event_ids = list(dict.fromkeys(resolved_event_ids))

                    if not resolved_event_ids:
                        raise BookingCheckError(
                            f"BookMyShow returned no booking variants for '{config.movie_name}'."
                        )
                    if debug_enabled:
                        print(
                            json.dumps(
                                {
                                    "movie_page_url": movie_page_url,
                                    "variant_discovery_url": variant_discovery_url,
                                    "discovered_variants": variants,
                                    "event_ids": resolved_event_ids,
                                }
                            ),
                            file=sys.stderr,
                        )
                finally:
                    discovery_page.close()

            for event_id, url in _booking_urls_for_discovered_variants(
                config, resolved_event_ids
            ):
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if response is not None and response.status in {403, 429}:
                        errors.append(f"{event_id}: HTTP {response.status}")
                        continue
                    page.wait_for_selector("main", state="attached", timeout=timeout_ms)
                    successfully_loaded += 1

                    # A redirect to another date must never produce a positive result.
                    if compact_date not in page.url:
                        continue

                    # Theatre rows are populated asynchronously after the initial
                    # document load. Wait for a cinema booking link rather than
                    # relying on a short fixed delay that can produce false negatives.
                    try:
                        page.wait_for_selector(
                            'a[alt][href*="/buytickets/"]',
                            state="attached",
                            timeout=min(timeout_ms, 10_000),
                        )
                    except PlaywrightTimeoutError:
                        # No cinema row is a valid "not available yet" response.
                        pass

                    if debug_enabled:
                        diagnostics = page.evaluate(
                            """
                            ({ theatreName, compactDate }) => {
                              const normalize = (value) => (value || "")
                                .normalize("NFKC")
                                .replace(/\\s+/g, " ")
                                .trim()
                                .toLocaleLowerCase("en-IN");
                              const links = Array.from(
                                document.querySelectorAll('a[alt][href*="/buytickets/"]')
                              );
                              return {
                                cinemaLinks: links.length,
                                targetLinks: links.filter((link) =>
                                  normalize(link.getAttribute("alt")) === normalize(theatreName) &&
                                  (link.href || "").includes(compactDate)
                                ).length,
                                showtimeRoles: document.querySelectorAll(
                                  '[role="button"][aria-label]'
                                ).length,
                              };
                            }
                            """,
                            {
                                "theatreName": config.theatre_name,
                                "compactDate": compact_date,
                            },
                        )
                        print(
                            json.dumps(
                                {
                                    "event_id": event_id,
                                    "requested_url": url,
                                    "loaded_url": page.url,
                                    **diagnostics,
                                }
                            ),
                            file=sys.stderr,
                        )

                    extracted = page.evaluate(
                        EXTRACT_THEATRE_SCRIPT,
                        {
                            "theatreName": config.theatre_name,
                            "compactDate": compact_date,
                        },
                    )
                    if extracted and extracted.get("showtimes"):
                        showtimes = tuple(
                            Showtime(
                                time=str(value.get("time") or "").strip(),
                                format=str(value.get("format") or "").strip(),
                                availability=str(value.get("availability") or "Unknown").strip(),
                                ticket_classes=tuple(
                                    TicketClass(
                                        class_name=str(ticket_class.get("className") or "Unknown").strip(),
                                        price=str(ticket_class.get("price") or "").strip(),
                                        availability=str(
                                            ticket_class.get("availability") or "Unknown"
                                        ).strip(),
                                    )
                                    for ticket_class in value.get("ticketClasses", [])
                                    if isinstance(ticket_class, dict)
                                ),
                            )
                            for value in extracted["showtimes"]
                            if isinstance(value, dict) and str(value.get("time") or "").strip()
                        )
                        matched_theatre_name = str(
                            extracted.get("theatreName") or config.theatre_name
                        )
                        discovered_showtimes.extend(showtimes)
                        matching_urls.append(url)
                        matching_event_ids.append(event_id)
                except PlaywrightTimeoutError:
                    errors.append(f"{event_id}: page timed out")
                except Exception as exc:  # A site change should not cause a false alert.
                    errors.append(f"{event_id}: {type(exc).__name__}")
                    if debug_enabled:
                        print(
                            json.dumps(
                                {
                                    "event_id": event_id,
                                    "error_type": type(exc).__name__,
                                    "error": str(exc).strip().splitlines()[0][:300],
                                }
                            ),
                            file=sys.stderr,
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

    if discovered_showtimes:
        unique_showtimes = tuple(dict.fromkeys(discovered_showtimes))
        return BookingResult(
            available=True,
            theatre_name=matched_theatre_name,
            showtimes=unique_showtimes,
            booking_url=matching_urls[0],
            event_id=",".join(matching_event_ids),
        )

    if successfully_loaded == 0:
        detail = "; ".join(errors) if errors else "no listing loaded"
        raise BookingCheckError(f"BookMyShow could not be checked reliably ({detail}).")

    return BookingResult(available=False, theatre_name=config.theatre_name)


def check_bookings(
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

    city_slug = slugify(config.city)
    theatre_slug = slugify(config.theatre_name)
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
                cinema_catalog_url = f"{BMS_BASE_URL}/{quote(city_slug)}/cinemas"
                response = page.goto(
                    cinema_catalog_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                if response is not None and response.status in {403, 429}:
                    raise BookingCheckError(
                        f"Theatre discovery was blocked by BookMyShow (HTTP {response.status})."
                    )
                page.wait_for_selector("main", state="attached", timeout=timeout_ms)
                theatre = page.evaluate(
                    DISCOVER_THEATRE_SCRIPT,
                    {"theatreName": config.theatre_name},
                )
                if not theatre:
                    raise BookingCheckError(
                        f"Theatre '{config.theatre_name}' was not found in BookMyShow "
                        f"cinemas for {config.city}."
                    )

                venue_code = str(theatre.get("venueCode") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9]+", venue_code):
                    raise BookingCheckError(
                        "BookMyShow returned an invalid theatre identifier."
                    )
                theatre_name = str(
                    theatre.get("theatreName") or config.theatre_name
                ).strip()
                schedule_url = (
                    f"{BMS_BASE_URL}/cinemas/{quote(city_slug)}/{quote(theatre_slug)}/"
                    f"buytickets/{quote(venue_code)}/{compact_date}"
                )

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
                        theatre_name=theatre_name,
                    )

                extracted = page.evaluate(
                    EXTRACT_MOVIE_FROM_THEATRE_SCRIPT,
                    {
                        "movieName": config.movie_name,
                        "theatreName": theatre_name,
                    },
                )
                if debug_enabled:
                    print(
                        json.dumps(
                            {
                                "cinema_catalog_url": cinema_catalog_url,
                                "venue_code": venue_code,
                                "schedule_url": schedule_url,
                                "loaded_url": page.url,
                                "event_ids": extracted.get("eventIds", []) if extracted else [],
                            }
                        ),
                        file=sys.stderr,
                    )

                if not extracted or not extracted.get("showtimes"):
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
        f"Theatre: {config.theatre_name}\n"
        f"City: {config.city}\n"
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
