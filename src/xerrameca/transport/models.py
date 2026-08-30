"""UX-5.2 transport domain types."""
from __future__ import annotations

import enum
from typing import Optional

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskStatus(str, enum.Enum):
    pending = "pending"
    leased = "leased"
    failed = "failed"
    discarded = "discarded"
    done = "done"


class ResponseStatus(str, enum.Enum):
    none = "none"
    submitted = "submitted"
    applied = "applied"
    rejected = "rejected"


class TaskEnvelope(StrictModel):
    task_id: str
    idempotency_key: str
    agent_id: str
    conversation_id: str
    turn_id: str
    sequence: int
    epoch: int
    objective: str
    roles_json: str
    history_ref: str
    claim_token: str
    lease_owner: str
    lease_expires_at: float
    response_payload: Optional[str] = None
    response_status: ResponseStatus = ResponseStatus.none


class ClaimDecision(StrictModel):
    ok: bool
    reason: Optional[str] = None
