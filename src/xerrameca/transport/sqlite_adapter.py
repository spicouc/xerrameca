"""SqliteTaskQueueAdapter — PHASE 3.

Only touches task_queue. No worker, no reconciler, no claim adapter.
Atomic lease via BEGIN IMMEDIATE.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
import uuid
from typing import Optional

from xerrameca.transport import (
    TaskEnvelope,
    ClaimDecision,
    make_idempotency_key,
    AgentTransport,

)


class SqliteTaskQueueAdapter(AgentTransport):
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit off for BEGIN IMMEDIATE
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def insert_task_idempotent(self, envelope: TaskEnvelope) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "SELECT 1 FROM task_queue WHERE idempotency_key = ?",
                (envelope.idempotency_key,),
            )
            if cursor.fetchone() is not None:
                conn.execute("COMMIT")
                return False  # controlled duplicate
            conn.execute(
                """INSERT INTO task_queue
                (task_id, idempotency_key, agent_id, conversation_id, turn_id, sequence, epoch,
                 objective, roles_json, history_ref, status, lease_owner, lease_expires_at,
                 claim_token, response_payload, response_status, last_error, discard_reason,
                 created_at, updated_at, available_after, retries, max_retries)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    envelope.task_id,
                    envelope.idempotency_key,
                    envelope.agent_id,
                    envelope.conversation_id,
                    envelope.turn_id,
                    envelope.sequence,
                    envelope.epoch,
                    envelope.objective,
                    envelope.roles_json,
                    envelope.history_ref,
                    "pending",
                    None,
                    None,
                    None,
                    None,
                    "none",
                    None,
                    None,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    0,
                    3,
                ),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def lease_next_task(self, agent_id: str) -> TaskEnvelope | None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Select eligible pending task deterministically
            cursor = conn.execute(
                """SELECT task_id, idempotency_key, agent_id, conversation_id, turn_id, sequence, epoch,
                    objective, roles_json, history_ref, status, lease_owner, lease_expires_at,
                    claim_token, response_payload, response_status, retries, max_retries,
                    available_after, created_at
                    FROM task_queue
                    WHERE status = 'pending'
                      AND available_after <= datetime('now')
                      AND (agent_id = ? OR agent_id IS NULL)
                    ORDER BY available_after ASC, created_at ASC, task_id ASC
                    LIMIT 1""",
                (agent_id,),
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            task_id = row[0]
            claim_token = str(uuid.uuid4())
            lease_owner = agent_id
            lease_expires_at = time.time() + 300  # 5 min lease
            conn.execute(
                "UPDATE task_queue SET status = 'leased', lease_owner = ?, lease_expires_at = ?, claim_token = ? WHERE task_id = ?",
                (lease_owner, lease_expires_at, claim_token, task_id),
            )
            conn.execute("COMMIT")
            return TaskEnvelope(
                task_id=task_id,
                idempotency_key=row[1],
                agent_id=row[2] or agent_id,
                conversation_id=row[3],
                turn_id=row[4],
                sequence=row[5],
                epoch=row[6],
                objective=row[7],
                roles_json=row[8],
                history_ref=row[9],
                claim_token=claim_token,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                response_payload=None,
                response_status="none",
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def submit_response(self, envelope: TaskEnvelope, response: str) -> ClaimDecision:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "SELECT claim_token FROM task_queue WHERE task_id = ?",
                (envelope.task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute("COMMIT")
                return ClaimDecision(ok=False, reason="task_not_found")
            current_token = row[0]
            if current_token != envelope.claim_token:
                conn.execute("COMMIT")
                return ClaimDecision(ok=False, reason="stale_claim_token")
            conn.execute(
                "UPDATE task_queue SET status = 'leased', response_payload = ?, response_status = 'submitted', updated_at = datetime('now') WHERE task_id = ?",
                (response, envelope.task_id),
            )
            conn.execute("COMMIT")
            return ClaimDecision(ok=True, reason="submitted")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def fail_task(self, envelope: TaskEnvelope, reason: str) -> ClaimDecision:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "SELECT claim_token, retries, max_retries FROM task_queue WHERE task_id = ?",
                (envelope.task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute("COMMIT")
                return ClaimDecision(ok=False, reason="task_not_found")
            current_token, retries, max_retries = row
            if current_token != envelope.claim_token:
                conn.execute("COMMIT")
                return ClaimDecision(ok=False, reason="stale_claim_token")
            new_retries = retries + 1
            if new_retries < max_retries:
                # retry: pending with backoff
                conn.execute(
                    "UPDATE task_queue SET status = 'pending', retries = ?, lease_owner = NULL, lease_expires_at = NULL, claim_token = NULL, available_after = datetime('now', '+30 seconds'), updated_at = datetime('now'), last_error = ? WHERE task_id = ?",
                    (new_retries, reason, envelope.task_id),
                )
            else:
                # terminal failed
                conn.execute(
                    "UPDATE task_queue SET status = 'failed', retries = ?, lease_owner = NULL, lease_expires_at = NULL, claim_token = NULL, last_error = ?, updated_at = datetime('now') WHERE task_id = ?",
                    (new_retries, reason, envelope.task_id),
                )
            conn.execute("COMMIT")
            return ClaimDecision(ok=True, reason="failed")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def discard_task(self, envelope: TaskEnvelope) -> ClaimDecision:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "SELECT claim_token FROM task_queue WHERE task_id = ?",
                (envelope.task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute("COMMIT")
                return ClaimDecision(ok=False, reason="task_not_found")
            if row[0] != envelope.claim_token:
                conn.execute("COMMIT")
                return ClaimDecision(ok=False, reason="stale_claim_token")
            conn.execute(
                "UPDATE task_queue SET status = 'discarded', discard_reason = 'stale_or_invalid', lease_owner = NULL, lease_expires_at = NULL, claim_token = NULL, updated_at = datetime('now') WHERE task_id = ?",
                (envelope.task_id,),
            )
            conn.execute("COMMIT")
            return ClaimDecision(ok=True, reason="discarded")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def reset_expired_leases(self) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE task_queue SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL, claim_token = NULL, available_after = datetime('now'), updated_at = datetime('now') WHERE status = 'leased' AND lease_expires_at <= ?",
                (time.time(),),
            )
            count = cursor.rowcount
            conn.execute("COMMIT")
            return count
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


    def mark_response_applied(self, envelope: TaskEnvelope) -> bool:
        """Mark response_status = applied (from submitted)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "SELECT 1 FROM task_queue WHERE task_id = ? AND response_status = 'submitted'",
                (envelope.task_id,),
            )
            if cursor.fetchone() is None:
                conn.execute("COMMIT")
                return False  # not submitted or already processed
            conn.execute(
                "UPDATE task_queue SET response_status = 'applied', updated_at = datetime('now') "
                "WHERE task_id = ?",
                (envelope.task_id,),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def mark_task_done(self, envelope: TaskEnvelope) -> bool:
        """Mark task.status = done (from applied)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "SELECT 1 FROM task_queue WHERE task_id = ? AND response_status = 'applied'",
                (envelope.task_id,),
            )
            if cursor.fetchone() is None:
                conn.execute("COMMIT")
                return False  # not applied or already done
            conn.execute(
                "UPDATE task_queue SET status = 'done', updated_at = datetime('now') "
                "WHERE task_id = ?",
                (envelope.task_id,),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

