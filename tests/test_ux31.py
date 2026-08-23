import asyncio
import os
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

from xerrameca.command.wizard import XerramecaWizardService, WizardError
from xerrameca.integrations.telegram import TelegramUXAdapter
from xerrameca.ui import CallbackStore, TelegramWizardBridge
from xerrameca.node.identity import initialize_node, load_node_state
from xerrameca.node.trust import accept_invite_over_http, create_invite
from xerrameca.command.dto import AgentChoice


class FakeTelegramTransport:
    def __init__(self):
        self.messages = []

    async def send(self, chat_id, text):
        self.messages.append((chat_id, text))

    def last_screen_tokens(self, chat_id):
        seq = []
        for cid, text in self.messages:
            if cid == chat_id and text.startswith("[") and "::" in text:
                label, token = text[1:].split("::", 1)
                label = label.replace("]", "").strip()
                seq.append((label, token))
            else:
                if seq:
                    seq = []
        return seq

    def all_text(self, chat_id):
        return [t for cid, t in self.messages if cid == chat_id]

    def last_text(self, chat_id):
        for cid, text in reversed(self.messages):
            if cid == chat_id:
                return text
        return None


def _start_node(sd, port):
    p = subprocess.Popen(
        [sys.executable, "-m", "xerrameca.cli", "node", "--state-dir", sd, "--host", "127.0.0.1", "--port", str(port)],
        env=dict(os.environ, PYTHONPATH=str(SRC_DIR)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200:
                return p
        except Exception:
            time.sleep(0.2)
    p.kill()
    raise RuntimeError("node did not start")


def _node_api_key(state_dir):
    return open(load_node_state(state_dir).local_api_key_path).read().strip()


from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def peer_dir():
    d = tempfile.mkdtemp(prefix="ux31_peer_")
    initialize_node(d, agent_id="agent_peer", display_name="PeerAgent", endpoint="http://127.0.0.1:8922")
    yield d


@pytest.fixture
def state_dir():
    d = tempfile.mkdtemp(prefix="ux31_state_")
    initialize_node(d, agent_id="agent_test", display_name="TestAgent", endpoint="http://127.0.0.1:8921")
    yield d


@pytest.fixture
async def adapter(state_dir, peer_dir):
    pa = _start_node(state_dir, 8921)
    pb = _start_node(peer_dir, 8922)
    try:
        t = create_invite(state_dir, ttl_seconds=600)
        await accept_invite_over_http(peer_dir, t, timeout_seconds=10.0)
        t2 = create_invite(peer_dir, ttl_seconds=600)
        await accept_invite_over_http(state_dir, t2, timeout_seconds=10.0)
        wizard = XerramecaWizardService(state_dir, ttl_seconds=60, node_port=8921)
        callbacks = CallbackStore(ttl_seconds=60)
        bridge = TelegramWizardBridge(wizard, callbacks)
        transport = FakeTelegramTransport()
        adapter_obj = TelegramUXAdapter(
            node_base_url="http://127.0.0.1:8921", api_key=_node_api_key(state_dir),
            transport=transport, wizard=bridge,
        )
        yield {"wizard": wizard, "callbacks": callbacks, "bridge": bridge,
               "transport": transport, "peer_dir": peer_dir, "state_dir": state_dir,
               "adapter": adapter_obj}
    finally:
        pa.kill()
        pb.kill()


def _peer_node_id(peer_dir):
    return load_node_state(peer_dir).node_id


async def _drive(adapter, chat_id, token_label=None):
    tokens = adapter["transport"].last_screen_tokens(chat_id)
    if not tokens:
        raise AssertionError("no buttons rendered")
    if token_label:
        match = [t for t in tokens if token_label in t[0]]
        if not match:
            raise AssertionError(f"no button with label {token_label}; got {[t[0] for t in tokens]}")
        token = match[0][1]
    else:
        token = tokens[0][1]
    await adapter["adapter"].handle_callback(chat_id, token)
    return token


def _capture_token(adapter, chat_id, token_label):
    tokens = adapter["transport"].last_screen_tokens(chat_id)
    match = [t for t in tokens if token_label in t[0]]
    if not match:
        raise AssertionError(f"no button with label {token_label}; got {[t[0] for t in tokens]}")
    return match[0][1]


# 1. /xerrameca via handle_text opens ROOT
@pytest.mark.asyncio
async def test_xerrameca_root_via_handle_text(adapter):
    chat = "c1"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert "Nova conversa" in labels
    assert "Converses" in labels


# 2. objective via handle_text (full public surface)
@pytest.mark.asyncio
async def test_objective_via_handle_text(adapter):
    chat = "c2"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_text(chat, "Objectiu real per prova")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("proposer" in l for l in labels), labels


# 3. custom role via handle_text
@pytest.mark.asyncio
async def test_custom_role_via_handle_text(adapter):
    chat = "c3"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_text(chat, "Objectiu custom")
    await _drive(adapter, chat, "custom")
    await adapter["adapter"].handle_text(chat, "arquitecte de seguretat")
    sess = adapter["wizard"].get_session(adapter["bridge"].active_session_id(chat), chat)
    assert sess.data["role_a"] == "arquitecte de seguretat"
    # once a custom role is set, further arbitrary text is NOT consumed as wizard input
    before = len(adapter["transport"].messages)
    handled = await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "text arbitrari")
    assert handled is False
    assert len(adapter["transport"].messages) == before


# 4. full public E2E -> START -> exactly one conversation
@pytest.mark.asyncio
async def test_full_public_e2e_start(adapter, monkeypatch):
    from xerrameca.command.service import XerramecaCommandService

    calls = {"n": 0}

    def fake(self, **kw):
        calls["n"] += 1
        return {"id": "xfc_e2e123"}

    monkeypatch.setattr(XerramecaCommandService, "create_conversation", fake)
    chat = "c4"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_text(chat, "E2E objective")
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, "5")
    await _drive(adapter, chat, "summary")
    start_token = _capture_token(adapter, chat, "INICIAR")
    await adapter["adapter"].handle_callback(chat, start_token)
    sess = adapter["wizard"].get_session(adapter["bridge"].active_session_id(chat), chat)
    assert sess.state == "STARTED"
    assert calls["n"] == 1


# 5. double START same callback -> one conversation
@pytest.mark.asyncio
async def test_double_start_same_callback(adapter, monkeypatch):
    from xerrameca.command.service import XerramecaCommandService

    calls = {"n": 0}

    def fake(self, **kw):
        calls["n"] += 1
        return {"id": "xfc_dbl123"}

    monkeypatch.setattr(XerramecaCommandService, "create_conversation", fake)
    chat = "c5"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_text(chat, "Dobla")
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, "5")
    await _drive(adapter, chat, "summary")
    start_token = _capture_token(adapter, chat, "INICIAR")
    await adapter["adapter"].handle_callback(chat, start_token)
    cid1 = adapter["wizard"].get_session(adapter["bridge"].active_session_id(chat), chat).data["conversation_id"]
    await adapter["adapter"].handle_callback(chat, start_token)
    cid2 = adapter["wizard"].get_session(adapter["bridge"].active_session_id(chat), chat).data["conversation_id"]
    assert cid1 == cid2
    assert calls["n"] == 1


# 6. new /xerrameca during active session -> resume, no ghost session
@pytest.mark.asyncio
async def test_resume_active_session(adapter):
    chat = "c6"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    sid1 = adapter["bridge"].active_session_id(chat)
    # second /xerrameca must resume the SAME session (not create a new one)
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    sid2 = adapter["bridge"].active_session_id(chat)
    assert sid1 == sid2
    # tokens from the first ROOT render are still valid (same session)
    assert len(adapter["bridge"].callbacks._by_session.get(sid1, set())) > 0


# 7. callback of an invalidated session -> rejected
@pytest.mark.asyncio
async def test_invalidated_session_callback_rejected(adapter):
    chat = "c7"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    sid = adapter["bridge"].active_session_id(chat)
    token = _capture_token(adapter, chat, "Nova conversa")
    adapter["bridge"].callbacks.invalidate_session(sid)
    before = len(adapter["transport"].all_text(chat))
    await adapter["adapter"].handle_callback(chat, token)
    new = adapter["transport"].all_text(chat)
    assert any("no vàlida" in m or "expirada" in m for m in new[before:])
    # session still exists but token gone
    assert adapter["wizard"].get_session(sid, chat) is not None


# 8. different caller -> rejected
@pytest.mark.asyncio
async def test_callback_other_caller_rejected(adapter):
    chat = "c8"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    token = adapter["transport"].last_screen_tokens(chat)[0][1]
    before = len(adapter["transport"].all_text("other"))
    await adapter["adapter"].handle_callback("other", token)
    new = adapter["transport"].all_text("other")
    assert any("no vàlida" in m or "expirada" in m for m in new[before:])


# 9. expired callback -> rejected
@pytest.mark.asyncio
async def test_expired_callback_rejected(adapter):
    import time as _t

    chat = "c9"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    token = adapter["transport"].last_screen_tokens(chat)[0][1]
    adapter["callbacks"]._tokens[token].expires_at = int(_t.time()) - 1
    before = len(adapter["transport"].all_text(chat))
    await adapter["adapter"].handle_callback(chat, token)
    new = adapter["transport"].all_text(chat)
    assert any("no vàlida" in m or "expirada" in m for m in new[before:])


# 10. peer status None -> unknown (not offline)
@pytest.mark.asyncio
async def test_peer_unknown_status(adapter, monkeypatch):
    from xerrameca.command.service import XerramecaCommandService

    def fake_list(self, *, check_online=False):
        return [AgentChoice(node_id="xn_unknown1", display_name="Ghost", endpoint="http://x", trusted=True, online=None)]

    monkeypatch.setattr(XerramecaCommandService, "list_agents", fake_list)
    chat = "c10"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    await _drive(adapter, chat, "Nova conversa")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("unknown" in l for l in labels), labels
    assert not any("offline" in l for l in labels), labels


# 11. raw WizardError -> no internal detail leaked to Telegram
@pytest.mark.asyncio
async def test_wizard_error_no_internal_leak(adapter):
    chat = "c11"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    await _drive(adapter, chat, "Nova conversa")
    # build a state that will trigger a controlled WizardError on an invalid action
    sid = adapter["bridge"].active_session_id(chat)
    # mint a token bound to a bogus action at the SELECT_PEER state
    token = adapter["callbacks"].mint(caller_id=chat, session_id=sid, action_id="peer:not_trusted_node")
    before = len(adapter["transport"].all_text(chat))
    await adapter["adapter"].handle_callback(chat, token)
    msgs = adapter["transport"].all_text(chat)[before:]
    joined = " ".join(msgs)
    # must NOT contain internal phrasing from wizard errors
    assert "peer no trusted" not in joined
    assert "sessió" not in joined
    assert any("no vàlida" in m or "expirada" in m for m in msgs)


# 12. legacy commands still PASS
@pytest.mark.asyncio
async def test_legacy_commands_still_work(adapter, peer_dir):
    conv = await adapter["adapter"].start(peer_node_id=_peer_node_id(peer_dir), objective="legacy", max_rounds=3)
    assert "id" in conv
    status = await adapter["adapter"].status(conv["id"])
    assert status["id"] == conv["id"]
    await adapter["adapter"].sync(conv["id"])
    mode = adapter["adapter"].set_mode(conv["id"], "live")
    assert mode.value == "live"



# 13. ROOT CAUSE GUARD: exactly one "custom" button, action = role_*_custom_input,
#      legacy role_*:custom must NOT be a selectable button.
@pytest.mark.asyncio
async def test_role_custom_button_uniqueness(adapter):
    chat = "c13"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "Objetiu")

    # SELECT_ROLE_A
    scr_a = adapter["wizard"].current_screen(adapter["bridge"].active_session_id(chat), chat)
    labels_a = [b.action_id for b in scr_a.buttons]
    custom_a = [b for b in scr_a.buttons if b.label == "custom"]
    assert len(custom_a) == 1, custom_a
    assert custom_a[0].action_id == "role_a:custom_input", custom_a[0].action_id
    assert "role_a:custom" not in labels_a, labels_a

    await _drive(adapter, chat, None)  # -> SELECT_ROLE_B

    scr_b = adapter["wizard"].current_screen(adapter["bridge"].active_session_id(chat), chat)
    labels_b = [b.action_id for b in scr_b.buttons]
    custom_b = [b for b in scr_b.buttons if b.label == "custom"]
    assert len(custom_b) == 1, custom_b
    assert custom_b[0].action_id == "role_b:custom_input", custom_b[0].action_id
    assert "role_b:custom" not in labels_b, labels_b

    # BACK -> SELECT_ROLE_A -> forward -> SELECT_ROLE_B keeps uniqueness
    await _drive(adapter, chat, "Enrere")
    await _drive(adapter, chat, None)
    scr_b2 = adapter["wizard"].current_screen(adapter["bridge"].active_session_id(chat), chat)
    custom_b2 = [b for b in scr_b2.buttons if b.label == "custom"]
    assert len(custom_b2) == 1
    assert custom_b2[0].action_id == "role_b:custom_input"


# 14. free text without choosing custom is NOT consumed as a role.
@pytest.mark.asyncio
async def test_free_text_without_custom_rejected(adapter):
    chat = "c14"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "Objetiu")
    await _drive(adapter, chat, None)  # SELECT_ROLE_A -> SELECT_ROLE_B
    sid = adapter["bridge"].active_session_id(chat)
    before = adapter["wizard"].get_session(sid, chat).data.get("role_b")
    # arbitrary text must NOT be stored as the role
    handled = await adapter["adapter"].handle_wizard_text(chat, sid, "text-aleatori-sense-custom")
    assert handled is False
    after = adapter["wizard"].get_session(sid, chat).data.get("role_b")
    assert before == after
