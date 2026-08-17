from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.adapters.in_memory_identity import InMemoryIdentityAdapter
from xerrameca.app import create_app
from xerrameca.api.mcp import TOOL_NAMES
from xerrameca.ports.identity import AgentIdentity


EXPECTED_TOOLS = {
    "xerrameca_command",
    "xerrameca_inbox",
    "xerrameca_claim",
    "xerrameca_reply",
    "xerrameca_list",
    "xerrameca_get",
    "xerrameca_messages",
}


def agent(agent_id: str) -> AgentIdentity:
    return AgentIdentity(
        id=agent_id,
        name=agent_id,
        permissions={"read": True, "write": True, "admin": False},
        allowed_scopes=("shared",),
        capabilities={"xerrameca": True},
    )


def call(client: TestClient, key: str, name: str, arguments: dict, id_: int = 1):
    return client.post(
        "/mcp/",
        headers={"X-API-Key": key},
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": id_,
        },
    )


def test_mcp_lists_exactly_seven_xerrameca_tools(tmp_path: Path) -> None:
    assert TOOL_NAMES == EXPECTED_TOOLS
    app = create_app(db_path=str(tmp_path / "xerrameca.db"))
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1},
        )
    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert names == EXPECTED_TOOLS


def test_mcp_seven_tool_flow_uses_api_key_only(tmp_path: Path) -> None:
    a, b = agent("agent-a"), agent("agent-b")
    provider = InMemoryIdentityAdapter(
        [a, b], {"agent-a": "secret-a", "agent-b": "secret-b"}
    )
    app = create_app(identity=provider, db_path=str(tmp_path / "xerrameca.db"))

    with TestClient(app) as client:
        started = call(
            client,
            "secret-a",
            "xerrameca_command",
            {"command": "/xerrameca agent-b Review MCP extraction --rounds 2 --delay 0"},
        )
        assert started.status_code == 200, started.text
        conversation_id = started.json()["result"]["conversation"]["id"]

        inbox = call(client, "secret-a", "xerrameca_inbox", {})
        assert inbox.status_code == 200
        turn_id = inbox.json()["result"]["turns"][0]["id"]

        claimed = call(
            client, "secret-a", "xerrameca_claim", {"turn_id": turn_id}
        )
        assert claimed.status_code == 200
        lease_token = claimed.json()["result"]["lease_token"]

        replied = call(
            client,
            "secret-a",
            "xerrameca_reply",
            {
                "turn_id": turn_id,
                "lease_token": lease_token,
                "content": "Standalone MCP is ready.",
                "result": "continue",
            },
        )
        assert replied.status_code == 200, replied.text
        assert replied.json()["result"]["current_turn"]["assigned_agent_id"] == "agent-b"

        listed = call(client, "secret-a", "xerrameca_list", {})
        assert listed.status_code == 200
        assert any(item["id"] == conversation_id for item in listed.json()["result"])

        got = call(
            client,
            "secret-a",
            "xerrameca_get",
            {"conversation_id": conversation_id},
        )
        assert got.status_code == 200
        assert got.json()["result"]["id"] == conversation_id

        messages = call(
            client,
            "secret-a",
            "xerrameca_messages",
            {"conversation_id": conversation_id},
        )
        assert messages.status_code == 200
        assert len(messages.json()["result"]) == 2


def test_mcp_tool_call_requires_api_key(tmp_path: Path) -> None:
    app = create_app(db_path=str(tmp_path / "xerrameca.db"))
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "xerrameca_inbox", "arguments": {}},
                "id": 9,
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == 401
