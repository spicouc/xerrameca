from __future__ import annotations

from typing import Any

import httpx

from ..domain.errors import (
    ForbiddenError,
    NotFoundError,
    ProviderUnavailableError,
    ValidationError,
)
from ..ports.identity import AgentIdentity


class PluribusIdentityAdapter:
    """Identity provider backed only by Pluribus public HTTP endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def _get(
        self,
        path: str,
        *,
        credential: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.get(
                    path,
                    params=params,
                    headers={"X-API-Key": credential},
                )
        except httpx.RequestError as exc:
            raise ProviderUnavailableError("Pluribus identity provider no disponible") from exc

        if response.status_code in {401, 403}:
            raise ForbiddenError("credencial Pluribus invàlida o sense permisos")
        if response.status_code == 404:
            raise NotFoundError("identitat Pluribus no trobada")
        if response.status_code >= 500:
            raise ProviderUnavailableError("Pluribus identity provider no disponible")
        if response.status_code >= 400:
            detail = "resposta invàlida del provider"
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
                    detail = payload["detail"]
            except ValueError:
                pass
            raise ValidationError(detail)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderUnavailableError("resposta JSON invàlida de Pluribus") from exc

    @staticmethod
    def _identity(payload: Any) -> AgentIdentity:
        if not isinstance(payload, dict):
            raise ProviderUnavailableError("payload d'identitat Pluribus invàlid")
        try:
            agent_id = payload["id"]
            name = payload["name"]
            permissions = payload["permissions"]
            allowed_scopes = payload["allowed_scopes"]
            capabilities = payload.get("capabilities", {})
            is_active = payload.get("is_active", True)
        except KeyError as exc:
            raise ProviderUnavailableError("payload d'identitat Pluribus incomplet") from exc
        if not isinstance(agent_id, str) or not isinstance(name, str):
            raise ProviderUnavailableError("payload d'identitat Pluribus invàlid")
        if not isinstance(permissions, dict) or not isinstance(allowed_scopes, list):
            raise ProviderUnavailableError("permisos/scopes Pluribus invàlids")
        if not isinstance(capabilities, dict):
            capabilities = {}
        return AgentIdentity(
            id=agent_id,
            name=name,
            permissions={str(k): bool(v) for k, v in permissions.items()},
            allowed_scopes=tuple(str(scope) for scope in allowed_scopes),
            capabilities=capabilities,
            is_active=bool(is_active),
        )

    async def authenticate(
        self, api_key: str, *, agent_id_hint: str | None = None
    ) -> AgentIdentity:
        payload = await self._get("/v1/identity/me", credential=api_key)
        identity = self._identity(payload)
        if agent_id_hint is not None and identity.id != agent_id_hint:
            raise ForbiddenError("X-Agent-ID no coincideix amb la credencial Pluribus")
        if not identity.is_active:
            raise ForbiddenError("agent Pluribus inactiu")
        return identity

    async def list_available_agents(
        self,
        *,
        requester: AgentIdentity,
        scope: str,
        credential: str,
    ) -> list[AgentIdentity]:
        payload = await self._get(
            "/v1/identity/peers",
            credential=credential,
            params={"scope": scope},
        )
        if not isinstance(payload, list):
            raise ProviderUnavailableError("payload de peers Pluribus invàlid")
        peers = [self._identity(item) for item in payload]
        return [peer for peer in peers if peer.id != requester.id and peer.is_active]

    async def get_agent(self, agent_id: str, *, credential: str) -> AgentIdentity:
        caller = await self.authenticate(credential)
        if caller.id == agent_id:
            return caller
        scopes = caller.allowed_scopes or ("shared",)
        for scope in scopes:
            try:
                peers = await self.list_available_agents(
                    requester=caller, scope=scope, credential=credential
                )
            except ValidationError:
                continue
            for peer in peers:
                if peer.id == agent_id:
                    return peer
        raise NotFoundError("agent Pluribus no disponible per al caller")
