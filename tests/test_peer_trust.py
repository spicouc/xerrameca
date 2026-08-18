from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xerrameca.domain.errors import ForbiddenError
from xerrameca.node.app import create_node_app
from xerrameca.node.identity import initialize_node, load_node_state
from xerrameca.node.trust import (
    PEERS_FILENAME,
    accept_incoming,
    build_acceptance,
    complete_acceptance,
    create_invite,
    get_peer,
    list_peers,
    revoke_peer,
    sign_peer_request,
    verify_invite,
)


def _nodes(tmp_path: Path):
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a = initialize_node(
        a_dir,
        agent_id="agent-a",
        display_name="Agent A",
        endpoint="http://node-a:8791",
    )
    b = initialize_node(
        b_dir,
        agent_id="agent-b",
        display_name="Agent B",
        endpoint="http://node-b:8791",
    )
    return a_dir, b_dir, a, b


def _trust_pair(tmp_path: Path):
    a_dir, b_dir, a, b = _nodes(tmp_path)
    token = create_invite(a_dir, ttl_seconds=600)
    acceptance, _ = build_acceptance(b_dir, token)
    confirmation = accept_incoming(a_dir, acceptance)
    complete_acceptance(b_dir, token, confirmation)
    return a_dir, b_dir, a, b, token, acceptance


def test_invite_establishes_durable_mutual_trust_without_central_server(
    tmp_path: Path,
) -> None:
    a_dir, b_dir, a, b, token, acceptance = _trust_pair(tmp_path)

    assert [peer.node_id for peer in list_peers(a_dir)] == [b.node_id]
    assert [peer.node_id for peer in list_peers(b_dir)] == [a.node_id]
    assert get_peer(a_dir, b.node_id).trust_status == "trusted"
    assert get_peer(b_dir, a.node_id).trust_status == "trusted"

    # Restart/load does not change either node identity or trust projection.
    assert load_node_state(a_dir).node_id == a.node_id
    assert load_node_state(b_dir).node_id == b.node_id
    assert get_peer(a_dir, b.node_id).public_key == b.public_key
    assert get_peer(b_dir, a.node_id).public_key == a.public_key

    # Same acceptor replay is idempotently confirmed, not duplicated.
    replay_confirmation = accept_incoming(a_dir, acceptance)
    replay_peer = complete_acceptance(b_dir, token, replay_confirmation)
    assert replay_peer.node_id == a.node_id
    assert len(list_peers(a_dir)) == 1
    assert len(list_peers(b_dir)) == 1

    peers_raw = (a_dir / PEERS_FILENAME).read_text(encoding="utf-8").lower()
    assert "private" not in peers_raw
    assert "api_key" not in peers_raw


def test_tampered_invite_signature_is_rejected(tmp_path: Path) -> None:
    a_dir, _, _, _ = _nodes(tmp_path)
    token = create_invite(a_dir, ttl_seconds=600)
    payload, signature = token.split(".", 1)
    replacement = "A" if signature[-1] != "A" else "B"
    tampered = payload + "." + signature[:-1] + replacement
    with pytest.raises(ForbiddenError):
        verify_invite(tampered)


def test_revoked_peer_cannot_authenticate_new_peer_traffic(tmp_path: Path) -> None:
    a_dir, b_dir, _, b, _, _ = _trust_pair(tmp_path)
    path = "/v1/node/peer/ping"
    headers = sign_peer_request(
        b_dir,
        method="POST",
        path=path,
        body=b"",
    )

    app = create_node_app(str(a_dir))
    with TestClient(app) as client:
        accepted = client.post(path, headers=headers)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["peer_node_id"] == b.node_id

        revoked = revoke_peer(a_dir, b.node_id)
        assert revoked.trust_status == "revoked"

        denied = client.post(path, headers=headers)
        assert denied.status_code == 403
        assert "revoked" in denied.json()["detail"]

    assert list_peers(a_dir) == []
    assert get_peer(a_dir, b.node_id).trust_status == "revoked"


def test_invite_contains_only_public_issuer_material(tmp_path: Path) -> None:
    a_dir, _, a, _ = _nodes(tmp_path)
    token = create_invite(a_dir, ttl_seconds=600)
    payload = verify_invite(token)
    serialized = json.dumps(payload).lower()
    assert payload["issuer"]["node_id"] == a.node_id
    assert "private" not in serialized
    assert "api_key" not in serialized
