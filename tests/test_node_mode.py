from __future__ import annotations

import json
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.node.app import create_node_app
from xerrameca.node.identity import (
    LOCAL_API_KEY_FILENAME,
    LOCAL_IDENTITIES_FILENAME,
    PRIVATE_KEY_FILENAME,
    initialize_node,
    load_node_state,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_two_nodes_have_unique_durable_identity_without_pluribus(tmp_path: Path) -> None:
    a_dir = tmp_path / "node-a"
    b_dir = tmp_path / "node-b"

    a = initialize_node(
        a_dir,
        agent_id="agent-a",
        display_name="Agent A",
        endpoint="http://127.0.0.1:8801",
    )
    b = initialize_node(
        b_dir,
        agent_id="agent-b",
        display_name="Agent B",
        endpoint="http://127.0.0.1:8802",
    )

    assert a.node_id != b.node_id
    assert a.public_key != b.public_key
    assert load_node_state(a_dir) == a
    assert load_node_state(b_dir) == b

    assert _mode(a_dir / PRIVATE_KEY_FILENAME) == 0o600
    assert _mode(a_dir / LOCAL_API_KEY_FILENAME) == 0o600
    assert _mode(a_dir / LOCAL_IDENTITIES_FILENAME) == 0o600

    public_state = json.loads((a_dir / "node.json").read_text(encoding="utf-8"))
    assert "private" not in json.dumps(public_state).lower()
    assert "api_key" not in json.dumps(public_state).lower()


def test_node_app_exposes_public_identity_and_survives_restart(tmp_path: Path) -> None:
    state_dir = tmp_path / "node"
    created = initialize_node(
        state_dir,
        agent_id="agent-a",
        display_name="Agent A",
        endpoint="http://127.0.0.1:8791",
    )

    app = create_node_app(str(state_dir))
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["identity_provider"] == "local-node"

        identity = client.get("/v1/node/identity")
        assert identity.status_code == 200
        assert identity.json() == created.public_dict()
        serialized = json.dumps(identity.json()).lower()
        assert "private" not in serialized
        assert "api_key" not in serialized

    restarted = create_node_app(str(state_dir))
    with TestClient(restarted) as client:
        identity = client.get("/v1/node/identity")
        assert identity.json()["node_id"] == created.node_id
        assert identity.json()["public_key"] == created.public_key

    assert load_node_state(state_dir).node_id == created.node_id


def test_node_local_agent_key_authenticates_only_locally(tmp_path: Path) -> None:
    state_dir = tmp_path / "node"
    created = initialize_node(
        state_dir,
        agent_id="agent-a",
        display_name="Agent A",
        endpoint="http://127.0.0.1:8791",
    )
    local_key = (state_dir / LOCAL_API_KEY_FILENAME).read_text(encoding="utf-8").strip()

    app = create_node_app(str(state_dir))
    with TestClient(app) as client:
        agents = client.post(
            "/v1/xerrameca/command",
            headers={"X-API-Key": local_key},
            json={"command": "/xerrameca agents"},
        )
        assert agents.status_code == 200
        assert agents.json()["agents"] == []

        wrong = client.get(
            "/v1/xerrameca/inbox", headers={"X-API-Key": "not-the-key"}
        )
        assert wrong.status_code == 403

    assert created.db_path == str(state_dir / "xerrameca.db")
