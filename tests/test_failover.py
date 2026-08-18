from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from xerrameca.domain.errors import ConflictError, ForbiddenError
from xerrameca.node.dialogue import FederatedDialogueService
from xerrameca.node.events import EventStore
from xerrameca.node.failover import FailoverManager, LeasedDialogueService
from xerrameca.node.identity import initialize_node
from xerrameca.node.replication import ReplicationService
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


def _replicate_epoch(
    source_dir: Path,
    destination_dir: Path,
    sender: str,
    cid: str,
    epoch: int,
) -> int:
    source = EventStore(source_dir)
    destination = ReplicationService(destination_dir)
    events = source.list_events(cid, epoch=epoch, from_sequence=1)
    if not events:
        return 0
    ack = destination.receive_events(
        sender_node_id=sender,
        raw_events=[event.to_dict() for event in events],
    )
    return ack.acked_sequence


def test_lease_extension_is_not_effective_until_peer_ack(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="Lease ACK", delay_seconds=0)
    cid = view.id
    manager = FailoverManager(a_dir)

    manager.grant_initial_lease(cid, lease_seconds=20, now=100)
    _replicate_epoch(a_dir, b_dir, a.node_id, cid, 1)
    head = EventStore(a_dir).get_head(cid)
    EventStore(a_dir).ack(b.node_id, cid, 1, head.last_sequence)

    acknowledged = manager.status(cid, now=105)
    assert acknowledged.effective_lease_until == 120
    assert acknowledged.latest_acknowledged is True

    manager.renew_lease(cid, lease_seconds=20, now=110)
    pending = manager.status(cid, now=111)
    assert pending.latest_lease_until == 130
    assert pending.latest_acknowledged is False
    assert pending.effective_lease_until == 120

    renewed_head = EventStore(a_dir).get_head(cid)
    EventStore(a_dir).ack(b.node_id, cid, 1, renewed_head.last_sequence)
    effective = manager.status(cid, now=111)
    assert effective.latest_acknowledged is True
    assert effective.effective_lease_until == 130


def test_expired_coordinator_self_fences_before_peer_takeover(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    base_a = FederatedDialogueService(a_dir)
    view = base_a.create(b.node_id, objective="Safe failover", delay_seconds=0)
    cid = view.id
    base_time = int(view.current_turn["available_at"])
    manager_a = FailoverManager(a_dir)
    manager_a.grant_initial_lease(cid, lease_seconds=15, now=base_time)

    _replicate_epoch(a_dir, b_dir, a.node_id, cid, 1)
    head_a = EventStore(a_dir).get_head(cid)
    EventStore(a_dir).ack(b.node_id, cid, 1, head_a.last_sequence)

    leased_a = LeasedDialogueService(a_dir)
    leased_a.claim(
        cid,
        claimant_node_id=a.node_id,
        expected_epoch=1,
        now=base_time + 1,
    )
    leased_a.reply(
        cid,
        author_node_id=a.node_id,
        content="A hands the next turn to B.",
        result="continue",
        expected_epoch=1,
        now=base_time + 1,
    )
    _replicate_epoch(a_dir, b_dir, a.node_id, cid, 1)

    with pytest.raises(ConflictError, match="coordinator lease expired"):
        manager_a.require_local_write_lease(cid, now=base_time + 16)

    manager_b = FailoverManager(b_dir)
    actions = asyncio.run(
        manager_b.tick(
            None,
            now=base_time + 21,
            lease_seconds=15,
            renew_before_seconds=5,
            grace_seconds=5,
        )
    )
    takeover = next(action for action in actions if action["action"] == "takeover")
    assert takeover["epoch"] == 2

    status_b = manager_b.status(cid, now=base_time + 21)
    assert status_b.coordinator_id == b.node_id
    assert status_b.coordinator_epoch == 2
    assert status_b.takeover_grant is True
    assert status_b.expired is False

    leased_b = LeasedDialogueService(b_dir)
    leased_b.claim(
        cid,
        claimant_node_id=b.node_id,
        expected_epoch=2,
        now=base_time + 21,
    )
    progressed = leased_b.reply(
        cid,
        author_node_id=b.node_id,
        content="B continues under epoch 2.",
        result="continue",
        expected_epoch=2,
        now=base_time + 21,
    )
    assert progressed.coordinator_id == b.node_id
    assert progressed.coordinator_epoch == 2

    _replicate_epoch(b_dir, a_dir, b.node_id, cid, 2)
    view_a = LeasedDialogueService(a_dir).get(cid)
    assert view_a.coordinator_id == b.node_id
    assert view_a.coordinator_epoch == 2

    with pytest.raises(ForbiddenError, match="local node is not"):
        LeasedDialogueService(a_dir).claim(
            cid,
            claimant_node_id=a.node_id,
            expected_epoch=2,
            now=base_time + 22,
        )

    history_a = [event.to_dict() for event in EventStore(a_dir).list_events(cid)]
    history_b = [event.to_dict() for event in EventStore(b_dir).list_events(cid)]
    assert history_a == history_b


def test_old_coordinator_never_reacquires_its_expired_epoch(tmp_path: Path) -> None:
    a_dir, b_dir, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="No stale recovery", delay_seconds=0)
    cid = view.id
    manager_a = FailoverManager(a_dir)
    manager_a.grant_initial_lease(cid, lease_seconds=15, now=10)
    _replicate_epoch(a_dir, b_dir, a.node_id, cid, 1)
    EventStore(a_dir).ack(
        b.node_id,
        cid,
        1,
        EventStore(a_dir).get_head(cid).last_sequence,
    )

    actions = asyncio.run(manager_a.tick(None, now=30, grace_seconds=1))
    assert any(action["action"] == "self_fenced" for action in actions)
    assert EventStore(a_dir).get_head(cid).coordinator_epoch == 1
    assert EventStore(a_dir).get_head(cid).coordinator_id == a.node_id

    with pytest.raises(ConflictError, match="current coordinator cannot take over"):
        manager_a.takeover(cid, now=30, grace_seconds=1)
