import asyncio
import os
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

from xerrameca.command.wizard import XerramecaWizardService
from xerrameca.integrations.telegram import TelegramUXAdapter
from xerrameca.ui import CallbackStore, TelegramWizardBridge
from xerrameca.node.identity import initialize_node, load_node_state
from xerrameca.node.trust import accept_invite_over_http, create_invite


class FakeTelegramTransport:
    """Minimal TelegramTransport implementation (no external dependency)."""

    def __init__(self):
        self.messages = []

    async def send(self, chat_id: str, text: str) -> None:
        self.messages.append((chat_id, text))

    def tokens(self, chat_id):
        out = []
        for cid, text in self.messages:
            if cid == chat_id and text.startswith("[") and "::" in text:
                label, token = text[1:].split("::", 1)
                label = label.replace("]", "").strip()
                out.append((label, token))
        return out

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

    def last_text(self, chat_id):
        for cid, text in reversed(self.messages):
            if cid == chat_id:
                return text
        return None

    def all_text(self, chat_id):
        return [t for cid, t in self.messages if cid == chat_id]


from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


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


@pytest.fixture
def peer_dir():
    d = tempfile.mkdtemp(prefix="ux3_peer_")
    initialize_node(d, agent_id="agent_peer", display_name="PeerAgent", endpoint="http://127.0.0.1:8922")
    yield d


@pytest.fixture
def state_dir():
    d = tempfile.mkdtemp(prefix="ux3_state_")
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
        yield {
            "wizard": wizard, "callbacks": callbacks, "bridge": bridge,
            "transport": transport, "peer_dir": peer_dir, "state_dir": state_dir,
            "adapter": adapter_obj,
        }
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
def test_neutral_button_model(adapter):
    from xerrameca.ui import NeutralButton, NeutralScreen

    b = NeutralButton(label="X", callback_token="abc")
    s = NeutralScreen(text="hi", buttons=[b], state="ROOT")
    assert s.to_dict()["buttons"][0]["callback_token"] == "abc"
    assert b.label == "X"


def test_telegram_transport_compatibility(adapter):
    assert hasattr(adapter["transport"], "send")


@pytest.mark.asyncio
async def test_xerrameca_root(adapter):
    await adapter["adapter"].start_wizard("chat1")
    texts = adapter["transport"].all_text("chat1")
    joined = "\n".join(texts)
    assert "Nova conversa" in joined
    labels = [t[0] for t in adapter["transport"].tokens("chat1")]
    assert any("Nova conversa" in l for l in labels)
    assert any("Converses" in l for l in labels)
    assert any("Agents" in l for l in labels)
    assert any("Ajuda" in l for l in labels)


@pytest.mark.asyncio
async def test_wizard_via_buttons(adapter, peer_dir):
    chat = "chatA"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Brainstorm")
    await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "Dissenyar API")
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, "6")
    await _drive(adapter, chat, "live")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("INICIAR" in l for l in labels), labels
    # the INICIAR button must be present and capturable at CONFIRM
    start_token = _capture_token(adapter, chat, "INICIAR")
    assert start_token


@pytest.mark.asyncio
async def test_text_input_objective(adapter):
    chat = "chatT"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    handled = await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "El meu objectiu lliure")
    assert handled is True
    sess = adapter["wizard"]._require_session(adapter["bridge"].active_session_id(chat), chat)
    assert sess.data["user_objective"] == "El meu objectiu lliure"


@pytest.mark.asyncio
async def test_text_input_custom_role(adapter):
    chat = "chatR"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "El meu objectiu")
    await _drive(adapter, chat, None)
    handled = await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "arquitecte")
    assert handled is True
    sess = adapter["wizard"]._require_session(adapter["bridge"].active_session_id(chat), chat)
    assert sess.data["role_b"] == "arquitecte"


@pytest.mark.asyncio
async def test_trusted_peer_rendering(adapter, peer_dir):
    chat = "chatP"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any(_peer_node_id(peer_dir)[:8] in l or "PeerAgent" in l for l in labels)


@pytest.mark.asyncio
async def test_presets(adapter):
    chat = "chatPre"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    for key in ("Conversa", "Brainstorm", "Debat", "Revisió", "Decisió", "Tasca"):
        assert any(key in l for l in labels), key


@pytest.mark.asyncio
async def test_roles(adapter):
    chat = "chatRole"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Tasca")
    await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "Objectiu")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    for r in ("proposer", "reviewer", "researcher", "critic", "executor", "supervisor", "custom"):
        assert any(r in l.lower() for l in labels), r


@pytest.mark.asyncio
async def test_rounds(adapter):
    chat = "chatRnd"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "Obj")
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, None)
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    for r in ("3", "5", "6", "10"):
        assert any(r == l for l in labels), r


@pytest.mark.asyncio
async def test_output_mode(adapter):
    chat = "chatOut"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "Obj")
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, "5")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    for m in ("summary", "live", "silent"):
        assert any(m in l.lower() for l in labels), m
@pytest.mark.asyncio
async def test_back(adapter):
    chat = "chatBack"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Enrere")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("Nova conversa" in l or "agent" in l.lower() for l in labels)


@pytest.mark.asyncio
async def test_wizard_cancel(adapter):
    chat = "chatCancel"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Cancel")
    last = adapter["transport"].last_text(chat)
    assert "cancel" in last.lower()
    sess = adapter["wizard"]._require_session(adapter["bridge"].active_session_id(chat), chat)
    assert sess.state == "CANCELLED"


@pytest.mark.asyncio
async def test_confirm_start_creates_conversation(adapter):
    chat = "chatStart"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "Fer prova")
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, "5")
    await _drive(adapter, chat, "summary")
    await _drive(adapter, chat, "INICIAR")
    sess = adapter["wizard"]._require_session(adapter["bridge"].active_session_id(chat), chat)
    assert sess.state == "STARTED"
    assert "conversation_id" in sess.data


@pytest.mark.asyncio
async def test_duplicate_start_protection(adapter, monkeypatch):
    """Double confirm:start (same captured token) -> exactly one conversation (idempotent)."""
    chat = "chatDup"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    await _drive(adapter, chat, "PeerAgent")
    await _drive(adapter, chat, "Conversa")
    await adapter["adapter"].handle_wizard_text(chat, adapter["bridge"].active_session_id(chat), "Dup test")
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, None)
    await _drive(adapter, chat, "5")
    await _drive(adapter, chat, "summary")
    # capture the INICIAR token while still at CONFIRM (before any START)
    start_token = _capture_token(adapter, chat, "INICIAR")
    from xerrameca.command.service import XerramecaCommandService

    calls = {"n": 0}

    def fake(self, **kw):
        calls["n"] += 1
        return {"id": "xfc_dup123"}

    monkeypatch.setattr(XerramecaCommandService, "create_conversation", fake)
    # first START
    await adapter["adapter"].handle_callback(chat, start_token)
    cid = adapter["wizard"]._require_session(adapter["bridge"].active_session_id(chat), chat).data["conversation_id"]
    # second START with the EXACT same token (simulated double-click)
    await adapter["adapter"].handle_callback(chat, start_token)
    cid2 = adapter["wizard"]._require_session(adapter["bridge"].active_session_id(chat), chat).data["conversation_id"]
    assert cid == cid2
    assert calls["n"] == 1, f"create_conversation called {calls['n']} times, expected 1"


@pytest.mark.asyncio
async def test_callback_ownership(adapter):
    chat = "chatOwn"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    token = adapter["transport"].last_screen_tokens(chat)[0][1]
    # another caller uses the token -> adapter sends an error, no new screen
    before = len(adapter["transport"].messages)
    await adapter["adapter"].handle_callback("other_chat", token)
    new_msgs = adapter["transport"].all_text("other_chat")
    assert any("error" in m.lower() for m in new_msgs)


@pytest.mark.asyncio
async def test_callback_expiry(adapter):
    import time

    chat = "chatExp"
    await adapter["adapter"].start_wizard(chat)
    await _drive(adapter, chat, "Nova conversa")
    token = adapter["transport"].last_screen_tokens(chat)[0][1]
    adapter["callbacks"]._tokens[token].expires_at = int(time.time()) - 1
    await adapter["adapter"].handle_callback(chat, token)
    new_msgs = adapter["transport"].all_text(chat)
    assert any("error" in m.lower() for m in new_msgs)


def test_callback_size(adapter):
    token = adapter["callbacks"].mint(caller_id="c", session_id="s1", action_id="root:new")
    assert adapter["callbacks"].size_ok(token)
    assert len(token) <= 64
    assert "api_key" not in token and "objective" not in token


def test_secret_leakage_in_tokens(adapter):
    token = adapter["callbacks"].mint(caller_id="c", session_id="s", action_id="peer:secretnode")
    assert "secretnode" not in token
    assert "api" not in token


@pytest.mark.asyncio
async def test_legacy_start_command(adapter, peer_dir):
    conv = await adapter["adapter"].start(peer_node_id=_peer_node_id(peer_dir), objective="legacy test", max_rounds=3)
    assert "id" in conv


@pytest.mark.asyncio
async def test_legacy_status_sync_mode(adapter, peer_dir):
    conv = await adapter["adapter"].start(peer_node_id=_peer_node_id(peer_dir), objective="legacy2", max_rounds=3)
    cid = conv["id"]
    status = await adapter["adapter"].status(cid)
    assert status["id"] == cid
    await adapter["adapter"].sync(cid)
    mode = adapter["adapter"].set_mode(cid, "live")
    assert mode.value == "live"


def test_telegram_no_hard_external_imports():
    """telegram.py must not import python-telegram-bot/aiogram/telebot."""
    import ast

    src_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "xerrameca"
        / "integrations"
        / "telegram.py"
    )
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    assert "telegram" not in imported
    assert "aiogram" not in imported
    assert "telebot" not in imported
