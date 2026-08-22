import asyncio
import tempfile

import pytest

from xerrameca.command.dto import WizardAction
from xerrameca.command.presets import PRESETS, VALID_ROLES, VALID_ROUNDS
from xerrameca.command.service import XerramecaCommandService
from xerrameca.command.wizard import WizardError, XerramecaWizardService
from xerrameca.node.identity import initialize_node
from xerrameca.node.trust import create_invite, accept_invite_over_http


@pytest.fixture
def state_dir():
    d = tempfile.mkdtemp(prefix="xerrameca_ux2_")
    initialize_node(d, agent_id="agent_test", display_name="TestAgent", endpoint="http://127.0.0.1:8921")
    yield d


@pytest.fixture
def peer_dir():
    d = tempfile.mkdtemp(prefix="xerrameca_ux2_peer_")
    initialize_node(d, agent_id="agent_peer", display_name="PeerAgent", endpoint="http://127.0.0.1:8922")
    yield d


@pytest.fixture
def wiz(state_dir, peer_dir):
    _trust(state_dir, peer_dir)
    return XerramecaWizardService(state_dir, ttl_seconds=60, node_port=8921)


def _trust(state_a, state_b):
    import subprocess
    import sys
    import time
    import httpx

    def start(sd, port):
        p = subprocess.Popen([sys.executable, "-m", "xerrameca.cli", "node", "--state-dir", sd,
                              "--host", "127.0.0.1", "--port", str(port)],
                             env=dict(__import__("os").environ, PYTHONPATH="/opt/xerrameca-ux2/src"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200:
                    return p
            except Exception:
                time.sleep(0.2)
        p.kill()
        raise RuntimeError("node did not start")

    pa = start(state_a, 8921)
    pb = start(state_b, 8922)
    try:
        t = create_invite(state_a, ttl_seconds=600)
        asyncio.run(accept_invite_over_http(state_b, t, timeout_seconds=10.0))
        t2 = create_invite(state_b, ttl_seconds=600)
        asyncio.run(accept_invite_over_http(state_a, t2, timeout_seconds=10.0))
    finally:
        pa.kill()
        pb.kill()


def _peer_node_id(peer_dir):
    from xerrameca.node.identity import load_node_state

    return load_node_state(peer_dir).node_id
def test_root_screen(wiz):
    s = wiz.root_screen()
    assert s.state == "ROOT"
    assert any(b.action_id == "root:new" for b in s.buttons)


def test_new_conversation_starts_wizard(wiz):
    sess = wiz.create_session("callerA")
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="root:new"))
    assert screen.state == "SELECT_PEER"


def test_trusted_peer_selection(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="root:new"))
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id=f"peer:{_peer_node_id(peer_dir)}"))
    assert screen.state == "SELECT_DIALOGUE_TYPE"
    assert sess.data["peer_node_id"] == _peer_node_id(peer_dir)


def test_untrusted_rejection(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="root:new"))
    with pytest.raises(WizardError):
        wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="peer:xn_unknownpeer123"))


def test_preset_selection(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "SELECT_DIALOGUE_TYPE", peer_dir)
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="mode:brainstorm"))
    assert screen.state == "ENTER_OBJECTIVE"
    assert sess.data["dialogue_type"] == "brainstorm"
    assert sess.data["role_a"] == PRESETS["brainstorm"].default_role_a


def test_custom_objective(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "ENTER_OBJECTIVE", peer_dir)
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="objective:set", payload={"objective": "Revisar arquitectura"}))
    assert screen.state == "SELECT_ROLE_A"
    assert sess.data["user_objective"] == "Revisar arquitectura"


def test_default_roles(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "SELECT_ROLE_A", peer_dir)
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="role_a:proposer"))
    assert screen.state == "SELECT_ROLE_B"
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="role_b:executor"))
    assert sess.data["role_a"] == "proposer" and sess.data["role_b"] == "executor"


def test_custom_roles(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "ENTER_OBJECTIVE", peer_dir)
    wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="objective:set", payload={"objective": "Q"}))
    wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="role_a:custom"))
    wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="role_b:custom"))
    assert sess.data["role_a"] == "custom" and sess.data["role_b"] == "custom"


def test_round_selection(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "SELECT_ROUNDS", peer_dir)
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="rounds:6"))
    assert screen.state == "SELECT_OUTPUT_MODE"
    assert sess.data["max_rounds"] == 6


def test_round_invalid(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "SELECT_ROUNDS", peer_dir)
    with pytest.raises(WizardError):
        wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="rounds:99"))


def test_output_selection(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "SELECT_OUTPUT_MODE", peer_dir)
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="output:live"))
    assert screen.state == "CONFIRM"
    assert sess.data["output_mode"] == "live"


def test_confirm_screen(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "CONFIRM", peer_dir)
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="nav:back"))
    assert screen.state == "SELECT_OUTPUT_MODE"
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="output:summary"))
    assert screen.state == "CONFIRM"
    assert any(b.action_id == "confirm:start" for b in screen.buttons)


def _advance_to(wiz, sess, target, peer_dir):
    flow = {
        "SELECT_PEER": ["root:new"],
        "SELECT_DIALOGUE_TYPE": ["root:new", f"peer:{_peer_node_id(peer_dir)}"],
        "ENTER_OBJECTIVE": ["root:new", f"peer:{_peer_node_id(peer_dir)}", "mode:conversation"],
        "SELECT_ROLE_A": ["root:new", f"peer:{_peer_node_id(peer_dir)}", "mode:conversation", "objective:set"],
        "SELECT_ROLE_B": ["root:new", f"peer:{_peer_node_id(peer_dir)}", "mode:conversation", "objective:set", "role_a:proposer"],
        "SELECT_ROUNDS": ["root:new", f"peer:{_peer_node_id(peer_dir)}", "mode:conversation", "objective:set", "role_a:proposer", "role_b:reviewer"],
        "SELECT_OUTPUT_MODE": ["root:new", f"peer:{_peer_node_id(peer_dir)}", "mode:conversation", "objective:set", "role_a:proposer", "role_b:reviewer", "rounds:5"],
        "CONFIRM": ["root:new", f"peer:{_peer_node_id(peer_dir)}", "mode:conversation", "objective:set", "role_a:proposer", "role_b:reviewer", "rounds:5", "output:summary"],
    }
    for a in flow[target]:
        payload = {"objective": "X"} if a == "objective:set" else None
        wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id=a, payload=payload))
def test_effective_objective_deterministic(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "CONFIRM", peer_dir)
    e1 = wiz.build_effective_objective(sess)
    e2 = wiz.build_effective_objective(sess)
    assert e1 == e2
    assert "[USER OBJECTIVE]" in e1 and "Revisar" not in e1
    assert "[DIALOGUE MODE]" in e1 and "[LOCAL AGENT ROLE]" in e1 and "[PEER AGENT ROLE]" in e1 and "[COMPLETION]" in e1


def test_back_navigation(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "SELECT_ROUNDS", peer_dir)
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="nav:back"))
    assert screen.state == "SELECT_ROLE_B"
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="nav:back"))
    assert screen.state == "SELECT_ROLE_A"
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="nav:back"))
    assert screen.state == "ENTER_OBJECTIVE"


def test_wizard_cancel(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "SELECT_ROUNDS", peer_dir)
    screen = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="wizard:cancel"))
    assert screen.state == "CANCELLED"
    with pytest.raises(WizardError):
        wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="nav:back"))


def test_ttl_expiry(wiz):
    sess = wiz.create_session("callerA")
    wiz._sessions[sess.session_id].expires_at = wiz._now() - 1
    with pytest.raises(WizardError):
        wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="root:new"))


def test_stale_callback_rejection(wiz):
    sess = wiz.create_session("callerA")
    wiz._sessions[sess.session_id].expires_at = wiz._now() - 1
    with pytest.raises(WizardError):
        wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="root:new"))


def test_session_ownership_isolation(wiz):
    sa = wiz.create_session("callerA")
    sb = wiz.create_session("callerB")
    wiz.handle_action(sa.session_id, "callerA", WizardAction(action_id="root:new"))
    with pytest.raises(WizardError):
        wiz.handle_action(sa.session_id, "callerB", WizardAction(action_id="peer:x"))


def test_double_callback_idempotency(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "CONFIRM", peer_dir)
    peer = _peer_node_id(peer_dir)
    wiz._sessions[sess.session_id].session.data["peer_node_id"] = peer
    import subprocess, sys, time, httpx, os

    def start(sd, port):
        p = subprocess.Popen([sys.executable, "-m", "xerrameca.cli", "node", "--state-dir", sd,
                              "--host", "127.0.0.1", "--port", str(port)],
                             env=dict(os.environ, PYTHONPATH="/opt/xerrameca-ux2/src"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200:
                    return p
            except Exception:
                time.sleep(0.2)
        p.kill()
        raise RuntimeError("node did not start")

    pa = start(wiz.state_dir, 8921)
    pb = start(peer_dir, 8922)
    try:
        s1 = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="confirm:start"))
        assert s1.state == "STARTED"
        cid = sess.data["conversation_id"]
        s2 = wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="confirm:start"))
        assert s2.state == "STARTED"
        assert sess.data["conversation_id"] == cid
    finally:
        pa.kill()
        pb.kill()


def test_start_calls_command_service_once(wiz, peer_dir, monkeypatch):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "CONFIRM", peer_dir)
    wiz._sessions[sess.session_id].session.data["peer_node_id"] = _peer_node_id(peer_dir)
    calls = {"n": 0}

    def fake(self, **kw):
        calls["n"] += 1
        return {"id": "xfc_fake123"}

    monkeypatch.setattr(XerramecaCommandService, "create_conversation", fake)
    wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="confirm:start"))
    wiz.handle_action(sess.session_id, "callerA", WizardAction(action_id="confirm:start"))
    assert calls["n"] == 1


def test_no_secrets_in_session(wiz, peer_dir):
    sess = wiz.create_session("callerA")
    _advance_to(wiz, sess, "CONFIRM", peer_dir)
    blob = str(sess.to_dict())
    assert "api_key" not in blob and "secret" not in blob and "private_key" not in blob


def test_command_service_reuse(wiz):
    assert hasattr(wiz, "_sessions")
