#!/usr/bin/env python3
"""Certify the real Pluribus identity-provider contract without printing secrets."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx


SENSITIVE_FIELDS = {
    "api_key",
    "api_key_hash",
    "api_key_fingerprint",
    "last_ip",
    "metadata",
    "password",
    "token",
    "secret",
}


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def ensure(response: httpx.Response, label: str) -> Any:
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"{label}: HTTP {response.status_code}: {response.text[:500]}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label}: response is not JSON") from exc


def assert_public_identity(payload: Any, label: str) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label}: identity is not an object")
    required = {"id", "name", "permissions", "allowed_scopes", "capabilities", "is_active"}
    missing = required - set(payload)
    if missing:
        raise RuntimeError(f"{label}: missing fields {sorted(missing)}")
    leaked = SENSITIVE_FIELDS & set(payload)
    if leaked:
        raise RuntimeError(f"{label}: sensitive fields exposed: {sorted(leaked)}")
    encoded = json.dumps(payload, sort_keys=True).casefold()
    for marker in ("api_key_hash", "api_key_fingerprint", "last_ip"):
        if marker in encoded:
            raise RuntimeError(f"{label}: sensitive marker exposed: {marker}")


def main() -> int:
    base_url = os.environ.get("PLURIBUS_URL", "http://127.0.0.1:8790").rstrip("/")
    api_key = required_env("PLURIBUS_AGENT_KEY")
    expected_peer = os.environ.get("PLURIBUS_EXPECTED_PEER", "").strip()

    with httpx.Client(base_url=base_url, timeout=15.0) as client:
        health = ensure(client.get("/health"), "health")
        if health.get("status") not in {"ok", "degraded"}:
            raise RuntimeError(f"Pluribus health is not usable: {health.get('status')}")

        headers = {"X-API-Key": api_key}
        me = ensure(client.get("/v1/identity/me", headers=headers), "identity/me")
        assert_public_identity(me, "identity/me")
        if not me.get("is_active"):
            raise RuntimeError("authenticated agent is inactive")

        peers = ensure(
            client.get("/v1/identity/peers", headers=headers, params={"scope": "shared"}),
            "identity/peers",
        )
        if not isinstance(peers, list):
            raise RuntimeError("identity/peers: response is not a list")
        for index, peer in enumerate(peers):
            assert_public_identity(peer, f"identity/peers[{index}]")
            if peer["id"] == me["id"]:
                raise RuntimeError("identity/peers returned the authenticated caller")

        if expected_peer and not any(
            peer["id"] == expected_peer or peer["name"] == expected_peer for peer in peers
        ):
            raise RuntimeError(f"expected peer not available: {expected_peer}")

    print(
        f"PASS X2 Pluribus provider caller={me['id']} peers={len(peers)} scope=shared"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL X2 provider smoke: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
