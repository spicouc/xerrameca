from __future__ import annotations

from typing import Any

from ..db import get_db
from ..domain.errors import ConflictError, ForbiddenError, NotFoundError
from ..ports.identity import AgentIdentity
from ..validation import clean_identifier
from .engine import _audit, _conversation, _now, _payload


def _is_admin(agent: AgentIdentity) -> bool:
    return bool(agent.permissions.get("admin", False))


async def cancel_conversation(
    db_path: str,
    caller: AgentIdentity,
    conversation_id: str,
) -> dict[str, Any]:
    conversation_id = clean_identifier(conversation_id, "conversation_id")
    async with get_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            conv = await _conversation(db, conversation_id)
            if conv["status"] == "cancelled":
                return await _payload(db, conv)
            if conv["status"] == "completed":
                raise ConflictError("una conversa completada no es pot cancel·lar")
            if not _is_admin(caller) and conv["created_by_agent_id"] != caller.id:
                raise ForbiddenError("només l'iniciador o un admin pot cancel·lar la conversa")
            if not _is_admin(caller):
                cursor = await db.execute(
                    "SELECT 1 FROM participants WHERE conversation_id=? AND agent_id=? AND enabled=1",
                    (conversation_id, caller.id),
                )
                if not await cursor.fetchone():
                    raise ForbiddenError("no ets participant d'aquesta conversa")
            now = _now()
            if conv["current_turn_id"]:
                await db.execute(
                    """UPDATE turns SET status='cancelled', lease_token=NULL,
                              claimed_by=NULL, claimed_at=NULL, lease_until=NULL,
                              completed_at=? WHERE id=? AND status IN ('ready','claimed')""",
                    (now, conv["current_turn_id"]),
                )
            await db.execute(
                """UPDATE conversations SET status='cancelled', current_turn_id=NULL,
                          block_reason='cancelled_by_initiator', finished_at=?, updated_at=?
                   WHERE id=?""",
                (now, now, conversation_id),
            )
            await _audit(
                db,
                agent_id=caller.id,
                action="CONVERSATION_CANCEL",
                conversation_id=conversation_id,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        refreshed = await _conversation(db, conversation_id)
        return await _payload(db, refreshed)
