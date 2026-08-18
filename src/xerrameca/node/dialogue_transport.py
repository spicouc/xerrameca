from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ..domain.errors import (
    ConflictError,
    ProviderUnavailableError,
    ValidationError,
)
from .dialogue import FederatedConversationView, project_conversation
from .events import EventEnvelope, EventStore
from .replication import canonical_json_bytes
from .trust import get_peer, sign_peer_request


class RemoteDialogueClient:
    """Submit a participant action to the current conversation coordinator."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = str(state_dir)
        self.store = EventStore(state_dir)

    async def _post(
        self,
        coordinator_node_id: str,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> FederatedConversationView:
        peer = get_peer(self.state_dir, coordinator_node_id)
        body = canonical_json_bytes(payload)
        headers = {
            **sign_peer_request(
                self.state_dir,
                method="POST",
                path=path,
                body=body,
            ),
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(
                    f"{peer.endpoint.rstrip('/')}{path}", content=body, headers=headers
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("conversation coordinator unavailable") from exc
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"coordinator action rejected: HTTP {response.status_code}"
            )
        try:
            raw = response.json()
            rows = raw["events"]
            if not isinstance(rows, list):
                raise TypeError("events must be a list")
            events = [EventEnvelope.from_dict(item) for item in rows]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError("invalid coordinator action response") from exc

        if events:
            self.store.ingest_many(events)
        try:
            return project_conversation(self.store.list_events(payload["conversation_id"]))
        except KeyError as exc:
            raise ValidationError("conversation_id missing from remote action") from exc

    async def claim(
        self,
        coordinator_node_id: str,
        conversation_id: str,
        *,
        expected_epoch: int,
        timeout_seconds: float = 10.0,
    ) -> FederatedConversationView:
        return await self._post(
            coordinator_node_id,
            "/v1/node/federation/dialogue/claim",
            {
                "conversation_id": conversation_id,
                "expected_epoch": expected_epoch,
            },
            timeout_seconds=timeout_seconds,
        )

    async def reply(
        self,
        coordinator_node_id: str,
        conversation_id: str,
        *,
        expected_epoch: int,
        content: str,
        result: str,
        timeout_seconds: float = 10.0,
    ) -> FederatedConversationView:
        return await self._post(
            coordinator_node_id,
            "/v1/node/federation/dialogue/reply",
            {
                "conversation_id": conversation_id,
                "expected_epoch": expected_epoch,
                "content": content,
                "result": result,
            },
            timeout_seconds=timeout_seconds,
        )
