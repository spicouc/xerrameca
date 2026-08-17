from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.adapters.in_memory_identity import InMemoryIdentityAdapter
from xerrameca.app import create_app
from xerrameca.ports.identity import AgentIdentity


def identity(agent_id: str, name: str) -> AgentIdentity:
    return AgentIdentity(
        id=agent_id,
        name=name,
        permissions={"read": True, "write": True, "delete": False, "admin": False},
        allowed_scopes=("shared",),
        capabilities={"xerrameca": True},
        is_active=True,
    )


def headers(agent_id: str, key: str) -> dict[str, str]:
    return {"X-Agent-ID": agent_id, "X-API-Key": key}


def test_rest_command_inbox_claim_reply_complete(tmp_path: Path) -> None:
    a = identity("agent-a", "Agent A")
    b = identity("agent-b", "Agent B")
    provider = InMemoryIdentityAdapter(
        [a, b],
        {"agent-a": "secret-a", "agent-b": "secret-b"},
    )
    app = create_app(identity=provider, db_path=str(tmp_path / "xerrameca.db"))

    with TestClient(app) as client:
        help_response = client.post(
            "/v1/xerrameca/command",
            headers=headers("agent-a", "secret-a"),
            json={"command": "/xerrameca help"},
        )
        assert help_response.status_code == 200
        assert help_response.json()["action"] == "help"

        agents_response = client.post(
            "/v1/xerrameca/command",
            headers=headers("agent-a", "secret-a"),
            json={"command": "/xerrameca agents"},
        )
        assert agents_response.status_code == 200
        assert [item["id"] for item in agents_response.json()["agents"]] == ["agent-b"]

        start = client.post(
            "/v1/xerrameca/command",
            headers=headers("agent-a", "secret-a"),
            json={
                "command": "/xerrameca agent-b Review the extraction architecture --rounds 3 --timeout 60 --delay 0"
            },
        )
        assert start.status_code == 200, start.text
        conversation = start.json()["conversation"]
        conversation_id = conversation["id"]
        assert conversation["status"] == "active"
        assert conversation["current_turn"]["assigned_agent_id"] == "agent-a"

        inbox_a = client.get(
            "/v1/xerrameca/inbox",
            headers=headers("agent-a", "secret-a"),
        )
        turn_a = inbox_a.json()["turns"][0]["id"]
        claim_a = client.post(
            f"/v1/xerrameca/turns/{turn_a}/claim",
            headers=headers("agent-a", "secret-a"),
        )
        assert claim_a.status_code == 200
        lease_a = claim_a.json()["lease_token"]

        reply_a = client.post(
            f"/v1/xerrameca/turns/{turn_a}/reply",
            headers=headers("agent-a", "secret-a"),
            json={
                "content": "Keep orchestration state in a separate database.",
                "result": "continue",
                "lease_token": lease_a,
                "metadata": {},
            },
        )
        assert reply_a.status_code == 200, reply_a.text
        assert reply_a.json()["current_turn"]["assigned_agent_id"] == "agent-b"

        inbox_b = client.get(
            "/v1/xerrameca/inbox",
            headers=headers("agent-b", "secret-b"),
        )
        turn_b = inbox_b.json()["turns"][0]["id"]
        lease_b = client.post(
            f"/v1/xerrameca/turns/{turn_b}/claim",
            headers=headers("agent-b", "secret-b"),
        ).json()["lease_token"]
        proposal = client.post(
            f"/v1/xerrameca/turns/{turn_b}/reply",
            headers=headers("agent-b", "secret-b"),
            json={
                "content": "Agreed. Complete.",
                "result": "complete",
                "lease_token": lease_b,
                "metadata": {},
            },
        )
        assert proposal.status_code == 200, proposal.text
        assert proposal.json()["completion_pending"] is True

        confirmation_turn = client.get(
            "/v1/xerrameca/inbox",
            headers=headers("agent-a", "secret-a"),
        ).json()["turns"][0]["id"]
        confirmation_lease = client.post(
            f"/v1/xerrameca/turns/{confirmation_turn}/claim",
            headers=headers("agent-a", "secret-a"),
        ).json()["lease_token"]
        completed = client.post(
            f"/v1/xerrameca/turns/{confirmation_turn}/reply",
            headers=headers("agent-a", "secret-a"),
            json={
                "content": "Confirmed.",
                "result": "complete",
                "lease_token": confirmation_lease,
                "metadata": {},
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "completed"

        show = client.get(
            f"/v1/xerrameca/conversations/{conversation_id}",
            headers=headers("agent-b", "secret-b"),
        )
        assert show.status_code == 200
        assert show.json()["status"] == "completed"

        messages = client.get(
            f"/v1/xerrameca/conversations/{conversation_id}/messages",
            headers=headers("agent-a", "secret-a"),
        )
        assert messages.status_code == 200
        assert len(messages.json()) == 4


def test_missing_identity_provider_fails_closed(tmp_path: Path) -> None:
    app = create_app(db_path=str(tmp_path / "xerrameca.db"))
    with TestClient(app) as client:
        response = client.get(
            "/v1/xerrameca/inbox",
            headers=headers("agent-a", "not-a-real-key"),
        )
    assert response.status_code == 503
    assert "identity provider" in response.json()["detail"]
