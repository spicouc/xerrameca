from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from .events import EventEnvelope, EventStore
from .identity import NodeState, load_node_state
from .trust import PeerRecord, get_peer

FEDERATED_PROTOCOL_VERSION = "federated-dialogue-v1"
VALID_RESULTS = {"continue", "complete", "blocked", "needs_human", "error"}


@dataclass(slots=True)
class FederatedConversationView:
    id: str
    name: str = ""
    objective: str = ""
    protocol_version: str = FEDERATED_PROTOCOL_VERSION
    status: str = "unknown"
    participants: list[dict[str, str]] = field(default_factory=list)
    first_node_id: str = ""
    coordinator_id: str = ""
    coordinator_epoch: int = 0
    max_rounds: int = 5
    turn_timeout_seconds: int = 300
    delay_seconds: int = 0
    current_round: int = 0
    current_turn: dict[str, Any] | None = None
    completion_proposal: dict[str, Any] | None = None
    block_reason: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "objective": self.objective,
            "protocol_version": self.protocol_version,
            "status": self.status,
            "participants": self.participants,
            "first_node_id": self.first_node_id,
            "coordinator_id": self.coordinator_id,
            "coordinator_epoch": self.coordinator_epoch,
            "max_rounds": self.max_rounds,
            "turn_timeout_seconds": self.turn_timeout_seconds,
            "delay_seconds": self.delay_seconds,
            "current_round": self.current_round,
            "current_turn": self.current_turn,
            "completion_pending": self.completion_proposal is not None,
            "completion_proposal": self.completion_proposal,
            "block_reason": self.block_reason,
            "messages": self.messages,
        }

    @property
    def participant_node_ids(self) -> list[str]:
        return [participant["node_id"] for participant in self.participants]


def project_conversation(events: list[EventEnvelope]) -> FederatedConversationView:
    if not events:
        raise NotFoundError("federated conversation not found")
    view = FederatedConversationView(id=events[0].conversation_id)

    for event in events:
        if event.conversation_id != view.id:
            raise ValidationError("projection contains multiple conversations")
        view.coordinator_id = event.coordinator_id
        view.coordinator_epoch = event.coordinator_epoch
        payload = event.payload

        if event.event_type == "conversation.created":
            participants = payload.get("participants")
            if not isinstance(participants, list) or len(participants) != 2:
                raise ValidationError("federated dialogue requires exactly two participants")
            view.name = str(payload.get("name") or "Xerrameca")
            view.objective = str(payload.get("objective") or "")
            view.protocol_version = str(
                payload.get("protocol_version") or FEDERATED_PROTOCOL_VERSION
            )
            view.status = "active"
            view.participants = [dict(item) for item in participants]
            view.first_node_id = str(payload.get("first_node_id") or "")
            view.max_rounds = int(payload.get("max_rounds") or 5)
            view.turn_timeout_seconds = int(
                payload.get("turn_timeout_seconds") or 300
            )
            view.delay_seconds = int(payload.get("delay_seconds") or 0)
        elif event.event_type == "coordinator.changed":
            view.coordinator_id = event.coordinator_id
            view.coordinator_epoch = event.coordinator_epoch
        elif event.event_type == "turn.opened":
            view.current_round = max(view.current_round, int(payload["round"]))
            view.current_turn = {
                "turn_id": str(payload["turn_id"]),
                "assigned_node_id": str(payload["assigned_node_id"]),
                "round": int(payload["round"]),
                "slot": int(payload["slot"]),
                "phase": str(payload.get("phase") or "dialogue"),
                "available_at": int(payload.get("available_at") or 0),
                "claimed_by_node_id": None,
                "lease_until": None,
            }
        elif event.event_type == "turn.claimed":
            if view.current_turn is None or view.current_turn["turn_id"] != payload.get(
                "turn_id"
            ):
                raise ValidationError("turn.claimed does not match current turn")
            view.current_turn["claimed_by_node_id"] = str(payload["claimed_by_node_id"])
            view.current_turn["lease_until"] = int(payload["lease_until"])
        elif event.event_type == "reply.recorded":
            view.messages.append(
                {
                    "event_id": event.event_id,
                    "author_node_id": str(payload["author_node_id"]),
                    "content": str(payload["content"]),
                    "result": str(payload["result"]),
                    "round": int(payload["round"]),
                    "slot": int(payload["slot"]),
                    "phase": str(payload.get("phase") or "dialogue"),
                    "timestamp": event.timestamp,
                }
            )
            view.current_turn = None
        elif event.event_type == "completion.proposed":
            view.completion_proposal = {
                "by_node_id": str(payload["by_node_id"]),
                "round": int(payload["round"]),
                "slot": int(payload["slot"]),
            }
        elif event.event_type == "completion.rejected":
            view.completion_proposal = None
        elif event.event_type == "conversation.completed":
            view.status = "completed"
            view.current_turn = None
            view.completion_proposal = None
        elif event.event_type == "conversation.blocked":
            view.status = "blocked"
            view.block_reason = str(payload.get("reason") or "blocked")
            view.current_turn = None
            view.completion_proposal = None
        elif event.event_type == "conversation.error":
            view.status = "error"
            view.block_reason = str(payload.get("reason") or "error")
            view.current_turn = None
            view.completion_proposal = None
        elif event.event_type == "conversation.cancelled":
            view.status = "cancelled"
            view.current_turn = None
            view.completion_proposal = None

    if not view.objective or len(view.participants) != 2:
        raise ValidationError("incomplete federated conversation projection")
    return view


class FederatedDialogueService:
    """Two-node alternating dialogue coordinated by one node per epoch."""

    def __init__(self, state_dir: str | Path):
        self.state: NodeState = load_node_state(state_dir)
        self.state_dir = self.state.state_dir
        self.store = EventStore(state_dir)

    def _view(self, conversation_id: str) -> FederatedConversationView:
        return project_conversation(self.store.list_events(conversation_id))

    def get(self, conversation_id: str) -> FederatedConversationView:
        return self._view(conversation_id)

    def list_conversations(self) -> list[str]:
        """List all local federated conversation ids (reconstructed from events)."""
        return self.store.list_conversation_ids()

    def pending_turns(self, now: int | None = None) -> list[FederatedConversationView]:
        """Return conversations whose current turn is assignable to THIS node now.

        Read-only: never claims, never mutates state, never appends events.
        A turn is "pending for the local node" when:
          - conversation is active
          - a current_turn exists
          - current_turn.assigned_node_id == this node's id
          - current_turn.available_at <= now (no future/leased-out turn)
          - not already claimed by another node (claimed_by_node_id is None
            or equals this node — defensive against stale lease views)
        """
        if now is None:
            now = int(time.time())
        pending: list[FederatedConversationView] = []
        for cid in self.store.list_conversation_ids():
            view = self._view(cid)
            turn = view.current_turn
            if view.status != "active" or turn is None:
                continue
            if turn.get("assigned_node_id") != self.state.node_id:
                continue
            if int(turn.get("available_at") or 0) > now:
                continue
            claimed = turn.get("claimed_by_node_id")
            if claimed is not None and claimed != self.state.node_id:
                continue
            pending.append(view)
        return pending

    def _participant(self, peer: PeerRecord) -> dict[str, str]:
        return {
            "node_id": peer.node_id,
            "agent_id": peer.agent_id,
            "display_name": peer.display_name,
        }

    def create(
        self,
        peer_node_id: str,
        *,
        objective: str,
        name: str = "Xerrameca",
        max_rounds: int = 5,
        turn_timeout_seconds: int = 300,
        delay_seconds: int = 0,
    ) -> FederatedConversationView:
        objective = objective.strip()
        name = name.strip() or "Xerrameca"
        if not objective:
            raise ValidationError("objective is required")
        if not 1 <= max_rounds <= 200:
            raise ValidationError("max_rounds must be between 1 and 200")
        if not 10 <= turn_timeout_seconds <= 86400:
            raise ValidationError("turn_timeout_seconds must be between 10 and 86400")
        if not 0 <= delay_seconds <= 3600:
            raise ValidationError("delay_seconds must be between 0 and 3600")

        peer = get_peer(self.state_dir, peer_node_id)
        if peer.trust_status != "trusted":
            raise ForbiddenError("peer is not trusted")

        conversation_id = f"xfc_{uuid.uuid4().hex}"
        participants = [
            {
                "node_id": self.state.node_id,
                "agent_id": self.state.agent_id,
                "display_name": self.state.display_name,
            },
            self._participant(peer),
        ]
        self.store.append_local(
            conversation_id,
            author_id=self.state.node_id,
            event_type="conversation.created",
            payload={
                "name": name,
                "objective": objective,
                "protocol_version": FEDERATED_PROTOCOL_VERSION,
                "participants": participants,
                "first_node_id": self.state.node_id,
                "max_rounds": max_rounds,
                "turn_timeout_seconds": turn_timeout_seconds,
                "delay_seconds": delay_seconds,
            },
        )
        self._open_turn(
            conversation_id,
            assigned_node_id=self.state.node_id,
            round_number=1,
            slot=1,
            phase="dialogue",
            expected_epoch=1,
            available_at=int(time.time()),
        )
        return self._view(conversation_id)

    def _require_coordinator(
        self, view: FederatedConversationView, expected_epoch: int
    ) -> None:
        if view.coordinator_epoch != expected_epoch:
            raise ConflictError("coordinator epoch mismatch")
        if view.coordinator_id != self.state.node_id:
            raise ForbiddenError("local node is not the conversation coordinator")

    def _open_turn(
        self,
        conversation_id: str,
        *,
        assigned_node_id: str,
        round_number: int,
        slot: int,
        phase: str,
        expected_epoch: int,
        available_at: int,
    ) -> EventEnvelope:
        return self.store.append_local(
            conversation_id,
            author_id=self.state.node_id,
            event_type="turn.opened",
            payload={
                "turn_id": f"xft_{uuid.uuid4().hex}",
                "assigned_node_id": assigned_node_id,
                "round": round_number,
                "slot": slot,
                "phase": phase,
                "available_at": available_at,
            },
            expected_epoch=expected_epoch,
        )

    def claim(
        self,
        conversation_id: str,
        *,
        claimant_node_id: str,
        expected_epoch: int,
        now: int | None = None,
    ) -> FederatedConversationView:
        current_time = int(time.time()) if now is None else int(now)
        view = self._view(conversation_id)
        self._require_coordinator(view, expected_epoch)
        if view.status != "active" or view.current_turn is None:
            raise ConflictError("conversation has no claimable active turn")
        turn = view.current_turn
        if turn["assigned_node_id"] != claimant_node_id:
            raise ForbiddenError("turn belongs to another node")
        if int(turn["available_at"]) > current_time:
            raise ConflictError("turn is still in delay")
        claimed_by = turn.get("claimed_by_node_id")
        lease_until = turn.get("lease_until")
        if claimed_by is not None and lease_until is not None and int(lease_until) > current_time:
            if claimed_by == claimant_node_id:
                return view
            raise ConflictError("turn is already claimed")

        self.store.append_local(
            conversation_id,
            author_id=claimant_node_id,
            event_type="turn.claimed",
            payload={
                "turn_id": turn["turn_id"],
                "claimed_by_node_id": claimant_node_id,
                "lease_until": current_time + view.turn_timeout_seconds,
            },
            expected_epoch=expected_epoch,
        )
        return self._view(conversation_id)

    def _other_node(self, view: FederatedConversationView, node_id: str) -> str:
        ids = view.participant_node_ids
        if node_id not in ids or len(ids) != 2:
            raise ConflictError("invalid participant order")
        return ids[1] if ids[0] == node_id else ids[0]

    def reply(
        self,
        conversation_id: str,
        *,
        author_node_id: str,
        content: str,
        result: str,
        expected_epoch: int,
        now: int | None = None,
    ) -> FederatedConversationView:
        current_time = int(time.time()) if now is None else int(now)
        content = content.strip()
        if not content:
            raise ValidationError("reply content is required")
        if result not in VALID_RESULTS:
            raise ValidationError("invalid reply result")

        view = self._view(conversation_id)
        self._require_coordinator(view, expected_epoch)
        if view.status != "active" or view.current_turn is None:
            raise ConflictError("conversation has no active turn")
        turn = dict(view.current_turn)
        if turn["assigned_node_id"] != author_node_id:
            raise ForbiddenError("turn belongs to another node")
        if turn.get("claimed_by_node_id") != author_node_id:
            raise ConflictError("turn must be claimed before reply")
        if not turn.get("lease_until") or int(turn["lease_until"]) <= current_time:
            raise ConflictError("turn lease expired")

        self.store.append_local(
            conversation_id,
            author_id=author_node_id,
            event_type="reply.recorded",
            payload={
                "turn_id": turn["turn_id"],
                "author_node_id": author_node_id,
                "content": content,
                "result": result,
                "round": int(turn["round"]),
                "slot": int(turn["slot"]),
                "phase": str(turn["phase"]),
            },
            expected_epoch=expected_epoch,
        )

        logical_round = int(turn["round"])
        slot = int(turn["slot"])
        confirmation = (
            turn["phase"] == "completion_confirmation"
            and view.completion_proposal is not None
        )

        if result in {"blocked", "needs_human", "error"}:
            event_type = "conversation.error" if result == "error" else "conversation.blocked"
            self.store.append_local(
                conversation_id,
                author_id=author_node_id,
                event_type=event_type,
                payload={"reason": result, "final_content": content},
                expected_epoch=expected_epoch,
            )
            return self._view(conversation_id)

        if result == "complete":
            if (
                confirmation
                and view.completion_proposal is not None
                and view.completion_proposal["by_node_id"] != author_node_id
            ):
                self.store.append_local(
                    conversation_id,
                    author_id=author_node_id,
                    event_type="conversation.completed",
                    payload={"final_content": content},
                    expected_epoch=expected_epoch,
                )
                return self._view(conversation_id)

            other = self._other_node(view, author_node_id)
            self.store.append_local(
                conversation_id,
                author_id=author_node_id,
                event_type="completion.proposed",
                payload={
                    "by_node_id": author_node_id,
                    "round": logical_round,
                    "slot": slot,
                },
                expected_epoch=expected_epoch,
            )
            self._open_turn(
                conversation_id,
                assigned_node_id=other,
                round_number=logical_round,
                slot=0,
                phase="completion_confirmation",
                expected_epoch=expected_epoch,
                available_at=current_time + view.delay_seconds,
            )
            return self._view(conversation_id)

        # result == continue
        order = view.participant_node_ids
        if view.first_node_id not in order:
            raise ConflictError("first participant missing")
        first = view.first_node_id
        other = order[1] if order[0] == first else order[0]

        if confirmation:
            proposal = view.completion_proposal
            if proposal is None:
                raise ConflictError("completion proposal is inconsistent")
            proposal_round = int(proposal["round"])
            proposal_slot = int(proposal["slot"])
            self.store.append_local(
                conversation_id,
                author_id=author_node_id,
                event_type="completion.rejected",
                payload={"by_node_id": author_node_id},
                expected_epoch=expected_epoch,
            )
            if proposal_round >= view.max_rounds:
                self.store.append_local(
                    conversation_id,
                    author_id=author_node_id,
                    event_type="conversation.blocked",
                    payload={"reason": "max_rounds", "final_content": content},
                    expected_epoch=expected_epoch,
                )
                return self._view(conversation_id)
            if proposal_slot == 1:
                next_node, next_round, next_slot = first, proposal_round + 1, 1
            elif proposal_slot == 2:
                next_node, next_round, next_slot = other, proposal_round + 1, 2
            else:
                raise ConflictError("invalid completion proposal slot")
        elif slot == 1:
            next_node, next_round, next_slot = other, logical_round, 2
        elif slot == 2:
            if logical_round >= view.max_rounds:
                self.store.append_local(
                    conversation_id,
                    author_id=author_node_id,
                    event_type="conversation.blocked",
                    payload={"reason": "max_rounds", "final_content": content},
                    expected_epoch=expected_epoch,
                )
                return self._view(conversation_id)
            next_node, next_round, next_slot = first, logical_round + 1, 1
        else:
            raise ConflictError("invalid turn slot")

        self._open_turn(
            conversation_id,
            assigned_node_id=next_node,
            round_number=next_round,
            slot=next_slot,
            phase="dialogue",
            expected_epoch=expected_epoch,
            available_at=current_time + view.delay_seconds,
        )
        return self._view(conversation_id)