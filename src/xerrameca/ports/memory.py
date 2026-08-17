from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PersistedMemory:
    external_id: str


class MemoryPort(Protocol):
    """Optional long-term memory boundary.

    Conversation state always belongs to Xerrameca. This port is only for
    deliberate persistence of final summaries or derived knowledge.
    """

    async def persist_summary(
        self,
        *,
        conversation_id: str,
        scope: str,
        title: str,
        objective: str,
        status: str,
        rounds: int,
        content: str,
        metadata: dict[str, object],
    ) -> PersistedMemory:
        ...
