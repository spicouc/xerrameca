# X7 peer replication

X7 replicates signed protocol events between nodes that already established explicit X5.3 trust. It never copies or opens another node's SQLite database.

## Transport

Peer requests use the Ed25519 request-signing scheme established in X5.3. The signature covers HTTP method, path, timestamp and the exact request-body hash.

Endpoints:

```text
POST /v1/node/federation/events
GET  /v1/node/federation/conversations/{conversation_id}/events
POST /v1/node/federation/ack
```

## Push

The coordinator sends one contiguous batch for one conversation and one coordinator epoch:

```json
{"events":["<EventEnvelope>"]}
```

The receiver:

1. authenticates the peer request;
2. checks that the sender is the event coordinator for the X7 MVP;
3. verifies every event signature;
4. enforces epoch/sequence continuity through the local `EventStore`;
5. stores new events transactionally;
6. returns the highest contiguous acknowledged sequence.

Replaying exactly the same batch is idempotent: no duplicate logical events are created and the same/highest ACK is returned.

## ACK cursor

Each sender keeps a local monotonic cursor keyed by:

```text
peer_node_id
conversation_id
coordinator_epoch
```

ACK retries and older ACKs cannot move the cursor backwards.

## Bounded catch-up

A lagging node requests only its missing range:

```text
conversation=<id>
epoch=<epoch>
from_sequence=<n>
to_sequence=<optional>
```

Example: if a peer owns sequences `1..2` and the coordinator owns `1..5`, the peer requests `3..5`, not the full conversation.

## Failure model

- a failed push does not roll back already committed coordinator events;
- the sender's ACK cursor remains behind, so a later retry sends the missing range;
- duplicate delivery is safe;
- revoked peers cannot push, ACK or fetch event ranges;
- relay/gossip is deliberately out of X7 MVP: the signed event coordinator sends its own authoritative events directly.

X8 builds alternating conversation semantics on top of this transport.
