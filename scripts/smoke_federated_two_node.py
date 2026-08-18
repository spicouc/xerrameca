#!/usr/bin/env python3
"""Real two-node federated Xerrameca smoke.

Required environment variables are read only at runtime and never printed:

- XERRAMECA_NODE_A_URL
- XERRAMECA_NODE_B_URL
- XERRAMECA_NODE_A_KEY
- XERRAMECA_NODE_B_KEY
- XERRAMECA_NODE_B_ID

Both nodes must already be initialized, running and mutually trusted. Pluribus
is deliberately not part of this smoke.
"""

from __future__ import annotations

import os
import sys
import time

import httpx


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def call(
    client: httpx.Client,
    method: str,
    url: str,
    key: str,
    *,
    json_body: dict | None = None,
) -> dict:
    response = client.request(
        method,
        url,
        headers={"X-API-Key": key},
        json=json_body,
    )
    response.raise_for_status()
    return response.json()


def wait_for_conversation(
    client: httpx.Client,
    base_url: str,
    key: str,
    conversation_id: str,
    *,
    timeout_seconds: float = 20.0,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        response = client.get(
            f"{base_url}/v1/node/federation/conversations/{conversation_id}",
            headers={"X-API-Key": key},
        )
        last_status = response.status_code
        if response.status_code == 200:
            return response.json()
        time.sleep(0.25)
    raise RuntimeError(
        f"conversation did not replicate before timeout (last HTTP {last_status})"
    )


def main() -> int:
    a_url = required("XERRAMECA_NODE_A_URL").rstrip("/")
    b_url = required("XERRAMECA_NODE_B_URL").rstrip("/")
    a_key = required("XERRAMECA_NODE_A_KEY")
    b_key = required("XERRAMECA_NODE_B_KEY")
    b_node_id = required("XERRAMECA_NODE_B_ID")

    with httpx.Client(timeout=15.0) as client:
        for url in (a_url, b_url):
            response = client.get(f"{url}/health")
            response.raise_for_status()
            if response.json().get("status") != "ok":
                raise RuntimeError("node health is not ok")

        created = call(
            client,
            "POST",
            f"{a_url}/v1/node/federation/conversations",
            a_key,
            json_body={
                "peer_node_id": b_node_id,
                "objective": "Federated two-node certification",
                "max_rounds": 3,
                "delay_seconds": 0,
            },
        )
        conversation_id = created["id"]
        epoch = int(created["coordinator_epoch"])

        call(
            client,
            "POST",
            f"{a_url}/v1/node/federation/conversations/{conversation_id}/claim",
            a_key,
            json_body={"expected_epoch": epoch},
        )
        call(
            client,
            "POST",
            f"{a_url}/v1/node/federation/conversations/{conversation_id}/reply",
            a_key,
            json_body={
                "expected_epoch": epoch,
                "content": "Node A continues.",
                "result": "continue",
            },
        )

        wait_for_conversation(client, b_url, b_key, conversation_id)
        call(
            client,
            "POST",
            f"{b_url}/v1/node/federation/conversations/{conversation_id}/claim",
            b_key,
            json_body={"expected_epoch": epoch},
        )
        call(
            client,
            "POST",
            f"{b_url}/v1/node/federation/conversations/{conversation_id}/reply",
            b_key,
            json_body={
                "expected_epoch": epoch,
                "content": "Node B proposes completion.",
                "result": "complete",
            },
        )

        call(
            client,
            "POST",
            f"{a_url}/v1/node/federation/conversations/{conversation_id}/claim",
            a_key,
            json_body={"expected_epoch": epoch},
        )
        final_a = call(
            client,
            "POST",
            f"{a_url}/v1/node/federation/conversations/{conversation_id}/reply",
            a_key,
            json_body={
                "expected_epoch": epoch,
                "content": "Node A confirms completion.",
                "result": "complete",
            },
        )
        if final_a.get("status") != "completed":
            raise RuntimeError("coordinator did not complete the conversation")

        final_b = wait_for_conversation(client, b_url, b_key, conversation_id)
        if final_b.get("status") != "completed":
            # Force one explicit catch-up before failing.
            call(
                client,
                "POST",
                f"{b_url}/v1/node/federation/conversations/{conversation_id}/sync",
                b_key,
            )
            final_b = wait_for_conversation(client, b_url, b_key, conversation_id)
        if final_b.get("status") != "completed":
            raise RuntimeError("participant did not converge to completed")

        comparable_keys = (
            "id",
            "objective",
            "status",
            "coordinator_id",
            "coordinator_epoch",
            "current_round",
            "completion_pending",
            "messages",
        )
        if any(final_a.get(key) != final_b.get(key) for key in comparable_keys):
            raise RuntimeError("final logical conversation views diverged")

    print(
        f"PASS federated two-node smoke conversation={conversation_id} "
        "status=completed histories=logically-identical"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL federated two-node smoke: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
