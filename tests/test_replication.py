from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.node.app import create_node_app
from xerrameca.node.events import EventStore
from xerrameca.node.identity import initialize_node
from xerrameca.node.replication import ReplicationService, canonical_json_bytes
from xerrameca.node.trust import (
    accept_incoming,
    build_acceptance,
    complete_acceptance,
    create_invite,
    revoke_peer,
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


def _signed_json_headers(state_dir: Path, path: str, body: bytes) -> dict[str, str]:
    return {
        **sign_peer_request(state_dir, method="POST", path=path, body=body),
        "Content-Type": "application/json",
    }


def test_signed_push_is_idempotent_and_returns_contiguous_ack(tmp_path: Path) -> None:
    a_dir, b_dir, a, _ = _trusted_nodes(tmp_path)
    store_a = EventStore(a_dir)
    store_b = EventStore(b_dir)

    first = store_a.append_local(
        "conv-1",
        author_id=a.node_id,
        event_type="conversation.created",
        payload={"objective": "Replicate"},
    )
    second = store_a.append_local(
        "conv-1",
        author_id=a.node_id,
        event_type="turn.opened",
        payload={"assigned_node_id": a.node_id, "round": 1},
        expected_epoch=1,
    )

    path = "/v1/node/federation/events"
    body = canonical_json_bytes({"events": [first.to_dict(), second.to_dict()]})
    headers = _signed_json_headers(a_dir, path, body)

    app_b = create_node_app(str(b_dir))
    with TestClient(app_b) as client:
        response = client.post(path, content=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["acked_sequence"] == 2
        assert response.json()["inserted"] == 2

        replay = client.post(path, content=body, headers=headers)
        assert replay.status_code == 200, replay.text
        assert replay.json()["acked_sequence"] == 2
        assert replay.json()["inserted"] == 0

    assert [event.to_dict() for event in store_b.list_events("conv-1")] == [
        first.to_dict(),
        second.to_dict(),
    ]


def test_explicit_ack_is_monotonic_on_sender_cursor(tmp_path: Path) -> None:
    a_dir, b_dir, _, b = _trusted_nodes(tmp_path)
    store_a = EventStore(a_dir)
    path = "/v1/node/federation/ack"

    app_a = create_node_app(str(a_dir))
    with TestClient(app_a) as client:
        body = canonical_json_bytes(
            {
                "conversation_id": "conv-1",
                "coordinator_epoch": 1,
                "acked_sequence": 4,
            }
        )
        response = client.post(
            path,
            content=body,
            headers=_signed_json_headers(b_dir, path, body),
        )
        assert response.status_code == 200, response.text
        assert response.json()["acked_sequence"] == 4

        lower = canonical_json_bytes(
            {
                "conversation_id": "conv-1",
                "coordinator_epoch": 1,
                "acked_sequence": 2,
            }
        )
        response = client.post(
            path,
            content=lower,
            headers=_signed_json_headers(b_dir, path, lower),
        )
        assert response.status_code == 200, response.text
        assert response.json()["acked_sequence"] == 4

    assert store_a.cursor(b.node_id, "conv-1", 1) == 4


def test_catch_up_fetches_only_missing_sequence_range(tmp_path: Path) -> None:
    a_dir, b_dir, a, _ = _trusted_nodes(tmp_path)
    store_a = EventStore(a_dir)
    replication_b = ReplicationService(b_dir)

    events = [
        store_a.append_local(
            "conv-1",
            author_id=a.node_id,
            event_type="conversation.created",
            payload={},
        )
    ]
    for sequence in range(2, 6):
        events.append(
            store_a.append_local(
                "conv-1",
                author_id=a.node_id,
                event_type="message.recorded",
                payload={"logical_sequence": sequence},
                expected_epoch=1,
            )
        )

    replication_b.receive_events(
        sender_node_id=a.node_id,
        raw_events=[event.to_dict() for event in events[:2]],
    )
    assert replication_b.store.get_head("conv-1").last_sequence == 2

    path = "/v1/node/federation/conversations/conv-1/events"
    headers = sign_peer_request(
        b_dir,
        method="GET",
        path=path,
        body=b"",
    )
    app_a = create_node_app(str(a_dir))
    with TestClient(app_a) as client:
        response = client.get(
            path,
            params={"epoch": 1, "from_sequence": 3, "to_sequence": 5},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    rows = response.json()["events"]
    assert [row["sequence"] for row in rows] == [3, 4, 5]

    ack = replication_b.receive_events(sender_node_id=a.node_id, raw_events=rows)
    assert ack.acked_sequence == 5
    assert [event.to_dict() for event in replication_b.store.list_events("conv-1")] == [
        event.to_dict() for event in events
    ]


def test_revoked_peer_cannot_read_federated_event_range(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    store_a = EventStore(a_dir)
    store_a.append_local(
        "conv-1",
        author_id=a.node_id,
        event_type="conversation.created",
        payload={},
    )

    path = "/v1/node/federation/conversations/conv-1/events"
    revoke_peer(a_dir, b.node_id)
    headers = sign_peer_request(
        b_dir,
        method="GET",
        path=path,
        body=b"",
    )

    app_a = create_node_app(str(a_dir))
    with TestClient(app_a) as client:
        response = client.get(
            path,
            params={"epoch": 1, "from_sequence": 1},
            headers=headers,
        )
    assert response.status_code == 403


def test_replication_endpoint_rejects_unsigned_body_mutation(tmp_path: Path) -> None:
    a_dir, b_dir, a, _ = _trusted_nodes(tmp_path)
    store_a = EventStore(a_dir)
    event = store_a.append_local(
        "conv-1",
        author_id=a.node_id,
        event_type="conversation.created",
        payload={"value": "original"},
    )
    path = "/v1/node/federation/events"
    original = canonical_json_bytes({"events": [event.to_dict()]})
    headers = _signed_json_headers(a_dir, path, original)

    mutated_payload = event.to_dict()
    mutated_payload["payload"] = {"value": "mutated"}
    mutated = canonical_json_bytes({"events": [mutated_payload]})

    app_b = create_node_app(str(b_dir))
    with TestClient(app_b) as client:
        response = client.post(path, content=mutated, headers=headers)
    assert response.status_code == 403
