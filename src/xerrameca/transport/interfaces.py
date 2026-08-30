"""UX-5.2 transport interfaces (protocol contracts only)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from xerrameca.transport.models import TaskEnvelope, ClaimDecision, TaskStatus, ResponseStatus


@runtime_checkable
class AgentTransport(Protocol):
    """Lease, submit, fail, discard tasks — protocol v1 transport contract."""

    def lease_next_task(self, agent_id: str) -> TaskEnvelope | None:
        ...

    def submit_response(self, envelope: TaskEnvelope, response: str) -> ClaimDecision:
        ...

    def fail_task(self, envelope: TaskEnvelope, reason: str) -> ClaimDecision:
        ...

    def discard_task(self, envelope: TaskEnvelope) -> ClaimDecision:
        ...


@runtime_checkable
class TurnClaimPort(Protocol):
    """Port a local agent uses to claim a turn."""

    def claim_turn(self, envelope: TaskEnvelope) -> ClaimDecision:
        ...
