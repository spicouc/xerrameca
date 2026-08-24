"""Telegram Update ingestion boundary for the Xerrameca wizard.

This module is the INPUT layer of the Telegram UX: it parses a raw Telegram
Update JSON and routes it to the existing :class:`TelegramUXAdapter`, which in
turn drives the wizard / command layer and renders screens through the existing
:class:`TelegramBotAPITransport`.

This module adds NO external Telegram dependency and NO server. There is no
polling loop, no long-polling or webhook registration calls, no web framework
route and no background task; it only ever receives an update through
:meth:`TelegramUpdateDispatcher.dispatch`.

All Update *parsing* lives here in ``integrations/``. Nothing in
``command/``, ``node/`` or ``ui/`` parses Telegram JSON.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .telegram import TelegramUXAdapter

# Bounded in-memory dedup window per live dispatcher instance. There is NO
# global mutable, NO SQLite and NO persistence: after a dispatcher instance is
# destroyed (e.g. process restart) the cache is gone. We therefore only
# guarantee at-most-once execution per update_id within a single live
# dispatcher process — never a global exactly-once claim (which would require
# durable storage that is explicitly out of scope for this phase).
_MAX_SEEN_UPDATES = 1024


def _as_int(value: Any) -> int | None:
    """Coerce a Telegram Update id to ``int`` without throwing on bad payloads."""
    if isinstance(value, int):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _as_optional_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    return None


def _chat_id_from(obj: Any) -> str | None:
    """Extract a string chat id from a dict with a ``chat`` -> ``id`` shape."""
    if not isinstance(obj, Mapping):
        return None
    chat = obj.get("chat")
    if not isinstance(chat, Mapping):
        return None
    cid = chat.get("id")
    if cid is None:
        return None
    return str(cid)


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of routing one Telegram Update.

    Fields hold only controlled, user-safe values. ``reason`` never contains a
    raw Update payload, callback token, objective, API key, bot token, private
    key or local filesystem path.
    """

    kind: str  # "handled" | "ignored" | "duplicate" | "rejected"
    update_id: int | None = None
    reason: str | None = None

    @property
    def handled(self) -> bool:
        return self.kind == "handled"


class TelegramUpdateDispatcher:
    """Routes raw Telegram Update dicts to a :class:`TelegramUXAdapter`.

    Parameters
    ----------
    adapter:
        The adapter that performs wizard / command work and renders screens.
    allowed_chat_ids:
        Optional allowlist of chat ids (as ``str``) permitted to act. ``None``
        disables the filter and allows every chat. Unknown chats are rejected
        without any wizard mutation; rejected callbacks are still safely ACKed
        so Telegram stops showing its inline spinner.
    max_seen_updates:
        Upper bound on how many update_ids are remembered for dedup.
    """

    def __init__(
        self,
        adapter: TelegramUXAdapter,
        *,
        allowed_chat_ids: set[str] | None = None,
        max_seen_updates: int = _MAX_SEEN_UPDATES,
    ) -> None:
        self._adapter = adapter
        self._allowed: set[str] | None = allowed_chat_ids
        self._max = max_seen_updates
        self._seen: deque[int] = deque()
        self._seen_set: set[int] = set()
        self._lock = asyncio.Lock()

    # -- helpers ------------------------------------------------------------

    def _claim(self, update_id: int) -> bool:
        """Atomically claim an update_id for execution. Returns False on dup.

        Must be called while holding ``self._lock`` so that concurrent
        dispatches of the same update_id cannot both execute. The bound is
        enforced here so the cache can never grow beyond ``max_seen_updates``.
        """
        if update_id in self._seen_set:
            return False
        self._seen_set.add(update_id)
        self._seen.append(update_id)
        while len(self._seen) > self._max:
            oldest = self._seen.popleft()
            self._seen_set.discard(oldest)
        return True

    def _allowed_chat(self, chat_id: str) -> bool:
        if self._allowed is None:
            return True
        return chat_id in self._allowed

    async def _safe_ack(self, callback_query_id: str | None) -> None:
        """ACK the callback transport only — never the wizard, never state.

        ACK must be silent and non-authoritative: a failure (or a transport
        without ACK support) never escapes, never mutates wizard state, never
        creates an event and never triggers a retry.
        """
        if not callback_query_id:
            return
        try:
            await self._adapter.safe_ack(callback_query_id)
        except Exception:
            # Swallow everything at the Telegram boundary.
            return

    async def _run_message(self, chat_id: str, text: str) -> None:
        try:
            await self._adapter.handle_text(chat_id, text)
        except Exception:
            # Controlled boundary: unexpected exceptions never escape as raw
            # internals to the caller / user. No f"{exc}" is surfaced.
            return

    async def _run_callback(
        self, chat_id: str, data: str, callback_query_id: str | None
    ) -> None:
        try:
            await self._adapter.handle_callback(
                chat_id, data, callback_query_id=callback_query_id
            )
        except Exception:
            return

    # -- public API ----------------------------------------------------------

    async def dispatch(self, update: Mapping) -> DispatchResult:
        """Route one raw Telegram Update JSON and return a controlled result.

        At-most-once per ``update_id`` for the life of this dispatcher. Ids are
        always marked seen *before* execution so a concurrent duplicate cannot
        re-execute.
        """
        if not isinstance(update, Mapping):
            return DispatchResult(kind="ignored", update_id=None, reason="unsupported update type")

        update_id = _as_int(update.get("update_id"))
        if update_id is None:
            # No update_id means Telegram-level dedup is impossible; gracefully
            # drop without touching anything (controlled, no traceback).
            return DispatchResult(kind="ignored", update_id=None, reason="no update_id")

        # Claim atomically (concurrency + idempotency guard).
        async with self._lock:
            if not self._claim(update_id):
                # Duplicate callback: do NOT re-run the wizard action; do a
                # safe ACK of the callback transport only if possible so the
                # Telegram spinner is dismissed.
                cq = update.get("callback_query")
                if isinstance(cq, Mapping):
                    await self._safe_ack(_as_optional_str(cq.get("id")))
                return DispatchResult(
                    kind="duplicate", update_id=update_id, reason="duplicate update_id"
                )

        # --- message updates ------------------------------------------------
        message = update.get("message")
        if isinstance(message, Mapping):
            # No text (e.g. a poll, a sticker, an edited-style payload, a
            # photo) -> ignored. Polls carry no text, so both are covered here.
            text = message.get("text")
            if not isinstance(text, str):
                return DispatchResult(
                    kind="ignored", update_id=update_id, reason="message without text"
                )
            chat_id = _chat_id_from(message)
            if chat_id is None:
                return DispatchResult(
                    kind="ignored", update_id=update_id, reason="message without chat"
                )
            if not self._allowed_chat(chat_id):
                # Unauthorized message: NO wizard mutation, NO controlled reply
                # leaking anything about the allowlist.
                return DispatchResult(
                    kind="rejected", update_id=update_id, reason="chat not allowed"
                )
            await self._run_message(chat_id, text)
            return DispatchResult(kind="handled", update_id=update_id)

        # --- callback_query updates ------------------------------------------
        cq = update.get("callback_query")
        if isinstance(cq, Mapping):
            data = cq.get("data")
            if not isinstance(data, str):
                return DispatchResult(
                    kind="ignored", update_id=update_id, reason="callback without data"
                )
            chat_id = _chat_id_from(cq.get("message"))
            if chat_id is None:
                return DispatchResult(
                    kind="ignored", update_id=update_id, reason="callback without chat"
                )
            callback_query_id = _as_optional_str(cq.get("id"))
            if not self._allowed_chat(chat_id):
                # Unauthorized callback: NO wizard mutation, but always safe-ACK
                # so Telegram does not leave the inline spinner running.
                await self._safe_ack(callback_query_id)
                return DispatchResult(
                    kind="rejected", update_id=update_id, reason="chat not allowed"
                )
            await self._run_callback(chat_id, data, callback_query_id)
            return DispatchResult(kind="handled", update_id=update_id)

        # edited_message / inline_query / chosen_inline_result / polls / any
        # other unsupported type all land here -> controlled ignore.
        return DispatchResult(
            kind="ignored", update_id=update_id, reason="unsupported update type"
        )


__all__ = ["DispatchResult", "TelegramUpdateDispatcher"]
