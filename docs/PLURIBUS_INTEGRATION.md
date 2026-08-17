# Pluribus Integration

Pluribus is the first supported identity/memory provider for Xerrameca, but Xerrameca must remain able to run without importing Pluribus code or sharing its database.

## Identity

The Xerrameca application depends on `IdentityPort`.

The Pluribus adapter will authenticate an incoming agent credential against a public Pluribus HTTP capability and map the response to `AgentIdentity`:

- agent id
- name
- permissions
- allowed scopes
- capabilities
- active state

API keys may exist in request memory while being validated, but must never be written to Xerrameca SQLite, logs, events or traces.

### Minimum provider capabilities

X2 must establish provider APIs that allow Xerrameca to:

1. validate the current caller and obtain its identity;
2. resolve a target agent;
3. list agents eligible to participate with the caller in a scope.

If the current Pluribus public API cannot express one of these operations without direct SQL, the missing capability must be added to Pluribus as a small, generic provider API in a separate reviewed PR. Direct access to the Pluribus database is not an acceptable fallback.

## Memory

Conversation state is never stored in Brain.

When a conversation is configured with summary persistence, Xerrameca calls `MemoryPort.persist_summary()` after the conversation has reached its terminal state. The Pluribus adapter then uses a public memory/fact API.

Xerrameca must not:

- insert into `facts` directly;
- insert into `chunks` directly;
- generate Pluribus embeddings directly;
- run Pluribus migrations.

A persisted summary should carry metadata such as the Xerrameca conversation id, final status and round count so it can be traced back to its source.

## Failure semantics

- **Identity provider unavailable before authentication:** reject the new authenticated operation with a provider-unavailable error.
- **Provider becomes unavailable during an already claimed turn:** do not invent a new identity. Preserve the local lease/state and apply explicit retry/expiry policy.
- **Memory provider unavailable at final summary persistence:** keep the conversation terminal locally and mark summary persistence as pending/failed; do not roll back the conversation.

## Deployment

Initial target:

```text
Pluribus   :8790
Xerrameca  :8791
```

They use separate process supervisors, configuration files and SQLite databases.

The core isolation test is operational: restarting or failing one service must not stop the other.
