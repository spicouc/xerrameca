from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.dashboard import create_dashboard_app
from xerrameca.node.dialogue import FederatedDialogueService
from xerrameca.node.identity import initialize_node
from xerrameca.node.trust import (
    accept_incoming,
    build_acceptance,
    complete_acceptance,
    create_invite,
)


def _trusted_nodes(tmp_path: Path):
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a = initialize_node(
        a_dir,
        agent_id="agent-a",
        display_name="Agent A",
        endpoint="http://node-a.invalid:8791",
    )
    b = initialize_node(
        b_dir,
        agent_id="agent-b",
        display_name="Agent B",
        endpoint="http://node-b.invalid:8791",
    )
    token = create_invite(a_dir, ttl_seconds=600)
    acceptance, _ = build_acceptance(b_dir, token)
    confirmation = accept_incoming(a_dir, acceptance)
    complete_acceptance(b_dir, token, confirmation)
    return a_dir, b_dir, a, b


def test_dashboard_reconstructs_node_conversation_metrics_and_timeline(tmp_path: Path) -> None:
    a_dir, _, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(
        b.node_id,
        objective="Observe this conversation",
        max_rounds=3,
        delay_seconds=0,
    )
    dialogue.claim(view.id, claimant_node_id=a.node_id, expected_epoch=1)
    dialogue.reply(
        view.id,
        author_node_id=a.node_id,
        content="First observable reply",
        result="continue",
        expected_epoch=1,
    )

    app = create_dashboard_app(a_dir)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["service"] == "xerrameca-dashboard"

        summary = client.get("/api/summary")
        assert summary.status_code == 200
        payload = summary.json()
        assert payload["node"]["node_id"] == a.node_id
        assert len(payload["conversations"]) == 1
        assert payload["conversations"][0]["conversation"]["id"] == view.id
        assert "response_latency" in payload["conversations"][0]["metrics"]

        detail = client.get(f"/api/conversations/{view.id}")
        assert detail.status_code == 200
        assert detail.json()["events"]
        assert any(
            event["event_type"] == "reply.recorded"
            for event in detail.json()["events"]
        )

        index = client.get("/")
        assert index.status_code == 200
        assert "Xerrameca Dashboard" in index.text


def test_dashboard_is_not_required_for_dialogue_progress(tmp_path: Path) -> None:
    a_dir, _, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(
        b.node_id,
        objective="Dashboard may disappear",
        max_rounds=2,
        delay_seconds=0,
    )

    # Start and stop a dashboard client; the dialogue service owns independent
    # durable state and continues after the UI process is gone.
    with TestClient(create_dashboard_app(a_dir)) as client:
        assert client.get("/health").status_code == 200

    dialogue.claim(view.id, claimant_node_id=a.node_id, expected_epoch=1)
    progressed = dialogue.reply(
        view.id,
        author_node_id=a.node_id,
        content="Progress after dashboard shutdown",
        result="continue",
        expected_epoch=1,
    )
    assert progressed.status == "active"
    assert progressed.messages[-1]["content"] == "Progress after dashboard shutdown"
