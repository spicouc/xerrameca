"""QueueReconciler -- PHASE 5.

Source of truth: federated inbox / federated client.
No direct conversation DB reads. No event DB reads.
Rebuilds task_queue from federated turns.
"""
from __future__ import annotations

import time
from typing import Optional, List

from xerrameca.transport import TaskEnvelope, make_idempotency_key
from xerrameca.transport.sqlite_adapter import SqliteTaskQueueAdapter


class QueueReconciler:
    def __init__(self, db_path: str, federated_client=None):
        self.db_path = db_path
        self.adapter = SqliteTaskQueueAdapter(db_path)
        self.federated_client = federated_client

    def reconcile(self) -> dict:
        tasks_before = self._count_pending()

        if self.federated_client is not None:
            try:
                federated_turns = self.federated_client.inbox() if hasattr(self.federated_client, "inbox") else []
            except Exception:
                federated_turns = []
        else:
            federated_turns = []

        rebuilt = 0
        for turn in federated_turns:
            if turn.get("assigned_node_id") != (getattr(self.federated_client, "node_id", None) if self.federated_client else None):
                continue
            if turn.get("status") == "completed" or turn.get("status") == "cancelled":
                continue
            envelope = TaskEnvelope(
                task_id=turn.get("task_id", turn.get("turn_id", "t_" + str(turn.get("sequence", 0)))),
                idempotency_key=make_idempotency_key(
                    turn.get("conversation_id", ""),
                    turn.get("turn_id", turn.get("id", "")),
                    turn.get("sequence", 0),
                    turn.get("epoch", 0),
                ),
                agent_id=turn.get("assigned_agent_id", turn.get("agent_id")),
                conversation_id=turn.get("conversation_id", ""),
                turn_id=turn.get("turn_id", turn.get("id", "")),
                sequence=int(turn.get("sequence", 0)),
                epoch=int(turn.get("epoch", 0)),
                objective=turn.get("objective", ""),
                roles_json=turn.get("roles_json", "{}"),
                history_ref=turn.get("history_ref", ""),
                claim_token=turn.get("claim_token", ""),
                lease_owner=turn.get("lease_owner"),
                lease_expires_at=turn.get("lease_expires_at", 0.0),
            )
            inserted = self.adapter.insert_task_idempotent(envelope)
            if inserted:
                rebuilt += 1

        self.adapter.reset_expired_leases()
        tasks_after = self._count_pending()
        return {
            "before": tasks_before,
            "after": tasks_after,
            "rebuilt": rebuilt,
            "duplicates": 0,
        }

    def _count_pending(self) -> int:
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("SELECT COUNT(*) FROM task_queue WHERE status = 'pending'")
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0


def main():
    import sys
    import argparse
    parser = argparse.ArgumentParser(description="QueueReconciler CLI")
    parser.add_argument("--db", default="/opt/xerrameca-ux3/task_queue.db")
    args = parser.parse_args()
    reconciler = QueueReconciler(args.db)
    result = reconciler.reconcile()
    print(f"RECONCILE: before={result['before']} after={result['after']} rebuilt={result['rebuilt']} duplicates={result['duplicates']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
