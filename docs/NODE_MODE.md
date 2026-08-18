# Xerrameca node mode

X5.2 introduces a per-agent runtime without removing the existing standalone service.

## Existing standalone mode

```bash
xerrameca serve --host 0.0.0.0 --port 8791
```

This preserves the current standalone application/configuration behavior.

## Initialize one agent node

```bash
xerrameca init \
  --state-dir /var/lib/xerrameca-agent-a \
  --agent-id agent-a \
  --name "Agent A" \
  --endpoint http://10.0.0.10:8791
```

Initialization creates durable local state:

```text
node.json                  public node metadata
node-private-key.pem       Ed25519 private key (0600)
local-agent-api-key        local agent credential (0600)
local-identities.json      hashed local credential projection (0600)
xerrameca.db               node-owned SQLite database
```

The CLI prints paths and public identity only; it does not print the generated local API key or private key.

The node ID is derived from the Ed25519 public key fingerprint and therefore remains stable across process/host restarts as long as the node state directory is preserved.

## Run the node

```bash
xerrameca node --state-dir /var/lib/xerrameca-agent-a --port 8791
```

Public node identity:

```text
GET /v1/node/identity
```

The response contains node ID, agent ID, display name, public key, endpoint and DB path. It never exposes private key or local API credential material.

## Security and ownership

- each node owns its own state directory and SQLite database;
- nodes never open or synchronize another node's SQLite file;
- private key and local credential files are created mode `0600`;
- local identity projections store only API-key hashes;
- Pluribus is not required for node initialization, startup, identity or restart;
- X5.3 adds explicit peer trust; X5.2 nodes intentionally know only themselves.
