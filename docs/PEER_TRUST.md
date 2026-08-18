# Explicit peer trust bootstrap

X5.3 lets two autonomous Xerrameca nodes establish durable mutual trust without Pluribus, a central Xerrameca server, DHT or global discovery.

## Create an invitation on Node A

```bash
xerrameca invite create --state-dir /var/lib/xerrameca-a --ttl 600
```

The output is a short-lived, single-purpose signed invitation token. Transfer it through a channel you trust.

The token contains only Node A public identity, endpoint, expiry and nonce. It is signed by Node A's Ed25519 private key; no private key or API key material is embedded.

## Accept on Node B

```bash
xerrameca invite accept --state-dir /var/lib/xerrameca-b '<token>'
```

Node B:

1. verifies the invitation signature, public-key fingerprint and expiry;
2. signs an acceptance containing only its public node identity;
3. sends that acceptance to Node A's `/v1/node/invites/accept` endpoint;
4. verifies Node A's signed confirmation;
5. persists Node A as a trusted peer.

Node A verifies Node B's signed acceptance, consumes the invitation and persists Node B as trusted. Retrying the same acceptance from the same node is idempotent; a different node cannot reuse an already consumed invite.

## Inspect and revoke

```bash
xerrameca peer list --state-dir /var/lib/xerrameca-a
xerrameca peer revoke --state-dir /var/lib/xerrameca-a <node_id>
```

Peer records contain only:

- node ID
- agent ID/display name
- public key
- endpoint
- trust status
- timestamps
- public capabilities

Another node's private key or Pluribus/API credential is never stored.

## Peer authentication foundation

Trusted nodes can authenticate node-to-node requests with an Ed25519 signature over method, path, timestamp and body hash. X5.3 exposes a signed `/v1/node/peer/ping` proof endpoint used by the trust gate.

A revoked peer is rejected before signature-authorized peer traffic is accepted.

This signed peer-authentication primitive is the trust foundation for X7 event replication. It is not yet conversation replication itself.
