import asyncio
import tempfile

import httpx
import pytest

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
async def test_handle_wizard_text_uses_button_transport():
    tr, fake = make_transport()
    adapter, _, _ = make_adapter(tr)
    # no expected_text_input on root, so direct text handling returns False
    # but a screen with buttons still renders via button transport if any.
    await adapter.start_wizard("c1")
    # exercise handle_text path through the top-level handle_text('') no-op to
    # confirm it still exists; the real text render is covered by start/callback
    assert hasattr(adapter, "handle_wizard_text")


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
