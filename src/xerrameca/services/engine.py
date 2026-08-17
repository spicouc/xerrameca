from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import secrets
import uuid
from typing import Any

from ..db import get_db, init_db
from ..domain.errors import ConflictError, ForbiddenError, LockedError, NotFoundError, ValidationError
from ..domain.models import ConversationCreateRequest, ReplyRequest
from ..ports.identity import AgentIdentity
from ..validation import clean_content, clean_identifier, clean_metadata, clean_scope


PROTOCOL_VERSION = "dialogue-v1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _is_admin(agent: AgentIdentity) -> bool:
    return bool(agent.permissions.get("admin", False))


def _require_permission(agent: AgentIdentity, permission: str, scope: str) -> None:
    if _is_admin(agent):
        return
    if not agent.permissions.get(permission, False):
        raise ForbiddenError(f"falta permís '{permission}'")
    if scope not in agent.allowed_scopes:
        raise ForbiddenError(f"scope '{scope}' no permès")
    if not agent.is_active:
        raise ForbiddenError("agent inactiu")


async def _audit(
    db: Any,
    *,
    agent_id: str | None,
    action: str,
    conversation_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        """INSERT INTO audit_events
           (created_at, agent_id, action, conversation_id, payload_json)
           VALUES (?, ?, ?, ?, ?)""",
        (
            _now(),
            agent_id,
            action,
            conversation_id,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


async def _upsert_projection(db: Any, agent: AgentIdentity, provider: str) -> None:
    await db.execute(
        """INSERT INTO agent_projections
           (agent_id, name, permissions_json, allowed_scopes_json,
            capabilities_json, is_active, provider, verified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent_id) DO UPDATE SET
             name=excluded.name,
             permissions_json=excluded.permissions_json,
             allowed_scopes_json=excluded.allowed_scopes_json,
             capabilities_json=excluded.capabilities_json,
             is_active=excluded.is_active,
             provider=excluded.provider,
             verified_at=excluded.verified_at""",
        (
            agent.id,
            agent.name,
            json.dumps(agent.permissions, sort_keys=True),
            json.dumps(list(agent.allowed_scopes)),
            json.dumps(agent.capabilities, sort_keys=True),
            1 if agent.is_active else 0,
            provider,
            _now(),
        ),
    )


async def _conversation(db: Any, conversation_id: str) -> Any:
    conversation_id = clean_identifier(conversation_id, "conversation_id")
    cursor = await db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    row = await cursor.fetchone()
    if not row:
        raise NotFoundError("Xerrameca no trobada")
    return row


async def _participants(db: Any, conversation_id: str, enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = """SELECT p.agent_id, p.role, p.position, p.enabled,
                    a.name, a.is_active
             FROM participants p
             LEFT JOIN agent_projections a ON a.agent_id = p.agent_id
             WHERE p.conversation_id = ?"""
    params: list[Any] = [conversation_id]
    if enabled_only:
        sql += " AND p.enabled = 1"
    sql += " ORDER BY p.position"
    cursor = await db.execute(sql, params)
    return [dict(row) for row in await cursor.fetchall()]


async def _require_participant(db: Any, agent: AgentIdentity, conv: Any, write: bool = False) -> None:
    _require_permission(agent, "write" if write else "read", conv["scope"])
    if _is_admin(agent):
        return
    cursor = await db.execute(
        "SELECT enabled FROM participants WHERE conversation_id = ? AND agent_id = ?",
        (conv["id"], agent.id),
    )
    row = await cursor.fetchone()
    if not row:
        raise ForbiddenError("no ets participant d'aquesta Xerrameca")
    if not bool(row["enabled"]):
        raise LockedError("participant desactivat")


async def _payload(db: Any, conv: Any) -> dict[str, Any]:
    participants = await _participants(db, conv["id"])
    current_turn = None
    if conv["current_turn_id"]:
        cursor = await db.execute(
            """SELECT id, turn_seq, dialogue_round, turn_in_round, phase,
                      assigned_agent_id, status, available_at, claimed_by,
                      claimed_at, lease_until, created_at
               FROM turns WHERE id = ?""",
            (conv["current_turn_id"],),
        )
        row = await cursor.fetchone()
        current_turn = dict(row) if row else None
    return {
        "id": conv["id"],
        "name": conv["name"],
        "objective": conv["objective"],
        "scope": conv["scope"],
        "status": conv["status"],
        "enabled": bool(conv["enabled"]),
        "protocol_version": conv["protocol_version"],
        "turn_policy": conv["turn_policy"],
        "supervisor_agent_id": conv["supervisor_agent_id"],
        "first_agent_id": conv["first_agent_id"],
        "max_rounds": conv["max_rounds"],
        "turn_timeout_seconds": conv["turn_timeout_seconds"],
        "delay_seconds": conv["delay_seconds"],
        "current_round": conv["current_round"],
        "current_turn_id": conv["current_turn_id"],
        "block_reason": conv["block_reason"],
        "completion_proposed_by_agent_id": conv["completion_proposed_by_agent_id"],
        "completion_pending": bool(conv["completion_proposed_by_agent_id"]),
        "persist_summary": bool(conv["persist_summary"]),
        "summary_status": conv["summary_status"],
        "summary_external_id": conv["summary_external_id"],
        "created_by_agent_id": conv["created_by_agent_id"],
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
        "started_at": conv["started_at"],
        "finished_at": conv["finished_at"],
        "participants": participants,
        "current_turn": current_turn,
    }


def _kickoff(conv: Any, participants: list[dict[str, Any]]) -> str:
    names = "\n".join(
        f"- {p.get('name') or p['agent_id']} ({p['agent_id']})" for p in participants
    )
    completion = (
        "El supervisor pot finalitzar la conversa; una proposta externa requereix confirmació del supervisor."
        if conv["turn_policy"] == "supervisor"
        else "La finalització requereix consens: un agent proposa complete i l'altre confirma complete."
    )
    return (
        "XERRAMECA DIALOGUE PROTOCOL v1\n\n"
        f"Objectiu:\n{conv['objective']}\n\n"
        f"Participants:\n{names}\n\n"
        f"Política: {conv['turn_policy']}\n"
        f"Màxim de rondes: {conv['max_rounds']}\n"
        f"Timeout: {conv['turn_timeout_seconds']} s\n"
        f"Delay: {conv['delay_seconds']} s\n\n"
        f"Regla de finalització: {completion}"
    )


async def _next_seq(db: Any, conversation_id: str) -> int:
    cursor = await db.execute(
        "SELECT COALESCE(MAX(turn_seq), 0) + 1 FROM turns WHERE conversation_id = ?",
        (conversation_id,),
    )
    return int((await cursor.fetchone())[0])


async def _create_turn(
    db: Any,
    *,
    conv: Any,
    assigned_agent_id: str,
    input_message_id: str,
    dialogue_round: int,
    turn_in_round: int,
    phase: str,
    available_at: str,
) -> str:
    turn_id = str(uuid.uuid4())
    seq = await _next_seq(db, conv["id"])
    await db.execute(
        """INSERT INTO turns
           (id, conversation_id, turn_seq, dialogue_round, turn_in_round, phase,
            assigned_agent_id, input_message_id, status, available_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)""",
        (
            turn_id,
            conv["id"],
            seq,
            dialogue_round,
            turn_in_round,
            phase,
            assigned_agent_id,
            input_message_id,
            available_at,
            _now(),
        ),
    )
    return turn_id


def _order(conv: Any, participant_ids: list[str]) -> list[str]:
    if len(participant_ids) != 2:
        raise ConflictError("Dialogue v1 requereix exactament 2 participants")
    first = conv["first_agent_id"]
    if first not in participant_ids:
        raise ConflictError("first_agent_id no disponible")
    other = participant_ids[1] if participant_ids[0] == first else participant_ids[0]
    return [first, other]


async def _queue_summary(db: Any, conv: Any, final_content: str, final_status: str) -> None:
    if not bool(conv["persist_summary"]):
        return
    content = (
        f"Xerrameca: {conv['name']}\n"
        f"Objectiu: {conv['objective']}\n"
        f"Estat final: {final_status}\n"
        f"Rondes: {conv['current_round']}\n"
        f"Resultat final: {final_content}"
    )
    metadata = {
        "xerrameca_conversation_id": conv["id"],
        "status": final_status,
        "rounds": conv["current_round"],
    }
    now = _now()
    await db.execute(
        """INSERT INTO summary_outbox
           (conversation_id, scope, content, metadata_json, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'pending', ?, ?)
           ON CONFLICT(conversation_id) DO UPDATE SET
             content=excluded.content,
             metadata_json=excluded.metadata_json,
             status='pending', updated_at=excluded.updated_at""",
        (conv["id"], conv["scope"], content, json.dumps(metadata, sort_keys=True), now, now),
    )
    await db.execute(
        "UPDATE conversations SET summary_status = 'pending' WHERE id = ?",
        (conv["id"],),
    )


class ConversationEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def bootstrap(self) -> None:
        await init_db(self.db_path)

    async def create_conversation(
        self,
        caller: AgentIdentity,
        participants: list[AgentIdentity],
        body: ConversationCreateRequest,
        *,
        provider: str = "test",
    ) -> dict[str, Any]:
        scope = clean_scope(body.scope)
        _require_permission(caller, "write", scope)
        ids = [clean_identifier(value, "participant_agent_id") for value in body.participant_agent_ids]
        if len(set(ids)) != 2:
            raise ValidationError("els dos participants han de ser diferents")
        by_id = {agent.id: agent for agent in participants}
        if set(ids) != set(by_id):
            raise ValidationError("participant identities no coincideixen amb participant_agent_ids")
        for participant in participants:
            if not participant.is_active:
                raise ValidationError(f"agent '{participant.id}' inactiu")
            if scope not in participant.allowed_scopes and not _is_admin(participant):
                raise ValidationError(f"agent '{participant.id}' no té accés a '{scope}'")
            if not _is_admin(participant) and not (
                participant.permissions.get("read", False) and participant.permissions.get("write", False)
            ):
                raise ValidationError(f"agent '{participant.id}' requereix read + write")
        if caller.id not in ids and not _is_admin(caller):
            raise ForbiddenError("el creador ha de ser participant o admin")
        first = clean_identifier(body.first_agent_id or ids[0], "first_agent_id")
        if first not in ids:
            raise ValidationError("first_agent_id ha de ser participant")
        supervisor = body.supervisor_agent_id
        if body.turn_policy == "supervisor":
            supervisor = clean_identifier(supervisor or caller.id, "supervisor_agent_id")
            if supervisor not in ids:
                raise ValidationError("supervisor_agent_id ha de ser participant")
        elif supervisor is not None:
            raise ValidationError("supervisor_agent_id només és vàlid amb turn_policy=supervisor")

        conversation_id = str(uuid.uuid4())
        now = _now()
        async with get_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for participant in participants:
                    await _upsert_projection(db, participant, provider)
                await _upsert_projection(db, caller, provider)
                await db.execute(
                    """INSERT INTO conversations
                       (id, name, objective, scope, protocol_version, turn_policy,
                        supervisor_agent_id, first_agent_id, max_rounds,
                        turn_timeout_seconds, delay_seconds, persist_summary,
                        created_by_agent_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        conversation_id,
                        clean_content(body.name),
                        clean_content(body.objective),
                        scope,
                        PROTOCOL_VERSION,
                        body.turn_policy,
                        supervisor,
                        first,
                        body.max_rounds,
                        body.turn_timeout_seconds,
                        body.delay_seconds,
                        1 if body.persist_summary else 0,
                        caller.id,
                        now,
                        now,
                    ),
                )
                for position, agent_id in enumerate(ids):
                    role = "supervisor" if supervisor == agent_id else "participant"
                    await db.execute(
                        """INSERT INTO participants
                           (conversation_id, agent_id, role, position, enabled)
                           VALUES (?, ?, ?, ?, 1)""",
                        (conversation_id, agent_id, role, position),
                    )
                await _audit(
                    db,
                    agent_id=caller.id,
                    action="CONVERSATION_CREATE",
                    conversation_id=conversation_id,
                    payload={"participants": ids, "scope": scope},
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            conv = await _conversation(db, conversation_id)
            return await _payload(db, conv)

    async def start_conversation(self, caller: AgentIdentity, conversation_id: str) -> dict[str, Any]:
        async with get_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                conv = await _conversation(db, conversation_id)
                await _require_participant(db, caller, conv, write=True)
                if conv["status"] != "draft":
                    raise ConflictError("la conversa ja ha estat iniciada")
                participants = await _participants(db, conv["id"], enabled_only=True)
                if len(participants) != 2:
                    raise ConflictError("participants no disponibles")
                message_id = str(uuid.uuid4())
                now = _now()
                await db.execute(
                    """INSERT INTO messages
                       (id, conversation_id, turn_seq, dialogue_round, from_agent_id,
                        to_agent_id, message_type, content, metadata_json, created_at)
                       VALUES (?, ?, 0, 1, NULL, ?, 'control', ?, ?, ?)""",
                    (
                        message_id,
                        conv["id"],
                        conv["first_agent_id"],
                        _kickoff(conv, participants),
                        json.dumps({"protocol": PROTOCOL_VERSION, "kind": "kickoff"}),
                        now,
                    ),
                )
                turn_id = await _create_turn(
                    db,
                    conv=conv,
                    assigned_agent_id=conv["first_agent_id"],
                    input_message_id=message_id,
                    dialogue_round=1,
                    turn_in_round=1,
                    phase="dialogue",
                    available_at=now,
                )
                await db.execute(
                    """UPDATE conversations
                       SET status='active', current_round=1, current_turn_id=?,
                           started_at=?, updated_at=? WHERE id=?""",
                    (turn_id, now, now, conv["id"]),
                )
                await _audit(db, agent_id=caller.id, action="CONVERSATION_START", conversation_id=conv["id"])
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return await _payload(db, await _conversation(db, conversation_id))

    async def get_conversation(self, caller: AgentIdentity, conversation_id: str) -> dict[str, Any]:
        async with get_db(self.db_path) as db:
            conv = await _conversation(db, conversation_id)
            await _require_participant(db, caller, conv)
            return await _payload(db, conv)

    async def list_conversations(self, caller: AgentIdentity) -> list[dict[str, Any]]:
        async with get_db(self.db_path) as db:
            if _is_admin(caller):
                cursor = await db.execute("SELECT * FROM conversations ORDER BY created_at DESC")
            else:
                cursor = await db.execute(
                    """SELECT c.* FROM conversations c
                       JOIN participants p ON p.conversation_id=c.id
                       WHERE p.agent_id=? ORDER BY c.created_at DESC""",
                    (caller.id,),
                )
            result = []
            for conv in await cursor.fetchall():
                if _is_admin(caller) or conv["scope"] in caller.allowed_scopes:
                    result.append(await _payload(db, conv))
            return result

    async def list_messages(self, caller: AgentIdentity, conversation_id: str) -> list[dict[str, Any]]:
        async with get_db(self.db_path) as db:
            conv = await _conversation(db, conversation_id)
            await _require_participant(db, caller, conv)
            cursor = await db.execute(
                """SELECT id, turn_id, turn_seq, dialogue_round, from_agent_id,
                          to_agent_id, message_type, content, metadata_json,
                          turn_result, created_at
                   FROM messages WHERE conversation_id=? ORDER BY turn_seq, created_at""",
                (conv["id"],),
            )
            rows = []
            for row in await cursor.fetchall():
                item = dict(row)
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
                rows.append(item)
            return rows

    async def inbox(self, caller: AgentIdentity) -> dict[str, Any]:
        _require_permission(caller, "read", "shared") if not _is_admin(caller) and "shared" in caller.allowed_scopes else None
        now = _now()
        async with get_db(self.db_path) as db:
            cursor = await db.execute(
                """SELECT t.*, c.name, c.objective, c.scope, c.turn_policy,
                          c.turn_timeout_seconds, c.max_rounds, c.delay_seconds,
                          m.content, m.message_type, m.metadata_json
                   FROM turns t
                   JOIN conversations c ON c.id=t.conversation_id
                   JOIN participants p ON p.conversation_id=c.id AND p.agent_id=?
                   JOIN messages m ON m.id=t.input_message_id
                   WHERE t.assigned_agent_id=? AND p.enabled=1
                     AND c.enabled=1 AND c.status='active'
                     AND t.available_at<=?
                     AND (t.status='ready' OR (t.status='claimed' AND t.lease_until<=?))
                   ORDER BY t.created_at""",
                (caller.id, caller.id, now, now),
            )
            rows = []
            for row in await cursor.fetchall():
                if not _is_admin(caller) and row["scope"] not in caller.allowed_scopes:
                    continue
                item = dict(row)
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
                rows.append(item)
            return {"turns": rows}

    async def claim_turn(self, caller: AgentIdentity, turn_id: str) -> dict[str, Any]:
        turn_id = clean_identifier(turn_id, "turn_id")
        async with get_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """SELECT t.*, c.scope, c.status AS conversation_status,
                              c.enabled AS conversation_enabled, c.turn_timeout_seconds
                       FROM turns t JOIN conversations c ON c.id=t.conversation_id
                       WHERE t.id=?""",
                    (turn_id,),
                )
                turn = await cursor.fetchone()
                if not turn:
                    raise NotFoundError("torn no trobat")
                conv = await _conversation(db, turn["conversation_id"])
                await _require_participant(db, caller, conv, write=True)
                if turn["assigned_agent_id"] != caller.id:
                    raise ForbiddenError("aquest torn correspon a un altre agent")
                if conv["status"] != "active" or not bool(conv["enabled"]):
                    raise LockedError("la Xerrameca no està activa")
                now = _now()
                if turn["available_at"] > now:
                    raise ConflictError("el torn encara està en delay")
                if turn["status"] == "claimed" and turn["claimed_by"] == caller.id and turn["lease_until"] > now:
                    token = turn["lease_token"]
                    lease_until = turn["lease_until"]
                else:
                    if turn["status"] not in {"ready", "claimed"}:
                        raise ConflictError("el torn no està disponible")
                    if turn["status"] == "claimed" and turn["lease_until"] and turn["lease_until"] > now:
                        raise ConflictError("el torn ja està reclamat")
                    token = secrets.token_urlsafe(32)
                    lease_until = _after(int(conv["turn_timeout_seconds"]))
                    cursor = await db.execute(
                        """UPDATE turns SET status='claimed', claimed_by=?, lease_token=?,
                                  claimed_at=?, lease_until=?
                           WHERE id=? AND (status='ready' OR (status='claimed' AND lease_until<=?))""",
                        (caller.id, token, now, lease_until, turn_id, now),
                    )
                    if cursor.rowcount != 1:
                        raise ConflictError("el torn acaba de ser reclamat")
                    await _audit(db, agent_id=caller.id, action="TURN_CLAIM", conversation_id=conv["id"], payload={"turn_id": turn_id})
                    await db.commit()
                cursor = await db.execute("SELECT * FROM messages WHERE id=?", (turn["input_message_id"],))
                message = await cursor.fetchone()
                payload = dict(message) if message else None
                if payload:
                    payload["metadata"] = json.loads(payload.pop("metadata_json") or "{}")
                return {
                    "turn_id": turn_id,
                    "conversation_id": turn["conversation_id"],
                    "round": turn["dialogue_round"],
                    "turn_in_round": turn["turn_in_round"],
                    "phase": turn["phase"],
                    "lease_token": token,
                    "lease_until": lease_until,
                    "input_message": payload,
                }
            except Exception:
                await db.rollback()
                raise

    async def reply_turn(self, caller: AgentIdentity, turn_id: str, body: ReplyRequest) -> dict[str, Any]:
        turn_id = clean_identifier(turn_id, "turn_id")
        content = clean_content(body.content)
        metadata = clean_metadata(body.metadata)
        async with get_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("SELECT * FROM turns WHERE id=?", (turn_id,))
                turn = await cursor.fetchone()
                if not turn:
                    raise NotFoundError("torn no trobat")
                conv = await _conversation(db, turn["conversation_id"])
                await _require_participant(db, caller, conv, write=True)
                if conv["status"] != "active" or not bool(conv["enabled"]):
                    raise LockedError("la Xerrameca no està activa")
                if turn["assigned_agent_id"] != caller.id:
                    raise ForbiddenError("aquest torn correspon a un altre agent")
                now = _now()
                if turn["status"] != "claimed" or turn["claimed_by"] != caller.id:
                    raise ConflictError("cal reclamar el torn abans de respondre")
                if not secrets.compare_digest(turn["lease_token"] or "", body.lease_token):
                    raise ConflictError("lease token invàlid")
                if not turn["lease_until"] or turn["lease_until"] <= now:
                    raise ConflictError("la lease del torn ha caducat")

                participants = await _participants(db, conv["id"], enabled_only=True)
                participant_ids = [p["agent_id"] for p in participants]
                order = _order(conv, participant_ids)
                logical_round = int(turn["dialogue_round"])
                slot = int(turn["turn_in_round"])
                confirmation = turn["phase"] == "completion_confirmation" and bool(conv["completion_proposed_by_agent_id"])

                output_id = str(uuid.uuid4())
                await db.execute(
                    """INSERT INTO messages
                       (id, conversation_id, turn_id, turn_seq, dialogue_round,
                        from_agent_id, message_type, content, metadata_json,
                        turn_result, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'message', ?, ?, ?, ?)""",
                    (
                        output_id,
                        conv["id"],
                        turn_id,
                        turn["turn_seq"],
                        logical_round,
                        caller.id,
                        content,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        body.result,
                        now,
                    ),
                )
                await db.execute(
                    "UPDATE turns SET status='completed', completed_at=? WHERE id=?",
                    (now, turn_id),
                )

                terminal_status: str | None = None
                block_reason: str | None = None
                next_agent: str | None = None
                next_round: int | None = None
                next_slot: int | None = None
                next_phase = "dialogue"

                if body.result in {"blocked", "needs_human", "error"}:
                    terminal_status = "error" if body.result == "error" else "blocked"
                    block_reason = body.result
                elif body.result == "complete":
                    if conv["turn_policy"] == "supervisor" and caller.id == conv["supervisor_agent_id"]:
                        terminal_status = "completed"
                    elif confirmation and conv["completion_proposed_by_agent_id"] != caller.id:
                        terminal_status = "completed"
                    else:
                        other = participant_ids[1] if participant_ids[0] == caller.id else participant_ids[0]
                        next_agent = conv["supervisor_agent_id"] if conv["turn_policy"] == "supervisor" else other
                        if not next_agent or next_agent == caller.id:
                            raise ConflictError("no hi ha agent de confirmació")
                        next_round = logical_round
                        next_slot = 0
                        next_phase = "completion_confirmation"
                        await db.execute(
                            """UPDATE conversations SET completion_proposed_by_agent_id=?,
                                      completion_proposed_at=?, completion_proposal_turn_id=?, updated_at=?
                               WHERE id=?""",
                            (caller.id, now, turn_id, now, conv["id"]),
                        )
                elif body.result == "continue":
                    if confirmation:
                        proposal_turn_id = conv["completion_proposal_turn_id"]
                        cursor = await db.execute(
                            "SELECT dialogue_round, turn_in_round FROM turns WHERE id=?",
                            (proposal_turn_id,),
                        )
                        proposal = await cursor.fetchone()
                        if not proposal:
                            raise ConflictError("proposta de finalització inconsistent")
                        p_round = int(proposal["dialogue_round"])
                        p_slot = int(proposal["turn_in_round"])
                        await db.execute(
                            """UPDATE conversations SET completion_proposed_by_agent_id=NULL,
                                      completion_proposed_at=NULL, completion_proposal_turn_id=NULL
                               WHERE id=?""",
                            (conv["id"],),
                        )
                        if p_slot == 1:
                            if p_round >= int(conv["max_rounds"]):
                                terminal_status, block_reason = "blocked", "max_rounds"
                            else:
                                next_agent, next_round, next_slot = order[0], p_round + 1, 1
                        else:
                            if p_round >= int(conv["max_rounds"]):
                                terminal_status, block_reason = "blocked", "max_rounds"
                            else:
                                next_agent, next_round, next_slot = order[1], p_round + 1, 2
                    elif slot == 1:
                        next_agent, next_round, next_slot = order[1], logical_round, 2
                    elif slot == 2:
                        if logical_round >= int(conv["max_rounds"]):
                            terminal_status, block_reason = "blocked", "max_rounds"
                        else:
                            next_agent, next_round, next_slot = order[0], logical_round + 1, 1
                    else:
                        raise ConflictError("posició de torn invàlida")
                else:
                    raise ValidationError("result invàlid")

                if body.next_agent_id is not None:
                    requested = clean_identifier(body.next_agent_id, "next_agent_id")
                    if conv["turn_policy"] != "supervisor" or caller.id != conv["supervisor_agent_id"]:
                        raise ForbiddenError("només el supervisor pot indicar next_agent_id")
                    if next_agent is not None and requested != next_agent:
                        raise ValidationError("dialogue-v1 manté l'ordre dels dos participants")

                if terminal_status:
                    await db.execute(
                        """UPDATE conversations SET status=?, current_turn_id=NULL,
                                  block_reason=?, completion_proposed_by_agent_id=NULL,
                                  completion_proposed_at=NULL, completion_proposal_turn_id=NULL,
                                  finished_at=?, updated_at=? WHERE id=?""",
                        (terminal_status, block_reason, now, now, conv["id"]),
                    )
                    refreshed = await _conversation(db, conv["id"])
                    await _queue_summary(db, refreshed, content, terminal_status)
                    await _audit(
                        db,
                        agent_id=caller.id,
                        action="CONVERSATION_TERMINAL",
                        conversation_id=conv["id"],
                        payload={"status": terminal_status, "reason": block_reason},
                    )
                else:
                    if next_agent is None or next_round is None or next_slot is None:
                        raise ConflictError("no s'ha pogut determinar el torn següent")
                    next_turn_id = await _create_turn(
                        db,
                        conv=conv,
                        assigned_agent_id=next_agent,
                        input_message_id=output_id,
                        dialogue_round=next_round,
                        turn_in_round=next_slot,
                        phase=next_phase,
                        available_at=_after(int(conv["delay_seconds"])),
                    )
                    await db.execute(
                        """UPDATE conversations SET current_turn_id=?, current_round=?, updated_at=?
                           WHERE id=?""",
                        (next_turn_id, max(int(conv["current_round"]), next_round), now, conv["id"]),
                    )
                    await _audit(
                        db,
                        agent_id=caller.id,
                        action="TURN_REPLY",
                        conversation_id=conv["id"],
                        payload={"turn_id": turn_id, "result": body.result, "next_agent_id": next_agent},
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return await _payload(db, await _conversation(db, turn["conversation_id"]))
