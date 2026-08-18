# Xerrameca Federated v2 Architecture

Status: **DRAFT — design baseline**

Baseline commit: `0f45639b0dcc68a86957188ea10836566cfdad53`

Development branch: `federated-v2`

## 1. Purpose

Evolve Xerrameca from a standalone central orchestration service into an autonomous per-agent/federated runtime without discarding the current implementation.

The current standalone service remains a supported compatibility mode while federation is developed.

The target architectural rule is:

> No essential Xerrameca conversation operation requires Pluribus or a central Xerrameca server.

Pluribus becomes an optional provider for identity, permissions, Brain/memory and future integrations.

## 2. Compatibility rule

X5 must not break the existing standalone mode.

The following capabilities remain protected by regression tests throughout X5–X8:

- standalone HTTP service
- `/health`
- current REST command/inbox/claim/reply behavior
- current conversation list/get/messages behavior
- exactly seven MCP tools
- turns, leases, rounds and delays
- completion proposal + second participant confirmation
- restart persistence
- passive monitor behavior
- Pluribus-backed identity mode when explicitly configured

Federated functionality is additive until its own gates pass.

## 3. Target layers

```text
OPTIONAL USER / OPS

Telegram     Dashboard     Global observability
    \           |           /
     +----------+----------+
                |
---------------------------------------------
CORE FEDERATED RUNTIME

 Agent A                 Agent B
    |                        |
 Xerrameca Node A <----> Xerrameca Node B
    |                        |
 local SQLite             local SQLite
 local identity           local identity
 event log                event log
 local supervisor         local supervisor

---------------------------------------------
OPTIONAL PROVIDERS

Pluribus / Brain / other identity-memory providers
```

Dashboard, Telegram and global supervision are explicitly outside the federated MVP.

## 4. Runtime modes

### 4.1 Existing standalone service

The current deployment remains supported.

```text
Agents -> Xerrameca standalone -> optional Pluribus adapters
```

### 4.2 Local node mode

Each agent can run a local Xerrameca node with its own identity and SQLite database.

```text
Agent -> Xerrameca node -> local SQLite
```

### 4.3 Federated mode

Trusted Xerrameca nodes communicate directly.

```text
Agent A + Node A <----> Agent B + Node B
```

No central server is required for core conversation traffic.

### 4.4 Pluribus-enhanced mode

A local/federated node may use Pluribus adapters, but loss of Pluribus must not invalidate the existence of the node itself.

## 5. Provider model

Core services depend on ports, not Pluribus implementations.

Initial provider intent:

```text
IdentityProvider
  - LocalIdentityProvider
  - PluribusIdentityProvider

MemoryProvider
  - NullMemoryProvider
  - LocalMemoryProvider (optional implementation)
  - PluribusMemoryProvider
```

Exact class/config names may evolve, but the dependency direction must remain:

```text
core -> ports <- adapters
```

Never:

```text
core -> pluribus internals
```

## 6. X5 phase specification

### X5.0 — Freeze baseline and regression gate

Objective: protect current behavior before adding federation.

Required:

- record current API/MCP contracts
- manifest current regression tests
- preserve current SQLite/restart behavior
- preserve Pluribus-backed mode
- add CI gate against accidental standalone regressions

Gate:

```text
CURRENT TEST SUITE PASS
REST CONTRACT PASS
7 MCP TOOLS PASS
RESTART PERSISTENCE PASS
PLURIBUS MODE REGRESSION PASS
```

No functional architecture changes are required to pass X5.0.

### X5.1 — Pluribus optional

Objective: Xerrameca can operate without Pluribus while retaining the existing Pluribus-backed mode.

Required:

- first-class local identity provider
- explicit provider selection
- no insecure fallback from failed Pluribus auth to local identity
- no Pluribus API keys persisted in Xerrameca SQLite
- local conversation engine works with Pluribus fully down

Gate A:

```text
PLURIBUS OFF
LOCAL PROVIDER
XERRAMECA HEALTH PASS
LOCAL CONVERSATION PASS
RESTART PASS
```

Gate B:

```text
PLURIBUS PROVIDER
CURRENT STANDALONE REGRESSION SUITE PASS
```

Both gates are mandatory.

### X5.2 — Agent node mode

Objective: create a durable per-agent Xerrameca runtime.

Conceptual CLI:

```text
xerrameca init
xerrameca node
```

Each node persists at least:

```text
node_id
agent_id
display_name
public_key
private_key or secure key reference
endpoint/config
database path
```

Rules:

- each node owns its SQLite database
- no shared DB between nodes
- private key is local-only
- restart preserves node identity
- existing standalone service remains supported

Gate:

```text
PLURIBUS OFF
NODE A INIT PASS
NODE B INIT PASS
A.node_id != B.node_id
RESTART A/B
IDENTITIES UNCHANGED
STANDALONE REGRESSIONS PASS
```

### X5.3 — Explicit peer trust

Objective: allow two nodes to establish durable trust without a central authority.

First version intentionally uses explicit bootstrap.

Conceptual CLI/API:

```text
xerrameca invite create
xerrameca invite accept <token>
xerrameca peer list
xerrameca peer revoke <peer>
```

Persist only public peer information:

```text
peer node_id
peer agent_id
display_name
public_key
endpoint(s)
trust status
created/updated timestamps
capabilities/version metadata (optional)
```

Security requirements:

- bounded/single-purpose invitations
- replay-safe acceptance
- explicit identity/public-key verification
- revoked peers cannot authenticate new peer traffic
- no peer private keys
- no Pluribus API keys

Gate:

```text
PLURIBUS OFF
A/B INDEPENDENT
A CREATES INVITE
B ACCEPTS
MUTUAL TRUST PASS
RESTART A/B
TRUST PERSISTS
REPLAY TEST PASS
REVOKE TEST PASS
NO CENTRAL SERVER CONTACTED
```

## 7. Federated MVP boundary

The MVP is exactly:

```text
X5.0
 -> X5.1
 -> X5.2
 -> X5.3
 -> X6 event log
 -> X7 replication
 -> X8 coordinator per conversation
 -> 2-node certification
```

MVP success requires:

- Pluribus fully off
- two independent nodes
- no central Xerrameca runtime
- full A <-> B conversation
- durable history on both nodes
- restart one node
- catch-up missing events
- identical logical history after reconciliation

## 8. Out of MVP

Do not block MVP on:

- Telegram integration
- web dashboard
- global supervisor
- automatic coordinator election/failover
- DHT/global discovery
- internet-wide federation
- multi-region HA
- advanced Pluribus integration

The existing local timeout/lease/max-round safety behavior may remain part of core conversation semantics.

## 9. Implementation workflow

Every implementation issue uses this loop:

```text
SPEC
 -> MINIMAL IMPLEMENTATION
 -> UNIT TESTS
 -> INTEGRATION TESTS
 -> REGRESSIONS
 -> LOCAL FAILURE TEST
 -> PR
 -> CI
 -> GATE
 -> MERGE
```

For distributed X6–X8 changes, additionally test:

```text
NORMAL
 -> RESTART
 -> OFFLINE
 -> RECOVERY
 -> DUPLICATE/RETRY
 -> CONSISTENCY CHECK
```

## 10. Source of truth

GitHub is the source of truth for architecture, issues, RFCs, commits, gates and CI.

Executors may work on clean local worktrees/checkouts, but production hosts are not development workspaces and no phase is considered complete until its GitHub gate is recorded as PASS.

## 11. Related work

- X5.0 issue: standalone baseline/regression gate
- X5.1 issue: Pluribus optional
- X5.2 issue: node mode
- X5.3 issue: peer trust
- X6–X8 epic: distributed protocol
- `docs/RFC_DISTRIBUTED_PROTOCOL.md`: protocol design draft
