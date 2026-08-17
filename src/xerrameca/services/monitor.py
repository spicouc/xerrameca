from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..db import get_db


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: str | None, now: datetime) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())[:8000]


def _looks_like_loop(rows: list[dict[str, Any]]) -> bool:
    if len(rows) < 4:
        return False
    tail = rows[-4:]
    return (
        tail[0]["from_agent_id"] == tail[2]["from_agent_id"]
        and tail[1]["from_agent_id"] == tail[3]["from_agent_id"]
        and tail[0]["from_agent_id"] != tail[1]["from_agent_id"]
        and _normalize(tail[0]["content"]) == _normalize(tail[2]["content"])
        and _normalize(tail[1]["content"]) == _normalize(tail[3]["content"])
    )


class PassiveMonitor:
    """Read-only health monitor over Xerrameca-owned state."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def snapshot(
        self,
        *,
        stalled_after_seconds: int = 300,
        near_rounds_threshold: int = 1,
        loop_window: int = 4,
    ) -> dict[str, Any]:
        now = _now_dt()
        async with get_db(self.db_path) as db:
            cursor = await db.execute(
                """SELECT * FROM conversations
                   WHERE status IN ('active','paused','blocked','error')
                   ORDER BY updated_at DESC"""
            )
            conversations = [dict(row) for row in await cursor.fetchall()]
            output: list[dict[str, Any]] = []
            alert_counts = {"critical": 0, "warning": 0, "info": 0}
            status_counts: dict[str, int] = {}

            for conv in conversations:
                status_counts[conv["status"]] = status_counts.get(conv["status"], 0) + 1
                current_turn = None
                if conv.get("current_turn_id"):
                    cursor = await db.execute(
                        """SELECT id, assigned_agent_id, status, available_at,
                                  claimed_by, claimed_at, lease_until, created_at,
                                  dialogue_round, turn_in_round, phase
                           FROM turns WHERE id=?""",
                        (conv["current_turn_id"],),
                    )
                    row = await cursor.fetchone()
                    current_turn = dict(row) if row else None

                cursor = await db.execute(
                    """SELECT from_agent_id, content, created_at
                       FROM messages
                       WHERE conversation_id=? AND from_agent_id IS NOT NULL
                       ORDER BY created_at DESC LIMIT ?""",
                    (conv["id"], max(4, loop_window)),
                )
                recent = [dict(row) for row in await cursor.fetchall()]
                recent.reverse()
                alerts: list[dict[str, Any]] = []

                def add(kind: str, severity: str, message: str, details: dict[str, Any] | None = None) -> None:
                    alerts.append({"type": kind, "severity": severity, "message": message, "details": details or {}})
                    alert_counts[severity] += 1

                if conv["status"] == "active":
                    if current_turn is None:
                        add("no_current_turn", "critical", "Conversa activa sense torn actual")
                    else:
                        if current_turn["status"] == "ready":
                            age = _age_seconds(current_turn["available_at"], now)
                            if age is not None and age >= stalled_after_seconds:
                                add("stalled_ready_turn", "warning", "Torn disponible sense progrés", {"age_seconds": round(age, 1)})
                        if current_turn["status"] == "claimed":
                            lease_until = _parse_time(current_turn["lease_until"])
                            if lease_until is not None and lease_until <= now:
                                add("expired_lease", "warning", "Lease reclamada ja caducada", {"claimed_by": current_turn["claimed_by"]})
                    remaining = int(conv["max_rounds"]) - int(conv["current_round"])
                    if remaining <= near_rounds_threshold:
                        add("near_max_rounds", "info", "Conversa a prop del límit de rondes", {"rounds_remaining": max(0, remaining)})
                    if _looks_like_loop(recent[-max(4, loop_window):]):
                        add("repeated_dialogue_loop", "warning", "Patró de respostes repetides detectat")

                output.append({
                    "id": conv["id"], "name": conv["name"], "status": conv["status"],
                    "current_round": conv["current_round"], "max_rounds": conv["max_rounds"],
                    "updated_at": conv["updated_at"], "current_turn": current_turn, "alerts": alerts,
                })

        return {"generated_at": _now(), "status_counts": status_counts, "alert_counts": alert_counts, "conversations": output}
