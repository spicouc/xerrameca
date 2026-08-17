from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


TurnPolicy = Literal["alternating", "supervisor"]
TurnResult = Literal["continue", "complete", "blocked", "needs_human", "error"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=100_000)
    scope: str = "shared"
    participant_agent_ids: list[str] = Field(min_length=2, max_length=2)
    turn_policy: TurnPolicy = "alternating"
    supervisor_agent_id: str | None = None
    first_agent_id: str | None = None
    max_rounds: int = Field(default=5, ge=1, le=200)
    turn_timeout_seconds: int = Field(default=300, ge=10, le=86_400)
    delay_seconds: int = Field(default=2, ge=0, le=3_600)
    persist_summary: bool = True


class ReplyRequest(StrictModel):
    content: str = Field(min_length=1, max_length=100_000)
    result: TurnResult = "continue"
    lease_token: str = Field(min_length=16, max_length=256)
    next_agent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationSettingsUpdate(StrictModel):
    enabled: bool | None = None
    max_rounds: int | None = Field(default=None, ge=1, le=200)
    turn_timeout_seconds: int | None = Field(default=None, ge=10, le=86_400)
    delay_seconds: int | None = Field(default=None, ge=0, le=3_600)
    persist_summary: bool | None = None
