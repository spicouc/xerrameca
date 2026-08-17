from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.adapters.in_memory_identity import InMemoryIdentityAdapter
from xerrameca.app import create_app
from xerrameca.ports.identity import AgentIdentity


def test_x_agent_id_is_not_required(tmp_path: Path) -> None:
    caller = AgentIdentity(
        id="agent-a",
        name="Agent A",
        permissions={"read": True, "write": True, "admin": False},
        allowed_scopes=("shared",),
        capabilities={"xerrameca": True},
    )
    provider = InMemoryIdentityAdapter([caller], {"agent-a": "secret-a"})
    app = create_app(identity=provider, db_path=str(tmp_path / "xerrameca.db"))
    with TestClient(app) as client:
        response = client.post(
            "/v1/xerrameca/command",
            headers={"X-API-Key": "secret-a"},
            json={"command": "/xerrameca help"},
        )
    assert response.status_code == 200
    assert response.json()["action"] == "help"


def test_x_agent_id_hint_cannot_spoof_identity(tmp_path: Path) -> None:
    caller = AgentIdentity(
        id="agent-a",
        name="Agent A",
        permissions={"read": True, "write": True, "admin": False},
        allowed_scopes=("shared",),
        capabilities={},
    )
    provider = InMemoryIdentityAdapter([caller], {"agent-a": "secret-a"})
    app = create_app(identity=provider, db_path=str(tmp_path / "xerrameca.db"))
    with TestClient(app) as client:
        response = client.get(
            "/v1/xerrameca/inbox",
            headers={"X-API-Key": "secret-a", "X-Agent-ID": "agent-b"},
        )
    assert response.status_code == 403
