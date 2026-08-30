"""Turn Opened Fast Path — PHASE 8.

Wired into the supervisor event loop.
Detects turn.opened events locally and enqueues TaskEnvelope immediately.

No claim, no agent invocation, no reply — purely local task provisioning.
"""
from __future__ import annotations

import os
import json
from typing import Optional

from xerrameca.transport.interfaces import AgentTransport


class TurnOpenedFastPath:
    """Detect turn.opened events and create Tasks locally.

    Responsibilities:
    - Ingest turn.opened events from the supervisor event loop
    - Build TaskEnvelope with conversation_id, turn_id, sequence, epoch, etc.
    - Use make_idempotency_key() for idempotent insertion
    - Call SqliteTaskQueueAdapter.insert_task_idempotent()
    - If fast path fails, reconciler is authoritative recovery path
    """

    def __init__(self, db_path: str, *, local_node_id: str):
        self.db_path = db_path
        self.local_node_id = local_node_id
        self.processed_turns: set[tuple[str, int, int, int]] = set()

    def on_turn_opened(
        self,
        conversation_id: str,
        turn_id: str,
        sequence: int,
        epoch: int,
        objective: str,
        agent_id: str,
        roles_json: str,
        history_ref: str,
        assigned_node_id: str,
        turn_status: str,
        available_at: int,
    ) -> Optional[str]:
        """Process a locally opened turn and enqueue the task.

        Returns task_id if created, None if already processed or failed.
        """
        # Verify turn is local and active
        if assigned_node_id != self.local_node_id:
            return None
        if turn_status != "active":
            return None

        key = (conversation_id, turn_id, sequence, epoch)
        if key in self.processed_turns:
            return None

        try:
            # Lazy import to avoid circular dependency
            from xerrameca.transport.sqlite_adapter import SqliteTaskQueueAdapter
            from xerrameca.transport.models import TaskEnvelope
            from xerrameca.transport import make_idempotency_key

            adapter = SqliteTaskQueueAdapter(db_path=self.db_path)
            idempotency_key = make_idempotency_key(
                conversation_id, turn_id, sequence, epoch
            )
            task_id = f"{conversation_id}:{turn_id}"

            envelope = TaskEnvelope(
                task_id=task_id,
                idempotency_key=idempotency_key,
                agent_id=agent_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                sequence=sequence,
                epoch=epoch,
                objective=objective,
                roles_json=roles_json,
                history_ref=history_ref,
                claim_token="",
                lease_owner=None,
                lease_expires_at=None,
                response_payload=None,
                response_status="none",
            )

            inserted = adapter.insert_task_idempotent(envelope)
            if inserted:
                self.processed_turns.add(key)
                return envelope.task_id
            return None
        except Exception:
            # Fast path failure — reconciliation will retry
            return None
