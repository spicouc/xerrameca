# X9 local supervisor

The X9 supervisor is per-node and passive by default. It derives findings and metrics from the durable signed event log; no central supervisor is required for a conversation to continue.

## Inspect

```bash
xerrameca supervisor inspect --state-dir /var/lib/xerrameca-agent-a
xerrameca supervisor inspect --state-dir /var/lib/xerrameca-agent-a --conversation <id>
```

Findings currently include:

- `turn_waiting`: an available turn has not been claimed within the warning window;
- `lease_expired`: a claimed turn lease has expired;
- `conversation_idle`: an active conversation has produced no events for the configured idle window;
- `possible_loop`: a bounded recent message window has become strongly repetitive;
- `round_limit_reached`: the active conversation is at its configured maximum round.

Metrics include event/message counts and response latency (average, p50, p95, max), globally and per participant node.

## Explicit recovery

X9 never reopens expired leases automatically. A human/operator can request a bounded retry:

```bash
xerrameca supervisor recover \
  --state-dir /var/lib/xerrameca-agent-a \
  <conversation_id> \
  --epoch 1
```

Recovery is allowed only on the authoritative coordinator, only after the current claim is expired, and only while the retry limit has not been reached. Recovery appends another authoritative `turn.opened` event for the same logical turn; the event log remains the source of truth and restart preserves the retry count.

## Failure isolation

- supervisor inspection does not mutate conversation state;
- supervisor failure cannot stop peer replication or normal dialogue;
- an active recovery requires an explicit command;
- stale coordinator epochs remain fenced by X6/X8;
- no API/private key material is added to supervisor findings or metrics.

Global aggregation and dashboard presentation are separate optional layers.
