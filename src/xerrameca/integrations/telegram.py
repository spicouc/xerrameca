from __future__ import annotations

from xerrameca.ui import CallbackStore, TelegramWizardBridge

from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, Protocol

from ..command.wizard import WizardError
from .telegram_bot_api import TelegramTransportError as _TelegramTransportError
from ..ui.neutral import CallbackError

import httpx


class TelegramMode(str, Enum):
    SILENT = "silent"
    SUMMARY = "summary"
    LIVE = "live"


class TelegramTransport(Protocol):
    async def send(self, chat_id: str, text: str) -> None: ...
    # Optional capabilities (duck-typed at runtime via hasattr):
    #   async def send_buttons(self, chat_id, text, buttons) -> None
    #   async def answer_callback_query(self, callback_query_id) -> None


class TelegramUXAdapter:
    """Optional Telegram-facing UX over one local Xerrameca node API.

    The adapter deliberately owns no conversation state. If it disappears,
    node-to-node dialogue, replication, failover and supervision continue.
    """

    def __init__(
        self,
        *,
        node_base_url: str,
        api_key: str,
        transport: TelegramTransport,
        client: httpx.AsyncClient | None = None,
        client_factory: Callable[[], Awaitable[httpx.AsyncClient]] | None = None,
        wizard: TelegramWizardBridge | None = None,
    ) -> None:
        self.node_base_url = node_base_url.rstrip("/")
        self._api_key = api_key
        self.transport = transport
        self._client = client
        self._client_factory = client_factory
        self._modes: dict[str, TelegramMode] = {}
        self._wizard = wizard

    def _transport_has_buttons(self) -> bool:
        """True if the transport can render a real inline keyboard."""
        send_buttons = getattr(self.transport, "send_buttons", None)
        return callable(send_buttons)

    def _transport_can_ack(self) -> bool:
        ack = getattr(self.transport, "answer_callback_query", None)
        return callable(ack)

    async def _send_screen(self, chat_id: str, screen: Any) -> None:
        """Render a NeutralScreen (or any text+buttons object) to the transport.

        If the transport supports real inline keyboards we send ONE message with
        reply_markup.inline_keyboard; otherwise we fall back to the legacy
        text + '[label] ::token' pseudo-button lines so older transports and
        tests keep working unchanged.
        """
        if self._transport_has_buttons():
            await self.transport.send_buttons(chat_id, screen.text, list(screen.buttons))
            return
        # Legacy fallback
        await self.transport.send(chat_id, screen.text)
        for b in screen.buttons:
            await self.transport.send(chat_id, f"[{b.label}] ::{b.callback_token}")

    def mode_for(self, conversation_id: str) -> TelegramMode:
        return self._modes.get(conversation_id, TelegramMode.SUMMARY)

    def set_mode(self, conversation_id: str, mode: TelegramMode | str) -> TelegramMode:
        parsed = mode if isinstance(mode, TelegramMode) else TelegramMode(str(mode).lower())
        self._modes[conversation_id] = parsed
        return parsed

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            response = await self._client.request(
                method,
                f"{self.node_base_url}{path}",
                headers=self._headers(),
                **kwargs,
            )
            response.raise_for_status()
            return response
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.request(
                method,
                f"{self.node_base_url}{path}",
                headers=self._headers(),
                **kwargs,
            )
            response.raise_for_status()
            return response

    async def start(
        self,
        *,
        peer_node_id: str,
        objective: str,
        max_rounds: int = 5,
        delay_seconds: int = 0,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/v1/node/federation/conversations",
            json={
                "peer_node_id": peer_node_id,
                "objective": objective,
                "max_rounds": max_rounds,
                "delay_seconds": delay_seconds,
            },
        )
        return dict(response.json())

    async def status(self, conversation_id: str) -> dict[str, Any]:
        response = await self._request(
            "GET", f"/v1/node/federation/conversations/{conversation_id}"
        )
        return dict(response.json())

    async def sync(self, conversation_id: str) -> dict[str, Any]:
        response = await self._request(
            "POST", f"/v1/node/federation/conversations/{conversation_id}/sync"
        )
        return dict(response.json())

    @staticmethod
    def _summary(conversation: dict[str, Any]) -> str:
        cid = conversation.get("id", "?")
        status = str(conversation.get("status", "unknown")).upper()
        round_no = conversation.get("current_round", "?")
        max_rounds = conversation.get("max_rounds", "?")
        objective = str(conversation.get("objective") or "")
        messages = conversation.get("messages") or []
        last = ""
        if messages:
            message = messages[-1]
            author = message.get("author_node_id") or message.get("author_id") or "agent"
            content = str(message.get("content") or "")
            last = f"\nÚltim: {author}: {content}"
        return (
            f"Xerrameca #{cid} — {status}\n"
            f"Ronda {round_no}/{max_rounds}\n"
            f"Objectiu: {objective}{last}"
        )

    @staticmethod
    def _live(conversation: dict[str, Any]) -> str:
        base = TelegramUXAdapter._summary(conversation)
        messages = conversation.get("messages") or []
        if not messages:
            return base
        rendered = []
        for message in messages:
            author = message.get("author_node_id") or message.get("author_id") or "agent"
            rendered.append(f"[{author}]\n{message.get('content', '')}")
        return f"{base}\n\n" + "\n\n".join(rendered)

    def render(self, conversation: dict[str, Any], *, mode: TelegramMode | None = None) -> str:
        selected = mode or self.mode_for(str(conversation.get("id", "")))
        if selected is TelegramMode.LIVE:
            return self._live(conversation)
        if selected is TelegramMode.SILENT:
            status = str(conversation.get("status", "unknown")).lower()
            if status not in {"completed", "blocked", "error", "cancelled"}:
                return f"Xerrameca #{conversation.get('id', '?')} — {status.upper()}"
        return self._summary(conversation)

    async def notify(self, chat_id: str, conversation: dict[str, Any]) -> None:
        await self.transport.send(chat_id, self.render(conversation))


    # ---- Wizard (button-driven) surface ----
    async def start_wizard(self, chat_id: str) -> None:
        """Render the root wizard screen with opaque callback buttons."""
        if self._wizard is None:
            await self.transport.send(chat_id, "Wizard no disponible en aquest node.")
            return
        screen = self._wizard.start(str(chat_id))
        await self._send_screen(chat_id, screen)

    async def _ack_safely(self, callback_query_id: str | None) -> None:
        """ACK a callback query without touching wizard or federated state.

        ACK is intentionally non-authoritative: a failure, an absent id, or a
        transport without ACK support is silently ignored. It never changes
        wizard state, never reverts, never creates an event and never triggers
        a retry.
        """
        if not callback_query_id:
            return
        if not self._transport_can_ack():
            return
        try:
            await self.transport.answer_callback_query(callback_query_id)
        except _TelegramTransportError:
            pass
        except Exception:
            # Any unexpected ACK failure stays silent & non-authoritative.
            pass

    async def safe_ack(self, callback_query_id: str | None) -> None:
        """Public, side-effect-free ACK used by the Update dispatcher (UX-4.3)."""
        await self._ack_safely(callback_query_id)

    async def handle_callback(
        self, chat_id: str, token: str, callback_query_id: str | None = None
    ) -> None:
        if self._wizard is None:
            await self.transport.send(chat_id, "Wizard no disponible.")
            await self._ack_safely(callback_query_id)
            return
        error_text = None
        try:
            screen = self._wizard.handle_callback(str(chat_id), token)
        except (WizardError, CallbackError):
            # Controlled, user-facing error. No raw internals leaked.
            error_text = "Acció no vàlida o expirada. Torna a obrir /xerrameca."
        except _TelegramTransportError:
            # Telegram API failed to deliver the screen; no internals exposed.
            error_text = "S'ha produït un error inesperat. Torna a obrir /xerrameca."
        except Exception:
            # Unexpected: do not expose internals. Internal log only.
            error_text = "S'ha produït un error inesperat. Torna a obrir /xerrameca."
        if error_text is not None:
            await self.transport.send(chat_id, error_text)
            # Always safe-ACK on the error path so Telegram's inline spinner is
            # dismissed even for expired/invalid callbacks. ACK never mutates
            # wizard/federated state.
            await self._ack_safely(callback_query_id)
            return
        await self._send_screen(chat_id, screen)
        # Single ACK helper: valid callback -> safe ACK; a transport or ACK
        # failure here is swallowed and non-authoritative.
        await self._ack_safely(callback_query_id)

    async def handle_wizard_text(self, chat_id: str, session_marker: str, text: str) -> bool:
        """Free-text input (objective / custom role). Returns True if handled."""
        if self._wizard is None:
            return False
        session_id = session_marker
        screen = self._wizard.handle_text(str(chat_id), session_id, text)
        if screen is None:
            return False
        await self._send_screen(chat_id, screen)
        return True

    async def handle_text(self, chat_id: str, text: str) -> dict[str, Any] | None:
        """Handle the small Telegram control surface.

        Supported forms:
          /xerrameca                -> open/resume the wizard (button surface)
          /xerrameca start <peer_node_id> <objective>
          /xerrameca status <conversation_id>
          /xerrameca sync <conversation_id>
          /xerrameca mode <conversation_id> silent|summary|live

        Free-text that is not a /xerrameca command is routed to the wizard only
        when that caller has an active wizard session expecting text input
        (objective or a custom role). Otherwise it is ignored (never invented as
        wizard state).

        Pause/continue/tell are intentionally not emulated when the core has no
        corresponding safe operation; UI adapters must never invent state.
        """
        if self._wizard is not None and text.strip().lower() == "/xerrameca":
            await self.start_wizard(chat_id)
            return None

        # FASE 2: free-text wizard input (objective / custom role) via public surface.
        if self._wizard is not None:
            active = self._wizard.active_session_id(str(chat_id))
            if active is not None:
                try:
                    handled = await self.handle_wizard_text(chat_id, active, text)
                except (WizardError, CallbackError):
                    handled = False
                if handled:
                    return None

        parts = text.strip().split()
        if len(parts) < 2 or parts[0].lower() != "/xerrameca":
            return None
        command = parts[1].lower()

        if command == "start" and len(parts) >= 4:
            conversation = await self.start(
                peer_node_id=parts[2], objective=" ".join(parts[3:])
            )
            await self.notify(chat_id, conversation)
            return conversation

        if command in {"status", "sync"} and len(parts) == 3:
            conversation = (
                await self.sync(parts[2])
                if command == "sync"
                else await self.status(parts[2])
            )
            await self.notify(chat_id, conversation)
            return conversation

        if command == "mode" and len(parts) == 4:
            mode = self.set_mode(parts[2], parts[3])
            await self.transport.send(
                chat_id, f"Xerrameca #{parts[2]} — mode {mode.value.upper()}"
            )
            return {"conversation_id": parts[2], "mode": mode.value}

        await self.transport.send(
            chat_id,
            "Ús: /xerrameca start <peer_node_id> <objectiu> | "
            "status <id> | sync <id> | mode <id> silent|summary|live",
        )
        return None
