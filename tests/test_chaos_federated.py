from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from xerrameca.domain.errors import ConflictError, ForbiddenError, ValidationError
from xerrameca.node.dialogue import FederatedDialogueService
from xerrameca.node.events import EventEnvelope, EventStore
from xerrameca.node.failover import FailoverManager, LeasedDialogueService
from xerrameca.node.identity import LOCAL_API_KEY_FILENAME, initialize_node
from xerrameca.node.replication import ReplicationService
from xerrameca.node.trust import (
    accept_incoming,
    build_acceptance,
    complete_acceptance,
    create_invite,
    revoke_peer,
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


def _events(store: EventStore, cid: str, epoch: int = 1, start: int = 1):
    return store.list_events(cid, epoch=epoch, from_sequence=start)


def _ingest(destination: Path, sender: str, events: list[EventEnvelope]):
    return ReplicationService(destination).receive_events(
        sender_node_id=sender,
        raw_events=[event.to_dict() for event in events],
    )


def _ack_sender(sender_dir: Path, peer_id: str, cid: str, epoch: int, sequence: int):
    return EventStore(sender_dir).ack(peer_id, cid, epoch, sequence)


def _quick_check(path: str) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute("PRAGMA quick_check").fetchone()
    return str(row[0])


def test_ack_loss_then_duplicate_retry_converges_without_duplicate_state(
    tmp_path: Path,
) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="ACK loss", delay_seconds=0)
    cid = view.id
    store_a = EventStore(a_dir)
    store_b = EventStore(b_dir)

    original = _events(store_a, cid)
    first_delivery = _ingest(b_dir, a.node_id, original)
    assert first_delivery.inserted == len(original)
    assert first_delivery.acked_sequence == original[-1].sequence

    # Simulate the ACK packet being lost: A's cursor remains at zero although B
    # durably owns the complete range.
    assert store_a.cursor(b.node_id, cid, 1) == 0

    retry = _ingest(b_dir, a.node_id, original)
    assert retry.inserted == 0
    assert retry.acked_sequence == original[-1].sequence
    assert _ack_sender(a_dir, b.node_id, cid, 1, retry.acked_sequence) == retry.acked_sequence

    assert [event.to_dict() for event in store_a.list_events(cid)] == [
        event.to_dict() for event in store_b.list_events(cid)
    ]


def test_gap_and_out_of_order_batch_rolls_back_atomically(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="Atomic batch", delay_seconds=0)
    cid = view.id
    dialogue.claim(cid, claimant_node_id=a.node_id, expected_epoch=1)
    dialogue.reply(
        cid,
        author_node_id=a.node_id,
        content="create more events",
        result="continue",
        expected_epoch=1,
    )
    events = EventStore(a_dir).list_events(cid)
    assert len(events) >= 4

    replication_b = ReplicationService(b_dir)
    with pytest.raises(ValidationError, match="sequence gap"):
        replication_b.receive_events(
            sender_node_id=a.node_id,
            raw_events=[events[0].to_dict(), events[2].to_dict()],
        )
    assert EventStore(b_dir).list_events(cid) == []

    # A contiguous but reversed batch is normalized and still converges.
    ack = replication_b.receive_events(
        sender_node_id=a.node_id,
        raw_events=[event.to_dict() for event in reversed(events)],
    )
    assert ack.acked_sequence == events[-1].sequence
    assert [event.to_dict() for event in EventStore(b_dir).list_events(cid)] == [
        event.to_dict() for event in events
    ]


def test_transaction_failure_during_ingest_leaves_no_partial_history(
    tmp_path: Path, monkeypatch
) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="Crash boundary", delay_seconds=0)
    cid = view.id
    source = EventStore(a_dir)
    destination = EventStore(b_dir)
    events = source.list_events(cid)

    original_insert = destination._insert_verified
    calls = {"count": 0}

    def fail_on_second(conn, event):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated process failure between event inserts")
        return original_insert(conn, event)

    monkeypatch.setattr(destination, "_insert_verified", fail_on_second)
    with pytest.raises(RuntimeError, match="simulated process failure"):
        destination.ingest_many(events)

    # One transaction owns the entire batch; no prefix may survive the failure.
    assert EventStore(b_dir).list_events(cid) == []
    assert _quick_check(EventStore(b_dir).db_path) == "ok"


def test_tampered_event_and_revoked_peer_are_rejected_without_db_damage(
    tmp_path: Path,
) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="Tamper", delay_seconds=0)
    cid = view.id
    event = EventStore(a_dir).list_events(cid)[0]
    tampered = EventEnvelope.from_dict(
        {**event.to_dict(), "payload": {**event.payload, "objective": "altered"}}
    )

    with pytest.raises(ForbiddenError, match="invalid federated event signature"):
        EventStore(b_dir).ingest(tampered)
    assert EventStore(b_dir).list_events(cid) == []

    revoke_peer(b_dir, a.node_id)
    with pytest.raises(ForbiddenError):
        EventStore(b_dir).ingest(event)
    assert _quick_check(EventStore(b_dir).db_path) == "ok"


def test_partition_failover_rejoin_fences_stale_coordinator_and_converges(
    tmp_path: Path,
) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    base_a = FederatedDialogueService(a_dir)
    view = base_a.create(b.node_id, objective="Partition recovery", delay_seconds=0)
    cid = view.id
    start = int(view.current_turn["available_at"])

    manager_a = FailoverManager(a_dir)
    manager_a.grant_initial_lease(cid, lease_seconds=15, now=start)
    initial = EventStore(a_dir).list_events(cid, epoch=1, from_sequence=1)
    ack = _ingest(b_dir, a.node_id, initial)
    _ack_sender(a_dir, b.node_id, cid, 1, ack.acked_sequence)

    leased_a = LeasedDialogueService(a_dir)
    leased_a.claim(cid, claimant_node_id=a.node_id, expected_epoch=1, now=start + 1)
    leased_a.reply(
        cid,
        author_node_id=a.node_id,
        content="A before partition",
        result="continue",
        expected_epoch=1,
        now=start + 1,
    )
    missing = EventStore(a_dir).list_events(
        cid,
        epoch=1,
        from_sequence=EventStore(b_dir).get_head(cid).last_sequence + 1,
    )
    _ingest(b_dir, a.node_id, missing)

    # Partition: no further A<->B ACK/replication. Old coordinator reaches its
    # last peer-acknowledged deadline and becomes fenced.
    with pytest.raises(ConflictError, match="coordinator lease expired"):
        manager_a.require_local_write_lease(cid, now=start + 16)

    manager_b = FailoverManager(b_dir)
    actions = asyncio.run(
        manager_b.tick(
            None,
            now=start + 21,
            lease_seconds=15,
            renew_before_seconds=5,
            grace_seconds=5,
        )
    )
    assert any(action["action"] == "takeover" for action in actions)

    leased_b = LeasedDialogueService(b_dir)
    leased_b.claim(cid, claimant_node_id=b.node_id, expected_epoch=2, now=start + 21)
    leased_b.reply(
        cid,
        author_node_id=b.node_id,
        content="B after failover",
        result="continue",
        expected_epoch=2,
        now=start + 21,
    )

    # Rejoin: epoch 2 catch-up reaches A. A cannot become authoritative again.
    epoch2 = EventStore(b_dir).list_events(cid, epoch=2, from_sequence=1)
    _ingest(a_dir, b.node_id, epoch2)
    assert EventStore(a_dir).get_head(cid).coordinator_epoch == 2
    assert EventStore(a_dir).get_head(cid).coordinator_id == b.node_id
    with pytest.raises(ForbiddenError):
        LeasedDialogueService(a_dir).claim(
            cid,
            claimant_node_id=a.node_id,
            expected_epoch=2,
            now=start + 22,
        )

    assert [event.to_dict() for event in EventStore(a_dir).list_events(cid)] == [
        event.to_dict() for event in EventStore(b_dir).list_events(cid)
    ]
    assert _quick_check(EventStore(a_dir).db_path) == "ok"
    assert _quick_check(EventStore(b_dir).db_path) == "ok"


def test_restart_preserves_identity_history_cursors_and_supervision_material(
    tmp_path: Path,
) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="Restart", delay_seconds=0)
    cid = view.id
    events = EventStore(a_dir).list_events(cid)
    ack = _ingest(b_dir, a.node_id, events)
    EventStore(a_dir).ack(b.node_id, cid, 1, ack.acked_sequence)

    before_history = [event.to_dict() for event in EventStore(a_dir).list_events(cid)]
    before_cursor = EventStore(a_dir).cursor(b.node_id, cid, 1)

    # Reconstruct every service from disk, equivalent to a process restart.
    restarted_dialogue = FederatedDialogueService(a_dir)
    restarted_store = EventStore(a_dir)
    assert restarted_dialogue.get(cid).id == cid
    assert [event.to_dict() for event in restarted_store.list_events(cid)] == before_history
    assert restarted_store.cursor(b.node_id, cid, 1) == before_cursor
    assert restarted_dialogue.state.node_id == a.node_id
    assert _quick_check(restarted_store.db_path) == "ok"


def test_plaintext_local_api_keys_never_enter_node_sqlite(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="Secret isolation", delay_seconds=0)
    cid = view.id
    _ingest(b_dir, a.node_id, EventStore(a_dir).list_events(cid))

    for state_dir in (a_dir, b_dir):
        local_key = (state_dir / LOCAL_API_KEY_FILENAME).read_text(encoding="utf-8").strip()
        assert local_key
        db_bytes = Path(EventStore(state_dir).db_path).read_bytes()
        assert local_key.encode("utf-8") not in db_bytes
        assert _quick_check(EventStore(state_dir).db_path) == "ok"
