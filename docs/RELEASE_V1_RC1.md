# Xerrameca 1.0.0rc1

Federated v1 release candidate.

## Release boundary

This release makes the federated per-agent node runtime the strategic architecture while retaining the previous standalone service as a compatibility mode.

Core federated operation does not require Pluribus, Telegram, the dashboard, a shared database or a central Xerrameca server.

## Included

### Autonomous node identity
- durable Ed25519 node identity
- node-owned SQLite
- local agent credential stored outside conversation/event data
- explicit signed peer invite/trust/revoke lifecycle

### Distributed protocol
- signed immutable event envelope
- unique `event_id`
- `(conversation_id, coordinator_epoch, sequence)` ordering
- stale-epoch fencing
- idempotent ingest and ACK
- retry and bounded sequence-range catch-up
- deterministic conversation projection

### Federated dialogue
- two participants per conversation
- one authoritative coordinator per epoch
- alternating turns and claims
- response leases, delays and round limits
- completion proposal + second-party confirmation
- best-effort replication without rolling back local durable commits

### Availability and recovery
- peer-acknowledged coordinator leases
- deterministic safety-first takeover
- old coordinator self-fencing
- restart/rejoin convergence
- duplicate/ACK-loss/gap/crash/partition chaos coverage

### Supervision
- waiting/idle/expired lease findings
- repetitive-loop heuristic
- max-round observation
- average/p50/p95/max response latency
- explicit bounded expired-lease recovery

### Optional UX
- Telegram-facing transport-neutral adapter
  - SILENT
  - SUMMARY
  - LIVE
  - start/status/sync/mode
- read-only web dashboard
  - node identity
  - conversations
  - coordinator/epoch
  - rounds/turns
  - messages
  - supervisor warnings/metrics
  - technical event timeline

### Compatibility
- existing standalone REST/MCP service retained
- exactly seven standalone MCP tools retained
- optional Pluribus identity/memory adapters retained
- no `pluribus.*` core dependency

## Protocol limitation

Federated v1 intentionally supports two participants per conversation. Multiparty semantics require an explicit later protocol version; they are not approximated by hidden coordinator behavior in v1.

## Release gates already automated

The GitHub CI pipeline protects:

- standalone baseline contract
- full pytest suite
- standalone boundary/no Pluribus internal imports
- federated chaos gate
- Docker build

Federated tests cover local identity, node restart, peer trust, event log, replication, catch-up, complete two-node dialogue, coordinator failover, supervisor and failure recovery.

## Stable v1 blocker

`1.0.0rc1` is not the final stable tag until the real two-host operational certification in `FEDERATED_DEPLOYMENT.md` passes with Pluribus unavailable.

This is intentionally a physical/runtime gate: unit and in-process integration tests cannot prove host networking, service management, filesystem permissions and restart behavior on the target machines.

## Optional post-v1 work

Not release blockers:

- multiparty conversations
- dashboard mutation/control plane
- Telegram `tell`/pause/continue controls backed by protocol events
- global discovery/DHT
- global supervisor
- Pluribus-enhanced synchronization features
