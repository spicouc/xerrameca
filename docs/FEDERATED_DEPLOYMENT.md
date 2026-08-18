# Federated deployment and recovery

Status: federated v1 release-candidate runbook.

## 1. Deployment rule

Each agent node owns its own process, identity material and SQLite database. Do not share a state directory between agents and do not place the live node database on a network filesystem.

Recommended layout per host:

```text
/var/lib/xerrameca/<agent>/
  node.json
  node-private-key.pem
  local-agent-api-key
  local-identities.json
  peers.json
  xerrameca.db
```

Protect the whole directory as secret-bearing state. The SQLite database itself must not contain plaintext local API/private key material, but adjacent identity files do.

## 2. Install

```bash
python3 -m venv /opt/xerrameca/venv
/opt/xerrameca/venv/bin/pip install .
install -d -m 0700 -o xerrameca -g xerrameca /var/lib/xerrameca/agent-a
```

Initialize once:

```bash
/opt/xerrameca/venv/bin/xerrameca init \
  --state-dir /var/lib/xerrameca/agent-a \
  --agent-id agent-a \
  --name 'Agent A' \
  --endpoint http://HOST_A:8791
```

Never re-run `init` over an existing state directory to rotate identity. Identity rotation requires an explicit migration/trust procedure.

## 3. Run node

```bash
/opt/xerrameca/venv/bin/xerrameca node \
  --state-dir /var/lib/xerrameca/agent-a \
  --host 0.0.0.0 \
  --port 8791
```

Health:

```text
GET http://HOST_A:8791/health
```

Public identity:

```text
GET http://HOST_A:8791/v1/node/identity
```

Expose the node port only to networks/peers that need it. Peer mutations are signed, but network-level filtering is still recommended.

## 4. Trust bootstrap

A creates a bounded invite:

```bash
xerrameca invite create --state-dir /var/lib/xerrameca/agent-a --ttl 600
```

Transfer the token through a secure channel while valid. B accepts:

```bash
xerrameca invite accept --state-dir /var/lib/xerrameca/agent-b '<token>'
```

Verify both peers after restart:

```bash
xerrameca peer list --state-dir /var/lib/xerrameca/agent-a
xerrameca peer list --state-dir /var/lib/xerrameca/agent-b
```

Revocation is local and explicit:

```bash
xerrameca peer revoke --state-dir /var/lib/xerrameca/agent-a <peer-node-id>
```

## 5. Docker

The image defaults to the legacy standalone service for compatibility. For node mode, override the command and mount durable state:

```bash
docker run --rm -p 8791:8791 \
  -v xerrameca-agent-a:/var/lib/xerrameca \
  xerrameca:1.0.0rc1 \
  xerrameca node --state-dir /var/lib/xerrameca/node --host 0.0.0.0 --port 8791
```

Initialize the mounted volume before starting the long-running node.

## 6. Optional dashboard

Run it as a separate process:

```bash
xerrameca dashboard --state-dir /var/lib/xerrameca/agent-a --host 127.0.0.1 --port 8792
```

Federated v1 dashboard is read-only. Keep it on loopback or behind an authenticated reverse proxy. Dashboard failure has no conversation impact.

## 7. Backup

For each node, back up together:

- `node.json`
- `node-private-key.pem`
- `local-agent-api-key`
- `local-identities.json`
- peer trust state
- `xerrameca.db`

Before copying a live SQLite database, use a SQLite-safe backup method or stop the node briefly. Do not copy only the database and discard identity material: the restored node would no longer be the same cryptographic peer.

Store backups encrypted and restrict access because node private identity material is included.

## 8. Restore

1. Stop the node process.
2. Restore the complete state directory to the same protected path.
3. Verify owner/mode, especially private key and local API credential.
4. Run `PRAGMA quick_check` against the restored SQLite database.
5. Start the node.
6. Verify `/health` and `/v1/node/identity` return the expected node id/public key.
7. Run peer catch-up/sync for any conversation that advanced while the node was offline.

Never generate a new key and pretend it is the restored old node.

## 9. Upgrade

Federated changes are additive to the legacy standalone mode. For each upgrade:

1. back up the full state directory;
2. install the new package/image;
3. run CI-compatible local smoke/tests where available;
4. start one node and verify identity did not change;
5. verify SQLite quick check;
6. reconnect one peer and test bounded catch-up;
7. then roll the remaining nodes.

Do not roll back only code after an incompatible schema migration without restoring the matching pre-upgrade database backup. Federated v1 RC1 should keep schema changes forward-compatible inside the RC line, but backups remain mandatory.

## 10. Real two-host release gate

Stable v1 requires a real network smoke on two independent hosts with Pluribus stopped/unreachable:

```text
Node A init/start
Node B init/start
A/B trust
A creates conversation
A replies
B claims/replies
completion proposal
other node confirms completion
both histories identical
restart B
B identity unchanged
bounded catch-up PASS
coordinator failover test PASS
PRAGMA quick_check both DBs = ok
Pluribus contacted = NO
```

Record only node ids, conversation id, commit/package version and PASS/FAIL evidence. Never copy local API keys, private keys or invite tokens into the certification report.
