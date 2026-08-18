from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.node.app import create_node_app
from xerrameca.node.dialogue import FederatedDialogueService, project_conversation
from xerrameca.node.events import EventStore
from xerrameca.node.identity import initialize_node
from xerrameca.node.replication import ReplicationService, canonical_json_bytes
from xerrameca.node.trust import (
    accept_incoming,
    build_acceptance,
    complete_acceptance,
    create_invite,
    sign_peer_request,
)


def _trusted_nodes(tmp_path: Path):
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a = initialize_node(
        a_dir,
        agent_id="agent-a",
        display_name="Agent A",
        endpoint="http://node-a:8791",
    )
    b = initialize_node(
        b_dir,
        agent_id="agent-b",
        display_name="Agent B",
        endpoint="http://node-b:8791",
    )
    token = create_invite(a_dir, ttl_seconds=600)
    acceptance, _ = build_acceptance(b_dir, token)
    confirmation = accept_incoming(a_dir, acceptance)
    complete_acceptance(b_dir, token, confirmation)
    return a_dir, b_dir, a, b


def _replicate_missing(
    source_dir: Path,
    destination_dir: Path,
    *,
    source_node_id: str,
    conversation_id: str,
    epoch: int = 1,
) -> None:
    source = EventStore(source_dir)
    destination = ReplicationService(destination_dir)
    try:
        from_sequence = destination.store.get_head(conversation_id).last_sequence + 1
    except Exception:
        from_sequence = 1
    events = source.list_events(
        conversation_id,
        epoch=epoch,
        from_sequence=from_sequence,
    )
    if events:
        destination.receive_events(
            sender_node_id=source_node_id,
            raw_events=[event.to_dict() for event in events],
        )


def _signed_post(
    state_dir: Path,
    app,
    path: str,
    payload: dict,
):
    body = canonical_json_bytes(payload)
    headers = {
        **sign_peer_request(
            state_dir,
            method="POST",
            path=path,
            body=body,
        ),
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        return client.post(path, content=body, headers=headers)


def test_two_nodes_complete_consensus_dialogue_and_restart_with_identical_history(
    tmp_path: Path,
) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    dialogue_a = FederatedDialogueService(a_dir)
    dialogue_b = FederatedDialogueService(b_dir)

    created = dialogue_a.create(
        b.node_id,
        objective="Reach a federated consensus",
        max_rounds=3,
        delay_seconds=0,
    )
    conversation_id = created.id
    assert created.coordinator_id == a.node_id
    assert created.coordinator_epoch == 1
    assert created.current_turn["assigned_node_id"] == a.node_id

    _replicate_missing(
        a_dir,
        b_dir,
        source_node_id=a.node_id,
        conversation_id=conversation_id,
    )
    assert dialogue_b.get(conversation_id).current_turn["assigned_node_id"] == a.node_id

    dialogue_a.claim(
        conversation_id,
        claimant_node_id=a.node_id,
        expected_epoch=1,
    )
    dialogue_a.reply(
        conversation_id,
        author_node_id=a.node_id,
        content="A proposes the event-log design.",
        result="continue",
        expected_epoch=1,
    )
    _replicate_missing(
        a_dir,
        b_dir,
        source_node_id=a.node_id,
        conversation_id=conversation_id,
    )
    assert dialogue_b.get(conversation_id).current_turn["assigned_node_id"] == b.node_id

    app_a = create_node_app(str(a_dir))
    claim_path = "/v1/node/federation/dialogue/claim"
    claimed = _signed_post(
        b_dir,
        app_a,
        claim_path,
        {"conversation_id": conversation_id, "expected_epoch": 1},
    )
    assert claimed.status_code == 200, claimed.text
    EventStore(b_dir).ingest_many(
        [
            __import__("xerrameca.node.events", fromlist=["EventEnvelope"]).EventEnvelope.from_dict(row)
            for row in claimed.json()["events"]
        ]
    )
    assert dialogue_b.get(conversation_id).current_turn["claimed_by_node_id"] == b.node_id

    reply_path = "/v1/node/federation/dialogue/reply"
    proposed = _signed_post(
        b_dir,
        app_a,
        reply_path,
        {
            "conversation_id": conversation_id,
            "expected_epoch": 1,
            "content": "B agrees and proposes completion.",
            "result": "complete",
        },
    )
    assert proposed.status_code == 200, proposed.text
    EventStore(b_dir).ingest_many(
        [
            __import__("xerrameca.node.events", fromlist=["EventEnvelope"]).EventEnvelope.from_dict(row)
            for row in proposed.json()["events"]
        ]
    )

    proposal_view = dialogue_a.get(conversation_id)
    assert proposal_view.completion_proposal["by_node_id"] == b.node_id
    assert proposal_view.current_turn["assigned_node_id"] == a.node_id
    assert proposal_view.current_turn["phase"] == "completion_confirmation"

    dialogue_a.claim(
        conversation_id,
        claimant_node_id=a.node_id,
        expected_epoch=1,
    )
    completed = dialogue_a.reply(
        conversation_id,
        author_node_id=a.node_id,
        content="A confirms completion.",
        result="complete",
        expected_epoch=1,
    )
    assert completed.status == "completed"
    _replicate_missing(
        a_dir,
        b_dir,
        source_node_id=a.node_id,
        conversation_id=conversation_id,
    )

    view_a = dialogue_a.get(conversation_id)
    view_b = dialogue_b.get(conversation_id)
    assert view_a.status == view_b.status == "completed"
    assert [message["content"] for message in view_a.messages] == [
        "A proposes the event-log design.",
        "B agrees and proposes completion.",
        "A confirms completion.",
    ]
    assert view_a.to_dict() == view_b.to_dict()

    history_a = [
        event.to_dict() for event in EventStore(a_dir).list_events(conversation_id)
    ]
    history_b = [
        event.to_dict() for event in EventStore(b_dir).list_events(conversation_id)
    ]
    assert history_a == history_b

    # Node/process restart: identity, local DB and projected history survive.
    restarted_b = FederatedDialogueService(b_dir)
    assert restarted_b.get(conversation_id).to_dict() == view_a.to_dict()
    assert [
        event.to_dict() for event in restarted_b.store.list_events(conversation_id)
    ] == history_a


def test_max_rounds_blocks_after_second_slot_continue(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(
        b.node_id,
        objective="One round only",
        max_rounds=1,
        delay_seconds=0,
    )
    cid = view.id

    dialogue.claim(cid, claimant_node_id=a.node_id, expected_epoch=1)
    dialogue.reply(
        cid,
        author_node_id=a.node_id,
        content="A",
        result="continue",
        expected_epoch=1,
    )
    dialogue.claim(cid, claimant_node_id=b.node_id, expected_epoch=1)
    blocked = dialogue.reply(
        cid,
        author_node_id=b.node_id,
        content="B",
        result="continue",
        expected_epoch=1,
    )
    assert blocked.status == "blocked"
    assert blocked.block_reason == "max_rounds"
    assert blocked.current_turn is None


def test_completion_rejection_preserves_standalone_turn_semantics(tmp_path: Path) -> None:
    a_dir, _, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(
        b.node_id,
        objective="Test rejection",
        max_rounds=3,
        delay_seconds=0,
    )
    cid = view.id

    dialogue.claim(cid, claimant_node_id=a.node_id, expected_epoch=1)
    dialogue.reply(
        cid,
        author_node_id=a.node_id,
        content="A continues",
        result="continue",
        expected_epoch=1,
    )
    dialogue.claim(cid, claimant_node_id=b.node_id, expected_epoch=1)
    dialogue.reply(
        cid,
        author_node_id=b.node_id,
        content="B proposes complete",
        result="complete",
        expected_epoch=1,
    )
    dialogue.claim(cid, claimant_node_id=a.node_id, expected_epoch=1)
    continued = dialogue.reply(
        cid,
        author_node_id=a.node_id,
        content="A rejects completion",
        result="continue",
        expected_epoch=1,
    )

    assert continued.completion_proposal is None
    # Standalone dialogue-v1 semantics: a rejected slot-2 proposal consumes the
    # confirmer's next-round slot, therefore the second participant goes next.
    assert continued.current_turn["assigned_node_id"] == b.node_id
    assert continued.current_turn["round"] == 2
    assert continued.current_turn["slot"] == 2


def test_projection_contains_no_api_key_or_private_key_material(tmp_path: Path) -> None:
    a_dir, _, _, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="No secrets", delay_seconds=0)
    raw = str(view.to_dict()).lower()
    assert "api_key" not in raw
    assert "private_key" not in raw

    projected = project_conversation(dialogue.store.list_events(view.id))
    assert projected.to_dict() == view.to_dict()
