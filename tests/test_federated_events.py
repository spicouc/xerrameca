from __future__ import annotations

from pathlib import Path

import pytest

from xerrameca.domain.errors import ConflictError, ForbiddenError
from xerrameca.node.events import EventEnvelope, EventStore
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


def test_signed_event_log_replicates_and_is_idempotent(tmp_path: Path) -> None:
    a_dir, b_dir, a, _ = _trusted_nodes(tmp_path)
    store_a = EventStore(a_dir)
    store_b = EventStore(b_dir)

    created = store_a.append_local(
        "conv-1",
        author_id=a.node_id,
        event_type="conversation.created",
        payload={"objective": "Design the protocol", "participants": [a.node_id]},
    )
    opened = store_a.append_local(
        "conv-1",
        author_id=a.node_id,
        event_type="turn.opened",
        payload={"assigned_node_id": a.node_id, "round": 1},
        expected_epoch=1,
    )

    assert created.sequence == 1
    assert opened.sequence == 2
    assert created.coordinator_epoch == opened.coordinator_epoch == 1

    assert store_b.ingest_many([created, opened]) == 2
    assert store_b.ingest(created) is False
    assert [event.to_dict() for event in store_b.list_events("conv-1")] == [
        event.to_dict() for event in store_a.list_events("conv-1")
    ]


def test_tampered_event_signature_is_rejected(tmp_path: Path) -> None:
    a_dir, b_dir, a, _ = _trusted_nodes(tmp_path)
    store_a = EventStore(a_dir)
    store_b = EventStore(b_dir)

    valid = store_a.append_local(
        "conv-1",
        author_id=a.node_id,
        event_type="conversation.created",
        payload={"objective": "Original"},
    )
    tampered = EventEnvelope.from_dict(
        {**valid.to_dict(), "payload": {"objective": "Tampered"}}
    )

    with pytest.raises(ForbiddenError):
        store_b.ingest(tampered)
    assert store_b.list_events("conv-1") == []


def test_ack_cursor_is_monotonic_and_idempotent(tmp_path: Path) -> None:
    a_dir, _, _, b = _trusted_nodes(tmp_path)
    store = EventStore(a_dir)

    assert store.cursor(b.node_id, "conv-1", 1) == 0
    assert store.ack(b.node_id, "conv-1", 1, 3) == 3
    assert store.ack(b.node_id, "conv-1", 1, 3) == 3
    assert store.ack(b.node_id, "conv-1", 1, 2) == 3
    assert store.ack(b.node_id, "conv-1", 1, 5) == 5
    assert store.cursor(b.node_id, "conv-1", 1) == 5


def test_bounded_sequence_range_catch_up(tmp_path: Path) -> None:
    a_dir, _, a, _ = _trusted_nodes(tmp_path)
    store = EventStore(a_dir)
    store.append_local(
        "conv-1",
        author_id=a.node_id,
        event_type="conversation.created",
        payload={},
    )
    for index in range(2, 7):
        store.append_local(
            "conv-1",
            author_id=a.node_id,
            event_type="message.recorded",
            payload={"index": index},
            expected_epoch=1,
        )

    events = store.list_events(
        "conv-1", epoch=1, from_sequence=3, to_sequence=5
    )
    assert [event.sequence for event in events] == [3, 4, 5]


def test_coordinator_epoch_fences_stale_writer_and_histories_converge(
    tmp_path: Path,
) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    store_a = EventStore(a_dir)
    store_b = EventStore(b_dir)

    first = store_a.append_local(
        "conv-1",
        author_id=a.node_id,
        event_type="conversation.created",
        payload={"participants": [a.node_id, b.node_id]},
    )
    second = store_a.append_local(
        "conv-1",
        author_id=a.node_id,
        event_type="turn.opened",
        payload={"assigned_node_id": a.node_id, "round": 1},
        expected_epoch=1,
    )
    store_b.ingest_many([first, second])

    epoch_two = store_b.advance_epoch(
        "conv-1",
        previous_epoch=1,
        previous_coordinator_id=a.node_id,
        reason="controlled-test-transfer",
    )
    assert epoch_two.coordinator_epoch == 2
    assert epoch_two.sequence == 1
    assert epoch_two.coordinator_id == b.node_id

    store_a.ingest(epoch_two)
    assert store_a.get_head("conv-1").coordinator_epoch == 2
    assert store_a.get_head("conv-1").coordinator_id == b.node_id

    with pytest.raises(ConflictError):
        store_a.append_local(
            "conv-1",
            author_id=a.node_id,
            event_type="message.recorded",
            payload={"stale": True},
            expected_epoch=1,
        )

    epoch_two_message = store_b.append_local(
        "conv-1",
        author_id=b.node_id,
        event_type="message.recorded",
        payload={"fresh": True},
        expected_epoch=2,
    )
    store_a.ingest(epoch_two_message)

    assert [event.to_dict() for event in store_a.list_events("conv-1")] == [
        event.to_dict() for event in store_b.list_events("conv-1")
    ]
