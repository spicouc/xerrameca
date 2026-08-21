"""Transport-independent DTOs for the Xerrameca command layer.

These types carry no transport, Telegram, or Pluribus knowledge. They are
the shared vocabulary reused by CLI, Telegram (future), MCP and web (future).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentChoice:
    node_id: str
    display_name: str
    endpoint: str
    trusted: bool
    online: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "endpoint": self.endpoint,
            "trusted": self.trusted,
            "online": self.online,
        }


@dataclass
class ConversationSummary:
    id: str
    objective: str
    status: str
    coordinator_id: str
    coordinator_epoch: int
    current_round: int
    max_rounds: int
    participants: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "status": self.status,
            "coordinator_id": self.coordinator_id,
            "coordinator_epoch": self.coordinator_epoch,
            "current_round": self.current_round,
            "max_rounds": self.max_rounds,
            "participants": self.participants,
        }


@dataclass
class ConversationPreset:
    key: str
    label: str
    instruction: str
    default_role_a: str
    default_role_b: str
    completion_instruction: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "instruction": self.instruction,
            "default_role_a": self.default_role_a,
            "default_role_b": self.default_role_b,
            "completion_instruction": self.completion_instruction,
        }


@dataclass
class WizardButton:
    label: str
    action_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "action_id": self.action_id}


@dataclass
class WizardScreen:
    state: str
    text: str
    buttons: list[WizardButton] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "text": self.text,
            "buttons": [b.to_dict() for b in self.buttons],
        }


@dataclass
class WizardAction:
    action_id: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class WizardSession:
    session_id: str
    caller_id: str
    state: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "caller_id": self.caller_id,
            "state": self.state,
            "data": self.data,
        }
