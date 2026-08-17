from __future__ import annotations


SCHEMA_SQL = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS xerrameca_runtime (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    default_max_rounds INTEGER NOT NULL DEFAULT 5 CHECK (default_max_rounds BETWEEN 1 AND 200),
    default_turn_timeout_seconds INTEGER NOT NULL DEFAULT 300 CHECK (default_turn_timeout_seconds BETWEEN 10 AND 86400),
    default_delay_seconds INTEGER NOT NULL DEFAULT 2 CHECK (default_delay_seconds BETWEEN 0 AND 3600),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO xerrameca_runtime
(singleton, enabled, default_max_rounds, default_turn_timeout_seconds, default_delay_seconds)
VALUES (1, 1, 5, 300, 2);

-- Provider identities are projections only. No credential, token or password column
-- is intentionally present in this table.
CREATE TABLE IF NOT EXISTS agent_projections (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    permissions_json TEXT NOT NULL DEFAULT '{}',
    allowed_scopes_json TEXT NOT NULL DEFAULT '[]',
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    provider TEXT NOT NULL DEFAULT 'unknown',
    verified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'shared',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','active','paused','blocked','completed','cancelled','error')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    protocol_version TEXT NOT NULL DEFAULT 'dialogue-v1',
    turn_policy TEXT NOT NULL DEFAULT 'alternating'
        CHECK (turn_policy IN ('alternating','supervisor')),
    supervisor_agent_id TEXT,
    first_agent_id TEXT NOT NULL,
    max_rounds INTEGER NOT NULL DEFAULT 5 CHECK (max_rounds BETWEEN 1 AND 200),
    turn_timeout_seconds INTEGER NOT NULL DEFAULT 300 CHECK (turn_timeout_seconds BETWEEN 10 AND 86400),
    delay_seconds INTEGER NOT NULL DEFAULT 2 CHECK (delay_seconds BETWEEN 0 AND 3600),
    current_round INTEGER NOT NULL DEFAULT 0 CHECK (current_round >= 0),
    current_turn_id TEXT,
    block_reason TEXT,
    completion_proposed_by_agent_id TEXT,
    completion_proposed_at TEXT,
    completion_proposal_turn_id TEXT,
    persist_summary INTEGER NOT NULL DEFAULT 1 CHECK (persist_summary IN (0, 1)),
    summary_status TEXT NOT NULL DEFAULT 'none'
        CHECK (summary_status IN ('none','pending','stored','failed')),
    summary_external_id TEXT,
    created_by_agent_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
CREATE INDEX IF NOT EXISTS idx_conversations_scope ON conversations(scope);

CREATE TABLE IF NOT EXISTS participants (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'participant'
        CHECK (role IN ('participant','supervisor')),
    position INTEGER NOT NULL CHECK (position IN (0, 1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    PRIMARY KEY (conversation_id, agent_id),
    UNIQUE (conversation_id, position)
);
CREATE INDEX IF NOT EXISTS idx_participants_agent ON participants(agent_id, enabled);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_id TEXT,
    turn_seq INTEGER NOT NULL CHECK (turn_seq >= 0),
    dialogue_round INTEGER NOT NULL CHECK (dialogue_round >= 0),
    from_agent_id TEXT,
    to_agent_id TEXT,
    message_type TEXT NOT NULL
        CHECK (message_type IN ('task','message','question','answer','result','error','control')),
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    turn_result TEXT
        CHECK (turn_result IS NULL OR turn_result IN ('continue','complete','blocked','needs_human','error')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(to_agent_id, created_at);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_seq INTEGER NOT NULL CHECK (turn_seq >= 1),
    dialogue_round INTEGER NOT NULL CHECK (dialogue_round >= 1),
    turn_in_round INTEGER NOT NULL CHECK (turn_in_round IN (0, 1, 2)),
    phase TEXT NOT NULL DEFAULT 'dialogue'
        CHECK (phase IN ('dialogue','completion_confirmation')),
    assigned_agent_id TEXT NOT NULL,
    input_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready','claimed','completed','skipped','cancelled')),
    available_at TEXT NOT NULL,
    claimed_by TEXT,
    lease_token TEXT,
    claimed_at TEXT,
    lease_until TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, turn_seq)
);
CREATE INDEX IF NOT EXISTS idx_turns_inbox ON turns(assigned_agent_id, status, available_at, lease_until);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id, turn_seq);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    agent_id TEXT,
    action TEXT NOT NULL,
    conversation_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_conversation ON audit_events(conversation_id, id);

CREATE TABLE IF NOT EXISTS summary_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','stored','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    external_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
