from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.adapters.in_memory_identity import InMemoryIdentityAdapter
from xerrameca.app import create_app
from xerrameca.db import get_db
from xerrameca.domain.models import ConversationCreateRequest
from xerrameca.ports.identity import AgentIdentity


def identity(agent_id: str, *, admin: bool = False) -> AgentIdentity:
    return AgentIdentity(
        id=agent_id,
        name=agent_id,
        permissions={"read": True, "write": True, "admin": admin},
        allowed_scopes=("shared",),
        capabilities={"xerrameca": True},
        is_active=True,
    )


def test_monitor_is_admin_only(tmp_path: Path) -> None:
    normal = identity("normal")
    admin = identity("admin", admin=True)
    provider = InMemoryIdentityAdapter(
        [normal, admin], {"normal": "normal-key", "admin": "admin-key"}
    )
    app = create_app(identity=provider, db_path=str(tmp_path / "xerrameca.db"))
    with TestClient(app) as client:
        denied = client.get(
            "/v1/xerrameca/monitor/snapshot",
            headers={"X-API-Key": "normal-key"},
        )
        assert denied.status_code == 403

        allowed = client.get(
            "/v1/xerrameca/monitor/snapshot",
            headers={"X-API-Key": "admin-key"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["alert_counts"] == {
            "critical": 0,
            "warning": 0,
            "info": 0,
        }


def test_monitor_detects_near_round_limit_and_stalled_ready_turn(tmp_path: Path) -> None:
    import asyncio

    a = identity("agent-a")
    b = identity("agent-b")
    admin = identity("admin", admin=True)
    provider = InMemoryIdentityAdapter(
        [a, b, admin],
        {"agent-a": "key-a", "agent-b": "key-b", "admin": "admin-key"},
    )
    db_path = str(tmp_path / "xerrameca.db")
    app = create_app(identity=provider, db_path=db_path)

    with TestClient(app) as client:
        engine = app.state.engine
        created = asyncio.run(
            engine.create_conversation(
                a,
                [a, b],
                ConversationCreateRequest(
                    name="Monitor case",
                    objective="Detect stalled turn",
                    participant_agent_ids=[a.id, b.id],
                    first_agent_id=a.id,
                    max_rounds=1,
                    delay_seconds=0,
                ),
            )
        )
        asyncio.run(engine.start_conversation(a, created["id"]))

        async def age_turn() -> None:
            async with get_db(db_path) as db:
                await db.execute(
                    """UPDATE turns SET available_at='2000-01-01T00:00:00.000000Z',
                              created_at='2000-01-01T00:00:00.000000Z'
                       WHERE conversation_id=? AND status='ready'""",
                    (created["id"],),
                )
                await db.commit()

        asyncio.run(age_turn())

        snapshot = client.get(
            "/v1/xerrameca/monitor/snapshot?stalled_after_seconds=30&near_rounds_threshold=1",
            headers={"X-API-Key": "admin-key"},
        )
        assert snapshot.status_code == 200, snapshot.text
        payload = snapshot.json()
        assert payload["status_counts"]["active"] == 1
        assert payload["alert_counts"]["warning"] >= 1
        assert payload["alert_counts"]["info"] >= 1
        alerts = payload["conversations"][0]["alerts"]
        kinds = {alert["type"] for alert in alerts}
        assert "stalled_ready_turn" in kinds
        assert "near_max_rounds" in kinds
