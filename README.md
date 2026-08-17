# Xerrameca

**Independent agent-to-agent orchestration service.**

Xerrameca coordinates structured conversations between autonomous agents: turns, leases, rounds, delays, completion, supervision and monitoring. It exposes REST and MCP interfaces and can integrate with Pluribus for identity, permissions and optional long-term memory.

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
| runner / monitor       |
| own SQLite database    |
+-----------+------------+
            |
      ports / adapters
            |
            v
+------------------------+
|        Pluribus        |
| identity / permissions |
| optional Brain memory  |
+------------------------+
```

If Xerrameca is stopped or broken, Pluribus must remain healthy.

## Compatibility target

REST:
- `POST /v1/xerrameca/command`
- `GET /v1/xerrameca/inbox`
- `POST /v1/xerrameca/turns/{turn_id}/claim`
- `POST /v1/xerrameca/turns/{turn_id}/reply`

MCP:
- `xerrameca_command`
- `xerrameca_inbox`
- `xerrameca_claim`
- `xerrameca_reply`
- `xerrameca_list`
- `xerrameca_get`
- `xerrameca_messages`

## Roadmap

- **X0 — Bootstrap & extraction contract**
- **X1 — Independent core**
- **X2 — Pluribus adapter**
- **X3 — Independent deployment**
- **X4 — Pluribus cleanup**

See `docs/ARCHITECTURE.md` and `docs/PLURIBUS_INTEGRATION.md`.

## Status

Early extraction/bootstrap. The currently certified behavior still lives in `spicouc/Pluribus`; this repository will preserve that behavior while eliminating runtime coupling.

## License

Apache-2.0.
