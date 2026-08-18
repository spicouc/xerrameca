from __future__ import annotations

import json
from pathlib import Path

import pytest

from xerrameca.cli import main as cli_main
from xerrameca.domain.errors import ConflictError
from xerrameca.node.dialogue import FederatedDialogueService
from xerrameca.node.identity import initialize_node
from xerrameca.node.supervisor import LocalSupervisor
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


def _codes(report: dict) -> set[str]:
    return {finding["code"] for finding in report["findings"]}


def test_passive_supervisor_detects_waiting_and_expired_lease(tmp_path: Path) -> None:
    a_dir, _, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="Observe timeout", delay_seconds=0)
    cid = view.id
    available = int(view.current_turn["available_at"])

    supervisor = LocalSupervisor(
        a_dir, waiting_warning_seconds=30, idle_warning_seconds=10_000
    )
    waiting = supervisor.inspect(cid, now=available + 31)
    assert "turn_waiting" in _codes(waiting)

    claimed = dialogue.claim(
        cid,
        claimant_node_id=a.node_id,
        expected_epoch=1,
        now=available + 31,
    )
    lease_until = int(claimed.current_turn["lease_until"])
    expired = supervisor.inspect(cid, now=lease_until + 1)
    assert "lease_expired" in _codes(expired)
    lease_finding = next(
        finding for finding in expired["findings"] if finding["code"] == "lease_expired"
    )
    assert lease_finding["details"]["retry_available"] is True


def test_explicit_lease_recovery_is_bounded_and_restart_safe(tmp_path: Path) -> None:
    a_dir, _, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="Recover lease", delay_seconds=0)
    cid = view.id
    start = int(view.current_turn["available_at"])
    claimed = dialogue.claim(
        cid,
        claimant_node_id=a.node_id,
        expected_epoch=1,
        now=start,
    )
    first_expiry = int(claimed.current_turn["lease_until"])

    supervisor = LocalSupervisor(a_dir, max_lease_retries=1)
    recovered = supervisor.recover_expired_lease(
        cid, expected_epoch=1, now=first_expiry + 1
    )
    turn = recovered["conversation"]["current_turn"]
    assert turn["claimed_by_node_id"] is None
    assert turn["lease_until"] is None

    reclaimed = dialogue.claim(
        cid,
        claimant_node_id=a.node_id,
        expected_epoch=1,
        now=first_expiry + 1,
    )
    second_expiry = int(reclaimed.current_turn["lease_until"])
    with pytest.raises(ConflictError, match="maximum lease recovery retries"):
        supervisor.recover_expired_lease(
            cid, expected_epoch=1, now=second_expiry + 1
        )

    restarted = LocalSupervisor(a_dir, max_lease_retries=1)
    report = restarted.inspect(cid, now=second_expiry + 1)
    finding = next(
        item for item in report["findings"] if item["code"] == "lease_expired"
    )
    assert finding["details"]["retry_count"] == 1
    assert finding["details"]["retry_available"] is False


def test_loop_detection_and_response_metrics_are_derived_from_event_log(
    tmp_path: Path,
) -> None:
    a_dir, _, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(
        b.node_id, objective="Detect repetition", max_rounds=5, delay_seconds=0
    )
    cid = view.id

    for author in (a.node_id, b.node_id, a.node_id, b.node_id):
        current = dialogue.get(cid).current_turn
        assert current["assigned_node_id"] == author
        now = int(current["available_at"])
        dialogue.claim(cid, claimant_node_id=author, expected_epoch=1, now=now)
        dialogue.reply(
            cid,
            author_node_id=author,
            content="same repeated answer",
            result="continue",
            expected_epoch=1,
            now=now,
        )

    supervisor = LocalSupervisor(a_dir, loop_window=4, idle_warning_seconds=10_000)
    report = supervisor.inspect(cid)
    assert "possible_loop" in _codes(report)
    assert report["metrics"]["message_count"] == 4
    assert report["metrics"]["response_latency"]["count"] == 4
    assert set(report["metrics"]["response_latency_by_node"]) == {
        a.node_id,
        b.node_id,
    }


def test_max_round_terminal_state_needs_no_active_supervisor_intervention(
    tmp_path: Path,
) -> None:
    a_dir, _, a, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(
        b.node_id, objective="Bound rounds", max_rounds=1, delay_seconds=0
    )
    cid = view.id

    for author in (a.node_id, b.node_id):
        current = dialogue.get(cid).current_turn
        now = int(current["available_at"])
        dialogue.claim(cid, claimant_node_id=author, expected_epoch=1, now=now)
        final = dialogue.reply(
            cid,
            author_node_id=author,
            content="continue",
            result="continue",
            expected_epoch=1,
            now=now,
        )
    assert final.status == "blocked"
    assert final.block_reason == "max_rounds"

    report = LocalSupervisor(a_dir).inspect(cid)
    assert report["conversation"]["status"] == "blocked"
    assert "lease_expired" not in _codes(report)


def test_supervisor_cli_emits_json_without_mutating_state(
    tmp_path: Path, capsys
) -> None:
    a_dir, _, _, b = _trusted_nodes(tmp_path)
    dialogue = FederatedDialogueService(a_dir)
    view = dialogue.create(b.node_id, objective="CLI inspect", delay_seconds=0)

    assert (
        cli_main(
            [
                "supervisor",
                "inspect",
                "--state-dir",
                str(a_dir),
                "--conversation",
                view.id,
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversation"]["id"] == view.id
    assert payload["metrics"]["event_count"] >= 2
