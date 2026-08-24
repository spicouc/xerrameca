"""Real Telegram Bot API transport for the Xerrameca wizard UI.

This module is a small, optional transport that turns a NeutralScreen into a
real Telegram `reply_markup.inline_keyboard` via the Bot API `sendMessage`
endpoint. It uses the `httpx` dependency Xerrameca already ships; it does NOT
add python-telegram-bot/aiogram/telebot.

The federated core has no Telegram dependency: if this transport is never
instantiated, Xerrameca behaves exactly as before.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx


class TelegramTransportError(RuntimeError):
    """Generic, sanitised error for Telegram Bot API failures.

    Raised with `raise ... from None` so the original exception (whose string
    can embed the request URL, which contains the bot token) is never chained
    or exposed to the user.
    """


class RenderableButton(Protocol):
    label: str
    callback_token: str


class RenderableScreen(Protocol):
    text: str
    buttons: list[RenderableButton]


_TG_API = "https://api.telegram.org"
_MAX_CALLBACK_BYTES = 64


def _sanitized_message(ok: bool, description: str | None = None) -> str:
    # Keep any native description out of user-facing errors entirely. We only
    # ever surface a stable generic message.
    return "Telegram API request failed"


class TelegramBotAPITransport:
    """Async transport that renders screens as real Telegram inline keyboards.

    Parameters
    ----------
    token:
        Telegram bot token. It lives only in memory (and in the request URL),
        is never persisted, never logged, and never included in errors that
        reach the user.
    client:
        Optional ``httpx.AsyncClient`` for tests / injection. When omitted a
        fresh client is created per request so no shared mutable state is kept.
    api_base:
        Overridable base URL (almost always the real Bot API). Tests inject a
        fake server here to avoid any real network call.
    """

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_base: str = _TG_API,
    ) -> None:
        self._token = token
        self._client = client
        self._api_base = api_base.rstrip("/")

    # -- protected helpers ----------------------------------------------------

    def _url(self, method: str) -> str:
        # The token is embedded in the URL by the Bot API. Any exception from
        # httpx could reference this full URL, so failures are always mapped to
        # a sanitised TelegramTransportError (never the raw exception).
        return f"{self._api_base}/bot{self._token}/{method}"

    async def _post(
        self, method: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            if self._client is not None:
                resp = await self._client.post(self._url(method), json=payload)
                resp.raise_for_status()
                data = resp.json()
            else:
                async with httpx.AsyncClient(timeout=15.0) as c:
                    resp = await c.post(self._url(method), json=payload)
                    resp.raise_for_status()
                    data = resp.json()
        except httpx.HTTPStatusError:
            raise TelegramTransportError(_sanitized_message(False)) from None
        except httpx.HTTPError:
            raise TelegramTransportError(_sanitized_message(False)) from None
        except Exception:
            # Any unexpected exception must not leak the URL/token.
            raise TelegramTransportError(_sanitized_message(False)) from None

        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramTransportError(_sanitized_message(False))
        return data

    @staticmethod
    def _validate_callback(token: str) -> str:
        if not isinstance(token, str) or len(token.encode("utf-8")) > _MAX_CALLBACK_BYTES:
            raise ValueError(
                f"callback_token exceeds Telegram's {_MAX_CALLBACK_BYTES}-byte limit"
            )
        return token

    # -- public API -----------------------------------------------------------

    async def send(self, chat_id: str, text: str) -> None:
        """Send a text-only message (no buttons)."""
        await self._post("sendMessage", {"chat_id": chat_id, "text": text})

    async def send_buttons(
        self, chat_id: str, text: str, buttons: list[RenderableButton]
    ) -> None:
        """Send one message with a real inline keyboard (1 button per row)."""
        validated: list[dict[str, str]] = []
        for b in buttons:
            token = self._validate_callback(b.callback_token)
            validated.append({"text": b.label, "callback_data": token})
        rows = [[cb] for cb in validated]  # 1 button per row, preserve order
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": {"inline_keyboard": rows},
        }
        await self._post("sendMessage", payload)

    async def answer_callback_query(self, callback_query_id: str) -> None:
        """Ack a callback query so Telegram stops showing the inline spinner."""
        if not callback_query_id:
            return
        await self._post(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": ""},
        )
