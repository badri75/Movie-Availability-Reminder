"""Telegram notification support for the BookMyShow monitor."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class TelegramConfigurationError(ValueError):
    """Raised when Telegram credentials are missing or malformed."""


class TelegramDeliveryError(RuntimeError):
    """Raised when Telegram rejects or cannot receive a notification."""


@dataclass(frozen=True)
class TelegramNotifier:
    bot_token: str
    chat_id: str
    timeout_seconds: int = 15

    @classmethod
    def from_environment(cls) -> "TelegramNotifier":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

        if not token or ":" not in token:
            raise TelegramConfigurationError(
                "TELEGRAM_BOT_TOKEN is missing or does not look like a bot token."
            )
        if not chat_id or not (chat_id.lstrip("-").isdigit() or chat_id.startswith("@")):
            raise TelegramConfigurationError(
                "TELEGRAM_CHAT_ID must be a numeric chat ID or an @channel username."
            )
        return cls(bot_token=token, chat_id=chat_id)

    def send(self, text: str) -> None:
        if not text.strip():
            raise ValueError("Telegram message cannot be empty.")

        endpoint = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_body = response.read().decode("utf-8", errors="replace")
                response_payload = _load_telegram_json(
                    response_body,
                    response.headers.get("Content-Type", "unknown"),
                )
        except urllib.error.HTTPError as exc:
            # Do not include exc or the request URL: both can expose the bot token.
            response_body = exc.read().decode("utf-8", errors="replace")
            detail = _telegram_response_preview(response_body)
            raise TelegramDeliveryError(
                f"Telegram returned HTTP status {exc.code}: {detail}"
            ) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TelegramDeliveryError(
                f"Telegram could not be reached: {type(exc).__name__}."
            ) from None

        if not response_payload.get("ok"):
            description = str(response_payload.get("description", "unknown Telegram error"))
            raise TelegramDeliveryError(f"Telegram rejected the message: {description}")


def _load_telegram_json(response_body: str, content_type: str) -> dict[str, Any]:
    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError:
        detail = _telegram_response_preview(response_body)
        raise TelegramDeliveryError(
            f"Telegram returned a non-JSON response ({content_type}): {detail}"
        ) from None
    if not isinstance(payload, dict):
        raise TelegramDeliveryError("Telegram returned an unexpected response shape.")
    return payload


def _telegram_response_preview(response_body: str) -> str:
    compact = " ".join(response_body.strip().split())
    return compact[:300] if compact else "empty response body"
