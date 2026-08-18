# RFC — Xerrameca Distributed Conversation Protocol (X6–X8)

Status: **DRAFT — protocol design, implementation MUST NOT precede review**

Applies to: X6 event log, X7 replication, X8 coordinator per conversation.

## 1. Goal

Define one coherent protocol for durable conversation events, peer replication and coordinator ownership so that X6, X7 and X8 cannot diverge semantically.

The protocol must support the federated MVP:

```text
Pluribus OFF
Node A <----> Node B
full conversation
restart/recovery
identical logical history
no central runtime
```

Automatic coordinator failover is not required for the first MVP, but the protocol must be safe for future failover from day one.

## 2. Design principles

1. Conversations are represented by append-only authoritative events.
2. SQLite files are never synchronized between nodes.
3. Protocol events, not mutable DB rows, are the replication unit.
4. Duplicate delivery is expected and must be harmless.
5. One coordinator is authoritative for a conversation epoch.
6. Coordinator ownership changes are fenced by `coordinator_epoch`.
7. Nodes reconcile missing ranges rather than blindly replaying complete histories.
8. Current Xerrameca turn/lease/round/completion semantics must remain representable.

## 3. Terminology

**Node**: one autonomous Xerrameca runtime with durable local identity.

**Peer**: a trusted remote Xerrameca node.

**Conversation**: a durable dialogue with a stable `conversation_id`.

**Coordinator**: the node authorized to assign authoritative ordering for a conversation at a given epoch.

**Epoch**: monotonically increasing fencing generation for coordinator authority.

**Event**: immutable append-only protocol record.

**Projection**: deterministic materialized state computed from accepted events.

**ACK cursor**: highest accepted contiguous sequence for a peer/conversation/epoch.

## 4. Event envelope

Every authoritative replicated event MUST include at least:

```text
conversation_id
 event_id
sequence
coordinator_id
coordinator_epoch
author_id
event_type
timestamp
payload
signature
```

Recommended wire representation is JSON initially. Canonical signing serialization must be specified before peer traffic is enabled.

### 4.1 `conversation_id`

Stable globally unique identifier for the conversation.

### 4.2 `event_id`

Globally unique immutable identifier generated once by the event creator.

Requirement:

```text
UNIQUE(event_id)
```

Receiving the same `event_id` more than once MUST NOT create a second logical event.

### 4.3 `sequence`

Positive monotonic integer assigned by the authoritative coordinator within one coordinator epoch.

Uniqueness requirement:

```text
UNIQUE(conversation_id, coordinator_epoch, sequence)
```

### 4.4 `coordinator_id`

Node identity that owns authoritative sequencing for the specified epoch.

### 4.5 `coordinator_epoch`

Monotonically increasing authority generation.

Fundamental fencing rule:

> After a node has accepted/observed epoch N+1 for a conversation, it MUST NOT create or accept new authoritative events for epoch N.

Persist the highest observed epoch durably before accepting authoritative traffic under that epoch.

### 4.6 `author_id`

Logical participant/node that produced the conversation action represented by the event. It may be the coordinator or another participant whose action was ordered by the coordinator.

### 4.7 `event_type`

Versioned semantic event type.

### 4.8 `timestamp`

Informational wall-clock timestamp. It MUST NOT determine authoritative ordering. Ordering comes from epoch + sequence.

### 4.9 `payload`

Versioned event-specific object.

### 4.10 `signature`

Cryptographic signature over a canonical representation of the immutable event fields. Exact algorithm/canonicalization is an X5.2/X5.3 implementation decision that must be frozen before X7 peer replication.

## 5. Initial event taxonomy

The first taxonomy should be minimal but sufficient to express current conversation semantics.

Candidate event types:

```text
conversation.created
conversation.started
participant.added
participant.removed
turn.created
turn.claimed
turn.released
turn.expired
message.created
round.advanced
completion.proposed
completion.confirmed
conversation.completed
conversation.cancelled
conversation.blocked
conversation.needs_human
conversation.error
```

Exact names are subject to review. State transitions must remain deterministic and invalid transitions must be rejected.

## 6. Authoritative ordering

For one conversation epoch:

```text
(epoch=7, sequence=1)
(epoch=7, sequence=2)
(epoch=7, sequence=3)
...
```

A new coordinator generation begins a new epoch:

```text
(epoch=8, sequence=1)
```

Sequence therefore only needs to be monotonic inside an epoch.

Logical total ordering is:

```text
(coordinator_epoch, sequence)
```

Epoch never decreases.

## 7. Event acceptance rules

A receiver MUST validate before durable acceptance:

1. conversation exists or event is a valid conversation creation bootstrap
2. peer is trusted/authorized
3. signature is valid
4. event schema/version is supported
5. event_id is not conflicting
6. coordinator epoch is not stale
7. coordinator identity is valid for the epoch
8. `(conversation_id, epoch, sequence)` does not conflict
9. event transition is valid relative to prior accepted authoritative state

### Duplicate same event

If `event_id` is already present with identical immutable content:

```text
ACCEPT IDEMPOTENTLY
RETURN ACK
DO NOT REAPPLY LOGICAL EFFECT
```

### Conflicting event_id

If the same `event_id` is received with different immutable content:

```text
REJECT
SECURITY/CONSISTENCY ERROR
```

### Conflicting sequence slot

If `(conversation_id, epoch, sequence)` already exists with another event_id:

```text
REJECT
DIVERGENCE ERROR
```

No last-write-wins behavior is permitted.

## 8. ACK model

ACK processing MUST be idempotent.

At minimum maintain per peer/conversation/epoch:

```text
peer_id
conversation_id
coordinator_epoch
acked_sequence
updated_at
```

`acked_sequence` represents the highest contiguous sequence the peer confirms as durably accepted for that epoch.

A repeated ACK for an already acknowledged range is success/no-op.

An ACK MUST NOT cause event deletion from the authoritative log in the MVP.

## 9. Replication flow

Normal flow:

```text
Coordinator A
  -> EVENT epoch=4 seq=27
Peer B
  -> durable append
  -> projection update
  -> ACK epoch=4 seq=27
Coordinator A
  -> replication cursor updated
```

Transport may retry on timeout.

```text
A -> event 27
(no ACK observed)
A -> event 27 again
B -> detects same event_id
B -> ACK 27
```

Result: one logical event.

## 10. Catch-up by range

Nodes must be able to request bounded missing history.

Conceptual request:

```text
conversation_id=<id>
coordinator_epoch=<epoch>
from_sequence=<inclusive>
to_sequence=<optional inclusive>
```

Example:

```text
B has epoch 4 through seq 26
A has epoch 4 through seq 31
B requests 27..31
```

Do not require complete-history replay when a missing range is known.

The wire/API endpoint shape remains to be defined, but range semantics are mandatory.

## 11. Gaps

A receiver encountering:

```text
known seq = 26
received seq = 29
```

must not silently project 29 as if 27–28 did not exist.

It should:

1. durably record or temporarily hold the out-of-order event according to implementation policy
2. request missing range 27–28
3. advance contiguous projection only after the gap is filled

Initial implementation may choose to reject out-of-order delivery and request catch-up instead of buffering. The choice must be consistent and tested.

## 12. Deterministic projection

Materialized conversation state must be reproducible from the authoritative event history.

Projection includes at least:

```text
conversation status
participants
current round
current turn
lease state
completion proposals/confirmations
coordinator_id
coordinator_epoch
last contiguous sequence
```

Two nodes with the same accepted authoritative event stream MUST compute the same logical projection.

This equality is a mandatory integration test.

## 13. Coordinator responsibilities

Within its active epoch, the coordinator owns:

- authoritative sequence assignment
- validation/order of participant actions
- turn creation and transition ordering
- lease/timeout event ordering
- round advancement
- completion ordering

A participant may submit an action to the coordinator; the authoritative replicated record is the coordinator-ordered event.

The exact request-vs-event wire split is still open for implementation design.

## 14. Coordinator epoch fencing

Critical stale-node scenario:

```text
A = coordinator epoch 7
A goes offline
conversation authority advances to epoch 8
A later returns with persisted epoch 7 state
```

Required behavior:

1. A receives/observes valid epoch 8 state.
2. A persists `highest_epoch=8` before continuing authoritative work.
3. A MUST NOT emit authoritative epoch 7 events.
4. A performs catch-up for epoch 8.
5. projections converge.

No implementation may treat a returning stale coordinator as authoritative merely because it was previously coordinator.

## 15. Epoch transitions

Automatic failover is outside MVP, but the protocol must define a valid epoch transition record/mechanism before X8 closes.

An epoch transition must establish at minimum:

```text
conversation_id
new_coordinator_id
new_epoch (> previous epoch)
proof/authorization mechanism
```

For MVP testing, a controlled test harness/manual administrative transition may be used to create epoch N+1.

What is not allowed:

- two coordinators independently selecting the same new epoch
- epoch decrement
- stale coordinator overwriting new epoch state
- implicit epoch reset on restart

## 16. Restart semantics

Node restart must preserve:

- node identity
- trusted peers
- event log
- highest observed coordinator epoch per conversation
- projection or enough history to rebuild it
- peer ACK cursors
- pending outbound replication work

A process restart must not imply conversation reset.

## 17. Offline peer semantics

If B is offline while A is coordinator:

```text
A continues only as allowed by conversation policy
A persists events
B replication cursor stops advancing
```

When B returns:

```text
compare cursors
request/send missing ranges
apply events idempotently
projection converges
```

Whether a conversation may continue while a required participant is offline is a conversation policy issue, not a replication correctness rule.

## 18. Local supervisor interaction

Current timeout/lease/max-round safety can remain local/core, but any state-changing supervisor action that affects replicated conversation state must become an authoritative event.

Examples:

```text
turn.expired
conversation.blocked
conversation.needs_human
conversation.cancelled
```

Pure metrics/alerts do not need replication.

## 19. Security requirements

- Trust is established before peer protocol traffic.
- Peer messages are authenticated.
- Authoritative events are signed.
- Private node keys never leave the owning node.
- Pluribus API keys never enter peer events or peer databases.
- Payloads must have strict size/schema limits.
- Unknown event versions/types are rejected or quarantined; never partially applied.
- Replay of an already accepted identical event is idempotent.
- Replay attempting to reoccupy an authoritative sequence with different content is rejected.

## 20. Storage sketch

Names are illustrative, not final migrations.

### `conversation_events`

```text
event_id PRIMARY KEY
conversation_id
coordinator_epoch
sequence
coordinator_id
author_id
event_type
timestamp
payload
signature
received_at
```

Constraint:

```text
UNIQUE(conversation_id, coordinator_epoch, sequence)
```

### `conversation_replication_cursors`

```text
conversation_id
peer_id
coordinator_epoch
acked_sequence
updated_at
```

### `conversation_coordination`

```text
conversation_id
coordinator_id
coordinator_epoch
highest_observed_epoch
updated_at
```

Schema must be reviewed for transaction/fencing correctness before implementation.

## 21. Transaction requirements

Critical acceptance operations must be atomic at SQLite transaction boundaries.

At minimum avoid states where:

- event is acknowledged before durable commit
- projection advances without corresponding durable event
- highest observed epoch is advanced only in memory
- replication cursor advances beyond durable accepted history

ACK is emitted only after the relevant durable transaction commits.

## 22. Two-node certification matrix

### Normal

```text
A coordinator
A <-> B conversation
complete
histories logically equal
```

### Duplicate delivery

```text
send same event twice
one logical event
ACK both deliveries safely
```

### ACK loss

```text
B commits event
ACK lost
A retries
B no duplicate
ACK succeeds
```

### Restart receiver

```text
B restart
identity/trust/history retained
catch-up continues
```

### Offline catch-up

```text
B offline
A advances history
B returns
missing range only
logical histories equal
```

### Gap

```text
receive seq 29 while last contiguous = 26
27–28 requested/recovered
29 not incorrectly projected first
```

### Stale epoch

```text
A epoch 7 stale
valid epoch 8 exists
A returns
A cannot author epoch 7
A catches up
histories converge
```

### Pluribus absence

Run the complete matrix with Pluribus stopped/unreachable.

## 23. MVP exit gate

X6–X8 MVP passes only when all are true:

- two independent trusted nodes
- no central Xerrameca runtime
- Pluribus OFF
- complete A <-> B conversation
- append-only durable event history
- idempotent event handling
- idempotent ACK/retry
- bounded sequence-range catch-up
- restart recovery
- coordinator epoch persisted
- stale-epoch fencing test PASS
- identical logical conversation history/projection after reconciliation
- current standalone regression suite still PASS

## 24. Explicitly deferred

- automatic coordinator election
- quorum consensus
- Raft/Paxos
- multi-coordinator CRDT conversation writes
- global discovery/DHT
- Telegram
- dashboard
- global supervisor
- internet-scale federation

The first federated protocol intentionally prefers a single coordinator per conversation over distributed consensus complexity.

## 25. Open design questions before implementation

These must be resolved/reviewed before X6 coding begins:

1. canonical event serialization for signatures
2. node signing algorithm/key storage strategy
3. exact peer authentication handshake after X5.3 trust
4. whether out-of-order events are buffered or rejected until catch-up
5. exact coordinator epoch transition authorization format
6. action submission protocol from participant to coordinator
7. event schema versioning strategy
8. projection migration path from the current mutable conversation schema
9. maximum event/payload sizes
10. retention/compaction policy after MVP (no compaction required initially)

## 26. Review rule

No X6/X7/X8 implementation should begin until this RFC has been reviewed against:

- current ConversationEngine semantics
- existing REST/MCP compatibility contracts
- SQLite transaction model
- two-node test harness requirements
- future coordinator failover fencing needs

The purpose of the RFC is to make incorrect distributed states impossible by contract before optimizing transport or UX.
