from __future__ import annotations

from pathlib import Path

import pytest

from xerrameca.db import get_db
from xerrameca.domain.errors import ConflictError
from xerrameca.domain.models import ConversationCreateRequest, ReplyRequest
from xerrameca.ports.identity import AgentIdentity
from xerrameca.services.engine import ConversationEngine


def agent(agent_id: str, name: str) -> AgentIdentity:
    return AgentIdentity(
        id=agent_id,
        name=name,
        permissions={"read": True, "write": True, "delete": False, "admin": False},
        allowed_scopes=("shared",),
        capabilities={"xerrameca": True},
        is_active=True,
    )


@pytest.fixture
async def setup_engine(tmp_path: Path):
    db_path = str(tmp_path / "xerrameca.db")
    engine = ConversationEngine(db_path)
    await engine.bootstrap()
    return engine, db_path, agent("agent-a", "Agent A"), agent("agent-b", "Agent B")


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent_and_independent(setup_engine) -> None:
    engine, db_path, _, _ = setup_engine
    await engine.bootstrap()
    async with get_db(db_path) as db:
        cursor = await db.execute("PRAGMA quick_check")
        assert (await cursor.fetchone())[0] == "ok"
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in await cursor.fetchall()}
    assert "conversations" in tables
    assert "turns" in tables
    assert "agent_projections" in tables
    assert "agents" not in tables
    assert "facts" not in tables
    assert "chunks" not in tables


@pytest.mark.asyncio
async def test_dialogue_claim_reply_alternation_and_consensus(setup_engine) -> None:
    engine, db_path, a, b = setup_engine
    created = await engine.create_conversation(
        a,
        [a, b],
        ConversationCreateRequest(
            name="Architecture review",
            objective="Agree on a safe extraction architecture",
            participant_agent_ids=[a.id, b.id],
            first_agent_id=a.id,
            max_rounds=3,
            turn_timeout_seconds=60,
            delay_seconds=0,
        ),
    )
    assert created["status"] == "draft"

    started = await engine.start_conversation(a, created["id"])
    assert started["status"] == "active"
    assert started["current_round"] == 1
    assert started["current_turn"]["assigned_agent_id"] == a.id
    assert started["current_turn"]["turn_in_round"] == 1

    inbox_a = await engine.inbox(a)
    assert len(inbox_a["turns"]) == 1
    turn_a = inbox_a["turns"][0]["id"]
    claim_a = await engine.claim_turn(a, turn_a)
    assert claim_a["lease_token"]

    after_a = await engine.reply_turn(
        a,
        turn_a,
        ReplyRequest(
            content="Separate orchestration state from Brain storage.",
            result="continue",
            lease_token=claim_a["lease_token"],
        ),
    )
    assert after_a["current_turn"]["assigned_agent_id"] == b.id
    assert after_a["current_turn"]["turn_in_round"] == 2

    inbox_b = await engine.inbox(b)
    turn_b = inbox_b["turns"][0]["id"]
    claim_b = await engine.claim_turn(b, turn_b)
    proposal = await engine.reply_turn(
        b,
        turn_b,
        ReplyRequest(
            content="Agreed. I propose completion.",
            result="complete",
            lease_token=claim_b["lease_token"],
        ),
    )
    assert proposal["status"] == "active"
    assert proposal["completion_pending"] is True
    assert proposal["current_turn"]["assigned_agent_id"] == a.id
    assert proposal["current_turn"]["phase"] == "completion_confirmation"

    confirmation_turn = (await engine.inbox(a))["turns"][0]["id"]
    confirmation_claim = await engine.claim_turn(a, confirmation_turn)
    completed = await engine.reply_turn(
        a,
        confirmation_turn,
        ReplyRequest(
            content="Confirmed: independent service plus thin Pluribus adapters.",
            result="complete",
            lease_token=confirmation_claim["lease_token"],
        ),
    )
    assert completed["status"] == "completed"
    assert completed["current_turn_id"] is None
    assert completed["summary_status"] == "pending"

    messages = await engine.list_messages(a, created["id"])
    assert len(messages) == 4
    assert messages[0]["message_type"] == "control"
    assert messages[-1]["turn_result"] == "complete"

    async with get_db(db_path) as db:
        cursor = await db.execute(
            "SELECT status, content FROM summary_outbox WHERE conversation_id=?",
            (created["id"],),
        )
        outbox = await cursor.fetchone()
        assert outbox["status"] == "pending"
        assert "independent service" in outbox["content"]


@pytest.mark.asyncio
async def test_reply_requires_valid_lease(setup_engine) -> None:
    engine, _, a, b = setup_engine
    created = await engine.create_conversation(
        a,
        [a, b],
        ConversationCreateRequest(
            name="Lease test",
            objective="Verify leases",
            participant_agent_ids=[a.id, b.id],
            delay_seconds=0,
        ),
    )
    await engine.start_conversation(a, created["id"])
    turn_id = (await engine.inbox(a))["turns"][0]["id"]
    await engine.claim_turn(a, turn_id)

    with pytest.raises(ConflictError, match="lease token invàlid"):
        await engine.reply_turn(
            a,
            turn_id,
            ReplyRequest(
                content="must fail",
                result="continue",
                lease_token="invalid-token-0000",
            ),
        )


@pytest.mark.asyncio
async def test_restart_retains_active_conversation(setup_engine) -> None:
    engine, db_path, a, b = setup_engine
    created = await engine.create_conversation(
        a,
        [a, b],
        ConversationCreateRequest(
            name="Restart",
            objective="Persistence",
            participant_agent_ids=[a.id, b.id],
            delay_seconds=0,
        ),
    )
    await engine.start_conversation(a, created["id"])

    restarted = ConversationEngine(db_path)
    await restarted.bootstrap()
    restored = await restarted.get_conversation(a, created["id"])
    assert restored["status"] == "active"
    assert restored["current_turn"]["assigned_agent_id"] == a.id
