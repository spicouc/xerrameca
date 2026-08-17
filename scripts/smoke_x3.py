#!/usr/bin/env python3
"""Real two-agent X3 smoke without printing credentials."""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx


EXPECTED_TOOLS = {
    "xerrameca_command",
    "xerrameca_inbox",
    "xerrameca_claim",
    "xerrameca_reply",
    "xerrameca_list",
    "xerrameca_get",
    "xerrameca_messages",
}


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def ensure(response: httpx.Response, label: str) -> Any:
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"{label}: HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


def main() -> int:
    base_url = os.environ.get("XERRAMECA_URL", "http://127.0.0.1:8791").rstrip("/")
    key_a = required_env("XERRAMECA_AGENT_A_KEY")
    key_b = required_env("XERRAMECA_AGENT_B_KEY")
    target_b = required_env("XERRAMECA_AGENT_B")

    with httpx.Client(base_url=base_url, timeout=20.0) as client:
        health = ensure(client.get("/health"), "health")
        if health.get("status") != "ok":
            raise RuntimeError(f"health not ok: {health}")

        tools = ensure(
            client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1},
            ),
            "mcp tools/list",
        )
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        if tool_names != EXPECTED_TOOLS:
            raise RuntimeError(f"unexpected MCP tools: {sorted(tool_names)}")

        agents = ensure(
            client.post(
                "/v1/xerrameca/command",
                headers=headers(key_a),
                json={"command": "/xerrameca agents"},
            ),
            "agents",
        )
        if not any(item["id"] == target_b or item["name"] == target_b for item in agents["agents"]):
            raise RuntimeError(f"target agent not available: {target_b}")

        started = ensure(
            client.post(
                "/v1/xerrameca/command",
                headers=headers(key_a),
                json={
                    "command": f"/xerrameca {target_b} X3 standalone certification smoke --rounds 2 --timeout 60 --delay 0"
                },
            ),
            "start conversation",
        )
        conversation_id = started["conversation"]["id"]

        inbox_a = ensure(
            client.get("/v1/xerrameca/inbox", headers=headers(key_a)),
            "agent A inbox",
        )
        turn_a = inbox_a["turns"][0]["id"]
        claim_a = ensure(
            client.post(
                f"/v1/xerrameca/turns/{turn_a}/claim", headers=headers(key_a)
            ),
            "agent A claim",
        )
        ensure(
            client.post(
                f"/v1/xerrameca/turns/{turn_a}/reply",
                headers=headers(key_a),
                json={
                    "content": "Agent A confirms standalone REST/MCP routing.",
                    "result": "continue",
                    "lease_token": claim_a["lease_token"],
                    "metadata": {"smoke": "x3"},
                },
            ),
            "agent A reply",
        )

        inbox_b = ensure(
            client.get("/v1/xerrameca/inbox", headers=headers(key_b)),
            "agent B inbox",
        )
        turn_b = inbox_b["turns"][0]["id"]
        claim_b = ensure(
            client.post(
                f"/v1/xerrameca/turns/{turn_b}/claim", headers=headers(key_b)
            ),
            "agent B claim",
        )
        proposed = ensure(
            client.post(
                f"/v1/xerrameca/turns/{turn_b}/reply",
                headers=headers(key_b),
                json={
                    "content": "Agent B confirms independent orchestration and proposes completion.",
                    "result": "complete",
                    "lease_token": claim_b["lease_token"],
                    "metadata": {"smoke": "x3"},
                },
            ),
            "agent B completion proposal",
        )
        if not proposed.get("completion_pending"):
            raise RuntimeError("completion confirmation was not scheduled")

        confirm_inbox = ensure(
            client.get("/v1/xerrameca/inbox", headers=headers(key_a)),
            "agent A confirmation inbox",
        )
        confirm_turn = confirm_inbox["turns"][0]["id"]
        confirm_claim = ensure(
            client.post(
                f"/v1/xerrameca/turns/{confirm_turn}/claim",
                headers=headers(key_a),
            ),
            "agent A confirmation claim",
        )
        final = ensure(
            client.post(
                f"/v1/xerrameca/turns/{confirm_turn}/reply",
                headers=headers(key_a),
                json={
                    "content": "Agent A confirms completion.",
                    "result": "complete",
                    "lease_token": confirm_claim["lease_token"],
                    "metadata": {"smoke": "x3"},
                },
            ),
            "agent A completion confirmation",
        )
        if final.get("status") != "completed":
            raise RuntimeError(f"conversation did not complete: {final.get('status')}")

        persisted = ensure(
            client.get(
                f"/v1/xerrameca/conversations/{conversation_id}",
                headers=headers(key_a),
            ),
            "final persisted state",
        )
        if persisted.get("status") != "completed":
            raise RuntimeError("completed state was not persisted")

    print(f"PASS X3 standalone smoke conversation={conversation_id} tools=7 status=completed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL X3 standalone smoke: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
