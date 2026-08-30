
"""Autonomous Worker Core — PHASE 6.

Task → Lease → Federated Claim → Agent → Submitted Response.
NO federated reply. NO signed events. NO direct DB access.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Protocol

from xerrameca.transport import TaskEnvelope, ClaimDecision
from xerrameca.transport.sqlite_adapter import SqliteTaskQueueAdapter
from xerrameca.transport.federated_claim_adapter import FederatedTurnClaimAdapter

# ──────────────────────────────────────────────────────────────────────
# Agent Invoker Interface
# ──────────────────────────────────────────────────────────────────────

class AgentInvoker(Protocol):
    def invoke(self, context: "TurnContext") -> "AgentResponse":
        ...

@dataclass
class TurnContext:
    conversation_id: str
    objective: str
    round: int
    max_rounds: int
    turn_id: str
    sequence: int
    role: Optional[str] = None
    history: str = ""

@dataclass
class AgentResponse:
    content: str
    result: str  # continue | complete | blocked | needs_human | error

    def is_valid(self) -> bool:
        return self.result in ("continue", "complete", "blocked", "needs_human", "error")

class FakeAgentInvoker:
    """Deterministic invoker for tests."""
    def __init__(self, response: Optional[AgentResponse] = None):
        self.response = response or AgentResponse(content="ok", result="continue")
        self.call_count = 0

    def invoke(self, context: TurnContext) -> AgentResponse:
        self.call_count += 1
        return self.response

class HermesInvoker:
    """Real Hermes programmatic adapter (stub for now)."""
    def __init__(self, timeout_seconds: float = 30.0):
        self.timeout_seconds = timeout_seconds

    def invoke(self, context: TurnContext) -> AgentResponse:
        # Placeholder: real implementation would call Hermes via programmatic API
        return AgentResponse(content="[hermes response]", result="continue")

# ──────────────────────────────────────────────────────────────────────
# Autonomous Worker
# ──────────────────────────────────────────────────────────────────────

class AutonomousWorker:
    def __init__(
        self,
        db_path: str,
        state_dir: str,
        *,
        agent_invoker: Optional[AgentInvoker] = None,
        claim_adapter: Optional[FederatedTurnClaimAdapter] = None,
        task_adapter: Optional[SqliteTaskQueueAdapter] = None,
        timeout_seconds: float = 10.0,
    ):
        self.db_path = db_path
        self.state_dir = state_dir
        self.agent_invoker = agent_invoker or FakeAgentInvoker()
        self.claim_adapter = claim_adapter or FederatedTurnClaimAdapter(state_dir=state_dir, timeout_seconds=timeout_seconds)
        self.task_adapter = task_adapter or SqliteTaskQueueAdapter(db_path)

    def run_once(self) -> dict:
        """Execute one lease→claim→invoke→submit cycle. Returns summary dict."""
        # 1. Lease next task
        envelope = self.task_adapter.lease_next_task("worker")
        if envelope is None:
            return {"status": "no_task", "lease": 0, "claim": 0, "invoke": 0, "submit": 0}

        lease_result = {"task_id": envelope.task_id, "status": "leased"}

        # 2. Federated claim
        claim_decision = self.claim_adapter.claim_turn(envelope)
        claim_result = {"ok": claim_decision.ok, "reason": claim_decision.reason}

        if not claim_decision.ok:
            # Classify failure: authoritative vs transport
            if self._is_authoritative_rejection(claim_decision.reason or ""):
                # A) Authoritative definitive rejection → discard
                self.task_adapter.discard_task(envelope)
                return {"status": "discarded", "lease": 1, "claim": 1, "invoke": 0, "submit": 0, "claim_reason": claim_decision.reason}
            else:
                # B) Transport failure → fail (retry path)
                self.task_adapter.fail_task(envelope, claim_decision.reason or "transport_failure")
                return {"status": "failed_transport", "lease": 1, "claim": 1, "invoke": 0, "submit": 0, "claim_reason": claim_decision.reason}

        # 3. Invoke agent ONLY after successful claim
        context = self._build_context(envelope)
        try:
            agent_response = self.agent_invoker.invoke(context)
        except Exception as exc:
            self.task_adapter.fail_task(envelope, f"agent_error: {exc}")
            return {"status": "agent_error", "lease": 1, "claim": 1, "invoke": 1, "submit": 0, "error": str(exc)}

        if not agent_response.is_valid():
            self.task_adapter.fail_task(envelope, "invalid_agent_response")
            return {"status": "invalid_response", "lease": 1, "claim": 1, "invoke": 1, "submit": 0}

        # 4. Submit response
        submit_decision = self.task_adapter.submit_response(envelope, agent_response.content)
        submit_result = {"ok": submit_decision.ok, "reason": submit_decision.reason}

        if not submit_decision.ok:
            self.task_adapter.fail_task(envelope, submit_decision.reason or "submit_failed")
            return {"status": "submit_failed", "lease": 1, "claim": 1, "invoke": 1, "submit": 0}

        return {
            "status": "submitted",
            "lease": 1, "claim": 1, "invoke": 1, "submit": 1,
            "task_id": envelope.task_id,
            "agent_result": agent_response.result,
        }

    def _is_authoritative_rejection(self, reason: str) -> bool:
        # A) definitive authoritative rejections
        authoritative = {"stale_epoch", "claim_conflict_409", "conversation_completed"}
        return any(k in reason for k in authoritative)

    def _build_context(self, envelope: TaskEnvelope) -> TurnContext:
        return TurnContext(
            conversation_id=envelope.conversation_id,
            objective=envelope.objective,
            round=envelope.sequence,  # approximation
            max_rounds=5,
            turn_id=envelope.turn_id,
            sequence=envelope.sequence,
            history=envelope.history_ref,
        )
