"""UX-5.2 task queue schema and contract tests."""
from __future__ import annotations

import sqlite3
import tempfile

import pytest

from xerrameca.transport import (
    TaskStatus,
    ResponseStatus,
    TaskEnvelope,
    ClaimDecision,
    make_idempotency_key,
)
from xerrameca.persistence.schema import SCHEMA_SQL


class TestTaskQueueSchema:
    def test_table_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_queue'")
        assert cursor.fetchone() is not None
        conn.close()
        import os
        os.unlink(db_path)

    def test_all_required_columns(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        required = [
            "task_id", "idempotency_key", "agent_id", "conversation_id",
            "turn_id", "sequence", "epoch", "objective", "roles_json",
            "history_ref", "status", "lease_owner", "lease_expires_at",
            "claim_token", "response_payload", "response_status",
            "last_error", "discard_reason", "created_at", "updated_at",
            "available_after", "retries", "max_retries",
        ]
        cursor = conn.execute("PRAGMA table_info(task_queue)")
        cols = {row[1] for row in cursor.fetchall()}
        for c in required:
            assert c in cols, f"Missing column: {c}"
        conn.close()
        import os
        os.unlink(db_path)

    def test_unique_idempotency_key(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO task_queue (task_id, idempotency_key, agent_id, conversation_id, turn_id, sequence, epoch, objective, roles_json, history_ref, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "key1", "agent1", "conv1", "turn1", 1, 1, "obj", "{}", "ref", "pending"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_queue (task_id, idempotency_key, agent_id, conversation_id, turn_id, sequence, epoch, objective, roles_json, history_ref, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("t2", "key1", "agent1", "conv1", "turn2", 2, 1, "obj2", "{}", "ref", "pending"),
            )
            conn.commit()
        conn.close()
        import os
        os.unlink(db_path)

    def test_indexes(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='task_queue'")
        idx_names = {row[0] for row in cursor.fetchall()}
        assert "sqlite_autoindex_task_queue_1" in idx_names  # UNIQUE idempotency_key
        assert "sqlite_autoindex_task_queue_2" in idx_names  # PRIMARY KEY on task_id (sqlite naming may vary)
        conn.close()
        import os
        os.unlink(db_path)

    def test_defaults(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO task_queue (task_id, idempotency_key, agent_id, conversation_id, turn_id, sequence, epoch, objective, roles_json, history_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "key1", "agent1", "conv1", "turn1", 1, 1, "obj", "{}", "ref"),
        )
        conn.commit()
        cursor = conn.execute("SELECT status, response_status, retries, max_retries FROM task_queue WHERE task_id='t1'")
        row = cursor.fetchone()
        assert row == ("pending", "none", 0, 3)
        conn.close()
        import os
        os.unlink(db_path)

    def test_duplicate_insert_controlled(self):
        # Insert same idempotency_key should fail (controlled duplicate)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO task_queue (task_id, idempotency_key, agent_id, conversation_id, turn_id, sequence, epoch, objective, roles_json, history_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "dup_key", "agent1", "conv1", "turn1", 1, 1, "obj", "{}", "ref"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_queue (task_id, idempotency_key, agent_id, conversation_id, turn_id, sequence, epoch, objective, roles_json, history_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("t2", "dup_key", "agent1", "conv1", "turn1", 1, 1, "obj", "{}", "ref"),
            )
        conn.close()
        import os
        os.unlink(db_path)

    def test_existing_schema_unaffected(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        # Verify that conversations, turns, messages tables still exist
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "conversations" in tables
        assert "turns" in tables
        assert "messages" in tables
        assert "task_queue" in tables
        conn.close()
        import os
        os.unlink(db_path)

    def test_queue_reconstructible(self):
        # A queue entry should be reconstructible from federated events (simulated here by checking fields reference external objects)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO task_queue (task_id, idempotency_key, agent_id, conversation_id, turn_id, sequence, epoch, objective, roles_json, history_ref, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "key1", "agent1", "conv1", "turn1", 5, 2, "reconstruct", "{}", "conv_ref", "pending"),
        )
        conn.commit()
        cursor = conn.execute("SELECT conversation_id, turn_id, sequence, epoch FROM task_queue WHERE task_id='t1'")
        row = cursor.fetchone()
        assert row == ("conv1", "turn1", 5, 2)
        conn.close()
        import os
        os.unlink(db_path)
