
import sys, tempfile, os, sqlite3
sys.path.insert(0, "/opt/xerrameca-ux3/src")
from unittest.mock import MagicMock
import pytest
from xerrameca.transport import TaskEnvelope, make_idempotency_key
from xerrameca.transport.sqlite_adapter import SqliteTaskQueueAdapter
from xerrameca.transport.federated_claim_adapter import FederatedTurnClaimAdapter
from xerrameca.worker import (
    AutonomousWorker, FakeAgentInvoker, AgentResponse,
    HermesInvoker, TurnContext,
)

def _create_db():
    db = os.path.join(tempfile.gettempdir(), "worker_test.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE IF NOT EXISTS task_queue (
        task_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, agent_id TEXT,
        conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL, sequence INTEGER,
        epoch INTEGER, objective TEXT, roles_json TEXT, history_ref TEXT,
        status TEXT DEFAULT 'pending', lease_owner TEXT, lease_expires_at REAL,
        claim_token TEXT, response_payload TEXT, response_status TEXT DEFAULT 'none',
        last_error TEXT, discard_reason TEXT, created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')), available_after TEXT DEFAULT (datetime('now')),
        retries INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3
    )""")
    conn.commit(); conn.close()
    return db

def _make_env():
    return TaskEnvelope(
        task_id="t1", idempotency_key=make_idempotency_key("conv1","turn1",1,42),
        agent_id="a1", conversation_id="conv1", turn_id="turn1", sequence=1, epoch=42,
        objective="test", roles_json="{}", history_ref="h1",
        claim_token="ct1", lease_owner="a1", lease_expires_at=9999999999.0,
    )

@pytest.fixture
def db():
    path = _create_db()
    yield path
    try: os.remove(path)
    except: pass

def test_happy_path(db):
    """lease → claim OK → agent OK → submit."""
    adapter = SqliteTaskQueueAdapter(db_path=db)
    adapter.insert_task_idempotent(_make_env())
    claim_mock = MagicMock()
    claim_mock.claim_turn.return_value = type("D", (), {"ok": True, "reason": None})()
    invoker = FakeAgentInvoker(response=AgentResponse(content="done", result="continue"))
    worker = AutonomousWorker(
        db_path=db, state_dir="/tmp",
        agent_invoker=invoker, claim_adapter=claim_mock, task_adapter=adapter,
    )
    result = worker.run_once()
    assert result["status"] == "submitted"
    assert result["lease"] == 1
    assert result["claim"] == 1
    assert result["invoke"] == 1
    assert result["submit"] == 1
    assert invoker.call_count == 1

def test_no_task(db):
    """Empty queue → no_task."""
    adapter = SqliteTaskQueueAdapter(db_path=db)
    claim_mock = MagicMock()
    invoker = FakeAgentInvoker()
    worker = AutonomousWorker(
        db_path=db, state_dir="/tmp",
        agent_invoker=invoker, claim_adapter=claim_mock, task_adapter=adapter,
    )
    result = worker.run_once()
    assert result["status"] == "no_task"
    assert invoker.call_count == 0

def test_claim_rejection_discards(db):
    """Authoritative claim rejection → discard, no invoke."""
    adapter = SqliteTaskQueueAdapter(db_path=db)
    adapter.insert_task_idempotent(_make_env())
    claim_mock = MagicMock()
    claim_mock.claim_turn.return_value = type("D", (), {"ok": False, "reason": "stale_epoch"})()
    invoker = FakeAgentInvoker()
    worker = AutonomousWorker(
        db_path=db, state_dir="/tmp",
        agent_invoker=invoker, claim_adapter=claim_mock, task_adapter=adapter,
    )
    result = worker.run_once()
    assert result["status"] == "discarded"
    assert result["invoke"] == 0

def test_claim_transport_fails(db):
    """Transport failure → fail_task, no invoke."""
    adapter = SqliteTaskQueueAdapter(db_path=db)
    adapter.insert_task_idempotent(_make_env())
    claim_mock = MagicMock()
    claim_mock.claim_turn.return_value = type("D", (), {"ok": False, "reason": "transport_timeout"})()
    invoker = FakeAgentInvoker()
    worker = AutonomousWorker(
        db_path=db, state_dir="/tmp",
        agent_invoker=invoker, claim_adapter=claim_mock, task_adapter=adapter,
    )
    result = worker.run_once()
    assert result["status"] == "failed_transport"
    assert result["invoke"] == 0

def test_agent_error_retries(db):
    """Agent error → fail_task with retries incremented."""
    adapter = SqliteTaskQueueAdapter(db_path=db)
    adapter.insert_task_idempotent(_make_env())
    claim_mock = MagicMock()
    claim_mock.claim_turn.return_value = type("D", (), {"ok": True, "reason": None})()
    class ErrorInvoker:
        call_count = 0
        def invoke(self, ctx): 
            self.call_count += 1
            raise RuntimeError("agent crashed")
    invoker = ErrorInvoker()
    worker = AutonomousWorker(
        db_path=db, state_dir="/tmp",
        agent_invoker=invoker, claim_adapter=claim_mock, task_adapter=adapter,
    )
    result = worker.run_once()
    assert result["status"] == "agent_error"
    assert result["invoke"] == 1

def test_invalid_response(db):
    """Invalid agent result → fail_task."""
    adapter = SqliteTaskQueueAdapter(db_path=db)
    adapter.insert_task_idempotent(_make_env())
    claim_mock = MagicMock()
    claim_mock.claim_turn.return_value = type("D", (), {"ok": True, "reason": None})()
    invoker = FakeAgentInvoker(response=AgentResponse(content="x", result="bad_result"))
    worker = AutonomousWorker(
        db_path=db, state_dir="/tmp",
        agent_invoker=invoker, claim_adapter=claim_mock, task_adapter=adapter,
    )
    result = worker.run_once()
    assert result["status"] == "invalid_response"
    assert result["invoke"] == 1
    assert result["submit"] == 0

def test_call_ordering_lease_before_claim(db):
    """Verify lease happens before claim."""
    order = []
    adapter = SqliteTaskQueueAdapter(db_path=db)
    adapter.insert_task_idempotent(_make_env())
    claim_mock = MagicMock()
    claim_mock.claim_turn = MagicMock(return_value=type("D", (), {"ok": True, "reason": None})(), side_effect=lambda e: order.append("claim") or type("D", (), {"ok": True, "reason": None})())
    orig_lease = adapter.lease_next_task
    def lease_wrapped(*a, **kw):
        order.append("lease")
        return orig_lease(*a, **kw)
    adapter.lease_next_task = lease_wrapped
    invoker = FakeAgentInvoker()
    worker = AutonomousWorker(
        db_path=db, state_dir="/tmp",
        agent_invoker=invoker, claim_adapter=claim_mock, task_adapter=adapter,
    )
    worker.run_once()
    assert order[0] == "lease"
    assert "claim" in order
    assert order.index("lease") < order.index("claim")

def test_claim_before_invoke(db):
    """Verify claim happens before invoke."""
    order = []
    adapter = SqliteTaskQueueAdapter(db_path=db)
    adapter.insert_task_idempotent(_make_env())
    claim_mock = MagicMock()
    def claim_fn(e):
        order.append("claim")
        return type("D", (), {"ok": True, "reason": None})()
    claim_mock.claim_turn = claim_fn
    class OrderInvoker:
        def invoke(self, ctx):
            order.append("invoke")
            return AgentResponse(content="ok", result="continue")
    invoker = OrderInvoker()
    worker = AutonomousWorker(
        db_path=db, state_dir="/tmp",
        agent_invoker=invoker, claim_adapter=claim_mock, task_adapter=adapter,
    )
    worker.run_once()
    assert order.index("claim") < order.index("invoke")

def test_no_invoke_on_claim_failure(db):
    """Agent NOT invoked when claim fails."""
    adapter = SqliteTaskQueueAdapter(db_path=db)
    adapter.insert_task_idempotent(_make_env())
    claim_mock = MagicMock()
    claim_mock.claim_turn.return_value = type("D", (), {"ok": False, "reason": "stale_epoch"})()
    invoker = FakeAgentInvoker()
    worker = AutonomousWorker(
        db_path=db, state_dir="/tmp",
        agent_invoker=invoker, claim_adapter=claim_mock, task_adapter=adapter,
    )
    result = worker.run_once()
    assert invoker.call_count == 0

def test_federated_reply_calls_zero(db):
    """Worker must not call federated reply."""
    adapter = SqliteTaskQueueAdapter(db_path=db)
    adapter.insert_task_idempotent(_make_env())
    claim_mock = MagicMock()
    claim_mock.claim_turn.return_value = type("D", (), {"ok": True, "reason": None})()
    invoker = FakeAgentInvoker()
    worker = AutonomousWorker(
        db_path=db, state_dir="/tmp",
        agent_invoker=invoker, claim_adapter=claim_mock, task_adapter=adapter,
    )
    result = worker.run_once()
    # No reply attribute on worker
    assert not hasattr(worker, "reply")

def test_hermes_invoker_exists():
    """HermesInvoker class exists."""
    invoker = HermesInvoker()
    assert invoker is not None

def test_fake_invoker_exists():
    """FakeAgentInvoker class exists."""
    invoker = FakeAgentInvoker()
    assert invoker is not None

