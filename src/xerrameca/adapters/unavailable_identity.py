from __future__ import annotations

from ..domain.errors import ProviderUnavailableError
from ..ports.identity import AgentIdentity


class UnavailableIdentityAdapter:
    """Fail-closed provider used when no production identity adapter is configured."""

    async def authenticate(self, agent_id: str, api_key: str) -> AgentIdentity:
        raise ProviderUnavailableError("identity provider no configurat")

    async def get_agent(self, agent_id: str) -> AgentIdentity:
        raise ProviderUnavailableError("identity provider no configurat")

    async def list_available_agents(
        self, *, requester: AgentIdentity, scope: str
    ) -> list[AgentIdentity]:
        raise ProviderUnavailableError("identity provider no configurat")
