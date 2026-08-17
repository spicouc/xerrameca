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
    """Identity and authorization boundary used by the Xerrameca domain.

    Credentials are request-scoped inputs. Implementations must never persist
    them in Xerrameca storage, events, logs or traces.
    """

    async def authenticate(
        self, api_key: str, *, agent_id_hint: str | None = None
    ) -> AgentIdentity:
        """Validate a credential and return the current caller identity."""
        ...

    async def get_agent(
        self, agent_id: str, *, credential: str
    ) -> AgentIdentity:
        """Resolve an agent through the provider's public API."""
        ...

    async def list_available_agents(
        self,
        *,
        requester: AgentIdentity,
        scope: str,
        credential: str,
    ) -> list[AgentIdentity]:
        """Return active agents eligible for a conversation in the scope."""
        ...
