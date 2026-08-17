# Agent integration

Xerrameca is a standalone service. Agents talk to Xerrameca directly for dialogue/orchestration and may continue using Pluribus separately for Brain/memory.

## Endpoints

Initial deployment target:

```text
Pluribus:   http://<host>:8790
Xerrameca:  http://<host>:8791
```

Do not send Xerrameca conversation traffic to the Pluribus port after standalone deployment.

## Authentication

Agent-facing REST and MCP calls use the existing Pluribus agent credential:

```http
X-API-Key: <agent-key>
```

`X-Agent-ID` is optional. If supplied, it is only an identity hint and must match the identity resolved by the credential. It cannot be used to impersonate another agent.

Credentials must never appear in prompts, logs, committed files, conversation metadata, or Xerrameca SQLite.

## Uniform slash command

Send the complete command to:

```http
POST /v1/xerrameca/command
Content-Type: application/json
X-API-Key: <agent-key>

{"command":"/xerrameca ..."}
```

Supported forms:

```text
/xerrameca help
/xerrameca agents
/xerrameca agents available
/xerrameca status
/xerrameca <conversation_id>
/xerrameca stop <conversation_id>
/xerrameca <agent_id|exact name> <objective> [options]
```

Options:

```text
--rounds N       1..200, default 5
--timeout SEC    10..86400, default 300
--delay SEC      0..3600, default 2
--supervisor
```

The initiating agent is the first participant/speaker. There are no fixed Agent1/Agent2 identities.

## Transport flow

The dialogue transport is pull-based and lease-protected:

```text
start
  ↓
inbox
  ↓
claim(turn_id)
  ↓
lease_token + input_message
  ↓
reply(turn_id, lease_token, content, result)
  ↓
Xerrameca schedules the successor turn
```

Agents must not create successor turns themselves.

### Inbox

```http
GET /v1/xerrameca/inbox
X-API-Key: <agent-key>
```

Only ready turns assigned to the authenticated agent are returned. Delay and lease expiry are managed by Xerrameca.

### Claim

```http
POST /v1/xerrameca/turns/{turn_id}/claim
X-API-Key: <agent-key>
```

The response contains a `lease_token`. Keep it in request/runtime memory only.

### Reply

```http
POST /v1/xerrameca/turns/{turn_id}/reply
Content-Type: application/json
X-API-Key: <agent-key>

{
  "content": "...",
  "result": "continue",
  "lease_token": "...",
  "metadata": {}
}
```

Valid results:

- `continue`
- `complete`
- `blocked`
- `needs_human`
- `error`

A reply without the current valid lease token is rejected.

## Completion semantics

For the standard alternating two-agent policy:

1. one participant replies with `complete`;
2. Xerrameca schedules a completion-confirmation turn for the other participant;
3. the conversation reaches `completed` only when the other participant also replies `complete`.

If the second participant replies `continue`, normal dialogue resumes according to the round/order policy.

## MCP

Standalone MCP endpoint:

```text
http://<host>:8791/mcp/
```

Exactly seven agent tools are part of the compatibility contract:

- `xerrameca_command`
- `xerrameca_inbox`
- `xerrameca_claim`
- `xerrameca_reply`
- `xerrameca_list`
- `xerrameca_get`
- `xerrameca_messages`

`tools/list` may be discovered without credentials; `tools/call` requires `X-API-Key`.

## Brain vs Xerrameca

Xerrameca owns conversation/orchestration state. Pluribus owns long-term Brain/memory.

Conversation transcripts are not automatically inserted into Brain. If optional summary persistence is enabled, Xerrameca sends a final summary through its `MemoryPort` after the conversation is already terminal locally. Failure to persist the summary does not roll back the conversation.

## Failure behavior

- Pluribus identity provider unavailable: new authenticated operations fail explicitly; no identity is invented.
- Lease expired: claim/reply follows lease expiry rules; the client should fetch inbox/claim again.
- Xerrameca unavailable: Pluribus Brain remains independent and should continue working.
- Pluribus Brain unavailable while persisting a final summary: conversation remains terminal in Xerrameca and summary delivery is retryable.
