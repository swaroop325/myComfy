"""Telegram Bot API client and webhook helpers."""

import hmac
from dataclasses import dataclass

import requests

_SECRET_HEADER = "x-telegram-bot-api-secret-token"


def verify_webhook(headers: dict, secret: str) -> bool:
    """Constant-time check of the Telegram secret token header."""
    received = None
    for name, value in headers.items():
        if name.lower() == _SECRET_HEADER:
            received = value
            break
    if received is None:
        return False
    return hmac.compare_digest(str(received).encode(), secret.encode())


@dataclass
class Update:
    update_id: int
    chat_id: int
    text: str | None
    callback_data: str | None
    callback_id: str | None


class TelegramError(Exception):
    """Telegram API failure; message never contains the bot token."""


def parse_update(body: dict) -> Update | None:
    """Return None for updates we do not handle."""
    cq = body.get("callback_query")
    if cq:
        if not cq.get("message"):
            return None
        return Update(
            update_id=body["update_id"],
            chat_id=cq["message"]["chat"]["id"],
            text=None,
            callback_data=cq.get("data"),
            callback_id=cq["id"],
        )
    msg = body.get("message")
    if not msg:
        return None
    return Update(
        update_id=body["update_id"],
        chat_id=msg["chat"]["id"],
        text=msg.get("text"),
        callback_data=None,
        callback_id=None,
    )


def _markup(buttons: list[list[tuple[str, str]]] | None) -> dict | None:
    if not buttons:
        return None
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in buttons
        ]
    }


class TelegramClient:
    def __init__(self, token: str, timeout: float = 30):
        self._base = f"https://api.telegram.org/bot{token}"
        self._timeout = timeout  # the bot Lambda uses a short one (API Gateway waits 30 s max)

    def _post(self, method: str, payload: dict, buttons=None) -> None:
        markup = _markup(buttons)
        if markup:
            payload = {**payload, "reply_markup": markup}
        try:
            resp = requests.post(f"{self._base}/{method}", json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            raise TelegramError(f"{method} failed: {status}") from None
        except requests.RequestException as e:
            raise TelegramError(f"{method} failed: {type(e).__name__}") from None

    def send_message(self, chat_id: int, text: str, buttons=None) -> None:
        self._post("sendMessage", {"chat_id": chat_id, "text": text}, buttons)

    def send_video(self, chat_id: int, url: str, caption: str, buttons=None) -> None:
        self._post(
            "sendVideo", {"chat_id": chat_id, "video": url, "caption": caption}, buttons
        )

    def send_document(self, chat_id: int, url: str, caption: str) -> None:
        self._post(
            "sendDocument", {"chat_id": chat_id, "document": url, "caption": caption}
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._post(
            "answerCallbackQuery", {"callback_query_id": callback_id, "text": text}
        )
