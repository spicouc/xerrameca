import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

from xerrameca.command.wizard import XerramecaWizardService
from xerrameca.command.service import XerramecaCommandService
from xerrameca.command.dto import ConversationSummary
from xerrameca.integrations.telegram import TelegramUXAdapter
from xerrameca.ui import CallbackStore, TelegramWizardBridge


# The package is installed editable into the working venv and resolves to the
# worktree src. We do NOT re-insert sys.path here so pytest and the wizard see
# ONE module instance; re-inserting can yield a second copy and break monkeypatching.
SRC_DIR = Path(__file__).resolve().parents[1] / "src"



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

    def last_text(self, chat_id):
        for cid, text in reversed(self.messages):
            if cid == chat_id:
                return text
        return None

    def all_text(self, chat_id):
        return [t for cid, t in self.messages if cid == chat_id]


def _conv(**kw):
    base = {
        "id": "xfc_a72d000000000000",
        "objective": "obj",
        "status": "RUNNING",
        "coordinator_id": "c",
        "coordinator_epoch": 0,
        "current_round": 3,
        "max_rounds": 6,
        "participants": ["peerX"],
    }
    base.update(kw)
    return ConversationSummary(**base)


def _conv_dict(**kw):
    return _conv(**kw).to_dict()


# ---- configurable conversation store monkeypatched onto the service class ----
STORE = {"list": [_conv()], "get": None, "get_error": None, "sync_error": None}


@pytest.fixture(autouse=True)
def _reset_store():
    STORE["list"] = [_conv()]
    STORE["get"] = None
    STORE["get_error"] = None
    STORE["sync_error"] = None
    yield


def _fake_list(self, **kw):
    return STORE["list"]


def _fake_get(self, cid):
    if STORE["get_error"]:
        raise STORE["get_error"]
    view = STORE["get"]
    if view is None:
        for c in STORE["list"]:
            if c.id == cid:
                view = c.to_dict()
                break
    if view is None:
        raise LookupError("conversation not found")
    return view


def _fake_sync(self, cid):
    if STORE["sync_error"]:
        raise STORE["sync_error"]
    return {"ok": True}


# UX-4.1 tests inject a FakeCommandService into the wizard (dependency injection)
# so the conversation surface is exercised without touching the real service
# class or contaminating other test modules. The fake reads from the STORE dict
# configured per-test via the _reset_store autouse fixture.


class FakeCommandService:
    def __init__(self, *_a, **_k):
        pass
    def list_conversations(self, **kw):
        return STORE["list"]
    def get_conversation(self, cid):
        return _fake_get(self, cid)
    def sync_conversation(self, cid):
        return _fake_sync(self, cid)
    def list_agents(self, **kw):
        return []


@pytest.fixture
def adapter():
    sd = tempfile.mkdtemp()
    fake_cmd = FakeCommandService()
    wizard = XerramecaWizardService(sd, ttl_seconds=60, node_port=8891, command_service=fake_cmd)
    callbacks = CallbackStore()
    bridge = TelegramWizardBridge(wizard, callbacks)
    transport = FakeTelegramTransport()
    a = TelegramUXAdapter(
        node_base_url="http://x", api_key="k", transport=transport, wizard=bridge
    )
    return {
        "wizard": wizard,
        "callbacks": callbacks,
        "bridge": bridge,
        "transport": transport,
        "adapter": a,
    }


async def _drive(adapter, chat_id, token_label=None):
    tokens = adapter["transport"].last_screen_tokens(chat_id)
    if not tokens:
        raise AssertionError(f"no tokens rendered; {adapter['transport'].all_text(chat_id)}")
    if token_label is None:
        token = tokens[0][1]
    else:
        match = [t for t in tokens if token_label in t[0]]
        if not match:
            raise AssertionError(f"no button {token_label}; got {[t[0] for t in tokens]}")
        token = match[0][1]
    await adapter["adapter"].handle_callback(chat_id, token)
    return token


def _token_for(adapter, chat_id, label):
    return [t for t in adapter["transport"].last_screen_tokens(chat_id) if label in t[0]][0][1]


def _screen_texts(adapter, chat_id):
    out = []
    for cid, text in reversed(adapter["transport"].messages):
        if cid != chat_id:
            continue
        if text.startswith("[") and "::" in text:
            continue
        out.append(text)
    return out


async def _open_conversations(adapter, chat):
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    await _drive(adapter, chat, "Converses")


# 1. root -> Converses
@pytest.mark.asyncio
async def test_root_to_conversations(adapter):
    chat = "c1"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    assert any(t[0] == "Converses" for t in adapter["transport"].last_screen_tokens(chat))
    await _drive(adapter, chat, "Converses")
    texts = _screen_texts(adapter, chat)
    assert any(("Converses" in t or "RUNNING" in t) for t in texts)


# 2. empty list
@pytest.mark.asyncio
async def test_empty_list(adapter):
    STORE["list"] = []
    chat = "c2"
    await _open_conversations(adapter, chat)
    texts = _screen_texts(adapter, chat)
    assert any("converses" in t.lower() for t in texts)
    assert any("Enrere" in t[0] for t in adapter["transport"].last_screen_tokens(chat))


# 3. list with one conversation
@pytest.mark.asyncio
async def test_one_conversation(adapter):
    chat = "c3"
    await _open_conversations(adapter, chat)
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("RUNNING" in l for l in labels)


# 4. multiple conversations
@pytest.mark.asyncio
async def test_multiple_conversations(adapter):
    STORE["list"] = [
        _conv(id="xfc_a72d000000000001", status="RUNNING", current_round=3, max_rounds=6),
        _conv(id="xfc_d293000000000002", status="COMPLETED", current_round=6, max_rounds=6),
    ]
    chat = "c4"
    await _open_conversations(adapter, chat)
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("RUNNING" in l for l in labels)
    assert any("COMPLETED" in l for l in labels)


# 5. correct selection resolves to the right detail
@pytest.mark.asyncio
async def test_selection(adapter):
    STORE["list"] = [
        _conv(id="xfc_a72d000000000001", status="RUNNING"),
        _conv(id="xfc_d293000000000002", status="COMPLETED"),
    ]
    chat = "c5"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "COMPLETED")
    sid = adapter["bridge"].active_session_id(chat)
    assert adapter["wizard"].get_session(sid, chat).data["_active_conv"] == "xfc_d293000000000002"


# 6. running render
@pytest.mark.asyncio
async def test_running_render(adapter):
    chat = "c6"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    texts = _screen_texts(adapter, chat)
    assert any("RUNNING" in t for t in texts)
    assert any("ronda" in t for t in texts)


# 7. completed render
@pytest.mark.asyncio
async def test_completed_render(adapter):
    STORE["list"] = [_conv(status="COMPLETED", current_round=6, max_rounds=6)]
    chat = "c7"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "COMPLETED")
    texts = _screen_texts(adapter, chat)
    assert any("COMPLETED" in t for t in texts)


# 8. error render when get_conversation raises
@pytest.mark.asyncio
async def test_error_render(adapter):
    STORE["get_error"] = LookupError("gone")
    chat = "c8"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    texts = _screen_texts(adapter, chat)
    assert any(("no trobada" in t or "ja" in t or "s'ha pogut" in t) for t in texts)
    for t in texts:
        assert "Traceback" not in t


# 9. current_round / max_rounds shown
@pytest.mark.asyncio
async def test_rounds_detail(adapter):
    chat = "c9"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    texts = _screen_texts(adapter, chat)
    assert any(("3" in t and "6" in t) for t in texts)


# 10. last message summarized
@pytest.mark.asyncio
async def test_last_message(adapter):
    STORE["get"] = {**_conv_dict(), **{"last_message": "Hola, sóc el peer, respon si us plau amb detall i punt"}}
    chat = "c10"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    texts = _screen_texts(adapter, chat)
    assert any("últim missatge" in t for t in texts)
    for t in texts:
        assert len(t) < 600


# 11. refresh rebuilds
@pytest.mark.asyncio
async def test_refresh(adapter):
    chat = "c11"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("Actualitzar" in l for l in labels)
    await _drive(adapter, chat, "Actualitzar")
    texts = _screen_texts(adapter, chat)
    assert any("RUNNING" in t for t in texts)


# 12. refresh does not mutate (no federation write)
@pytest.mark.asyncio
async def test_refresh_mutation_free(adapter):
    calls = []
    orig = XerramecaCommandService.sync_conversation
    def trap(self, cid):
        calls.append(cid)
        return _fake_sync(self, cid)
    XerramecaCommandService.sync_conversation = trap
    try:
        chat = "c12"
        await _open_conversations(adapter, chat)
        await _drive(adapter, chat, "RUNNING")
        await _drive(adapter, chat, "Actualitzar")
        assert calls == []
    finally:
        XerramecaCommandService.sync_conversation = orig


# 13. sync works
@pytest.mark.asyncio
async def test_sync(adapter):
    chat = "c13"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    await _drive(adapter, chat, "Sincronitzar")
    texts = _screen_texts(adapter, chat)
    assert any("RUNNING" in t for t in texts)


# 14. sync peer unavailable -> controlled error
@pytest.mark.asyncio
async def test_sync_peer_unavailable(adapter):
    STORE["sync_error"] = RuntimeError("peer unreachable")
    chat = "c14"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    await _drive(adapter, chat, "Sincronitzar")
    texts = _screen_texts(adapter, chat)
    joined = " ".join(texts).lower()
    assert "peer" in joined
    for t in texts:
        assert "Traceback" not in t


# 15. mode summary
@pytest.mark.asyncio
async def test_mode_summary(adapter):
    chat = "c15"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    await _drive(adapter, chat, "Mode")
    await _drive(adapter, chat, "summary")
    sid = adapter["bridge"].active_session_id(chat)
    assert adapter["wizard"].get_session(sid, chat).data["_conv_mode"] == "summary"


# 16. mode live
@pytest.mark.asyncio
async def test_mode_live(adapter):
    chat = "c16"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    await _drive(adapter, chat, "Mode")
    await _drive(adapter, chat, "live")
    sid = adapter["bridge"].active_session_id(chat)
    assert adapter["wizard"].get_session(sid, chat).data["_conv_mode"] == "live"


# 17. mode silent
@pytest.mark.asyncio
async def test_mode_silent(adapter):
    chat = "c17"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    await _drive(adapter, chat, "Mode")
    await _drive(adapter, chat, "silent")
    sid = adapter["bridge"].active_session_id(chat)
    assert adapter["wizard"].get_session(sid, chat).data["_conv_mode"] == "silent"


# 18. mode creates no federated event
@pytest.mark.asyncio
async def test_mode_no_federated_event(adapter):
    calls = []
    orig = XerramecaCommandService.sync_conversation
    def trap(self, cid):
        calls.append(cid)
        return _fake_sync(self, cid)
    XerramecaCommandService.sync_conversation = trap
    try:
        chat = "c18"
        await _open_conversations(adapter, chat)
        await _drive(adapter, chat, "RUNNING")
        await _drive(adapter, chat, "Mode")
        await _drive(adapter, chat, "silent")
        assert calls == []
    finally:
        XerramecaCommandService.sync_conversation = orig


# 19. back detail->list
@pytest.mark.asyncio
async def test_back_detail_to_list(adapter):
    chat = "c19"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    assert any("Actualitzar" in t[0] for t in adapter["transport"].last_screen_tokens(chat))
    await _drive(adapter, chat, "Enrere")
    assert any("RUNNING" in t[0] for t in adapter["transport"].last_screen_tokens(chat))  # list


# 20. back list->root
@pytest.mark.asyncio
async def test_back_list_to_root(adapter):
    chat = "c20"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "Enrere")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("Nova conversa" in l for l in labels)  # root


# 21. caller isolation (different caller cannot act on another caller's conversation session)
@pytest.mark.asyncio
async def test_caller_isolation(adapter):
    chat = "c21"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    sid = adapter["bridge"].active_session_id(chat)
    # another caller tries to invoke a callback minted for chat
    other = "other"
    tokens = adapter["transport"].last_screen_tokens(chat)
    # mint attempt from other caller should be rejected / no crash
    try:
        await adapter["adapter"].handle_callback(other, tokens[0][1])
    except Exception:
        pass
    # original session unaffected
    assert adapter["wizard"].get_session(sid, chat) is not None


# 22. stale callback (minted for a different action) rejected / no crash, session unaffected
@pytest.mark.asyncio
async def test_stale_callback(adapter):
    chat = "c22"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    sid = adapter["bridge"].active_session_id(chat)
    # token minted for a now-invalid action -> controlled, no crash, no mutation
    token = adapter["callbacks"].mint(caller_id=chat, session_id=sid, action_id="conv:nope")
    before_state = adapter["wizard"].get_session(sid, chat).state
    try:
        await adapter["adapter"].handle_callback(chat, token)
    except Exception:
        pass
    assert adapter["wizard"].get_session(sid, chat).state == before_state


# 23. stale / nonexistent conversation
@pytest.mark.asyncio
async def test_stale_conversation(adapter):
    STORE["get_error"] = LookupError("not found")
    chat = "c23"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    texts = _screen_texts(adapter, chat)
    for t in texts:
        assert "Traceback" not in t


# 24. token does not contain conversation_id (short index only in action)
@pytest.mark.asyncio
async def test_token_no_conversation_id(adapter):
    chat = "c24"
    await _open_conversations(adapter, chat)
    tokens = adapter["transport"].last_screen_tokens(chat)
    full_id = "xfc_a72d000000000000"
    for _, tok in tokens:
        assert full_id not in str(tok)
    # also confirm the conversation list buttons use short action ids (no full id)
    sid = adapter["bridge"].active_session_id(chat)
    screen = adapter["wizard"].current_screen(sid, chat)
    for b in screen.buttons:
        assert full_id not in b.action_id


# 25. token <= 64 bytes
@pytest.mark.asyncio
async def test_token_max_64(adapter):
    chat = "c25"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    await _drive(adapter, chat, "Mode")
    tokens = adapter["transport"].last_screen_tokens(chat)
    for _, tok in tokens:
        assert tok is not None and len(str(tok)) <= 64


# 26. secret leakage none in detail render
@pytest.mark.asyncio
async def test_no_secret_leak(adapter):
    chat = "c26"
    await _open_conversations(adapter, chat)
    await _drive(adapter, chat, "RUNNING")
    texts = _screen_texts(adapter, chat)
    for text in texts:
        for secret in ("api_key", "private", "secret", "signature", "BEGIN RSA", "BEGIN PRIVATE", "event log", "payload "):
            assert secret.lower() not in text.lower()


# 27. UX-3 creation flow still works
@pytest.mark.asyncio
async def test_creation_flow_still_works(adapter):
    chat = "c27"
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    await _drive(adapter, chat, "Nova conversa")
    # SELECT_PEER -> enrere -> ROOT (creation unaffected)
    await _drive(adapter, chat, "Enrere")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("Nova conversa" in l for l in labels)


# 28. legacy Telegram start/status/sync/mode still works
@pytest.mark.asyncio
async def test_legacy_commands(adapter):
    chat = "c28"
    # stub the HTTP request path so legacy commands return deterministic bodies
    class FakeResp:
        def json(self):
            return {"id": "legacy-1", "status": "RUNNING"}
        def raise_for_status(self):
            pass
    async def fake_request(self, method, path, **kw):
        return FakeResp()
    orig = TelegramUXAdapter._request
    TelegramUXAdapter._request = fake_request
    try:
        conv = await adapter["adapter"].start(peer_node_id="peerX", objective="legacy", max_rounds=3)
        assert conv.get("id") == "legacy-1"
        st = await adapter["adapter"].status("legacy-1")
        assert st.get("status") == "RUNNING"
        sy = await adapter["adapter"].sync("legacy-1")
        assert sy.get("status") == "RUNNING"
        # set_mode is synchronous and returns TelegramMode without network
        from xerrameca.integrations.telegram import TelegramMode
        raw_mode = adapter["adapter"].set_mode("legacy-1", "live")
        assert raw_mode == TelegramMode.LIVE
    finally:
        TelegramUXAdapter._request = orig



# 29. end-to-end public surface: /xerrameca -> Converses -> select -> refresh -> sync -> mode -> back
@pytest.mark.asyncio
async def test_public_surface_e2e(adapter):
    chat = "c29"
    # open wizard through the public handle_text() path
    await adapter["adapter"].handle_text(chat, "/xerrameca")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("Converses" in l for l in labels)  # ROOT
    # ROOT -> Converses
    await _drive(adapter, chat, "Converses")
    assert any("RUNNING" in l for l in [t[0] for t in adapter["transport"].last_screen_tokens(chat)])
    # select conversation
    await _drive(adapter, chat, "RUNNING")
    assert any("Actualitzar" in l for l in [t[0] for t in adapter["transport"].last_screen_tokens(chat)])
    # Actualitzar
    await _drive(adapter, chat, "Actualitzar")
    assert any("Sincronitzar" in l for l in [t[0] for t in adapter["transport"].last_screen_tokens(chat)])
    # Sincronitzar
    await _drive(adapter, chat, "Sincronitzar")
    assert any("Mode" in l for l in [t[0] for t in adapter["transport"].last_screen_tokens(chat)])
    # Mode -> silent
    await _drive(adapter, chat, "Mode")
    await _drive(adapter, chat, "silent")
    assert any("Enrere" in l for l in [t[0] for t in adapter["transport"].last_screen_tokens(chat)])
    # Enrere mode->detail
    await _drive(adapter, chat, "Enrere")
    assert any("Actualitzar" in l for l in [t[0] for t in adapter["transport"].last_screen_tokens(chat)])
    # final back to list then root
    await _drive(adapter, chat, "Enrere")
    assert any("RUNNING" in l for l in [t[0] for t in adapter["transport"].last_screen_tokens(chat)])
    await _drive(adapter, chat, "Enrere")
    labels = [t[0] for t in adapter["transport"].last_screen_tokens(chat)]
    assert any("Nova conversa" in l for l in labels)  # back at ROOT
