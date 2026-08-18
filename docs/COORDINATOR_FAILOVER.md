# X12 coordinator failover

X12 adds automatic two-node coordinator failover while preserving the `coordinator_epoch` fencing designed in X6–X8.

## Safety rule

A timeout alone is not enough to make failover safe. During a network partition, the old coordinator and participant must not both become authoritative.

X12 therefore uses bounded coordinator leases:

1. the coordinator appends a signed `coordinator.lease_granted` event;
2. normal lease grants/renewals become effective for the coordinator only after the participant ACKs that event through the X7 replication cursor;
3. if the participant cannot be reached, the coordinator may continue only until its last peer-acknowledged lease expires;
4. after expiry the old coordinator self-fences and rejects new authoritative dialogue writes;
5. the participant waits for the observed lease plus a grace period, then advances `coordinator_epoch` and becomes coordinator;
6. a returning old coordinator observes the newer epoch, catches up and remains fenced from the stale epoch.

This chooses consistency over unlimited availability during a prolonged two-node partition.

## Initial takeover grant

The first bounded lease in a takeover epoch is marked `takeover_grant`. It is locally effective because the previous coordinator's acknowledged lease already expired before epoch advancement. Subsequent renewal follows the normal peer-ACK rule.

## Runtime loop

Every `xerrameca node` runs a small local failover loop. It:

- renews an acknowledged coordinator lease before expiry;
- retries replication when a renewal is still pending;
- self-fences an expired local coordinator;
- performs deterministic participant takeover after lease + grace expiry;
- never terminates the node process if a monitoring/failover iteration itself fails.

Manual observability/control is also available through node-local authenticated endpoints:

```text
GET  /v1/node/failover/{conversation_id}
POST /v1/node/failover/tick
POST /v1/node/failover/{conversation_id}/renew
POST /v1/node/failover/{conversation_id}/takeover
```

## Compatibility

Federated conversations created before X12 that contain no coordinator-lease events remain in compatibility mode and retain X8 behavior. New node-API conversations enable a coordinator lease by default; the duration can be configured per conversation or explicitly disabled with `coordinator_lease_seconds=0` for compatibility/testing.

## Critical certification

```text
A coordinator epoch 1 + peer-ACKed lease
A lease expires
A self-fences
B waits through grace
B advances to epoch 2
B continues the conversation
A returns
A ingests epoch 2
A cannot write as stale coordinator
A/B histories converge
```
