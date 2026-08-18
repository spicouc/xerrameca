from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..domain.errors import ConflictError, ForbiddenError, NotFoundError
from .dialogue import FederatedDialogueService, FederatedConversationView
from .events import EventEnvelope, EventStore
from .identity import load_node_state


@dataclass(frozen=True, slots=True)
class SupervisorFinding:
    code: str
    severity: str
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LatencyMetrics:
    count: int
    average_seconds: float
    p50_seconds: float
    p95_seconds: float
    max_seconds: float

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * fraction))
    return float(ordered[index])


def _latency(values: list[float]) -> LatencyMetrics:
    if not values:
        return LatencyMetrics(0, 0.0, 0.0, 0.0, 0.0)
    return LatencyMetrics(
        count=len(values),
        average_seconds=round(sum(values) / len(values), 3),
        p50_seconds=round(_percentile(values, 0.50), 3),
        p95_seconds=round(_percentile(values, 0.95), 3),
        max_seconds=round(max(values), 3),
    )


def _normalize_message(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


class LocalSupervisor:
    """Passive-first per-node supervision derived from durable conversation events."""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        waiting_warning_seconds: int = 60,
        idle_warning_seconds: int = 300,
        max_lease_retries: int = 3,
        loop_window: int = 4,
    ):
        self.state = load_node_state(state_dir)
        self.store = EventStore(state_dir)
        self.dialogue = FederatedDialogueService(state_dir)
        self.waiting_warning_seconds = waiting_warning_seconds
        self.idle_warning_seconds = idle_warning_seconds
        self.max_lease_retries = max_lease_retries
        self.loop_window = max(3, loop_window)

    def conversation_ids(self) -> list[str]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT conversation_id FROM federated_heads ORDER BY updated_at DESC"
            ).fetchall()
        return [str(row["conversation_id"]) for row in rows]

    def _events(self, conversation_id: str) -> list[EventEnvelope]:
        events = self.store.list_events(conversation_id)
        if not events:
            raise NotFoundError("federated conversation not found")
        return events

    def _latencies(
        self, events: list[EventEnvelope]
    ) -> tuple[LatencyMetrics, dict[str, LatencyMetrics]]:
        opened: dict[str, int] = {}
        samples: list[float] = []
        by_author: dict[str, list[float]] = {}
        for event in events:
            if event.event_type == "turn.opened":
                opened[str(event.payload.get("turn_id"))] = event.timestamp
            elif event.event_type == "reply.recorded":
                turn_id = str(event.payload.get("turn_id"))
                started = opened.get(turn_id)
                if started is None:
                    continue
                duration = max(0.0, float(event.timestamp - started))
                samples.append(duration)
                author = str(event.payload.get("author_node_id") or event.author_id)
                by_author.setdefault(author, []).append(duration)
        return _latency(samples), {
            author: _latency(values) for author, values in sorted(by_author.items())
        }

    def _retry_count(self, events: list[EventEnvelope], turn_id: str) -> int:
        openings = sum(
            1
            for event in events
            if event.event_type == "turn.opened"
            and str(event.payload.get("turn_id")) == turn_id
        )
        return max(0, openings - 1)

    def inspect(
        self, conversation_id: str, *, now: int | None = None
    ) -> dict[str, Any]:
        current_time = int(time.time()) if now is None else int(now)
        events = self._events(conversation_id)
        view: FederatedConversationView = self.dialogue.get(conversation_id)
        findings: list[SupervisorFinding] = []

        last_event_at = max(event.timestamp for event in events)
        idle_seconds = max(0, current_time - last_event_at)
        if view.status == "active" and idle_seconds >= self.idle_warning_seconds:
            findings.append(
                SupervisorFinding(
                    "conversation_idle",
                    "warning",
                    "active conversation has had no events for an extended period",
                    {"idle_seconds": idle_seconds},
                )
            )

        if view.status == "active" and view.current_turn is not None:
            turn = view.current_turn
            available_at = int(turn.get("available_at") or 0)
            claimed_by = turn.get("claimed_by_node_id")
            lease_until = turn.get("lease_until")
            if claimed_by is None and current_time >= available_at + self.waiting_warning_seconds:
                findings.append(
                    SupervisorFinding(
                        "turn_waiting",
                        "warning",
                        "turn has remained available without a claim",
                        {
                            "turn_id": turn["turn_id"],
                            "assigned_node_id": turn["assigned_node_id"],
                            "waiting_seconds": current_time - available_at,
                        },
                    )
                )
            if claimed_by is not None and lease_until is not None and current_time >= int(lease_until):
                retries = self._retry_count(events, str(turn["turn_id"]))
                findings.append(
                    SupervisorFinding(
                        "lease_expired",
                        "warning",
                        "claimed turn lease has expired",
                        {
                            "turn_id": turn["turn_id"],
                            "claimed_by_node_id": claimed_by,
                            "lease_until": int(lease_until),
                            "retry_count": retries,
                            "retry_available": retries < self.max_lease_retries,
                        },
                    )
                )

        normalized = [
            _normalize_message(message["content"])
            for message in view.messages[-self.loop_window :]
        ]
        if len(normalized) >= self.loop_window and len(set(normalized)) <= 2:
            findings.append(
                SupervisorFinding(
                    "possible_loop",
                    "warning",
                    "recent replies appear repetitive",
                    {
                        "window": self.loop_window,
                        "unique_normalized_messages": len(set(normalized)),
                    },
                )
            )

        if view.status == "active" and view.current_round >= view.max_rounds:
            findings.append(
                SupervisorFinding(
                    "round_limit_reached",
                    "info",
                    "conversation is at its configured maximum round",
                    {
                        "current_round": view.current_round,
                        "max_rounds": view.max_rounds,
                    },
                )
            )

        overall_latency, latency_by_node = self._latencies(events)
        return {
            "conversation": view.to_dict(),
            "findings": [finding.to_dict() for finding in findings],
            "metrics": {
                "event_count": len(events),
                "message_count": len(view.messages),
                "idle_seconds": idle_seconds,
                "response_latency": overall_latency.to_dict(),
                "response_latency_by_node": {
                    node_id: metrics.to_dict()
                    for node_id, metrics in latency_by_node.items()
                },
            },
        }

    def inspect_all(self, *, now: int | None = None) -> list[dict[str, Any]]:
        return [self.inspect(cid, now=now) for cid in self.conversation_ids()]

    def recover_expired_lease(
        self,
        conversation_id: str,
        *,
        expected_epoch: int,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Explicitly reopen an expired claim, bounded by max_lease_retries.

        This is the first active supervisor action and is never automatic in X9.
        """

        current_time = int(time.time()) if now is None else int(now)
        events = self._events(conversation_id)
        view = self.dialogue.get(conversation_id)
        if view.coordinator_id != self.state.node_id:
            raise ForbiddenError("only the conversation coordinator may recover a lease")
        if view.coordinator_epoch != expected_epoch:
            raise ConflictError("coordinator epoch mismatch")
        if view.status != "active" or view.current_turn is None:
            raise ConflictError("conversation has no active turn")
        turn = view.current_turn
        if turn.get("claimed_by_node_id") is None or turn.get("lease_until") is None:
            raise ConflictError("current turn has no claimed lease")
        if int(turn["lease_until"]) > current_time:
            raise ConflictError("current turn lease has not expired")

        retries = self._retry_count(events, str(turn["turn_id"]))
        if retries >= self.max_lease_retries:
            raise ConflictError("maximum lease recovery retries reached")

        self.store.append_local(
            conversation_id,
            author_id=self.state.node_id,
            event_type="turn.opened",
            payload={
                "turn_id": turn["turn_id"],
                "assigned_node_id": turn["assigned_node_id"],
                "round": int(turn["round"]),
                "slot": int(turn["slot"]),
                "phase": str(turn["phase"]),
                "available_at": current_time,
                "recovery_retry": retries + 1,
            },
            expected_epoch=expected_epoch,
        )
        return self.inspect(conversation_id, now=current_time)
