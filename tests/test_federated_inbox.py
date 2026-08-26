from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.node.app import create_node_app
from xerrameca.node.dialogue import FederatedDialogueService
from xerrameca.node.events import EventStore
from xerrameca.node.identity import LOCAL_API_KEY_FILENAME, initialize_node
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
        a_dir, agent_id="agent-a", display_name="Agent A", endpoint="http://node-a:8791"
    )
    b = initialize_node(
        b_dir, agent_id="agent-b", display_name="Agent B", endpoint="http://node-b:8791"
    )
    token = create_invite(a_dir, ttl_seconds=600)
    acceptance, _ = build_acceptance(b_dir, token)
    confirmation = accept_incoming(a_dir, acceptance)
    complete_acceptance(b_dir, token, confirmation)
    return a_dir, b_dir, a, b


def _local_headers(state_dir: Path) -> dict:
    key = (state_dir / LOCAL_API_KEY_FILENAME).read_text(encoding="utf-8").strip()
    return {"X-API-Key": key, "Content-Type": "application/json"}


def _local_post(app, state_dir: Path, path: str, payload: dict):
    with TestClient(app) as client:
        return client.post(path, json=payload, headers=_local_headers(state_dir))


def _replicate(source_dir, dest_dir, conv_id, epoch=1):
    from xerrameca.node.replication import ReplicationService

    source = EventStore(source_dir)
    dest = ReplicationService(dest_dir)
    try:
        from_seq = dest.store.get_head(conv_id).last_sequence + 1
    except Exception:
        from_seq = 1
    events = source.list_events(conv_id, epoch=epoch, from_sequence=from_seq)
    if events:
        dest.receive_events(
            sender_node_id=load_node_state(source_dir).node_id,
            raw_events=[e.to_dict() for e in events],
        )


def load_node_state(d):
    from xerrameca.node.identity import load_node_state as _l

    return _l(d)


def test_federated_inbox_returns_only_local_actionable_turn(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    b_app = create_node_app(str(b_dir))
    a_dlg = FederatedDialogueService(str(a_dir))
    b_dlg = FederatedDialogueService(str(b_dir))

    # Create conversation on A (coordinator) using local agent auth.
    create = _local_post(
        a_app,
        a_dir,
        "/v1/node/federation/conversations",
        {
            "peer_node_id": b.node_id,
            "objective": "inbox smoke",
            "name": "Inbox Smoke",
            "max_rounds": 2,
            "turn_timeout_seconds": 60,
            "delay_seconds": 0,
        },
    )
    assert create.status_code == 200, create.text
    conv_id = create.json()["id"]

    # Initial inbox: A (coordinator, slot 1) sees exactly 1 turn; B sees 0.
    with TestClient(a_app) as c:
        a_inbox = c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
        assert a_inbox.status_code == 200
        a_turns = a_inbox.json()["turns"]
        assert a_inbox.json()["count"] == 1
        t = a_turns[0]
        assert t["conversation_id"] == conv_id
        assert t["turn_id"] is not None
        assert t["name"] == "Inbox Smoke"
        assert t["objective"] == "inbox smoke"
        assert t["round"] == 1
        assert t["max_rounds"] == 2
        assert t["phase"] == "dialogue"

    with TestClient(b_app) as c:
        b_inbox = c.get("/v1/node/federation/inbox", headers=_local_headers(b_dir))
        assert b_inbox.status_code == 200
        assert b_inbox.json()["count"] == 0

    # Inbox is read-only: no events appended by merely polling.
    events_before = len(EventStore(a_dir).list_events(conv_id))
    with TestClient(a_app) as c:
        c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
    events_after = len(EventStore(a_dir).list_events(conv_id))
    assert events_before == events_after

    # A advances its turn via the service (bypass HTTP lease middleware),
    # then replicate to B and confirm B's inbox now shows the turn.
    a_dlg.claim(conv_id, expected_epoch=1, claimant_node_id=a.node_id)
    a_dlg.reply(conv_id, expected_epoch=1, result="continue", content="ack A", author_node_id=a.node_id)
    _replicate(a_dir, b_dir, conv_id)

    with TestClient(b_app) as c:
        b_inbox2 = c.get("/v1/node/federation/inbox", headers=_local_headers(b_dir))
        assert b_inbox2.status_code == 200
        assert b_inbox2.json()["count"] == 1
        assert b_inbox2.json()["turns"][0]["conversation_id"] == conv_id
        assert b_inbox2.json()["turns"][0]["round"] == 1
        assert b_inbox2.json()["turns"][0]["phase"] == "dialogue"


def test_federated_inbox_excludes_future_turns(tmp_path: Path) -> None:
    """A turn whose available_at is in the future must not appear in pending_turns."""
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    a_dlg = FederatedDialogueService(str(a_dir))
    create = _local_post(
        a_app,
        a_dir,
        "/v1/node/federation/conversations",
        {
            "peer_node_id": b.node_id,
            "objective": "future",
            "name": "Future",
            "max_rounds": 2,
            "turn_timeout_seconds": 60,
            "delay_seconds": 0,
        },
    )
    assert create.status_code == 200, create.text
    conv_id = create.json()["id"]
    # At now=0 (epoch start) the turn's available_at (>=now) is in the future.
    pending = a_dlg.pending_turns(now=0)
    assert all(v.id != conv_id for v in pending)
    # At the real current time the turn is actionable.
    assert len(a_dlg.pending_turns()) >= 1
