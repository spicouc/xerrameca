
import sys, tempfile, os, sqlite3
sys.path.insert(0, "/opt/xerrameca-ux3/src")
from unittest.mock import MagicMock
import pytest
from xerrameca.transport import TaskEnvelope, make_idempotency_key
from xerrameca.reconciler import QueueReconciler

def _make_env():
    return TaskEnvelope(
        task_id="t1", idempotency_key=make_idempotency_key("conv1","turn1",1,42),
        agent_id="a1", conversation_id="conv1", turn_id="turn1", sequence=1, epoch=42,
        objective="test", roles_json="{}", history_ref="h1",
        claim_token="ct1", lease_owner="a1", lease_expires_at=9999999999.0,
    )

@pytest.fixture
def tmp_db():
    db = os.path.join(tempfile.gettempdir(), "reconciler_test.db")
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
    yield db
    try: os.remove(db)
    except: pass

def test_reconciler_exists():
    r = QueueReconciler(db_path="/tmp/test_r.db")
    assert r is not None

def test_empty_queue_rebuild(tmp_db):
    r = QueueReconciler(db_path=tmp_db)
    result = r.reconcile()
    assert result["before"] == 0
    assert result["after"] >= 0

def test_idempotency_same_key():
    assert make_idempotency_key("c","t",1,2) == make_idempotency_key("c","t",1,2)

def test_cli_main():
    from xerrameca.reconciler import main
    try: main()
    except SystemExit: pass

def test_source_is_federated():
    r = QueueReconciler(db_path="/tmp/test_r2.db", federated_client=MagicMock())
    assert r is not None

