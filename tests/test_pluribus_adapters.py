from __future__ import annotations

import json

import httpx
import pytest

from xerrameca.adapters.pluribus_identity import PluribusIdentityAdapter
from xerrameca.adapters.pluribus_memory import PluribusMemoryAdapter
from xerrameca.domain.errors import ForbiddenError, ProviderUnavailableError


@pytest.mark.asyncio
async def test_pluribus_identity_uses_public_provider_endpoints() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("X-API-Key", "")))
        if request.url.path == "/v1/identity/me":
            return httpx.Response(
                200,
                json={
                    "id": "agent-a",
                    "name": "Agent A",
                    "permissions": {"read": True, "write": True, "admin": False},
                    "allowed_scopes": ["shared"],
                    "capabilities": {"xerrameca": True},
                    "is_active": True,
                },
            )
        if request.url.path == "/v1/identity/peers":
            assert request.url.params["scope"] == "shared"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "agent-b",
                        "name": "Agent B",
                        "permissions": {"read": True, "write": True, "admin": False},
                        "allowed_scopes": ["shared"],
                        "capabilities": {"xerrameca": True},
                        "is_active": True,
                    }
                ],
            )
        return httpx.Response(404)

    adapter = PluribusIdentityAdapter(
        "http://pluribus.test",
        transport=httpx.MockTransport(handler),
    )
    caller = await adapter.authenticate("caller-secret")
    assert caller.id == "agent-a"
    peers = await adapter.list_available_agents(
        requester=caller,
        scope="shared",
        credential="caller-secret",
    )
    assert [peer.id for peer in peers] == ["agent-b"]
    assert seen == [
        ("/v1/identity/me", "caller-secret"),
        ("/v1/identity/peers", "caller-secret"),
    ]


@pytest.mark.asyncio
async def test_identity_hint_must_match_provider_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "real-agent",
                "name": "Real",
                "permissions": {"read": True, "write": True},
                "allowed_scopes": ["shared"],
                "capabilities": {},
                "is_active": True,
            },
        )

    adapter = PluribusIdentityAdapter(
        "http://pluribus.test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ForbiddenError):
        await adapter.authenticate("secret", agent_id_hint="spoofed-agent")


@pytest.mark.asyncio
async def test_identity_provider_failure_is_explicit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    adapter = PluribusIdentityAdapter(
        "http://pluribus.test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderUnavailableError):
        await adapter.authenticate("secret")


@pytest.mark.asyncio
async def test_pluribus_memory_writes_summary_via_public_api() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/memory/write"
        captured["key"] = request.headers.get("X-API-Key")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            201,
            json={"fact_id": "fact-123", "message": "ok", "chunks_generated": 1},
        )

    adapter = PluribusMemoryAdapter(
        "http://pluribus.test",
        "service-secret",
        transport=httpx.MockTransport(handler),
    )
    persisted = await adapter.persist_summary(
        conversation_id="conv-1",
        scope="shared",
        title="Architecture",
        objective="Agree on separation",
        status="completed",
        rounds=2,
        content="Final consensus",
        metadata={"source": "xerrameca"},
    )
    assert persisted.external_id == "fact-123"
    assert captured["key"] == "service-secret"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["category"] == "x-xerrameca"
    assert body["key"] == "xerrameca:conv-1"
    assert body["metadata"]["xerrameca_conversation_id"] == "conv-1"
