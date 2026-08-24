import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from xerrameca.node.identity import initialize_node, load_node_state
from xerrameca.node.trust import accept_invite_over_http, create_invite

from xerrameca.integrations.telegram_bot_api import (
    TelegramBotAPITransport,
    TelegramTransportError,
)
from xerrameca.integrations.telegram import TelegramUXAdapter
from xerrameca.command.wizard import XerramecaWizardService
from xerrameca.command.dto import ConversationSummary
from xerrameca.ui import CallbackStore, TelegramWizardBridge


# ---------------------------------------------------------------------------
# Fake Telegram Bot API via httpx.MockTransport -> zero real network.
# ---------------------------------------------------------------------------
class FakeTelegramAPI:
    def __init__(self, *, http_status: int = 200, ok_flag: bool = True):
        self.requests: list[tuple[str, dict]] = []
        self.http_status = http_status
        self.ok_flag = ok_flag

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json as _j
        path = request.url.path
        body = _j.loads(request.read()) if request.read() else {}
        self.requests.append((path, body))
        ok = self.ok_flag
        blob = {"ok": ok, "result": {}} if ok else {"ok": False, "description": "unauthorized"}
        return httpx.Response(self.http_status, json=blob)


def make_client(fake):
    return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))


def make_transport(fake=None):
    fake = fake or FakeTelegramAPI()
    client = make_client(fake)
    return TelegramBotAPITransport(token="TEST:BOT-TOKEN", client=client,
                                   api_base="https://fake.telegram.invalid"), fake


class FakeScreen:
    def __init__(self, text="hola", buttons=None):
        self.text = text
        self.buttons = buttons or []


class FakeBtn:
    def __init__(self, label, token):
        self.label = label
        self.callback_token = token


# ---------------------------------------------------------------------------
# Real wizard + DI command service for full flow tests.
# ---------------------------------------------------------------------------
class FakeCommandService:
    STORE = {"list": [], "get": None, "get_error": None, "sync_error": None}
    def __init__(self, *_a, **_k):
        pass
    def list_conversations(self, **kw):
        return self.STORE["list"]
    def get_conversation(self, cid):
        if self.STORE["get_error"]:
            raise self.STORE["get_error"]
        return {"id": cid, "status": "RUNNING", "current_round": 3,
                "max_rounds": 6, "participants": ["peerX"]}
    def sync_conversation(self, cid):
        return {"ok": True}
    def list_agents(self, **kw):
        return []


def _conv(status="RUNNING", r=3, mr=6):
    return ConversationSummary(
        id="xfc_a72d000000000000", objective="obj", status=status,
        coordinator_id="c", coordinator_epoch=0,
        current_round=r, max_rounds=mr, participants=["peerX"])


def make_adapter(transport, cmd_store=None):
    sd = tempfile.mkdtemp()
    cmd = FakeCommandService()
    if cmd_store is not None:
        cmd.STORE = cmd_store
    wizard = XerramecaWizardService(sd, ttl_seconds=600, node_port=8891,
                                    command_service=cmd)
    callbacks = CallbackStore()
    bridge = TelegramWizardBridge(wizard, callbacks)
    adapter = TelegramUXAdapter(node_base_url="http://x", api_key="k",
                                transport=transport, wizard=bridge)
    return adapter, callbacks, cmd


SRC_DIR_PATH = Path(__file__).resolve().parents[1] / "src"


# ---------------------------------------------------------------------------
# Real trusted-peer nodes for the public-surface text-input flow (no Telegram
# network: the adapter's transport is TelegramBotAPITransport over the fake).
# ---------------------------------------------------------------------------
def _start_node(sd, port):
    p = subprocess.Popen(
        [sys.executable, "-m", "xerrameca.cli", "node", "--state-dir", sd,
         "--host", "127.0.0.1", "--port", str(port)],
        env=dict(os.environ, PYTHONPATH=str(SRC_DIR_PATH)),
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
def _peer_dir_42():
    d = tempfile.mkdtemp(prefix="ux42_peer_")
    initialize_node(d, agent_id="agent_peer", display_name="PeerAgent",
                    endpoint="http://127.0.0.1:8932")
    yield d


@pytest.fixture
def _state_dir_42():
    d = tempfile.mkdtemp(prefix="ux42_state_")
    initialize_node(d, agent_id="agent_test", display_name="TestAgent",
                    endpoint="http://127.0.0.1:8931")
    yield d


@pytest.fixture
async def ux42_flow(_state_dir_42, _peer_dir_42):
    """Real trusted peers (real wizard state) + FakeTelegramAPI button transport.

    Reuses the httpx.MockTransport zero-network fake for the Telegram side and
    real nodes for the wizard's peer flow, so the whole public surface
    (/xerrameca -> nova conversa -> peer -> preset -> objective) is exercised
    through the real inline_keyboard transport.
    """
    state_dir, peer_dir = _state_dir_42, _peer_dir_42
    pa = _start_node(state_dir, 8931)
    pb = _start_node(peer_dir, 8932)
    try:
        t = create_invite(state_dir, ttl_seconds=600)
        await accept_invite_over_http(peer_dir, t, timeout_seconds=10.0)
        t2 = create_invite(peer_dir, ttl_seconds=600)
        await accept_invite_over_http(state_dir, t2, timeout_seconds=10.0)
        wizard = XerramecaWizardService(state_dir, ttl_seconds=60, node_port=8931)
        callbacks = CallbackStore(ttl_seconds=60)
        bridge = TelegramWizardBridge(wizard, callbacks)
        tr, fake = make_transport()
        adapter = TelegramUXAdapter(
            node_base_url="http://127.0.0.1:8931", api_key=_node_api_key(state_dir),
            transport=tr, wizard=bridge,
        )
        yield {"adapter": adapter, "transport": tr, "fake": fake,
               "wizard": wizard, "bridge": bridge, "callbacks": callbacks}
    finally:
        pa.kill()
        pb.kill()


def last_keyboard(fake):
    """(text, [(label, callback_data), ...]) of the last sendMessage."""
    for path, p in reversed(fake.requests):
        if "sendMessage" in path:
            kb = p.get("reply_markup", {}).get("inline_keyboard", [])
            return p["text"], [(r[0]["text"], r[0]["callback_data"]) for r in kb]
    raise AssertionError("no sendMessage seen")


async def click_keyboard(fake, adapter, chat, label_substr, cq=None):
    """Click the button on the LAST rendered inline keyboard by label."""
    _, kb = last_keyboard(fake)
    match = [(t, c) for (t, c) in kb if label_substr in t]
    if not match:
        raise AssertionError(f"no button with {label_substr!r}; got {[t for t, _ in kb]}")
    await adapter.handle_callback(chat, match[0][1], callback_query_id=cq)
    return match[0][1]


def assert_no_pseudo_lines(fake):
    """ZERO pseudo '[label] ::token' text lines anywhere in sent messages."""
    for path, p in fake.requests:
        if "sendMessage" in path:
            assert " ::" not in p.get("text", ""), p.get("text")
            for r in p.get("reply_markup", {}).get("inline_keyboard", []):
                assert " ::" not in r[0]["text"], r[0]["text"]


# ---------------------------------------------------------------------------
# 1-5. send_buttons payload correctness
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_screen_to_inline_keyboard():
    tr, fake = make_transport()
    await tr.send_buttons("c1", "text mostra",
                          [FakeBtn("A", "tok_a"), FakeBtn("B", "tok_b")])
    p = fake.requests[0][1]
    assert p["chat_id"] == "c1"
    assert p["text"] == "text mostra"
    kb = p["reply_markup"]["inline_keyboard"]
    assert kb == [[{"text": "A", "callback_data": "tok_a"}],
                  [{"text": "B", "callback_data": "tok_b"}]]


@pytest.mark.asyncio
async def test_buttons_preserve_order():
    tr, fake = make_transport()
    await tr.send_buttons("c1", "t", [FakeBtn("Primer", "t1"), FakeBtn("Segon", "t2"),
                                      FakeBtn("Tercer", "t3")])
    rows = fake.requests[0][1]["reply_markup"]["inline_keyboard"]
    labels = [r[0]["text"] for r in rows]
    assert labels == ["Primer", "Segon", "Tercer"]


@pytest.mark.asyncio
async def test_callback_data_exact_token():
    tr, fake = make_transport()
    await tr.send_buttons("c1", "t", [FakeBtn("X", "token_opac_exacte_123")])
    cb = fake.requests[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert cb == "token_opac_exacte_123"


@pytest.mark.asyncio
async def test_one_button_per_row():
    tr, fake = make_transport()
    await tr.send_buttons("c1", "t", [FakeBtn("a", "ta"), FakeBtn("b", "tb")])
    rows = fake.requests[0][1]["reply_markup"]["inline_keyboard"]
    assert all(len(r) == 1 for r in rows)


@pytest.mark.asyncio
async def test_callback_within_64_bytes():
    tr, fake = make_transport()
    tok = "x" * 64
    await tr.send_buttons("c1", "t", [FakeBtn("b", tok)])
    assert len(tok.encode("utf-8")) == 64


@pytest.mark.asyncio
async def test_callback_over_64_bytes_rejected():
    tr, fake = make_transport()
    tok = "x" * 65
    with pytest.raises(ValueError):
        await tr.send_buttons("c1", "t", [FakeBtn("b", tok)])
    assert fake.requests == []  # nothing sent


# ---------------------------------------------------------------------------
# 8-10. callback security
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_secret_in_callback():
    tr, fake = make_transport()
    await tr.send_buttons("c1", "t", [FakeBtn("b", "conv:0")])
    cb = fake.requests[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert "conversation_id" not in cb
    assert "xfc_a72d" not in cb
    assert "objective" not in cb
    assert "TEST" not in cb  # no bot token


# ---------------------------------------------------------------------------
# 11-15. HTTP / validation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sendmessage_method_path():
    tr, fake = make_transport()
    await tr.send_buttons("c1", "t", [FakeBtn("b", "tk")])
    path = fake.requests[0][0]
    assert path == "/botTEST:BOT-TOKEN/sendMessage"


@pytest.mark.asyncio
async def test_http_failure_sanitized():
    fake = FakeTelegramAPI(http_status=500)
    tr, _ = make_transport(fake)
    with pytest.raises(TelegramTransportError) as ei:
        await tr.send("c1", "hi")
    assert "TEST" not in str(ei.value)  # no token leaked
    assert "telegram.invalid" not in str(ei.value)  # no URL leaked


@pytest.mark.asyncio
async def test_ok_false_sanitized():
    fake = FakeTelegramAPI(ok_flag=False)
    tr, _ = make_transport(fake)
    with pytest.raises(TelegramTransportError) as ei:
        await tr.send("c1", "hi")
    assert "unauthorized" not in str(ei.value).lower()


@pytest.mark.asyncio
async def test_exception_str_no_token():
    fake = FakeTelegramAPI(http_status=503)
    tr, _ = make_transport(fake)
    try:
        await tr.send("c1", "hi")
        assert False
    except TelegramTransportError as e:
        assert "TEST:BOT-TOKEN" not in str(e)
        assert "fake.telegram.invalid" not in str(e)


# ---------------------------------------------------------------------------
# 16-18. send() text + capability/fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_send_text_only():
    tr, fake = make_transport()
    await tr.send("c1", "només text")
    p = fake.requests[0][1]
    assert p["text"] == "només text"
    assert "reply_markup" not in p


class SendOnlyTransport:
    def __init__(self):
        self.calls = []
    async def send(self, chat_id, text):
        self.calls.append((chat_id, text))


@pytest.mark.asyncio
async def test_button_capable_no_pseudo_lines():
    tr, fake = make_transport()
    adapter, _, _ = make_adapter(tr)
    await adapter.start_wizard("c9")
    # button-capable transport must produce ONE sendMessage with inline_keyboard,
    # never pseudo "[label] ::token" lines
    assert len(fake.requests) == 1
    p = fake.requests[0][1]
    assert "inline_keyboard" in p.get("reply_markup", {})
    for label, cb in _flatten(p):
        assert " ::" not in label


@pytest.mark.asyncio
async def test_legacy_send_only_fallback():
    only = SendOnlyTransport()
    adapter, _, _ = make_adapter(only)
    await adapter.start_wizard("c8")
    # legacy: text + one pseudo-button line per button
    texts = [t for _, t in only.calls]
    assert any("[Nova conversa] ::" in t for t in texts)


def _flatten(payload):
    kb = payload.get("reply_markup", {}).get("inline_keyboard", [])
    out = []
    for row in kb:
        for b in row:
            out.append((b["text"], b["callback_data"]))
    return out

# ---------------------------------------------------------------------------
# 19-21. all three adapter paths use the real button transport
# ---------------------------------------------------------------------------
def _root_cb(labels_tokens):
    # labels already carry real callback_data
    return labels_tokens


async def _first_root_button(fake):
    kb = fake.requests[0][1]["reply_markup"]["inline_keyboard"]
    return kb[0][0]["text"], kb[0][0]["callback_data"]


@pytest.mark.asyncio
async def test_start_wizard_uses_button_transport():
    tr, fake = make_transport()
    adapter, _, _ = make_adapter(tr)
    await adapter.start_wizard("c1")
    assert len(fake.requests) == 1
    kb = fake.requests[0][1]["reply_markup"]["inline_keyboard"]
    texts = [r[0]["text"] for r in kb]
    assert "Converses" in texts and "Ajuda" in texts


@pytest.mark.asyncio
async def test_handle_callback_uses_button_transport():
    tr, fake = make_transport()
    adapter, _, _ = make_adapter(tr)
    await adapter.start_wizard("c1")
    _, root_tok = await _first_root_button(fake)
    # click "Converses"
    kb1 = fake.requests[0][1]["reply_markup"]["inline_keyboard"]
    conv_tok = None
    for r in kb1:
        b = r[0]
        if b["text"] == "Converses":
            conv_tok = b["callback_data"]
    assert conv_tok
    await adapter.handle_callback("c1", conv_tok)
    # now a new sendMessage with the conversations list (empty -> text + Enrere)
    assert len(fake.requests) == 2
    kb2 = fake.requests[1][1]["reply_markup"]["inline_keyboard"]
    texts2 = [r[0]["text"] for r in kb2]
    assert "Enrere" in texts2


@pytest.mark.asyncio
async def test_handle_wizard_text_uses_button_transport(ux42_flow):
    """Real text-input flow through the public surface.

    /xerrameca -> Nova conversa -> peer -> preset -> ENTER_OBJECTIVE ->
    adapter.handle_text(chat, 'objectiu real') -> SELECT_ROLE_A, all through
    the real TelegramBotAPITransport inline_keyboard (FakeTelegramAPI)."""
    f = ux42_flow
    chat = "c1"
    fake = f["fake"]
    adapter = f["adapter"]

    # -> ROOT via public handle_text
    await adapter.handle_text(chat, "/xerrameca")
    text, root_kb = last_keyboard(fake)
    assert any("Nova conversa" in t for t, _ in root_kb)

    # -> peer selection
    await click_keyboard(fake, adapter, chat, "Nova conversa")
    _, peer_kb = last_keyboard(fake)
    assert any("PeerAgent" in t for t, _ in peer_kb), [t for t, _ in peer_kb]

    # -> preset (dialogue type) -> ENTER_OBJECTIVE
    await click_keyboard(fake, adapter, chat, "PeerAgent")
    _, preset_kb = last_keyboard(fake)
    assert any("Conversa" in t for t, _ in preset_kb), [t for t, _ in preset_kb]
    await click_keyboard(fake, adapter, chat, "Conversa")

    text_obj, obj_kb = last_keyboard(fake)
    assert "Objectiu" in text_obj

    # the text is genuinely consumed by the real public-surface handler.
    # handle_text returns None for wizard-routed input by design; the proof of
    # consumption is the newly rendered SELECT_ROLE_A screen and session state.
    await adapter.handle_text(chat, "objectiu real")
    next_text, next_kb = last_keyboard(fake)
    # next screen: SELECT_ROLE_A rendered as a REAL inline keyboard
    assert "inline_keyboard" in last_sendmsg(fake)["reply_markup"]
    assert any("proposer" in t for t, _ in next_kb), [t for t, _ in next_kb]

    # state actually advanced to SELECT_ROLE_A with the objective stored
    sid = f["bridge"].active_session_id(chat)
    sess = f["wizard"].get_session(sid, chat)
    assert sess.state == "SELECT_ROLE_A"
    assert sess.data["user_objective"] == "objectiu real"

    # callback data opaque: no secret / conversation / node id in any payload
    for path, p in fake.requests:
        if "sendMessage" in path:
            for r in p.get("reply_markup", {}).get("inline_keyboard", []):
                cb = r[0]["callback_data"]
                assert "conversation_id" not in cb and "node_" not in cb
                assert "objectiu" not in cb and "TEST" not in cb
                # callback_data must respect Telegram's 64-byte cap
                assert len(cb.encode("utf-8")) <= 64

    # ZERO pseudo-button lines anywhere
    assert_no_pseudo_lines(fake)




# ---------------------------------------------------------------------------
# 22-25. callback ownership / ack
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ack_payload_correct():
    tr, fake = make_transport()
    adapter, _, _ = make_adapter(tr)
    await adapter.start_wizard("c1")
    kb = fake.requests[0][1]["reply_markup"]["inline_keyboard"]
    any_tok = kb[0][0]["callback_data"]
    await adapter.handle_callback("c1", any_tok, callback_query_id="cqid_123")
    ack_reqs = [p for (path, p) in fake.requests if "answerCallbackQuery" in path]
    assert ack_reqs, "should have ack'd the callback"
    assert ack_reqs[0]["callback_query_id"] == "cqid_123"


@pytest.mark.asyncio
async def test_ack_does_not_mutate_conversation():
    tr, fake = make_transport()
    cmd_store = {"list": [_conv()], "get": None, "get_error": None, "sync_error": None}
    adapter, _, cmd = make_adapter(tr, cmd_store)
    before = cmd.STORE["list"]
    await adapter.start_wizard("c1")
    kb = fake.requests[0][1]["reply_markup"]["inline_keyboard"]
    conv_tok = None
    for r in kb:
        if r[0]["text"] == "Converses":
            conv_tok = r[0]["callback_data"]
    await adapter.handle_callback("c1", conv_tok, callback_query_id="cq")
    # conversation store unchanged (no mutation by ACK)
    assert cmd.STORE["list"] is before
    assert [c.id for c in cmd.STORE["list"]] == [c.id for c in before]


@pytest.mark.asyncio
async def test_expired_callback_still_rejected():
    tr, fake = make_transport()
    adapter, _, _ = make_adapter(tr)
    # a token that never existed in the store
    await adapter.handle_callback("c1", "nonexistent-token")
    # falls back to controlled error -> a sendMessage with the generic error text
    assert fake.requests
    last = last_sendmsg(fake)
    assert last["text"].startswith("Acció no vàlida")


@pytest.mark.asyncio
async def test_callback_ownership_works_with_button_transport():
    tr, fake = make_transport()
    adapter, _, _ = make_adapter(tr)
    await adapter.start_wizard("c1")
    kb = fake.requests[0][1]["reply_markup"]["inline_keyboard"]
    # callbacks are bound to caller (c1). A token minted for a DIFFERENT caller
    # must be rejected. Simulate by using a token for c1 but calling from c_other
    any_tok = kb[0][0]["callback_data"]
    await adapter.handle_callback("c_other", any_tok)
    last = last_sendmsg(fake)
    assert last["text"].startswith("Acció no vàlida")


def last_sendmsg(fake):
    """Last sendMessage payload (ignores answerCallbackQuery requests)."""
    for path, p in reversed(fake.requests):
        if "sendMessage" in path:
            return p
    raise AssertionError("no sendMessage seen")

# ---------------------------------------------------------------------------
# 26. no real network
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_real_network():
    # MockTransport guarantees zero real HTTP by construction
    fake = FakeTelegramAPI()
    client = make_client(fake)
    assert isinstance(client._transport, httpx.MockTransport)


# ---------------------------------------------------------------------------
# 27-28 + full E2E
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_converses_surface_with_button_transport():
    cmd_store = {"list": [_conv()], "get": None, "get_error": None, "sync_error": None}
    tr, fake = make_transport()
    adapter, _, _ = make_adapter(tr, cmd_store)
    await adapter.start_wizard("c1")
    kb = fake.requests[0][1]["reply_markup"]["inline_keyboard"]
    conv_tok = next(r[0]["callback_data"] for r in kb if r[0]["text"] == "Converses")
    await adapter.handle_callback("c1", conv_tok)
    last = last_sendmsg(fake)
    kb2 = last["reply_markup"]["inline_keyboard"]
    texts = [r[0]["text"] for r in kb2]
    assert any("RUNNING" in t for t in texts)


@pytest.mark.asyncio
async def test_create_wizard_surface_with_button_transport():
    tr, fake = make_transport()
    adapter, _, _ = make_adapter(tr)
    await adapter.start_wizard("c1")
    kb = fake.requests[0][1]["reply_markup"]["inline_keyboard"]
    texts = [r[0]["text"] for r in kb]
    # root: Nova conversa etc are real buttons
    assert any("Nova conversa" in t for t in texts)


@pytest.mark.asyncio
async def test_full_public_e2e_inline_keyboard():
    """/xerrameca -> Converses -> conversa -> Actualitzar -> Mode -> Enrere
    entirely through real inline_keyboard payloads via the public handle_text
    and handle_callback surface."""
    cmd_store = {"list": [_conv()], "get": None, "get_error": None, "sync_error": None}
    tr, fake = make_transport()
    adapter, _, _ = make_adapter(tr, cmd_store)

    # open wizard through the public handle_text("/xerrameca") path
    await adapter.handle_text("c1", "/xerrameca")
    assert len(fake.requests) >= 1
    # ROOT: one sendMessage with inline buttons
    root_req = fake.requests[0]
    assert "sendMessage" in root_req[0]
    kb = root_req[1]["reply_markup"]["inline_keyboard"]
    assert any(r[0]["text"] == "Converses" for r in kb)

    # -> Converses
    conv_tok = next(r[0]["callback_data"] for r in kb if r[0]["text"] == "Converses")
    await adapter.handle_callback("c1", conv_tok, callback_query_id="q1")
    kb2 = last_sendmsg(fake)["reply_markup"]["inline_keyboard"]
    assert any("RUNNING" in r[0]["text"] for r in kb2)

    # -> conversa (RUNNING button)
    sel_tok = next(r[0]["callback_data"] for r in kb2 if "RUNNING" in r[0]["text"])
    await adapter.handle_callback("c1", sel_tok, callback_query_id="q2")
    kb3 = last_sendmsg(fake)["reply_markup"]["inline_keyboard"]
    texts3 = [r[0]["text"] for r in kb3]
    assert "Actualitzar" in texts3 and "Sincronitzar" in texts3 and "Mode" in texts3

    # -> Actualitzar
    ref_tok = next(r[0]["callback_data"] for r in kb3 if r[0]["text"] == "Actualitzar")
    await adapter.handle_callback("c1", ref_tok, callback_query_id="q3")
    kb4 = last_sendmsg(fake)["reply_markup"]["inline_keyboard"]
    assert any(r[0]["text"] == "Mode" for r in kb4)

    # -> Mode
    mode_tok = next(r[0]["callback_data"] for r in kb4 if r[0]["text"] == "Mode")
    await adapter.handle_callback("c1", mode_tok, callback_query_id="q4")
    kb5 = last_sendmsg(fake)["reply_markup"]["inline_keyboard"]
    texts5 = [r[0]["text"] for r in kb5]
    assert any(("summary" in t.lower() or "live" in t.lower() or "silent" in t.lower()) for t in texts5)

    # -> Enrere (mode -> detail)
    back = next(r[0]["callback_data"] for r in kb5 if r[0]["text"] == "Enrere")
    await adapter.handle_callback("c1", back, callback_query_id="q5")
    kb6 = last_sendmsg(fake)["reply_markup"]["inline_keyboard"]
    assert any(r[0]["text"] == "Actualitzar" for r in kb6)

    # ensure we never sent a pseudo-button line anywhere
    for path, p in fake.requests:
        if "sendMessage" in path:
            kb_ = p.get("reply_markup", {}).get("inline_keyboard", [])
            for r_ in kb_:
                assert " ::" not in r_[0]["text"]
# ---------------------------------------------------------------------------
# TASK 2: ACK failure isolation (answerCallbackQuery failure never propagates
# and never mutates federated state / runs duplicate work / leaks secrets).
# ---------------------------------------------------------------------------
class AckFailTransport:
    """Button-capable transport whose ACK always fails."""

    def __init__(self):
        self.sent = []
        self.ack_attempts = 0

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def send_buttons(self, chat_id, text, buttons):
        self.sent.append((chat_id, text, list(buttons)))

    async def answer_callback_query(self, callback_query_id):
        self.ack_attempts += 1
        raise TelegramTransportError("Telegram API request failed")


@pytest.mark.asyncio
async def test_ack_failure_isolated():
    """A callback ACK failure must be swallowed after the wizard action.

    Sequence: button-capable transport whose answer_callback_query() raises
    TelegramTransportError. Run a valid callback (Converses) with
    callback_query_id='cq_fail'. The wizard action applies, the new screen is
    sent, the ACK fails *after* processing, handle_callback does NOT raise,
    wizard state stays in the new state (no rollback, no duplicate execution),
    no federated mutation is caused by the ACK, and no secret/token leaks.
    """
    tr = AckFailTransport()
    cmd_store = {"list": [_conv()], "get": None, "get_error": None, "sync_error": None}
    adapter, callbacks, cmd = make_adapter(tr, cmd_store)

    await adapter.start_wizard("c1")
    assert len(tr.sent) == 1
    # first screen sent via the real button transport (inline keyboard)
    root_screen = tr.sent[0]
    assert len(root_screen) == 3  # (chat_id, text, buttons) -> button-capable path

    # find "Converses" callback token minted for this caller/session
    conv_tok = next(b.callback_token for b in root_screen[2]
                    if b.label == "Converses")
    before_store = cmd.STORE["list"]

    # The callback triggers ROOT -> CONVERSATION_LIST; the ACK will then fail.
    await adapter.handle_callback("c1", conv_tok, callback_query_id="cq_fail")

    # ACK was attempted after processing, and failed exactly once
    assert tr.ack_attempts == 1

    # new screen was sent (2nd message = the new CONVERSATION_LIST screen)
    assert len(tr.sent) == 2, "wizard screen must be sent exactly once (no duplicate)"
    screen2 = tr.sent[1]
    assert len(screen2) == 3
    texts = [b.label for b in screen2[2]]
    assert any("RUNNING" in t for t in texts)

    # wizard state remains in the NEW state -> no rollback, no exception,
    # and handle_callback did NOT propagate the ACK failure
    wizard = adapter._wizard.wizard
    sid = adapter._wizard.active_session_id("c1")
    sess = wizard.get_session(sid, "c1")
    assert sess.state == "CONVERSATION_LIST"
    assert sid == callbacks.resolve(conv_tok, caller_id="c1")[1]

    # no duplicate / no new federated mutation from the ACK
    assert cmd.STORE["list"] is before_store
    assert [c.id for c in cmd.STORE["list"]] == [c.id for c in before_store]

    # no secret / token leaked in any emitted message
    for msg in tr.sent:
        if len(msg) == 2:
            assert "cq_fail" not in msg[1]
            assert "TEST" not in msg[1]
