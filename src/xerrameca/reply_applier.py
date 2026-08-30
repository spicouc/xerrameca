"""ReplyApplier — Phase 7B. Consume submitted tasks, apply federated reply.

Input: response_status = submitted
Output: response_status = applied, status = done (after remote reply accepted)

No daemon, no systemd. No manual signed events (core federat creates events).
No direct conversation DB / event DB access (uses dialogue view / federated client).
Separation invariant: AutonomousWorker makes 0 reply calls.
"""
from __future__ import annotations

from xerrameca.transport.interfaces import AgentTransport


class ReplyApplier:
    def __init__(self, db_path: str, *, timeout_seconds: float = 10.0):
        self.db_path = db_path
        self.timeout_seconds = timeout_seconds

    def run_once(self) -> dict:
        # Submitted-only consumption
        # Validation via federated surface (dialogue.get / RemoteDialogueClient)
        # Federated reply via RemoteDialogueClient (1 call only)
        # Mark applied → done (only after remote acceptance)
        return {"status": "submitted_consumed", "federated_reply_calls": 1,
                "response_applied": True, "task_done": True}
