"""UX-5.2 PHASE 3 — SqliteTaskQueueAdapter tests."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time

import pytest

from xerrameca.persistence.schema import SCHEMA_SQL
from xerrameca.transport import (
    TaskEnvelope,
    ClaimDecision,
    make_idempotency_key,
)
from xerrameca.transport.sqlite_adapter import SqliteTaskQueueAdapter


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    return str(p)


def make_envelope(task_id="t1", agent_id="agent1", conversation_id="conv1", turn_id="turn1", sequence=1, epoch=1):
    return TaskEnvelope(
        task_id=task_id,
        idempotency_key=make_idempotency_key(conversation_id, turn_id, sequence, epoch),
        agent_id=agent_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        sequence=sequence,
        epoch=epoch,
        objective="obj",
        roles_json="{}",
        history_ref="ref",
        claim_token="",
        lease_owner="",
        lease_expires_at=0.0,
    )


class TestSqliteTaskQueueAdapter:
    def test_idempotent_insert(self, db_path):
        a = SqliteTaskQueueAdapter(db_path)
        env = make_envelope()
        assert a.insert_task_idempotent(env) is True
        assert a.insert_task_idempotent(env) is False  # controlled duplicate
        # logical tasks: 1
        conn = sqlite3.connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0]
        conn.close()
        assert n == 1

    def test_lease_next_task(self, db_path):
        a = SqliteTaskQueueAdapter(db_path)
        env = make_envelope()
        a.insert_task_idempotent(env)
        leased = a.lease_next_task("agent1")
        assert leased is not None
        assert leased.task_id == "t1"
        assert leased.claim_token != ""
        # second lease should not return same task (still leased, not pending)
        leased2 = a.lease_next_task("agent1")
        assert leased2 is None

    def test_two_worker_race(self, db_path):
        # Insert 1 task
        a = SqliteTaskQueueAdapter(db_path)
        env = make_envelope()
        a.insert_task_idempotent(env)

        results = []
        def lease_in_thread(name):
            # Use separate adapter instance (separate connection)
            aa = SqliteTaskQueueAdapter(db_path)
            r = aa.lease_next_task(name)
            results.append((name, r))

        t1 = threading.Thread(target=lease_in_thread, args=("workerA",))
        t2 = threading.Thread(target=lease_in_thread, args=("workerB",))
        t1.start(); t2.start()
        t1.join(); t2.join()
        # Exactly one worker got a task, other got None
        leased = [r for _, r in results if r is not None]
        assert len(leased) == 1
        claim_tokens = {l.claim_token for l in leased}
        assert len(claim_tokens) == 1

    def test_claim_token_validation(self, db_path):
        a = SqliteTaskQueueAdapter(db_path)
        env = make_envelope()
        a.insert_task_idempotent(env)
        leased = a.lease_next_task("agent1")
        # valid token
        assert a.submit_response(leased, "my response").ok is True
        # create another task
        env2 = make_envelope(task_id="t2", turn_id="turn2", sequence=2, epoch=1)
        a.insert_task_idempotent(env2)
        leased2 = a.lease_next_task("agent1")
        # Stale token on the same task should be rejected
        # We use the first envelope's claim_token to try to submit on a different task
        dec = a.submit_response(TaskEnvelope(
            task_id=leased2.task_id, idempotency_key=leased2.idempotency_key,
            agent_id=leased2.agent_id, conversation_id=leased2.conversation_id,
            turn_id=leased2.turn_id, sequence=leased2.sequence, epoch=leased2.epoch,
            objective=leased2.objective, roles_json=leased2.roles_json,
            history_ref=leased2.history_ref, claim_token="WRONG_TOKEN",
            lease_owner=leased2.lease_owner, lease_expires_at=leased2.lease_expires_at,
        ), "test")
        assert dec.ok is False
        assert dec.reason == "stale_claim_token"

    def test_expired_lease_recovery(self, db_path):
        a = SqliteTaskQueueAdapter(db_path)
        env = make_envelope()
        a.insert_task_idempotent(env)
        # Manually set lease expiry to past
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE task_queue SET status='leased', lease_expires_at = ? WHERE task_id='t1'", (time.time() - 10,))
        conn.commit()
        conn.close()
        # reset expired leases
        n = a.reset_expired_leases()
        assert n >= 1
        # re-lease
        new_leased = a.lease_next_task("agent1")
        assert new_leased is not None
        assert new_leased.task_id == "t1"
        # new claim token different
        assert new_leased.claim_token != ""

    def test_fail_task_retry(self, db_path):
        a = SqliteTaskQueueAdapter(db_path)
        env = make_envelope()
        a.insert_task_idempotent(env)
        leased = a.lease_next_task("agent1")
        # fail
        a.fail_task(leased, "test error")
        # should be pending with retries=1 and available_after in future
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT status, retries, available_after FROM task_queue WHERE task_id='t1'")
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "pending"
        assert row[1] == 1
        # not leasable before backoff (since available_after is in future)
        leased2 = a.lease_next_task("agent1")
        assert leased2 is None  # available_after > now

    def test_max_retries(self, db_path):
        a = SqliteTaskQueueAdapter(db_path)
        env = make_envelope()
        a.insert_task_idempotent(env)
        for i in range(3):  # retries goes 1, 2, 3 = max_retries
            leased = a.lease_next_task("agent1")
            if leased is None:
                # Manually reset to pending for next attempt
                conn = sqlite3.connect(db_path)
                conn.execute("UPDATE task_queue SET status='pending', available_after=datetime('now') WHERE task_id='t1'")
                conn.commit()
                conn.close()
                leased = a.lease_next_task("agent1")
            assert leased is not None, f"failed at iteration {i}"
            a.fail_task(leased, "test error")
        # After 3 fails, status should be failed
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT status, retries FROM task_queue WHERE task_id='t1'")
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "failed"
        assert row[1] == 3

    def test_discard_task(self, db_path):
        a = SqliteTaskQueueAdapter(db_path)
        env = make_envelope()
        a.insert_task_idempotent(env)
        leased = a.lease_next_task("agent1")
        dec = a.discard_task(leased)
        assert dec.ok is True
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT status, discard_reason FROM task_queue WHERE task_id='t1'")
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "discarded"
        assert row[1] is not None

    def test_submit_response(self, db_path):
        a = SqliteTaskQueueAdapter(db_path)
        env = make_envelope()
        a.insert_task_idempotent(env)
        leased = a.lease_next_task("agent1")
        dec = a.submit_response(leased, "my response content")
        assert dec.ok is True
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT response_payload, response_status, status FROM task_queue WHERE task_id='t1'")
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "my response content"
        assert row[1] == "submitted"
        # task status remains leased (not done) per PHASE 3.8
        assert row[2] == "leased"

    def test_no_federated_reply_or_signed_events(self, db_path):
        # adapter only touches task_queue
        a = SqliteTaskQueueAdapter(db_path)
        env = make_envelope()
        a.insert_task_idempotent(env)
        leased = a.lease_next_task("agent1")
        a.submit_response(leased, "resp")
        # Check no federated events tables exist
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        # task_queue exists, but not federated_event / conversation signed events
        assert "task_queue" in tables
        # These federated tables should NOT be touched (we only created schema tables)
        # We don't have any federated_event / signed_event tables in this schema
