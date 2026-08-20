Xerrameca Federated v1.0.0 — Release Notes

First stable release of the independent, Pluribus-free federated
agent-to-agent orchestration runtime.

Physical two-host certification

Runtime certified on two independent hosts (HOST A = LXC110 node,
HOST B = Dagobah node) running the exact commit promoted here:

- Certified runtime commit: 09468626d2c319612a1b9deee10c722f02b58870
- Package at certification: 1.0.0rc1
- Physical certification result: PASS

What was certified

- Pluribus-OFF autonomous conversation — the two nodes completed a
  federated conversation with Pluribus fully stopped; Pluribus is not
  required for federation.
- Restart + bounded catch-up — a node that disappears and returns
  recovers missed events within bounded replication; idempotent sync,
  zero duplicate event_ids.
- Automatic coordinator failover — on coordinator lease expiry the
  peer automatically assumes coordination (no manual action).
- Epoch fencing — the old coordinator cannot authoritatively write
  to its previous epoch after takeover; the previous epoch is immutable.
- Old-coordinator rejoin — the former coordinator rejoins under the
  new epoch as a participant and adopts the new coordinator.
- History convergence — both nodes hold byte-identical canonical
  event histories (verified by Ed25519 signature + content digest).
- SQLite quick_check — ok on both nodes.
- Secret exposure — NONE; API keys and private keys never enter
  SQLite, event payloads, or journals.

Protocol v1 invariants

- Federated v1 uses exactly two participants per conversation.
- Pluribus is an optional integration, not a core dependency.

Features

- Autonomous per-agent nodes
- Direct signed peer federation (Ed25519)
- Durable signed append-only event history
- Bounded replication / catch-up
- Automatic coordinator failover
- coordinator_epoch fencing
- Local persistence (SQLite)
- Optional adapters: Telegram, dashboard, Pluribus
- Package / container build
