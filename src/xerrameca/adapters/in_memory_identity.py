from __future__ import annotations

import secrets

from ..domain.errors import ForbiddenError, NotFoundError
from ..ports.identity import AgentIdentity


class InMemoryIdentityAdapter:
    """Test/development identity provider. Never used as a production fallback."""

    def __init__(self, identities: list[AgentIdentity], api_keys: dict[str, str]):
        self._identities = {identity.id: identity for identity in identities}
        self._api_keys = dict(api_keys)

    async def authenticate(self, agent_id: str, api_key: str) -> AgentIdentity:
        identity = self._identities.get(agent_id)
        expected = self._api_keys.get(agent_id)
        if not identity or not expected or not secrets.compare_digest(expected, api_key):
            raise ForbiddenError("credencials invàlides")
        if not identity.is_active:
            raise ForbiddenError("agent inactiu")
        return identity

    async def get_agent(self, agent_id: str) -> AgentIdentity:
        identity = self._identities.get(agent_id)
        if not identity:
            raise NotFoundError("agent no trobat")
        return identity

    async def list_available_agents(
        self, *, requester: AgentIdentity, scope: str
    ) -> list[AgentIdentity]:
        return [
            identity
            for identity in self._identities.values()
            if identity.is_active
            and (identity.permissions.get("admin", False) or scope in identity.allowed_scopes)
            and (
                identity.permissions.get("admin", False)
                or (
                    identity.permissions.get("read", False)
                    and identity.permissions.get("write", False)
                )
            )
        ]
