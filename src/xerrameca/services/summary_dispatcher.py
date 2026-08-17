from __future__ import annotations

import asyncio
import json
from typing import Any

from ..db import get_db
from ..ports.memory import MemoryPort


class SummaryDispatcher:
    """Deliver completed-conversation summaries through an optional MemoryPort.

    Conversation terminal state is authoritative locally. Provider failures only
    change summary delivery state; they never roll back a completed conversation.
    """

    def __init__(
        self,
        db_path: str,
        memory: MemoryPort,
        *,
        max_attempts: int = 10,
    ) -> None:
        self.db_path = db_path
        self.memory = memory
        self.max_attempts = max_attempts

    async def dispatch_pending(self, *, limit: int = 20) -> dict[str, int]:
        async with get_db(self.db_path) as db:
            cursor = await db.execute(
                """SELECT o.id AS outbox_id, o.conversation_id, o.scope,
                          o.content, o.metadata_json, o.attempts,
                          c.name, c.objective, c.status, c.current_round
                   FROM summary_outbox o
                   JOIN conversations c ON c.id = o.conversation_id
                   WHERE o.status IN ('pending','failed')
                     AND o.attempts < ?
                     AND c.status IN ('completed','blocked','error','cancelled')
                   ORDER BY o.id
                   LIMIT ?""",
                (self.max_attempts, limit),
            )
            rows = [dict(row) for row in await cursor.fetchall()]

        stored = 0
        failed = 0
        for row in rows:
            try:
                metadata_raw = row.get("metadata_json") or "{}"
                metadata = json.loads(metadata_raw)
                if not isinstance(metadata, dict):
                    metadata = {}
                result = await self.memory.persist_summary(
                    conversation_id=row["conversation_id"],
                    scope=row["scope"],
                    title=row["name"],
                    objective=row["objective"],
                    status=row["status"],
                    rounds=int(row["current_round"]),
                    content=row["content"],
                    metadata=metadata,
                )
            except Exception as exc:
                # Keep failure text bounded; provider credentials are never part
                # of the persisted payload and adapter errors must not echo them.
                error_text = f"{type(exc).__name__}: {exc}"[:500]
                async with get_db(self.db_path) as db:
                    await db.execute(
                        """UPDATE summary_outbox
                           SET status='failed', attempts=attempts+1,
                               last_error=?, updated_at=datetime('now')
                           WHERE id=?""",
                        (error_text, row["outbox_id"]),
                    )
                    await db.execute(
                        """UPDATE conversations SET summary_status='failed'
                           WHERE id=?""",
                        (row["conversation_id"],),
                    )
                    await db.commit()
                failed += 1
                continue

            async with get_db(self.db_path) as db:
                await db.execute(
                    """UPDATE summary_outbox
                       SET status='stored', attempts=attempts+1,
                           external_id=?, last_error=NULL,
                           updated_at=datetime('now')
                       WHERE id=?""",
                    (result.external_id, row["outbox_id"]),
                )
                await db.execute(
                    """UPDATE conversations
                       SET summary_status='stored', summary_external_id=?
                       WHERE id=?""",
                    (result.external_id, row["conversation_id"]),
                )
                await db.commit()
            stored += 1

        return {"processed": len(rows), "stored": stored, "failed": failed}

    async def loop(self, *, interval_seconds: float = 30.0) -> None:
        while True:
            try:
                await self.dispatch_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The dispatcher is best-effort and isolated from API health.
                pass
            await asyncio.sleep(interval_seconds)
