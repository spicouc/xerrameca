from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, Protocol

import httpx


class TelegramMode(str, Enum):
    SILENT = "silent"
    SUMMARY = "summary"
    LIVE = "live"


class TelegramTransport(Protocol):
    async def send(self, chat_id: str, text: str) -> None: ...


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
    ) -> None:
        self.node_base_url = node_base_url.rstrip("/")
        self._api_key = api_key
        self.transport = transport
        self._client = client
        self._client_factory = client_factory
        self._modes: dict[str, TelegramMode] = {}

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

    async def handle_text(self, chat_id: str, text: str) -> dict[str, Any] | None:
        """Handle the small Telegram control surface.

        Supported forms:
          /xerrameca start <peer_node_id> <objective>
          /xerrameca status <conversation_id>
          /xerrameca sync <conversation_id>
          /xerrameca mode <conversation_id> silent|summary|live

        Pause/continue/tell are intentionally not emulated when the core has no
        corresponding safe operation; UI adapters must never invent state.
        """

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
