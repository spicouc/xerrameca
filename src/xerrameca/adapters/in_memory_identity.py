from __future__ import annotations

import secrets

from ..domain.errors import ForbiddenError, NotFoundError
from ..ports.identity import AgentIdentity


class InMemoryIdentityAdapter:
    """Test/development identity provider. Never used as a production fallback."""

    def __init__(self, identities: list[AgentIdentity], api_keys: dict[str, str]):
        self._identities = {identity.id: identity for identity in identities}
        self._api_keys = dict(api_keys)

    def _identity_for_key(self, api_key: str) -> AgentIdentity | None:
        for agent_id, expected in self._api_keys.items():
            if secrets.compare_digest(expected, api_key):
                return self._identities.get(agent_id)
        return None

    async def authenticate(
        self, api_key: str, *, agent_id_hint: str | None = None
    ) -> AgentIdentity:
        identity = self._identity_for_key(api_key)
        if not identity:
            raise ForbiddenError("credencials invàlides")
        if agent_id_hint is not None and identity.id != agent_id_hint:
            raise ForbiddenError("X-Agent-ID no coincideix amb la credencial")
        if not identity.is_active:
            raise ForbiddenError("agent inactiu")
        return identity

    async def get_agent(self, agent_id: str, *, credential: str) -> AgentIdentity:
        await self.authenticate(credential)
        identity = self._identities.get(agent_id)
        if not identity:
            raise NotFoundError("agent no trobat")
        return identity

    async def list_available_agents(
        self,
        *,
        requester: AgentIdentity,
        scope: str,
        credential: str,
    ) -> list[AgentIdentity]:
        authenticated = await self.authenticate(credential)
        if authenticated.id != requester.id:
            raise ForbiddenError("credencial no correspon al requester")
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
