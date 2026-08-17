from __future__ import annotations

from pathlib import Path

import pytest

from xerrameca.db import get_db
from xerrameca.domain.models import ConversationCreateRequest, ReplyRequest
from xerrameca.ports.identity import AgentIdentity
from xerrameca.ports.memory import PersistedMemory
from xerrameca.services.engine import ConversationEngine
from xerrameca.services.summary_dispatcher import SummaryDispatcher


def agent(agent_id: str) -> AgentIdentity:
    return AgentIdentity(
        id=agent_id,
        name=agent_id,
        permissions={"read": True, "write": True, "admin": False},
        allowed_scopes=("shared",),
        capabilities={},
    )


class FlakyMemory:
    def __init__(self) -> None:
        self.calls = 0

    async def persist_summary(self, **kwargs) -> PersistedMemory:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary provider failure")
        return PersistedMemory(external_id="fact-summary-1")


async def complete_conversation(
    engine: ConversationEngine, a: AgentIdentity, b: AgentIdentity
) -> str:
    created = await engine.create_conversation(
        a,
        [a, b],
        ConversationCreateRequest(
            name="Summary isolation",
            objective="Prove memory delivery is decoupled",
            participant_agent_ids=[a.id, b.id],
            first_agent_id=a.id,
            max_rounds=2,
            delay_seconds=0,
            persist_summary=True,
        ),
    )
    await engine.start_conversation(a, created["id"])

    turn_a = (await engine.inbox(a))["turns"][0]["id"]
    lease_a = (await engine.claim_turn(a, turn_a))["lease_token"]
    await engine.reply_turn(
        a,
        turn_a,
        ReplyRequest(
            content="Independent state first.",
            result="continue",
            lease_token=lease_a,
        ),
    )

    turn_b = (await engine.inbox(b))["turns"][0]["id"]
    lease_b = (await engine.claim_turn(b, turn_b))["lease_token"]
    await engine.reply_turn(
        b,
        turn_b,
        ReplyRequest(
            content="I propose completion.",
            result="complete",
            lease_token=lease_b,
        ),
    )

    confirm = (await engine.inbox(a))["turns"][0]["id"]
    confirm_lease = (await engine.claim_turn(a, confirm))["lease_token"]
    completed = await engine.reply_turn(
        a,
        confirm,
        ReplyRequest(
            content="Confirmed.",
            result="complete",
            lease_token=confirm_lease,
        ),
    )
    assert completed["status"] == "completed"
    assert completed["summary_status"] == "pending"
    return created["id"]


@pytest.mark.asyncio
async def test_summary_failure_never_rolls_back_completed_conversation(tmp_path: Path) -> None:
    db_path = str(tmp_path / "xerrameca.db")
    engine = ConversationEngine(db_path)
    await engine.bootstrap()
    a, b = agent("agent-a"), agent("agent-b")
    conversation_id = await complete_conversation(engine, a, b)

    memory = FlakyMemory()
    dispatcher = SummaryDispatcher(db_path, memory, max_attempts=3)

    first = await dispatcher.dispatch_pending()
    assert first == {"processed": 1, "stored": 0, "failed": 1}

    after_failure = await engine.get_conversation(a, conversation_id)
    assert after_failure["status"] == "completed"
    assert after_failure["summary_status"] == "failed"

    async with get_db(db_path) as db:
        row = await (
            await db.execute(
                "SELECT status, attempts FROM summary_outbox WHERE conversation_id=?",
                (conversation_id,),
            )
        ).fetchone()
        assert row["status"] == "failed"
        assert row["attempts"] == 1

    second = await dispatcher.dispatch_pending()
    assert second == {"processed": 1, "stored": 1, "failed": 0}

    after_success = await engine.get_conversation(a, conversation_id)
    assert after_success["status"] == "completed"
    assert after_success["summary_status"] == "stored"
    assert after_success["summary_external_id"] == "fact-summary-1"
