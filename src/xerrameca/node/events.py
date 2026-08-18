from __future__ import annotations

import base64
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..domain.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from .identity import load_node_state
from .trust import _load_private_key, _public_key_bytes, get_peer


FEDERATED_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS federated_events (
    event_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    coordinator_id TEXT NOT NULL,
    coordinator_epoch INTEGER NOT NULL CHECK (coordinator_epoch >= 1),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    author_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    signature TEXT NOT NULL,
    UNIQUE (conversation_id, coordinator_epoch, sequence)
);
CREATE INDEX IF NOT EXISTS idx_federated_events_conversation
ON federated_events(conversation_id, coordinator_epoch, sequence);

CREATE TABLE IF NOT EXISTS federated_heads (
    conversation_id TEXT PRIMARY KEY,
    coordinator_id TEXT NOT NULL,
    coordinator_epoch INTEGER NOT NULL CHECK (coordinator_epoch >= 1),
    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 1),
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS replication_cursors (
    peer_node_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    coordinator_epoch INTEGER NOT NULL CHECK (coordinator_epoch >= 1),
    acked_sequence INTEGER NOT NULL DEFAULT 0 CHECK (acked_sequence >= 0),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (peer_node_id, conversation_id, coordinator_epoch)
);
"""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise ValidationError("invalid event signature encoding") from exc


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    conversation_id: str
    event_id: str
    sequence: int
    coordinator_id: str
    coordinator_epoch: int
    author_id: str
    event_type: str
    timestamp: int
    payload: dict[str, Any]
    signature: str

    def signing_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "coordinator_id": self.coordinator_id,
            "coordinator_epoch": self.coordinator_epoch,
            "author_id": self.author_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.signing_dict(), "signature": self.signature}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EventEnvelope":
        try:
            payload = raw["payload"]
            if not isinstance(payload, dict):
                raise TypeError("payload must be an object")
            event = cls(
                conversation_id=str(raw["conversation_id"]),
                event_id=str(raw["event_id"]),
                sequence=int(raw["sequence"]),
                coordinator_id=str(raw["coordinator_id"]),
                coordinator_epoch=int(raw["coordinator_epoch"]),
                author_id=str(raw["author_id"]),
                event_type=str(raw["event_type"]),
                timestamp=int(raw["timestamp"]),
                payload=dict(payload),
                signature=str(raw["signature"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid federated event envelope") from exc
        if (
            not event.conversation_id
            or not event.event_id
            or event.sequence < 1
            or event.coordinator_epoch < 1
            or not event.coordinator_id
            or not event.author_id
            or not event.event_type
            or not event.signature
        ):
            raise ValidationError("invalid federated event fields")
        return event


@dataclass(frozen=True, slots=True)
class ConversationHead:
    conversation_id: str
    coordinator_id: str
    coordinator_epoch: int
    last_sequence: int


class EventStore:
    """Durable append-only event store for one Xerrameca node.

    SQLite is strictly local. Replication operates on EventEnvelope objects;
    database files are never copied or opened by another node.
    """

    def __init__(self, state_dir: str | Path):
        self.state = load_node_state(state_dir)
        self.state_dir = self.state.state_dir
        self.db_path = self.state.db_path
        self._bootstrap()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _bootstrap(self) -> None:
        with self._connect() as conn:
            conn.executescript(FEDERATED_SCHEMA_SQL)

    def _head(self, conn: sqlite3.Connection, conversation_id: str) -> ConversationHead | None:
        row = conn.execute(
            "SELECT conversation_id, coordinator_id, coordinator_epoch, last_sequence "
            "FROM federated_heads WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return ConversationHead(
            conversation_id=row["conversation_id"],
            coordinator_id=row["coordinator_id"],
            coordinator_epoch=int(row["coordinator_epoch"]),
            last_sequence=int(row["last_sequence"]),
        )

    def get_head(self, conversation_id: str) -> ConversationHead:
        with self._connect() as conn:
            head = self._head(conn, conversation_id)
        if head is None:
            raise NotFoundError("federated conversation not found")
        return head

    def _public_key_for(self, coordinator_id: str) -> bytes:
        if coordinator_id == self.state.node_id:
            return _public_key_bytes(self.state.public_key)
        peer = get_peer(self.state_dir, coordinator_id)
        if peer.trust_status != "trusted":
            raise ForbiddenError("event coordinator is not a trusted peer")
        return _public_key_bytes(peer.public_key)

    def verify_event_signature(self, event: EventEnvelope) -> None:
        try:
            Ed25519PublicKey.from_public_bytes(
                self._public_key_for(event.coordinator_id)
            ).verify(_unb64(event.signature), _canonical(event.signing_dict()))
        except InvalidSignature as exc:
            raise ForbiddenError("invalid federated event signature") from exc

    def _sign_event(
        self,
        *,
        conversation_id: str,
        coordinator_id: str,
        coordinator_epoch: int,
        sequence: int,
        author_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> EventEnvelope:
        if coordinator_id != self.state.node_id:
            raise ForbiddenError("only the local coordinator may sign authoritative events")
        unsigned = EventEnvelope(
            conversation_id=conversation_id,
            event_id=uuid.uuid4().hex,
            sequence=sequence,
            coordinator_id=coordinator_id,
            coordinator_epoch=coordinator_epoch,
            author_id=author_id,
            event_type=event_type,
            timestamp=int(time.time()),
            payload=payload,
            signature="pending",
        )
        signature = _b64(
            _load_private_key(self.state_dir).sign(_canonical(unsigned.signing_dict()))
        )
        return EventEnvelope(**{**unsigned.signing_dict(), "signature": signature})

    def append_local(
        self,
        conversation_id: str,
        *,
        author_id: str,
        event_type: str,
        payload: dict[str, Any],
        expected_epoch: int | None = None,
    ) -> EventEnvelope:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            head = self._head(conn, conversation_id)
            if head is None:
                if event_type != "conversation.created":
                    raise ConflictError("first event must be conversation.created")
                epoch = 1
                sequence = 1
                coordinator_id = self.state.node_id
            else:
                epoch = head.coordinator_epoch
                if expected_epoch is not None and expected_epoch != epoch:
                    raise ConflictError("coordinator epoch mismatch")
                if head.coordinator_id != self.state.node_id:
                    raise ForbiddenError("local node is not conversation coordinator")
                coordinator_id = head.coordinator_id
                sequence = head.last_sequence + 1

            event = self._sign_event(
                conversation_id=conversation_id,
                coordinator_id=coordinator_id,
                coordinator_epoch=epoch,
                sequence=sequence,
                author_id=author_id,
                event_type=event_type,
                payload=payload,
            )
            self._insert_verified(conn, event)
            conn.commit()
            return event

    def advance_epoch(
        self,
        conversation_id: str,
        *,
        previous_epoch: int,
        previous_coordinator_id: str,
        reason: str = "controlled-transfer",
    ) -> EventEnvelope:
        """Controlled epoch transfer primitive used before automatic failover exists."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            head = self._head(conn, conversation_id)
            if head is None:
                raise NotFoundError("federated conversation not found")
            if head.coordinator_epoch != previous_epoch:
                raise ConflictError("stale previous coordinator epoch")
            if head.coordinator_id != previous_coordinator_id:
                raise ConflictError("previous coordinator mismatch")
            new_epoch = previous_epoch + 1
            event = self._sign_event(
                conversation_id=conversation_id,
                coordinator_id=self.state.node_id,
                coordinator_epoch=new_epoch,
                sequence=1,
                author_id=self.state.node_id,
                event_type="coordinator.changed",
                payload={
                    "previous_epoch": previous_epoch,
                    "previous_coordinator_id": previous_coordinator_id,
                    "coordinator_id": self.state.node_id,
                    "reason": reason,
                },
            )
            self._insert_verified(conn, event)
            conn.commit()
            return event

    def _existing_by_id(
        self, conn: sqlite3.Connection, event_id: str
    ) -> EventEnvelope | None:
        row = conn.execute(
            "SELECT * FROM federated_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return self._row_to_event(row) if row else None

    def _row_to_event(self, row: sqlite3.Row) -> EventEnvelope:
        return EventEnvelope(
            conversation_id=row["conversation_id"],
            event_id=row["event_id"],
            sequence=int(row["sequence"]),
            coordinator_id=row["coordinator_id"],
            coordinator_epoch=int(row["coordinator_epoch"]),
            author_id=row["author_id"],
            event_type=row["event_type"],
            timestamp=int(row["timestamp"]),
            payload=json.loads(row["payload_json"]),
            signature=row["signature"],
        )

    def _insert_verified(self, conn: sqlite3.Connection, event: EventEnvelope) -> bool:
        existing = self._existing_by_id(conn, event.event_id)
        if existing is not None:
            if existing.to_dict() != event.to_dict():
                raise ConflictError("event_id collision with different content")
            return False

        self.verify_event_signature(event)
        head = self._head(conn, event.conversation_id)
        if head is None:
            if (
                event.coordinator_epoch != 1
                or event.sequence != 1
                or event.event_type != "conversation.created"
            ):
                raise ConflictError("invalid first federated event")
        elif event.coordinator_epoch < head.coordinator_epoch:
            raise ConflictError("stale coordinator epoch")
        elif event.coordinator_epoch == head.coordinator_epoch:
            if event.coordinator_id != head.coordinator_id:
                raise ConflictError("coordinator changed without epoch increment")
            if event.sequence != head.last_sequence + 1:
                row = conn.execute(
                    "SELECT event_id FROM federated_events WHERE conversation_id = ? "
                    "AND coordinator_epoch = ? AND sequence = ?",
                    (
                        event.conversation_id,
                        event.coordinator_epoch,
                        event.sequence,
                    ),
                ).fetchone()
                if row is not None:
                    raise ConflictError("sequence collision")
                raise ConflictError("non-contiguous event sequence")
        else:
            if event.coordinator_epoch != head.coordinator_epoch + 1:
                raise ConflictError("coordinator epoch jump")
            if event.sequence != 1 or event.event_type != "coordinator.changed":
                raise ConflictError("new coordinator epoch must start with coordinator.changed")
            if int(event.payload.get("previous_epoch", -1)) != head.coordinator_epoch:
                raise ConflictError("coordinator.changed previous_epoch mismatch")
            if event.payload.get("previous_coordinator_id") != head.coordinator_id:
                raise ConflictError("coordinator.changed previous coordinator mismatch")
            if event.payload.get("coordinator_id") != event.coordinator_id:
                raise ConflictError("coordinator.changed new coordinator mismatch")

        conn.execute(
            "INSERT INTO federated_events "
            "(event_id, conversation_id, coordinator_id, coordinator_epoch, sequence, "
            "author_id, event_type, timestamp, payload_json, signature) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.conversation_id,
                event.coordinator_id,
                event.coordinator_epoch,
                event.sequence,
                event.author_id,
                event.event_type,
                event.timestamp,
                json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
                event.signature,
            ),
        )
        conn.execute(
            "INSERT INTO federated_heads "
            "(conversation_id, coordinator_id, coordinator_epoch, last_sequence, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET "
            "coordinator_id=excluded.coordinator_id, "
            "coordinator_epoch=excluded.coordinator_epoch, "
            "last_sequence=excluded.last_sequence, updated_at=excluded.updated_at",
            (
                event.conversation_id,
                event.coordinator_id,
                event.coordinator_epoch,
                event.sequence,
                int(time.time()),
            ),
        )
        return True

    def ingest(self, event: EventEnvelope) -> bool:
        return self.ingest_many([event]) > 0

    def ingest_many(self, events: Iterable[EventEnvelope]) -> int:
        inserted = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for event in events:
                    if self._insert_verified(conn, event):
                        inserted += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return inserted

    def list_events(
        self,
        conversation_id: str,
        *,
        epoch: int | None = None,
        from_sequence: int = 1,
        to_sequence: int | None = None,
    ) -> list[EventEnvelope]:
        if from_sequence < 1:
            raise ValidationError("from_sequence must be >= 1")
        clauses = ["conversation_id = ?"]
        params: list[object] = [conversation_id]
        if epoch is not None:
            clauses.append("coordinator_epoch = ?")
            params.append(epoch)
            clauses.append("sequence >= ?")
            params.append(from_sequence)
            if to_sequence is not None:
                if to_sequence < from_sequence:
                    raise ValidationError("to_sequence must be >= from_sequence")
                clauses.append("sequence <= ?")
                params.append(to_sequence)
        elif from_sequence != 1 or to_sequence is not None:
            raise ValidationError("sequence range requires an explicit epoch")
        sql = (
            "SELECT * FROM federated_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY coordinator_epoch, sequence"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_event(row) for row in rows]

    def ack(
        self,
        peer_node_id: str,
        conversation_id: str,
        coordinator_epoch: int,
        sequence: int,
    ) -> int:
        if sequence < 0:
            raise ValidationError("acked sequence must be >= 0")
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO replication_cursors "
                "(peer_node_id, conversation_id, coordinator_epoch, acked_sequence, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(peer_node_id, conversation_id, coordinator_epoch) DO UPDATE SET "
                "acked_sequence=MAX(replication_cursors.acked_sequence, excluded.acked_sequence), "
                "updated_at=excluded.updated_at",
                (peer_node_id, conversation_id, coordinator_epoch, sequence, now),
            )
            row = conn.execute(
                "SELECT acked_sequence FROM replication_cursors WHERE peer_node_id = ? "
                "AND conversation_id = ? AND coordinator_epoch = ?",
                (peer_node_id, conversation_id, coordinator_epoch),
            ).fetchone()
        return int(row["acked_sequence"])

    def cursor(
        self, peer_node_id: str, conversation_id: str, coordinator_epoch: int
    ) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT acked_sequence FROM replication_cursors WHERE peer_node_id = ? "
                "AND conversation_id = ? AND coordinator_epoch = ?",
                (peer_node_id, conversation_id, coordinator_epoch),
            ).fetchone()
        return int(row["acked_sequence"]) if row else 0
