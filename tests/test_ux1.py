import asyncio
import os
import tempfile
import subprocess
import sys
import time
import httpx

import pytest

from xerrameca.command.dto import AgentChoice, ConversationSummary
from xerrameca.command.service import XerramecaCommandService
from xerrameca.node.identity import initialize_node, load_node_state
from xerrameca.node.trust import create_invite, accept_invite_over_http


def _start_node(state_dir, port):
    env = dict(os.environ, PYTHONPATH="/opt/xerrameca-ux/src")
    proc = subprocess.Popen(
        [sys.executable, "-m", "xerrameca.cli", "node", "--state-dir", state_dir,
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if r.status_code == 200:
                return proc
        except Exception:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("node did not start")


@pytest.fixture
def state_dir():
    d = tempfile.mkdtemp(prefix="xerrameca_ux1_")
    initialize_node(d, agent_id="agent_test", display_name="TestAgent", endpoint="http://127.0.0.1:8911")
    yield d


@pytest.fixture
def peer_dir():
    d = tempfile.mkdtemp(prefix="xerrameca_ux1_peer_")
    initialize_node(d, agent_id="agent_peer", display_name="PeerAgent", endpoint="http://127.0.0.1:8912")
    yield d


def _trust(state_a, state_b):
    pa = _start_node(state_a, 8911)
    pb = _start_node(state_b, 8912)
    try:
        token = create_invite(state_a, ttl_seconds=600)
        asyncio.run(accept_invite_over_http(state_b, token, timeout_seconds=10.0))
        token2 = create_invite(state_b, ttl_seconds=600)
        asyncio.run(accept_invite_over_http(state_a, token2, timeout_seconds=10.0))
    finally:
        pa.kill()
        pb.kill()
def test_list_agents_only_trusted(state_dir, peer_dir):
    _trust(state_dir, peer_dir)
    svc = XerramecaCommandService(state_dir)
    agents = svc.list_agents()
    node_ids = {a.node_id for a in agents}
    assert load_node_state(peer_dir).node_id in node_ids
    assert all(a.trusted for a in agents)
    assert all(isinstance(a, AgentChoice) for a in agents)


def test_list_agents_untrusted_not_selectable(state_dir, peer_dir):
    svc = XerramecaCommandService(state_dir)
    agents = svc.list_agents()
    assert load_node_state(peer_dir).node_id not in {a.node_id for a in agents}


def test_command_service_list_conversations_empty(state_dir):
    svc = XerramecaCommandService(state_dir)
    assert svc.list_conversations() == []


def test_dto_no_secrets():
    a = AgentChoice(node_id="xn_1", display_name="A", endpoint="http://x", trusted=True)
    d = a.to_dict()
    assert "api_key" not in d and "secret" not in d and "private_key" not in d
    s = ConversationSummary(id="c1", objective="o", status="active", coordinator_id="xn_1",
                            coordinator_epoch=1, current_round=1, max_rounds=5)
    assert "api_key" not in s.to_dict()


def test_local_api_key_not_in_arguments(state_dir):
    key_path = load_node_state(state_dir).local_api_key_path
    assert os.path.exists(key_path)
    assert "X-API-Key" not in os.environ
import httpx


def test_create_status_list_sync(state_dir, peer_dir):
    _trust(state_dir, peer_dir)
    peer_id = load_node_state(peer_dir).node_id
    pa = _start_node(state_dir, 8911)
    pb = _start_node(peer_dir, 8912)
    try:
        svc = XerramecaCommandService(state_dir, node_port=8911)
        created = svc.create_conversation(peer_node_id=peer_id, objective="UX-1 test", max_rounds=3)
        cid = created["id"]
        assert created["status"] == "active"
        items = svc.list_conversations()
        assert any(i.id == cid for i in items)
        got = svc.get_conversation(cid)
        assert got["id"] == cid
        synced = svc.sync_conversation(cid)
        assert synced["id"] == cid
        key = open(load_node_state(state_dir).local_api_key_path).read().strip()
        r = httpx.get("http://127.0.0.1:8911/v1/node/federation/conversations",
                      headers={"X-API-Key": key}, timeout=5.0)
        assert r.status_code == 200
        assert any(c["id"] == cid for c in r.json()["conversations"])
    finally:
        pa.kill()
        pb.kill()


def test_list_endpoint_requires_auth(state_dir):
    pa = _start_node(state_dir, 8916)
    try:
        r = httpx.get("http://127.0.0.1:8916/v1/node/federation/conversations", timeout=5.0)
        assert r.status_code in (401, 403)
    finally:
        pa.kill()
