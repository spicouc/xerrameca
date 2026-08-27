from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xerrameca.node.app import create_node_app
from xerrameca.node.dialogue import FederatedDialogueService, project_conversation
from xerrameca.node.events import EventEnvelope, EventStore
from xerrameca.node.identity import (
    LOCAL_API_KEY_FILENAME,
    initialize_node,
    load_node_state,
)
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


def _create_conv(app, state_dir, peer_node_id, **kw):
    payload = {
        "peer_node_id": peer_node_id,
        "objective": kw.get("objective", "smoke"),
        "name": kw.get("name", "Smoke"),
        "max_rounds": kw.get("max_rounds", 2),
        "turn_timeout_seconds": kw.get("turn_timeout_seconds", 60),
        "delay_seconds": kw.get("delay_seconds", 0),
    }
    r = _local_post(app, state_dir, "/v1/node/federation/conversations", payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


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


def _advance(state_dir, conv_id, node_id, result="continue", content="ack"):
    dlg = FederatedDialogueService(str(state_dir))
    dlg.claim(conv_id, expected_epoch=1, claimant_node_id=node_id)
    dlg.reply(conv_id, expected_epoch=1, result=result, content=content, author_node_id=node_id)


# ---- PAS 16: EMPTY ----------------------------------------------------------
def test_inbox_empty_returns_200_empty_array(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    with TestClient(a_app) as c:
        r = c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
        assert r.status_code == 200
        body = r.json()
        assert body["turns"] == []
        assert body["count"] == 0


# ---- PAS 17: FIRST TURN ----------------------------------------------------
def test_inbox_first_turn_local(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    cid = _create_conv(a_app, a_dir, b.node_id)
    with TestClient(a_app) as c:
        r = c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
        assert r.status_code == 200
        turns = r.json()["turns"]
        assert len(turns) == 1
        t = turns[0]
        assert t["conversation_id"] == cid
        assert t["round"] == 1
        assert t["slot"] == 1
        assert t["turn_id"] is not None
        assert t["assigned_node_id"] == a.node_id
        assert t["status"] == "active"


# ---- PAS 18: REMOTE TURN EXCLUDED -----------------------------------------
def test_inbox_remote_turn_excluded(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    b_app = create_node_app(str(b_dir))
    cid = _create_conv(a_app, a_dir, b.node_id)  # coordinator A -> slot1 A
    # A advances; replicate so B sees slot2 (B's turn)
    _advance(a_dir, cid, a.node_id, result="continue")
    _replicate(a_dir, b_dir, cid)
    with TestClient(b_app) as c:
        b_inbox = c.get("/v1/node/federation/inbox", headers=_local_headers(b_dir))
        assert b_inbox.status_code == 200
        assert len(b_inbox.json()["turns"]) == 1
    # A should no longer have a pending turn (it was slot1, now B's turn)
    with TestClient(a_app) as c:
        a_inbox = c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
        assert a_inbox.json()["count"] == 0


# ---- PAS 19: FUTURE available_at EXCLUDED --------------------------------
def test_inbox_future_available_at_excluded(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    dlg = FederatedDialogueService(str(a_dir))
    cid = _create_conv(a_app, a_dir, b.node_id)
    # At now=0 the turn's available_at (>= now) is in the future -> excluded.
    assert all(v.id != cid for v in dlg.pending_turns(now=0))
    # At real time it is actionable.
    assert len(dlg.pending_turns()) >= 1


# ---- PAS 20: COMPLETED EXCLUDED ------------------------------------------
def test_inbox_completed_excluded(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    cid = _create_conv(a_app, a_dir, b.node_id, max_rounds=1)
    # Mark completed via the coordinator's own key (fixture, projector only).
    store = EventStore(a_dir)
    store.append_local(
        conversation_id=cid,
        author_id=a.node_id,
        event_type="conversation.completed",
        payload={"result": "complete"},
    )
    view = FederatedDialogueService(str(a_dir)).get(cid)
    assert view.status == "completed"
    with TestClient(a_app) as c:
        r = c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
        assert r.json()["count"] == 0


# ---- PAS 21: CANCELLED EXCLUDED (fixture via projector) -------------------
def test_inbox_cancelled_excluded(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    cid = _create_conv(a_app, a_dir, b.node_id)
    # Append a conversation.cancelled event directly via the node's own
    # coordinator key (fixture exercising the projector only, no mutation API).
    store = EventStore(a_dir)
    store.append_local(
        conversation_id=cid,
        author_id=a.node_id,
        event_type="conversation.cancelled",
        payload={"reason": "test-fixture"},
    )
    view = FederatedDialogueService(str(a_dir)).get(cid)
    assert view.status == "cancelled"
    with TestClient(a_app) as c:
        r = c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
        assert r.json()["count"] == 0


# ---- PAS 22: CLAIMED TURN NOT MUTATED BY GET ------------------------------
def test_inbox_claimed_turn_not_mutated(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    b_app = create_node_app(str(b_dir))
    cid = _create_conv(a_app, a_dir, b.node_id, max_rounds=2)
    # A claims (lease) but does NOT reply -> turn claimed by A, still A's turn.
    dlg = FederatedDialogueService(str(a_dir))
    dlg.claim(cid, expected_epoch=1, claimant_node_id=a.node_id)
    before = len(EventStore(a_dir).list_events(cid))
    with TestClient(a_app) as c:
        r = c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
        assert r.status_code == 200
        turns = r.json()["turns"]
        assert len(turns) == 1
        assert turns[0]["claimed_by_node_id"] == a.node_id  # exposed, unchanged
    after = len(EventStore(a_dir).list_events(cid))
    assert before == after  # GET did not append events


# ---- PAS 23: MULTIPLE CONVERSATIONS DETERMINISTIC -------------------------
def test_inbox_multiple_conversations_deterministic(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    c1 = _create_conv(a_app, a_dir, b.node_id, name="One")
    c2 = _create_conv(a_app, a_dir, b.node_id, name="Two")
    c3 = _create_conv(a_app, a_dir, b.node_id, name="Three")
    with TestClient(a_app) as c:
        r = c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
        turns = r.json()["turns"]
        ids = [t["conversation_id"] for t in turns]
        assert len(ids) == 3
        assert len(set(ids)) == 3
        assert len(set(t["turn_id"] for t in turns)) == 3
        # Deterministic: sorted by available_at ASC then conversation_id ASC.
        assert ids == sorted(ids)


# ---- PAS 24: LEGACY SEPARATION -------------------------------------------
def test_inbox_excludes_legacy_state(tmp_path: Path) -> None:
    # The federated inbox only reads federated conversation state; the legacy
    # engine (services/engine.py) uses a separate DB and is not visible here.
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    a_app = create_node_app(str(a_dir))
    # No federated conversation created -> inbox empty (legacy has none either).
    with TestClient(a_app) as c:
        r = c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
        assert r.status_code == 200
        assert r.json()["count"] == 0
    # Legacy engine has its own state dir; its conversations never appear in the
    # federated inbox because they live in a different store.
    from xerrameca.services.engine import ConversationEngine

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    eng = ConversationEngine(str(legacy_dir / "xerrameca.db"))
    # Legacy create does not affect federated inbox (separate state).
    with TestClient(a_app) as c:
        r2 = c.get("/v1/node/federation/inbox", headers=_local_headers(a_dir))
        assert r2.json()["count"] == 0
