from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path

from ..domain.errors import ForbiddenError, NotFoundError, ProviderUnavailableError
from ..ports.identity import AgentIdentity


class LocalIdentityAdapter:
    """File-backed local identity provider with hashed API credentials.

    The provider deliberately stores only SHA-256 digests of local API keys in
    its identity file. Credentials supplied by callers remain request-scoped and
    are never written to Xerrameca SQLite, events or logs.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._identities: dict[str, AgentIdentity] = {}
        self._key_hashes: dict[str, str] = {}
        self._load()

    @staticmethod
    def hash_api_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderUnavailableError(
                f"local identity file unavailable: {self.path}"
            ) from exc

        agents = raw.get("agents") if isinstance(raw, dict) else None
        if not isinstance(agents, list) or not agents:
            raise ProviderUnavailableError("local identity file has no agents")

        identities: dict[str, AgentIdentity] = {}
        key_hashes: dict[str, str] = {}
        for item in agents:
            if not isinstance(item, dict):
                raise ProviderUnavailableError("invalid local identity entry")
            agent_id = item.get("id")
            name = item.get("name")
            key_hash = item.get("api_key_sha256")
            if not isinstance(agent_id, str) or not agent_id.strip():
                raise ProviderUnavailableError("local identity id is required")
            if not isinstance(name, str) or not name.strip():
                raise ProviderUnavailableError(
                    f"local identity name is required for {agent_id}"
                )
            if (
                not isinstance(key_hash, str)
                or len(key_hash) != 64
                or any(ch not in "0123456789abcdefABCDEF" for ch in key_hash)
            ):
                raise ProviderUnavailableError(
                    f"valid api_key_sha256 is required for {agent_id}"
                )
            if agent_id in identities:
                raise ProviderUnavailableError(
                    f"duplicate local identity id: {agent_id}"
                )

            permissions = item.get("permissions") or {}
            scopes = item.get("allowed_scopes") or ["shared"]
            capabilities = item.get("capabilities") or {}
            if not isinstance(permissions, dict) or not isinstance(scopes, list):
                raise ProviderUnavailableError(
                    f"invalid permissions/scopes for {agent_id}"
                )
            if not isinstance(capabilities, dict):
                raise ProviderUnavailableError(
                    f"invalid capabilities for {agent_id}"
                )

            identities[agent_id] = AgentIdentity(
                id=agent_id,
                name=name,
                permissions={str(k): bool(v) for k, v in permissions.items()},
                allowed_scopes=tuple(str(scope) for scope in scopes),
                capabilities=capabilities,
                is_active=bool(item.get("is_active", True)),
            )
            key_hashes[agent_id] = key_hash.lower()

        self._identities = identities
        self._key_hashes = key_hashes

    def _identity_for_key(self, api_key: str) -> AgentIdentity | None:
        supplied_hash = self.hash_api_key(api_key)
        for agent_id, expected_hash in self._key_hashes.items():
            if secrets.compare_digest(expected_hash, supplied_hash):
                return self._identities.get(agent_id)
        return None

    async def authenticate(
        self, api_key: str, *, agent_id_hint: str | None = None
    ) -> AgentIdentity:
        identity = self._identity_for_key(api_key)
        if identity is None:
            raise ForbiddenError("credencials locals invàlides")
        if agent_id_hint is not None and identity.id != agent_id_hint:
            raise ForbiddenError("X-Agent-ID no coincideix amb la credencial")
        if not identity.is_active:
            raise ForbiddenError("agent local inactiu")
        return identity

    async def get_agent(self, agent_id: str, *, credential: str) -> AgentIdentity:
        await self.authenticate(credential)
        identity = self._identities.get(agent_id)
        if identity is None or not identity.is_active:
            raise NotFoundError("agent local no trobat")
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
            if identity.id != requester.id
            and identity.is_active
            and (
                identity.permissions.get("admin", False)
                or (
                    identity.permissions.get("read", False)
                    and identity.permissions.get("write", False)
                    and scope in identity.allowed_scopes
                )
            )
        ]
