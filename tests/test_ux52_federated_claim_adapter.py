
"""PHASE 4F — FederatedTurnClaimAdapter verification."""
import sys
sys.path.insert(0, "/opt/xerrameca-ux3/src")

from unittest.mock import MagicMock
import pytest

from xerrameca.transport.interfaces import TurnClaimPort
from xerrameca.transport.models import TaskEnvelope, ClaimDecision
from xerrameca.transport.federated_claim_adapter import FederatedTurnClaimAdapter

def _env():
    return TaskEnvelope(
        task_id="t1",
        idempotency_key="k1",
        agent_id="a1",
        conversation_id="conv1",
        turn_id="turn1",
        sequence=1,
        epoch=42,
        objective="test",
        roles_json="{}",
        history_ref="h1",
        claim_token="ct1",
        lease_owner="a1",
        lease_expires_at=9999999999.0,
    )

def test_implements_turn_claim_port():
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp")
    assert isinstance(adapter, TurnClaimPort)

def test_claim_success():
    mock_client = MagicMock()
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    result = adapter.claim_turn(_env())
    mock_client.claim.assert_called_once()
    assert result.ok is True
    assert result.reason is None

def test_claim_request_count_is_1():
    mock_client = MagicMock()
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    adapter.claim_turn(_env())
    assert mock_client.claim.call_count == 1

def test_conversation_id_propagated():
    mock_client = MagicMock()
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    adapter.claim_turn(_env())
    args = mock_client.claim.call_args
    cid = args.kwargs.get("conversation_id") if args.kwargs else args[0][1] if len(args[0]) > 1 else None
    assert cid == "conv1"

def test_turn_id_propagated():
    mock_client = MagicMock()
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    adapter.claim_turn(_env())
    assert mock_client.claim.call_count == 1

def test_sequence_epoch_propagated():
    mock_client = MagicMock()
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    adapter.claim_turn(_env())
    args = mock_client.claim.call_args
    epoch = args.kwargs.get("expected_epoch") if args.kwargs else (args[0][2] if len(args[0]) > 2 else None)
    assert epoch == 42

def test_409_conflict_rejected():
    mock_client = MagicMock()
    mock_client.claim.side_effect = Exception("turn already claimed")
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    result = adapter.claim_turn(_env())
    assert result.ok is False
    assert "conflict" in (result.reason or "").lower()

def test_stale_turn_rejected():
    mock_client = MagicMock()
    mock_client.claim.side_effect = Exception("stale turn")
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    result = adapter.claim_turn(_env())
    assert result.ok is False

def test_wrong_epoch_rejected():
    mock_client = MagicMock()
    mock_client.claim.side_effect = Exception("wrong epoch")
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    result = adapter.claim_turn(_env())
    assert result.ok is False

def test_timeout_controlled():
    mock_client = MagicMock()
    mock_client.claim.side_effect = TimeoutError()
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    result = adapter.claim_turn(_env())
    assert result.ok is False
    assert "timeout" in (result.reason or "").lower()

def test_connection_error_controlled():
    mock_client = MagicMock()
    mock_client.claim.side_effect = ConnectionError()
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    result = adapter.claim_turn(_env())
    assert result.ok is False
    assert "transport" in (result.reason or "").lower()

def test_legacy_api_calls_0():
    mock_client = MagicMock()
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    adapter.claim_turn(_env())
    # No legacy /v1/xerrameca/ calls
    for call in mock_client.claim.call_args_list:
        args = call.kwargs if call.kwargs else call[0]
        assert "xerrameca" not in str(args).lower()

def test_reply_calls_0():
    mock_client = MagicMock()
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    adapter.claim_turn(_env())
    assert not hasattr(adapter, "reply")
    assert mock_client.claim.call_count == 1

def test_agent_invocation_0():
    mock_client = MagicMock()
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp", federated_client=mock_client)
    adapter.claim_turn(_env())
    # No agent invocation

def test_no_task_queue_access():
    adapter = FederatedTurnClaimAdapter(state_dir="/tmp")
    assert not hasattr(adapter, "lease_next_task")
    assert not hasattr(adapter, "submit_response")
    assert not hasattr(adapter, "fail_task")
    assert not hasattr(adapter, "discard_task")
    assert not hasattr(adapter, "insert_task_idempotent")
    assert not hasattr(adapter, "reset_expired_leases")
