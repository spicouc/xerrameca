"""Transport layer for UX-5.2 autonomous worker protocol.

Protocol v1 only. No DB, HTTP, Telegram, Hermes, worker, task_queue,
reconciler, or ReplyApplier here.
"""
from __future__ import annotations

from xerrameca.transport.models import (
    ClaimDecision,
    TaskEnvelope,
    TaskStatus,
    ResponseStatus,
)
from xerrameca.transport.interfaces import AgentTransport, TurnClaimPort


def make_idempotency_key(
    conversation_id: str,
    turn_id: str,
    sequence: int,
    epoch: int,
) -> str:
    """Deterministic idempotency key.

    Format: ``conversation_id:turn_id:sequence:epoch``
    """
    return f"{conversation_id}:{turn_id}:{sequence}:{epoch}"


__all__ = [
    "TaskStatus",
    "ResponseStatus",
    "TaskEnvelope",
    "ClaimDecision",
    "AgentTransport",
    "TurnClaimPort",
    "make_idempotency_key",
]
