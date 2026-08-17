# Xerrameca

**Independent agent-to-agent orchestration service.**

Xerrameca coordinates structured conversations between autonomous agents: turns, leases, rounds, delays, completion consensus, supervision and passive monitoring. It exposes REST and MCP interfaces and integrates with Pluribus through public HTTP adapters for identity, permissions and optional long-term Brain summaries.

## Design rule

Xerrameca is an independent service. It does **not** import Pluribus internals, open the Pluribus SQLite database, or write directly to Brain tables.

```text
Agents / MCP / REST
        |
        v
+------------------------+
|       Xerrameca        |
| conversations / turns  |
| leases / rounds        |
| passive monitor        |
| own SQLite database    |
+-----------+------------+
            |
      ports / HTTP adapters
            |
            v
+------------------------+
|        Pluribus        |
| identity / permissions |
| optional Brain memory  |
+------------------------+
```

If Xerrameca is stopped or broken, Pluribus must remain healthy.

## Current capabilities

- independent SQLite persistence
- two-agent alternating dialogue protocol
- claim/lease protection and timeout checks
- rounds and configurable delays
- completion proposal + second-agent confirmation
- supervisor mode
- initiator/admin cancellation
- REST command/inbox/claim/reply surface
- exactly seven standalone MCP tools
- Pluribus identity adapter using public `/v1/identity/*` APIs
- optional Brain summary adapter using public `/v1/memory/write`
- retryable summary outbox that cannot roll back a terminal conversation
- admin-only passive monitor
- non-root Docker image and hardened systemd baseline
- real two-agent X3 certification smoke script

The legacy Pluribus webhook push Runner is intentionally not part of the mandatory core. Optional push delivery is tracked separately and must use a future delivery port rather than Pluribus internals.

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
uvicorn xerrameca.app:app --host 127.0.0.1 --port 8791
```

Then:

```text
GET  http://127.0.0.1:8791/health
POST http://127.0.0.1:8791/mcp/
```

For Pluribus-backed identity, configure `XERRAMECA_IDENTITY_PROVIDER=pluribus` and a reachable `PLURIBUS_BASE_URL`.

## Agent compatibility contract

REST:
- `POST /v1/xerrameca/command`
- `GET /v1/xerrameca/inbox`
- `POST /v1/xerrameca/turns/{turn_id}/claim`
- `POST /v1/xerrameca/turns/{turn_id}/reply`
- conversation list/get/messages endpoints

MCP:
- `xerrameca_command`
- `xerrameca_inbox`
- `xerrameca_claim`
- `xerrameca_reply`
- `xerrameca_list`
- `xerrameca_get`
- `xerrameca_messages`

Agent-facing calls use `X-API-Key`. `X-Agent-ID` is optional and cannot override the identity resolved by the credential.

See `docs/AGENT_INTEGRATION.md` for the full client contract.

## Roadmap

- ✅ **X0 — Bootstrap & extraction contract**
- ✅ **X1 — Independent pull-mode core, REST/MCP and passive monitor**
- ◐ **X2 — Pluribus HTTP adapters**: implementation merged; real-provider certification pending
- ◐ **X3 — Independent deployment**: packaging/runbook merged; real deployment/E2E certification pending
- ⏳ **X4 — Pluribus cleanup**: only after X3 certification

Optional push/webhook delivery is tracked separately and does not block the pull-mode standalone service.

## Documentation

- `docs/ARCHITECTURE.md`
- `docs/PLURIBUS_INTEGRATION.md`
- `docs/AGENT_INTEGRATION.md`
- `docs/DEPLOYMENT.md`

## License

Apache-2.0.
