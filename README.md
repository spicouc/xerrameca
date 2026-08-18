# Xerrameca

**Independent federated agent-to-agent conversation runtime.**

Xerrameca lets autonomous agents hold durable, supervised conversations without requiring a central Xerrameca server or Pluribus. Each agent can run its own node, keep its own SQLite state and trust selected peer nodes explicitly.

Pluribus remains supported as an optional identity/memory integration for the legacy standalone mode; it is not required by the federated core.

## Architecture

```text
OPTIONAL UX
Telegram adapter        Dashboard
       \                  /
        +----------------+
                |
Agent A -> Xerrameca Node A <----> Xerrameca Node B <- Agent B
               |                         |
          local SQLite              local SQLite
          local identity            local identity
          supervisor                supervisor
               \                         /
                optional providers
              Pluribus / Brain / others
```

Essential federated conversation traffic continues if Pluribus, Telegram or the dashboard are unavailable.

## Federated v1 capabilities

- durable per-agent Ed25519 node identity
- explicit signed peer invite/trust/revoke lifecycle
- node-owned SQLite; databases are never shared between peers
- signed append-only conversation event log
- globally unique `event_id`
- ordered `sequence` within `coordinator_epoch`
- idempotent ACK/retry and duplicate delivery
- bounded catch-up by sequence range
- one coordinator per conversation epoch
- stale-epoch fencing
- peer-acknowledged coordinator leases and deterministic failover
- restart/rejoin convergence
- two-participant alternating dialogue
- rounds, delay, turn claims, response leases and completion consensus
- passive local supervisor with timeout/loop findings and latency metrics
- bounded explicit lease recovery
- repeatable partition/ACK-loss/crash/duplicate chaos gate
- optional Telegram UX adapter with SILENT/SUMMARY/LIVE modes
- optional read-only web dashboard
- Pluribus-independent local mode
- existing standalone REST/MCP compatibility mode retained

## Federated quick start

Create two independent state directories on two agent hosts.

Node A:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
xerrameca init --state-dir /var/lib/xerrameca/node --agent-id agent-a --name "Agent A" --endpoint http://HOST_A:8791
xerrameca node --state-dir /var/lib/xerrameca/node --host 0.0.0.0 --port 8791
```

Node B uses the same commands with its own agent id, name and endpoint.

Establish explicit trust:

```bash
# On A
xerrameca invite create --state-dir /var/lib/xerrameca/node

# On B
xerrameca invite accept --state-dir /var/lib/xerrameca/node '<invite-token>'
```

The invite is bounded and single-purpose. Treat it as a credential while valid.

## Local node API

A node exposes health and public identity plus authenticated local-agent conversation endpoints and signed peer federation endpoints.

Examples:

```text
GET  /health
GET  /v1/node/identity
POST /v1/node/federation/conversations
GET  /v1/node/federation/conversations/{id}
POST /v1/node/federation/conversations/{id}/claim
POST /v1/node/federation/conversations/{id}/reply
POST /v1/node/federation/conversations/{id}/sync
```

Local-agent calls use the credential generated during `xerrameca init`. The plaintext credential remains in the node state directory with restrictive permissions and is never persisted in the conversation/event SQLite data.

## Supervisor

```bash
xerrameca supervisor inspect --state-dir /var/lib/xerrameca/node
```

It reports idle/waiting/expired turns, loop heuristics and response latency metrics. Recovery is explicit and bounded.

## Optional dashboard

```bash
xerrameca dashboard --state-dir /var/lib/xerrameca/node --host 127.0.0.1 --port 8792
```

The dashboard is read-only in federated v1 and reconstructs state from the local event log. Stopping it has no effect on conversations.

## Optional Telegram UX

`xerrameca.integrations.telegram.TelegramUXAdapter` provides a transport-neutral Telegram-facing layer with:

```text
/xerrameca start <peer_node_id> <objective>
/xerrameca status <conversation_id>
/xerrameca sync <conversation_id>
/xerrameca mode <conversation_id> silent|summary|live
```

The runtime has no Telegram SDK dependency. An agent/bot supplies the transport implementation.

## Existing standalone compatibility mode

The pre-federated service remains available:

```bash
xerrameca serve --host 127.0.0.1 --port 8791
```

It preserves the existing REST surface and exactly seven MCP tools:

- `xerrameca_command`
- `xerrameca_inbox`
- `xerrameca_claim`
- `xerrameca_reply`
- `xerrameca_list`
- `xerrameca_get`
- `xerrameca_messages`

Pluribus-backed identity can still be selected explicitly. Local/federated node mode does not require it.

## Protocol scope

Federated v1 supports **two participants per conversation**. A deployment may contain many independent nodes and many simultaneous pairwise conversations. Multiparty conversation semantics are intentionally a future protocol extension rather than an implicit v1 behavior change.

## Reliability contract

CI protects:

```text
standalone baseline
+ local identity/node/trust
+ signed event log
+ replication/catch-up
+ coordinator epoch fencing
+ two-node conversation/restart
+ failover
+ supervisor
+ chaos/recovery
+ optional UX regressions
```

The final stable tag additionally requires a real two-host smoke with Pluribus fully unavailable.

## Documentation

- `docs/FEDERATED_ARCHITECTURE.md`
- `docs/RFC_DISTRIBUTED_PROTOCOL.md`
- `docs/FEDERATED_DEPLOYMENT.md`
- `docs/AGENT_INTEGRATION.md`
- `docs/PLURIBUS_INTEGRATION.md`
- `docs/RELEASE_V1_RC1.md`

## License

Apache-2.0.
