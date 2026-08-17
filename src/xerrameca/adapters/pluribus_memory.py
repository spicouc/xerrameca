from __future__ import annotations

import httpx

from ..domain.errors import ForbiddenError, ProviderUnavailableError, ValidationError
from ..ports.memory import MemoryPort, PersistedMemory


class PluribusMemoryAdapter(MemoryPort):
    """Persist final summaries through Pluribus public memory REST API.

    The service credential is held only in process memory and is never written
    to Xerrameca SQLite, messages, audit events or summary outbox rows.
    """

    def __init__(
        self,
        base_url: str,
        service_api_key: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._service_api_key = service_api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def persist_summary(
        self,
        *,
        conversation_id: str,
        scope: str,
        title: str,
        objective: str,
        status: str,
        rounds: int,
        content: str,
        metadata: dict[str, object],
    ) -> PersistedMemory:
        payload = {
            "content": content,
            "scope": scope,
            "category": "x-xerrameca",
            "key": f"xerrameca:{conversation_id}",
            "metadata": {
                **metadata,
                "xerrameca_conversation_id": conversation_id,
                "title": title,
                "objective": objective,
                "status": status,
                "rounds": rounds,
            },
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    "/v1/memory/write",
                    headers={"X-API-Key": self._service_api_key},
                    json=payload,
                )
        except httpx.RequestError as exc:
            raise ProviderUnavailableError("Pluribus memory provider no disponible") from exc

        if response.status_code in {401, 403}:
            raise ForbiddenError("credencial de servei Pluribus sense permís de memòria")
        if response.status_code >= 500:
            raise ProviderUnavailableError("Pluribus memory provider no disponible")
        if response.status_code >= 400:
            detail = "Pluribus ha rebutjat el resum"
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("detail"), str):
                    detail = body["detail"]
            except ValueError:
                pass
            raise ValidationError(detail)
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError("resposta JSON invàlida de Pluribus") from exc
        fact_id = body.get("fact_id") if isinstance(body, dict) else None
        if not isinstance(fact_id, str) or not fact_id:
            raise ProviderUnavailableError("Pluribus no ha retornat fact_id")
        return PersistedMemory(external_id=fact_id)
