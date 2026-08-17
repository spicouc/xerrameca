from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    id: str
    name: str
    permissions: dict[str, bool]
    allowed_scopes: tuple[str, ...]
    capabilities: dict[str, object]
    is_active: bool = True


class IdentityPort(Protocol):
    """Identity and authorization boundary used by the Xerrameca domain."""

    async def authenticate(self, agent_id: str, api_key: str) -> AgentIdentity:
        """Validate credentials and return the current agent identity."""
        ...

    async def get_agent(self, agent_id: str) -> AgentIdentity:
        """Resolve an agent without exposing provider-specific persistence."""
        ...

    async def list_available_agents(
        self, *, requester: AgentIdentity, scope: str
    ) -> list[AgentIdentity]:
        """Return active agents eligible for a conversation in the scope."""
        ...
