# Xerrameca Architecture

## 1. Purpose

Xerrameca is the orchestration layer for structured agent-to-agent conversations. It owns conversation lifecycle and coordination state; it does not own long-term agent knowledge.

## 2. Ownership boundaries

### Xerrameca owns

- conversations and participants
- messages
- turns and assignments
- claim/lease state
- rounds and delays
- completion consensus
- supervisor policy
- runner and monitor state
- Xerrameca audit/events
- its own SQLite database

### Pluribus may provide

- agent identity
- credential verification
- permissions and scopes
- agent capabilities/presence
- optional persistence of a final summary into Brain

Pluribus is an adapter target, not a runtime library dependency.

## 3. Non-negotiable isolation rules

1. Xerrameca must not import `pluribus.*`.
2. Xerrameca must not open `pluribus.db`.
3. Xerrameca must not issue SQL against `agents`, `facts`, `chunks`, or any Pluribus table.
4. Xerrameca schema migrations affect only the Xerrameca database.
5. Pluribus migrations must never be required to deploy a Xerrameca-only release.
6. Stopping Xerrameca must not affect Pluribus health.
7. A failed Xerrameca migration must not prevent Pluribus startup.

## 4. Hexagonal boundary

The domain depends on ports, never on provider implementations.

```text
REST/MCP
   |
application/services
   |
conversation domain
   |
+-------------------+
| IdentityPort      |---- PluribusIdentityAdapter
| MemoryPort        |---- PluribusMemoryAdapter
| AuditPort         |---- local/remote adapter
| Repository ports  |---- SQLite repositories
+-------------------+
```

`MemoryPort` is optional. Failure to persist a summary must be represented explicitly and must not corrupt the completed conversation state.

## 5. Data model target

The standalone database will contain only Xerrameca-owned state, including equivalents of the currently embedded tables:

- runtime/settings
- conversations
- participants
- messages
- turns
- runner jobs/state where required
- monitor events/state where required

Agent records, if cached locally, are projections only. Credentials are never copied to Xerrameca persistence.

## 6. Compatibility contract

Extraction should preserve the current public behavior before adding new features.

REST compatibility target:

- `POST /v1/xerrameca/command`
- `GET /v1/xerrameca/inbox`
- `POST /v1/xerrameca/turns/{turn_id}/claim`
- `POST /v1/xerrameca/turns/{turn_id}/reply`
- list/get/messages endpoints used by the existing clients

MCP compatibility target:

- `xerrameca_command`
- `xerrameca_inbox`
- `xerrameca_claim`
- `xerrameca_reply`
- `xerrameca_list`
- `xerrameca_get`
- `xerrameca_messages`

Slash semantics, rounds, delay, lease and completion behavior remain compatible unless changed in a versioned contract.

## 7. Extraction strategy

### X0 — Bootstrap

Define standalone package, ports, architecture and compatibility tests. No production migration.

### X1 — Independent core

Copy the proven Xerrameca behavior from Pluribus and replace direct database/config/validation/audit dependencies with Xerrameca-owned equivalents. Run against an independent SQLite database.

Exit criterion: the engine test suite runs with zero `pluribus.*` imports.

### X2 — Pluribus adapter

Implement HTTP adapters for identity and optional memory. Do not access Pluribus storage directly.

Exit criterion: two-agent contract tests pass using a real Pluribus instance only through its public API.

### X3 — Deployment

Run Xerrameca as its own service, initially on port 8791. Certify REST, MCP and real two-agent E2E.

Exit criterion: killing/restarting/upgrading Xerrameca leaves Pluribus green.

### X4 — Embedded code retirement

Only after standalone certification, remove embedded Xerrameca startup/routes/storage from Pluribus and optionally leave a small compatibility/proxy integration.

## 8. Source of truth during migration

Until X3 is certified, the existing implementation in `spicouc/Pluribus` is the behavioral reference. New development should avoid expanding the embedded implementation except for critical compatibility fixes needed during extraction.
