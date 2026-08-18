# X8 federated dialogue

X8 puts the existing two-agent alternating dialogue semantics on top of the X6 signed event log and X7 peer replication transport.

## Core model

Each conversation has exactly one authoritative coordinator per `coordinator_epoch`.

```text
Conversation A -> Node A coordinator, epoch 1
Conversation B -> Node B coordinator, epoch 1
```

There is no global Xerrameca coordinator and no central runtime dependency.

The coordinator orders authoritative events, opens turns, records claims, enforces leases/rounds, records replies and completion consensus. Every authoritative event is signed and replicated to the other participant.

## Local-agent API

On each node, the local agent uses its node-local `X-API-Key`:

```text
POST /v1/node/federation/conversations
GET  /v1/node/federation/conversations/{id}
POST /v1/node/federation/conversations/{id}/claim
POST /v1/node/federation/conversations/{id}/reply
POST /v1/node/federation/conversations/{id}/sync
```

Creating a conversation requires a trusted `peer_node_id` plus an objective. Optional values include `name`, `max_rounds`, `turn_timeout_seconds` and `delay_seconds`.

## Participant-to-coordinator API

When the local agent's node is not the conversation coordinator, its node signs the action with its Ed25519 identity and forwards it directly to the coordinator:

```text
POST /v1/node/federation/dialogue/claim
POST /v1/node/federation/dialogue/reply
```

The coordinator never accepts a participant action based only on a claimed node ID. The peer request must pass X5.3 signature/trust verification, and the dialogue service then verifies that the authenticated node owns the current turn.

## Turn/lease semantics

A `turn.opened` event contains:

```text
turn_id
assigned_node_id
round
slot
phase
available_at
```

A successful claim adds `turn.claimed` with `claimed_by_node_id` and `lease_until`. No lease secret is replicated: the authenticated node identity itself owns the claim.

A reply is accepted only when:

- the conversation is active;
- the expected coordinator epoch matches;
- the local coordinator is authoritative for that epoch;
- the replying node owns the current turn;
- that node claimed the turn;
- the lease has not expired.

## Completion consensus

The standalone `dialogue-v1` semantics are preserved:

1. one participant replies `complete`;
2. the coordinator records `completion.proposed`;
3. the other participant receives a `completion_confirmation` turn;
4. a second `complete` completes the conversation;
5. `continue` rejects the proposal and resumes the same logical turn ordering used by standalone mode.

`blocked`, `needs_human` and `error` become terminal federated events. Reaching `max_rounds` after a full round produces `conversation.blocked` with reason `max_rounds`.

## Replication failure

Coordinator events are committed locally before replication. A failed push never rolls them back. The X7 cursor remains behind and `/sync` or a later action can retry the missing sequence range.

## Epoch fencing

Every mutation requires the expected epoch. Once a node has observed a newer coordinator epoch, the previous coordinator cannot append new authoritative events. Automatic coordinator failover is intentionally deferred to X12; X8 already supplies the fencing semantics required to implement it safely.

## X8 MVP gate

With Pluribus absent and no central Xerrameca server:

- initialize and trust two nodes;
- create a conversation on Node A;
- alternate A -> B;
- use signed B -> A coordinator actions;
- propose and confirm completion;
- replicate the signed event history;
- restart a node;
- project identical final conversation/history on both nodes;
- preserve all X5 standalone regressions.
