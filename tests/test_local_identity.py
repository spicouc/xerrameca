from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.adapters.local_identity import LocalIdentityAdapter
from xerrameca.app import create_app
from xerrameca.config import settings


def _identity_file(tmp_path: Path) -> Path:
    path = tmp_path / "local-identities.json"
    path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "id": "agent-a",
                        "name": "Agent A",
                        "api_key_sha256": LocalIdentityAdapter.hash_api_key("secret-a"),
                        "permissions": {"read": True, "write": True, "admin": False},
                        "allowed_scopes": ["shared"],
                        "capabilities": {"xerrameca": True},
                    },
                    {
                        "id": "agent-b",
                        "name": "Agent B",
                        "api_key_sha256": LocalIdentityAdapter.hash_api_key("secret-b"),
                        "permissions": {"read": True, "write": True, "admin": False},
                        "allowed_scopes": ["shared"],
                        "capabilities": {"xerrameca": True},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_local_provider_authenticates_hashed_credentials(tmp_path: Path) -> None:
    provider = LocalIdentityAdapter(_identity_file(tmp_path))
    assert provider._identity_for_key("secret-a").id == "agent-a"
    assert provider._identity_for_key("secret-b").id == "agent-b"
    assert provider._identity_for_key("wrong") is None

    raw = (tmp_path / "local-identities.json").read_text(encoding="utf-8")
    assert "secret-a" not in raw
    assert "secret-b" not in raw


def test_local_mode_runs_without_pluribus(tmp_path: Path, monkeypatch) -> None:
    identity_path = _identity_file(tmp_path)
    monkeypatch.setattr(settings, "XERRAMECA_IDENTITY_PROVIDER", "local")
    monkeypatch.setattr(
        settings, "XERRAMECA_LOCAL_IDENTITY_PATH", str(identity_path)
    )
    monkeypatch.setattr(settings, "PLURIBUS_SERVICE_API_KEY", None)

    app = create_app(db_path=str(tmp_path / "xerrameca.db"))
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["identity_provider"] == "local"

        agents = client.post(
            "/v1/xerrameca/command",
            headers={"X-API-Key": "secret-a"},
            json={"command": "/xerrameca agents"},
        )
        assert agents.status_code == 200, agents.text
        assert [item["id"] for item in agents.json()["agents"]] == ["agent-b"]

        started = client.post(
            "/v1/xerrameca/command",
            headers={"X-API-Key": "secret-a"},
            json={
                "command": "/xerrameca agent-b Local-only conversation --rounds 2 --delay 0"
            },
        )
        assert started.status_code == 200, started.text
        assert started.json()["conversation"]["status"] == "active"


def test_local_mode_fails_closed_on_invalid_key(tmp_path: Path, monkeypatch) -> None:
    identity_path = _identity_file(tmp_path)
    monkeypatch.setattr(settings, "XERRAMECA_IDENTITY_PROVIDER", "local")
    monkeypatch.setattr(
        settings, "XERRAMECA_LOCAL_IDENTITY_PATH", str(identity_path)
    )
    monkeypatch.setattr(settings, "PLURIBUS_SERVICE_API_KEY", None)

    app = create_app(db_path=str(tmp_path / "xerrameca.db"))
    with TestClient(app) as client:
        response = client.get(
            "/v1/xerrameca/inbox", headers={"X-API-Key": "wrong"}
        )
    assert response.status_code == 403


def test_explicit_pluribus_mode_is_still_selectable(monkeypatch) -> None:
    from xerrameca.app import _configured_identity_provider
    from xerrameca.adapters.pluribus_identity import PluribusIdentityAdapter

    monkeypatch.setattr(settings, "XERRAMECA_IDENTITY_PROVIDER", "pluribus")
    provider = _configured_identity_provider()
    assert isinstance(provider, PluribusIdentityAdapter)
