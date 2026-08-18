from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..domain.errors import (
    ConflictError,
    ProviderUnavailableError,
    ValidationError,
)
from .events import EventEnvelope, EventStore
from .trust import get_peer, sign_peer_request


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ReplicationAck:
    conversation_id: str
    coordinator_epoch: int
    acked_sequence: int
    inserted: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return {
            "conversation_id": self.conversation_id,
            "coordinator_epoch": self.coordinator_epoch,
            "acked_sequence": self.acked_sequence,
            "inserted": self.inserted,
        }


class ReplicationService:
    """Peer event replication over the signed X5.3 trust boundary."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = str(state_dir)
        self.store = EventStore(state_dir)

    @staticmethod
    def _batch_identity(events: list[EventEnvelope]) -> tuple[str, int, str]:
        if not events:
            raise ValidationError("replication batch must contain at least one event")
        conversation_id = events[0].conversation_id
        epoch = events[0].coordinator_epoch
        coordinator_id = events[0].coordinator_id
        for event in events:
            if event.conversation_id != conversation_id:
                raise ValidationError("replication batch spans multiple conversations")
            if event.coordinator_epoch != epoch:
                raise ValidationError("replication batch spans multiple epochs")
            if event.coordinator_id != coordinator_id:
                raise ValidationError("replication batch spans multiple coordinators")
        return conversation_id, epoch, coordinator_id

    def receive_events(
        self, *, sender_node_id: str, raw_events: list[dict[str, Any]]
    ) -> ReplicationAck:
        events = [EventEnvelope.from_dict(raw) for raw in raw_events]
        conversation_id, epoch, coordinator_id = self._batch_identity(events)

        # X7 MVP deliberately has no relay/gossip semantics: authoritative
        # events arrive directly from the node that signed/coordinated them.
        if coordinator_id != sender_node_id:
            raise ConflictError("event batch sender is not the coordinator")

        # Require an exact contiguous range inside the received batch. Store
        # ingestion separately checks continuity against the local head.
        ordered = sorted(events, key=lambda item: item.sequence)
        expected = list(range(ordered[0].sequence, ordered[-1].sequence + 1))
        if [event.sequence for event in ordered] != expected:
            raise ValidationError("replication batch contains a sequence gap")

        inserted = self.store.ingest_many(ordered)
        head = self.store.get_head(conversation_id)
        if head.coordinator_epoch != epoch:
            # A batch may legitimately make a newer epoch current, but it may
            # never ACK an epoch older than the resulting authoritative head.
            if head.coordinator_epoch > epoch:
                acked = max(
                    event.sequence
                    for event in self.store.list_events(
                        conversation_id,
                        epoch=epoch,
                        from_sequence=1,
                    )
                )
            else:
                raise ConflictError("replication head epoch mismatch")
        else:
            acked = head.last_sequence
        return ReplicationAck(
            conversation_id=conversation_id,
            coordinator_epoch=epoch,
            acked_sequence=acked,
            inserted=inserted,
        )

    def receive_ack(
        self,
        *,
        sender_node_id: str,
        conversation_id: str,
        coordinator_epoch: int,
        acked_sequence: int,
    ) -> ReplicationAck:
        # Only known/trusted peers can reach this method through the signed API,
        # but validating the peer here keeps the service safe outside HTTP too.
        get_peer(self.state_dir, sender_node_id)
        cursor = self.store.ack(
            sender_node_id,
            conversation_id,
            coordinator_epoch,
            acked_sequence,
        )
        return ReplicationAck(
            conversation_id=conversation_id,
            coordinator_epoch=coordinator_epoch,
            acked_sequence=cursor,
        )

    def event_range(
        self,
        conversation_id: str,
        *,
        epoch: int,
        from_sequence: int,
        to_sequence: int | None = None,
    ) -> list[EventEnvelope]:
        return self.store.list_events(
            conversation_id,
            epoch=epoch,
            from_sequence=from_sequence,
            to_sequence=to_sequence,
        )

    async def push_missing(
        self,
        peer_node_id: str,
        conversation_id: str,
        *,
        epoch: int,
        timeout_seconds: float = 10.0,
    ) -> ReplicationAck:
        peer = get_peer(self.state_dir, peer_node_id)
        cursor = self.store.cursor(peer_node_id, conversation_id, epoch)
        events = self.store.list_events(
            conversation_id,
            epoch=epoch,
            from_sequence=cursor + 1,
        )
        if not events:
            return ReplicationAck(conversation_id, epoch, cursor, inserted=0)

        path = "/v1/node/federation/events"
        body = canonical_json_bytes({"events": [event.to_dict() for event in events]})
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
            raise ProviderUnavailableError("peer replication request failed") from exc
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"peer replication rejected: HTTP {response.status_code}"
            )
        try:
            raw = response.json()
            acked = int(raw["acked_sequence"])
            response_epoch = int(raw["coordinator_epoch"])
            response_conversation = str(raw["conversation_id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError("invalid replication ACK response") from exc
        if response_conversation != conversation_id or response_epoch != epoch:
            raise ConflictError("replication ACK does not match request")
        if acked > events[-1].sequence:
            raise ConflictError("peer ACK exceeds transmitted sequence")
        cursor = self.store.ack(peer_node_id, conversation_id, epoch, acked)
        return ReplicationAck(conversation_id, epoch, cursor)

    async def fetch_range(
        self,
        peer_node_id: str,
        conversation_id: str,
        *,
        epoch: int,
        from_sequence: int,
        to_sequence: int | None = None,
        timeout_seconds: float = 10.0,
    ) -> list[EventEnvelope]:
        peer = get_peer(self.state_dir, peer_node_id)
        path = f"/v1/node/federation/conversations/{conversation_id}/events"
        headers = sign_peer_request(
            self.state_dir,
            method="GET",
            path=path,
            body=b"",
        )
        params: dict[str, int] = {
            "epoch": epoch,
            "from_sequence": from_sequence,
        }
        if to_sequence is not None:
            params["to_sequence"] = to_sequence
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.get(
                    f"{peer.endpoint.rstrip('/')}{path}",
                    params=params,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("peer catch-up request failed") from exc
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"peer catch-up rejected: HTTP {response.status_code}"
            )
        try:
            rows = response.json()["events"]
            if not isinstance(rows, list):
                raise TypeError("events must be a list")
            events = [EventEnvelope.from_dict(row) for row in rows]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError("invalid catch-up response") from exc
        if events:
            self.receive_events(
                sender_node_id=peer_node_id,
                raw_events=[event.to_dict() for event in events],
            )
        return events
