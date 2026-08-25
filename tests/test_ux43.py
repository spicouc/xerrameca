"""UX-4.3 — Telegram Update ingestion + TelegramUpdateDispatcher + simulated E2E.

Tests the INPUT boundary: raw Telegram Update JSON -> TelegramUpdateDispatcher
-> TelegramUXAdapter -> Wizard -> TelegramBotAPITransport (inline keyboard),
via :meth:`TelegramUpdateDispatcher.dispatch`. No Telegram SDK, no polling, no
webhook, no real Telegram network (always httpx.MockTransport).
"""

import asyncio
import inspect
import tempfile
from pathlib import Path

import httpx
import pytest

from xerrameca.command.dto import AgentChoice, ConversationSummary
from xerrameca.command.service import XerramecaCommandService
from xerrameca.command.wizard import XerramecaWizardService
from xerrameca.integrations.telegram import TelegramUXAdapter
from xerrameca.integrations.telegram_bot_api import (
    TelegramBotAPITransport,
    TelegramTransportError,
)
from xerrameca.integrations.telegram_updates import (
    DispatchResult,
    TelegramUpdateDispatcher,
)
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
        blob = {"ok": True, "result": {}}
        return httpx.Response(self.http_status, json=blob)


def make_transport(fake=None):
    fake = fake or FakeTelegramAPI()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return (
        TelegramBotAPITransport(
            token="TEST:BOT-TOKEN", client=client, api_base="https://fake.telegram.invalid"
        ),
        fake,
    )


class FakeCommandService:
    """Injected command service (list/agents/convos) for the wizard DI."""

    def __init__(self):
        self.agents: list[AgentChoice] = []
        self.convos: list[ConversationSummary] = []
        self.create_count = 0
        self.last_created: dict = {}

    def list_conversations(self, **kw):
        return list(self.convos)

    def list_agents(self, **kw):
        return list(self.agents)

    def get_conversation(self, cid):
        return {
            "id": cid, "status": "RUNNING", "current_round": 3, "max_rounds": 6,
            "participants": ["peer1"],
        }

    def sync_conversation(self, cid):
        return {"ok": True}

    def create_conversation(self, peer_node_id, objective, max_rounds=5,
                            delay_seconds=0):
        self.create_count += 1
        cid = f"xfc_ux43_{self.create_count}"
        self.convos.append(
            ConversationSummary(
                id=cid, objective=objective, status="RUNNING", coordinator_id="c",
                coordinator_epoch=0, current_round=1, max_rounds=max_rounds,
                participants=[peer_node_id],
            )
        )
        self.last_created = {"id": cid}
        return {"id": cid}


def _make_real_stack(transport):
    """Adapter + wizard + bridge + dispatcher with a *real* wizard.

    Returns (dispatcher, adapter, bridge, wizard, callbacks, fake_cmd).
    Uses an injected FakeCommandService so no real node is contacted for the
    state-dir-only wizard surface.
    """
    sd = tempfile.mkdtemp(prefix="ux43_")
    fake_cmd = FakeCommandService()
    wizard = XerramecaWizardService(sd, ttl_seconds=600, node_port=8891, command_service=fake_cmd)
    callbacks = CallbackStore()
    bridge = TelegramWizardBridge(wizard, callbacks)
    adapter = TelegramUXAdapter(
        node_base_url="http://127.0.0.1:9", api_key="TEST:APIKEY",
        transport=transport, wizard=bridge,
    )
    dispatcher = TelegramUpdateDispatcher(adapter)
    return dispatcher, adapter, bridge, wizard, callbacks, fake_cmd


def _patch_command(monkeypatch, fake_cmd):
    """Route the wizard's *direct* XerramecaCommandService calls to fake_cmd.

    The wizard calls `XerramecaCommandService(self.state_dir)` directly for
    list_agents (peer select) and create_conversation (START), bypassing the
    injected service. We monkeypatch those two class methods per-test.
    """
    def _list_agents(self, **kw):
        return list(fake_cmd.agents)

    def _create_conversation(self, *, peer_node_id, objective, max_rounds=5, delay_seconds=0):
        fake_cmd.create_count += 1
        cid = f"xfc_ux43_{fake_cmd.create_count}"
        fake_cmd.convos.append(
            ConversationSummary(
                id=cid, objective=objective, status="RUNNING", coordinator_id="c",
                coordinator_epoch=0, current_round=1, max_rounds=max_rounds,
                participants=[peer_node_id],
            )
        )
        fake_cmd.last_created = {"id": cid}
        return {"id": cid}

    monkeypatch.setattr(XerramecaCommandService, "list_agents", _list_agents)
    monkeypatch.setattr(XerramecaCommandService, "create_conversation", _create_conversation)


# --- Update builders -------------------------------------------------------
def msg_update(update_id, chat_id, text):
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": int(chat_id)}, "text": text},
    }


def cb_update(update_id, chat_id, data, callback_query_id=None):
    cq = callback_query_id if callback_query_id is not None else f"cq_{update_id}"
    return {
        "update_id": update_id,
        "callback_query": {
            "id": cq, "data": data,
            "message": {"message_id": 1, "chat": {"id": int(chat_id)}},
        },
    }


def last_keyboard(fake):
    """(text, [(label, callback_data), ...]) of the last sendMessage."""
    for path, p in reversed(fake.requests):
        if "sendMessage" in path:
            kb = p.get("reply_markup", {}).get("inline_keyboard", [])
            return p["text"], [(r[0]["text"], r[0]["callback_data"]) for r in kb]
    raise AssertionError("no sendMessage seen")


def sendmsg_count(fake):
    return sum(1 for path, _ in fake.requests if "sendMessage" in path)


def ack_count(fake):
    return sum(1 for path, _ in fake.requests if "answerCallbackQuery" in path)


async def click(dispatcher, fake, chat, label_substr):
    """Click the last rendered keyboard button by label, via dispatch()."""
    _, kb = last_keyboard(fake)
    match = [(t, c) for (t, c) in kb if label_substr in t]
    if not match:
        raise AssertionError(
            f"no button {label_substr!r}; got {[t for t, _ in kb]}"
        )
    return match[0][1]  # the opaque callback_data token


# ---------------------------------------------------------------------------
# Spy adapter: isolates the dispatcher's routing/parsing from the wizard.
# ---------------------------------------------------------------------------
class SpyAdapter:
    def __init__(self, raises_text=False, raises_cb=False, raises_ack=False):
        self.text_calls: list[tuple[str, str]] = []
        self.cb_calls: list[tuple[str, str, str | None]] = []
        self.acks: list[str | None] = []
        self.raises_text = raises_text
        self.raises_cb = raises_cb
        self.raises_ack = raises_ack

    async def handle_text(self, chat_id, text):
        if self.raises_text:
            raise RuntimeError("boom-text")
        self.text_calls.append((chat_id, text))

    async def handle_callback(self, chat_id, data, callback_query_id=None):
        if self.raises_cb:
            raise RuntimeError("boom-cb")
        self.cb_calls.append((chat_id, data, callback_query_id))

    async def safe_ack(self, callback_query_id):
        if self.raises_ack:
            raise RuntimeError("boom-ack")
        self.acks.append(callback_query_id)


def _spy_dispatcher(spy=None, allowed=None):
    spy = spy or SpyAdapter()
    return TelegramUpdateDispatcher(spy, allowed_chat_ids=allowed), spy


# ===========================================================================
# 1-6. Routing / parsing
# ===========================================================================
@pytest.mark.asyncio
async def test_message_command_routes_to_handle_text():
    disp, spy = _spy_dispatcher()
    r = await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    assert r.kind == "handled"
    assert spy.text_calls == [("100", "/xerrameca")]


@pytest.mark.asyncio
async def test_chat_id_converted_to_str():
    disp, spy = _spy_dispatcher()
    await disp.dispatch(msg_update(2, 4242, "hola"))
    assert spy.text_calls[0][0] == "4242"


@pytest.mark.asyncio
async def test_callback_query_routes_to_handle_callback():
    disp, spy = _spy_dispatcher()
    r = await disp.dispatch(cb_update(5, "100", "opaque_token_123"))
    assert r.kind == "handled"
    assert spy.cb_calls == [("100", "opaque_token_123", "cq_5")]


@pytest.mark.asyncio
async def test_callback_query_id_reaches_adapter():
    disp, spy = _spy_dispatcher()
    await disp.dispatch(cb_update(6, "100", "dataX", callback_query_id="cq_custom"))
    assert spy.cb_calls[0][2] == "cq_custom"


@pytest.mark.asyncio
async def test_callback_data_exact_no_reinterpretation():
    disp, spy = _spy_dispatcher()
    raw = "conv:3::xfc_a72d000000000000::§secret§"
    await disp.dispatch(cb_update(7, "100", raw))
    assert spy.cb_calls[0][1] == raw


@pytest.mark.asyncio
async def test_unsupported_type_ignored():
    disp, spy = _spy_dispatcher()
    r = await disp.dispatch(
        {"update_id": 8, "edited_message": {"chat": {"id": 100}, "text": "x"}}
    )
    assert r.kind == "ignored"
    assert spy.text_calls == [] and spy.cb_calls == []


@pytest.mark.asyncio
async def test_message_no_text_ignored():
    disp, spy = _spy_dispatcher()
    r = await disp.dispatch(
        {"update_id": 9, "message": {"message_id": 1, "chat": {"id": 100}, "sticker": {}}}
    )
    assert r.kind == "ignored"
    assert spy.text_calls == []


@pytest.mark.asyncio
async def test_poll_message_ignored():
    disp, spy = _spy_dispatcher()
    r = await disp.dispatch(
        {"update_id": 10, "message": {"message_id": 1, "chat": {"id": 100},
                                      "poll": {"id": "p1"}}}
    )
    assert r.kind == "ignored"


@pytest.mark.asyncio
async def test_callback_without_data_ignored():
    disp, spy = _spy_dispatcher()
    r = await disp.dispatch(
        {"update_id": 11, "callback_query": {
            "id": "cq", "message": {"chat": {"id": 100}}}}
    )
    assert r.kind == "ignored"
    assert spy.cb_calls == []


@pytest.mark.asyncio
async def test_callback_without_chat_ignored():
    disp, spy = _spy_dispatcher()
    r = await disp.dispatch(
        {"update_id": 12, "callback_query": {"id": "cq", "data": "tok"}}
    )
    assert r.kind == "ignored"
    assert spy.cb_calls == []


@pytest.mark.asyncio
async def test_missing_update_id_ignored():
    disp, spy = _spy_dispatcher()
    r = await disp.dispatch({"message": {"chat": {"id": 100}, "text": "/xerrameca"}})
    assert r.kind == "ignored"
    assert spy.text_calls == []


@pytest.mark.asyncio
async def test_malformed_update_no_exception():
    disp, spy = _spy_dispatcher()
    for bad in (None, [], "nope", 42, {"update_id": {} , "message": 5},
                {"update_id": "not-an-int", "message": {"chat": {"id": 1}, "text": "x"}}):
        r = await disp.dispatch(bad)
        assert isinstance(r, DispatchResult), repr(bad)
    assert "ignored" in {  # all malformed land in controlled ignored
        r.kind for r in [
            await disp.dispatch({"update_id": None, "message": {"chat": {"id": 1}, "text": "x"}}),
            await disp.dispatch({"update_id": "abc", "message": {"chat": {"id": 1}, "text": "x"}}),
        ]
    }


# ===========================================================================
# 7-10. ACK hardening (real adapter over transports)
# ===========================================================================
@pytest.mark.asyncio
async def test_valid_callback_acked_exactly_once():
    tr, fake = make_transport()
    disp, adapter, *_ = _make_real_stack(tr)
    await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    tok = await click(disp, fake, "100", "Converses")
    before = ack_count(fake)
    r = await disp.dispatch(cb_update(2, "100", tok, callback_query_id="cq_v"))
    assert r.kind == "handled"
    assert ack_count(fake) == before + 1


@pytest.mark.asyncio
async def test_invalid_callback_safely_acked():
    tr, fake = make_transport()
    disp, adapter, *_ = _make_real_stack(tr)
    before = ack_count(fake)
    r = await disp.dispatch(cb_update(3, "100", "no-such-token", callback_query_id="cq_inv"))
    assert r.kind == "handled"  # adapter controlled the error
    assert ack_count(fake) == before + 1


@pytest.mark.asyncio
async def test_expired_callback_safely_acked():
    tr, fake = make_transport()
    disp, adapter, bridge, wizard, callbacks, _ = _make_real_stack(tr)
    await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    tok = await click(disp, fake, "100", "Converses")
    # Force the token to be invalidated (expired session) before use.
    callbacks.invalidate_session(bridge.active_session_id("100"))
    before = ack_count(fake)
    r = await disp.dispatch(cb_update(2, "100", tok, callback_query_id="cq_exp"))
    assert r.kind == "handled"
    assert ack_count(fake) == before + 1


@pytest.mark.asyncio
async def test_ack_failure_swallowed_not_authoritative():
    tr = RecordingAckFailTransport()
    disp, adapter, bridge, wizard, callbacks, _ = _make_real_stack(tr)
    await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    tok = await click_rec(disp, tr, "100", "Converses")
    # click_rec already performed one valid dispatch; its ACK failed and was
    # swallowed (non-authoritative), and the wizard advanced.
    assert tr.ack_failures == 1
    sid = bridge.active_session_id("100")
    assert wizard.get_session(sid, "100").state == "CONVERSATION_LIST"
    # A second, fresh callback (new update_id) also applies and its ACK failure
    # is again contained — never propagates, never mutates federated state.
    r = await disp.dispatch(cb_update(2, "100", tok, callback_query_id="cq_f"))
    assert r.kind == "handled"
    assert not r.reason
    assert tr.ack_failures == 2
    assert wizard.get_session(sid, "100").state == "CONVERSATION_LIST"


class RecordingAckFailTransport:
    def __init__(self):
        self.screens = []
        self.ack_failures = 0
        self.ack_ids = []

    async def send(self, chat_id, text):
        self.screens.append((chat_id, text))

    async def send_buttons(self, chat_id, text, buttons):
        self.screens.append((chat_id, text, list(buttons)))

    async def answer_callback_query(self, callback_query_id):
        self.ack_failures += 1
        self.ack_ids.append(callback_query_id)
        raise TelegramTransportError("Telegram API request failed")


async def click_rec(dispatcher, tr, chat, label_substr):
    screen = tr.screens[-1]
    buttons = screen[2] if len(screen) == 3 else []
    for b in buttons:
        if label_substr in b.label:
            await dispatcher.dispatch(cb_update(9, chat, b.callback_token))
            return b.callback_token
    raise AssertionError([getattr(b, "label", b) for b in buttons])


# ===========================================================================
# 15-20. update_id idempotency / dedup
# ===========================================================================
@pytest.mark.asyncio
async def test_duplicate_message_update_single_execution():
    disp, spy = _spy_dispatcher()
    await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    r = await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    assert r.kind == "duplicate"
    assert len(spy.text_calls) == 1


@pytest.mark.asyncio
async def test_duplicate_callback_single_wizard_action():
    disp, spy = _spy_dispatcher()
    r1 = await disp.dispatch(cb_update(7, "100", "tok", callback_query_id="cq7"))
    r2 = await disp.dispatch(cb_update(7, "100", "tok", callback_query_id="cq7"))
    assert r1.kind == "handled"
    assert r2.kind == "duplicate"
    assert spy.cb_calls == [("100", "tok", "cq7")]


@pytest.mark.asyncio
async def test_duplicate_callback_seen_before_execution():
    disp, spy = _spy_dispatcher()

    async def one():
        return await disp.dispatch(cb_update(7, "100", "tok", callback_query_id="cq7"))

    r1, r2 = await asyncio.gather(one(), one())
    kinds = sorted([r1.kind, r2.kind])
    assert kinds == ["duplicate", "handled"]
    assert len(spy.cb_calls) == 1


@pytest.mark.asyncio
async def test_duplicate_callback_safe_ack_no_wizard_rerun(monkeypatch):
    tr, fake = make_transport()
    disp, adapter, bridge, wizard, callbacks, _ = _make_real_stack(tr)
    await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    tok = await click(disp, fake, "100", "Converses")
    upd = cb_update(2, "100", tok, callback_query_id="cq_d")
    before = ack_count(fake)
    m_root = sendmsg_count(fake)
    r1 = await disp.dispatch(upd)
    assert r1.kind == "handled" and ack_count(fake) == before + 1
    m_after_r1 = sendmsg_count(fake)
    # first dispatch legitimately rendered CONVERSATION_LIST (+1 sendMessage)
    assert m_after_r1 == m_root + 1
    r2 = await disp.dispatch(upd)
    assert r2.kind == "duplicate"
    assert ack_count(fake) == before + 2  # safe ACK on duplicate
    assert sendmsg_count(fake) == m_after_r1  # no re-render / no re-execution


@pytest.mark.asyncio
async def test_dedup_cache_bounded():
    spy = SpyAdapter()
    disp = TelegramUpdateDispatcher(spy, max_seen_updates=3)
    for i in (10, 11, 12):  # ids 10,11,12 in window
        assert (await disp.dispatch(msg_update(i, "100", "x"))).handled
    # 10 still in window -> duplicate
    assert (await disp.dispatch(msg_update(10, "100", "x"))).kind == "duplicate"
    # pushing 20,21 evicts 10 (bound = 3)
    await disp.dispatch(msg_update(20, "100", "x"))
    await disp.dispatch(msg_update(21, "100", "x"))
    assert len(disp._seen) == 3 and len(disp._seen_set) == 3
    # 10 evicted -> handled again (re-executed)
    assert (await disp.dispatch(msg_update(10, "100", "x"))).kind == "handled"


@pytest.mark.asyncio
async def test_dedup_cache_no_persistence():
    spy = SpyAdapter()
    disp1 = TelegramUpdateDispatcher(spy)
    assert (await disp1.dispatch(msg_update(5, "100", "x"))).handled
    assert (await disp1.dispatch(msg_update(5, "100", "x"))).kind == "duplicate"
    # A brand-new dispatcher instance shares NO cache with disp1.
    spy2 = SpyAdapter()
    disp2 = TelegramUpdateDispatcher(spy2)
    assert (await disp2.dispatch(msg_update(5, "100", "x"))).handled


@pytest.mark.asyncio
async def test_concurrent_duplicate_dispatch_single_execution():
    disp, spy = _spy_dispatcher()
    upd = msg_update(99, "100", "/xerrameca")
    await asyncio.gather(disp.dispatch(upd), disp.dispatch(upd))
    assert len(spy.text_calls) == 1


# ===========================================================================
# 21-24. allowlist
# ===========================================================================
@pytest.mark.asyncio
async def test_allowed_chat_passes():
    disp, spy = _spy_dispatcher(allowed={"100"})
    r = await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    assert r.kind == "handled"
    assert spy.text_calls == [("100", "/xerrameca")]


@pytest.mark.asyncio
async def test_denied_message_no_mutation():
    disp, spy = _spy_dispatcher(allowed={"100"})
    r = await disp.dispatch(msg_update(2, "999", "/xerrameca"))
    assert r.kind == "rejected"
    assert spy.text_calls == []


@pytest.mark.asyncio
async def test_denied_callback_no_mutation():
    disp, spy = _spy_dispatcher(allowed={"100"})
    r = await disp.dispatch(cb_update(3, "999", "tok", callback_query_id="cq_d"))
    assert r.kind == "rejected"
    assert spy.cb_calls == []


@pytest.mark.asyncio
async def test_denied_callback_safe_ack():
    disp, spy = _spy_dispatcher(allowed={"100"})
    r = await disp.dispatch(cb_update(4, "999", "tok", callback_query_id="cq_den"))
    assert r.kind == "rejected"
    assert spy.acks == ["cq_den"]  # spinner dismissed, no wizard work


@pytest.mark.asyncio
async def test_allowlist_no_filter_when_none():
    disp, spy = _spy_dispatcher()  # allowed=None
    assert (await disp.dispatch(msg_update(1, "123", "x"))).handled


# ===========================================================================
# 25-28. isolation / secrets / error boundary
# ===========================================================================
@pytest.mark.asyncio
async def test_caller_isolation_preserved():
    tr, fake = make_transport()
    disp, adapter, bridge, wizard, callbacks, _ = _make_real_stack(tr)
    await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    # Capture chat 100's own "Converses" token BEFORE any other chat renders.
    a_tok = await click(disp, fake, "100", "Converses")
    # give chat 200 its own session; the last rendered keyboard is now 200's
    await disp.dispatch(msg_update(2, "200", "/xerrameca"))
    assert bridge.active_session_id("100") != bridge.active_session_id("200")
    # chat 100's token used from chat 200 -> rejected (ownership), not advanced
    r = await disp.dispatch(cb_update(3, "200", a_tok, callback_query_id="cq_iso"))
    assert r.kind == "handled"  # dispatcher routed; adapter produced controlled error
    last = last_keyboard(fake)[0]
    assert "Acció no vàlida" in last


@pytest.mark.asyncio
async def test_callback_token_stays_opaque():
    tr, fake = make_transport()
    disp, adapter, *_ = _make_real_stack(tr)
    await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    _, kb = last_keyboard(fake)
    for _label, cb in kb:
        assert "conversation_id" not in cb
        assert "xfc_a72d" not in cb
        assert "node_" not in cb
        assert "objectiu" not in cb.lower()
        assert "TEST" not in cb


@pytest.mark.asyncio
async def test_no_secret_in_dispatch_result():
    disp, spy = _spy_dispatcher(allowed={"100"})
    res = await disp.dispatch(msg_update(1, "200", "objectiu secret"))
    assert res.kind == "rejected"
    s = f"{res.kind}|{res.update_id}|{res.reason}"
    assert "TEST:BOT-TOKEN" not in s
    assert "TEST:APIKEY" not in s
    assert "objectiu secret" not in s
    assert "/opt" not in s and "/root" not in s

    res2 = await disp.dispatch(msg_update(2, "100", "/xerrameca"))
    assert res2.reason is None
    assert res2.handled


@pytest.mark.asyncio
async def test_unexpected_adapter_exception_contained():
    spy = SpyAdapter(raises_text=True, raises_cb=True)
    disp = TelegramUpdateDispatcher(spy)
    r = await disp.dispatch(msg_update(1, "100", "/xerrameca"))
    assert r.kind == "handled"  # controlled return; no raw exception escapes
    assert r.reason is None
    r2 = await disp.dispatch(cb_update(2, "100", "tok"))
    assert r2.kind == "handled"


@pytest.mark.asyncio
async def test_no_raw_update_in_result_or_exception():
    disp, spy = _spy_dispatcher()
    try:
        await disp.dispatch({"update_id": 1, "message": {"chat": {"id": 100},
                                                        "text": "raw-payload"}})
        # also invalid payload catches nothing; ensure reason strings stay generic
        dupe = await disp.dispatch({"update_id": 1, "message": {"chat": {"id": 100},
                                                                "text": "raw-payload"}})
        assert dupe.reason == "duplicate update_id"
        # reason never embeds the raw update
        assert "raw-payload" not in str(dupe.reason)
    except Exception as exc:  # pragma: no cover
        assert "raw-payload" not in str(exc)
        raise


# ===========================================================================
# 29-30. no Telegram SDK / no real network
# ===========================================================================
def _module_text():
    mod = __import__("xerrameca.integrations.telegram_updates", fromlist=["*"])
    return inspect.getsource(mod)


def test_no_telegram_sdk_imports():
    src = _module_text()
    for banned in ("python-telegram-bot", "telegram.ext", "aiogram", "telebot"):
        assert banned not in src
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # Only *absolute* imports of the external SDK are banned. A relative
            # import (`from .telegram import ...`) is Xerrameca's own
            # integrations.telegram adapter and is legitimate.
            if node.level == 0 and node.module in ("telegram", "aiogram", "telebot"):
                raise AssertionError(f"forbidden external import from {node.module}")
        if isinstance(node, ast.Import):
            # Absolute bare `import telegram` / `import aiogram` are banned too.
            for alias in node.names:
                if alias.name in ("telegram", "aiogram", "telebot"):
                    raise AssertionError(f"forbidden external import {alias.name}")


def test_no_polling_or_webhook_in_module():
    src = _module_text()
    for banned in ("while True", "getUpdates", "setWebhook", "deleteWebhook",
                   "aiohttp", "FastAPI", "sleep", "num=asyncio"):
        assert banned not in src
    # the only network-ish thing allowed: nothing. Dispatcher parses only.
    assert "httpx" not in src
    assert "api.telegram.org" not in src


def test_no_telegram_sdk_in_adapter_source():
    import xerrameca.integrations.telegram as _t
    src = open(_t.__file__).read()
    for banned in ("python-telegram-bot", "aiogram", "telebot", "from telegram", "import telegram"):
        assert banned not in src


def test_real_telegram_network_never_used():
    # Every transport used in this suite is httpx.MockTransport by construction.
    fake = FakeTelegramAPI()
    tr, _ = make_transport(fake)
    assert isinstance(tr._client._transport, httpx.MockTransport)


# ===========================================================================
# Full simulated E2E via dispatch() only (item 18)
# ===========================================================================
@pytest.mark.asyncio
async def test_e2e_full_simulated(monkeypatch):
    tr, fake = make_transport()
    disp, adapter, bridge, wizard, callbacks, fake_cmd = _make_real_stack(tr)
    fake_cmd.agents.append(
        AgentChoice(node_id="peer1", display_name="PeerAgent",
                    endpoint="http://127.0.0.1:9", trusted=True)
    )
    _patch_command(monkeypatch, fake_cmd)
    chat = "100"
    uid = [0]

    def nid():
        uid[0] += 1
        return uid[0]

    # /xerrameca -> ROOT (inline keyboard)
    assert (await disp.dispatch(msg_update(nid(), chat, "/xerrameca"))).handled
    _, root_kb = last_keyboard(fake)
    assert any("Nova conversa" in t for t, _ in root_kb)
    assert sendmsg_count(fake) == 1  # ROOT = ONE sendMessage (real InlineKeyboard)

    # Nova conversa -> SELECT_PEER
    tok = await click(disp, fake, chat, "Nova conversa")
    r = await disp.dispatch(cb_update(nid(), chat, tok))
    assert r.handled
    _, peer_kb = last_keyboard(fake)
    assert any("PeerAgent" in t for t, _ in peer_kb)

    # peer -> preset
    tok = await click(disp, fake, chat, "PeerAgent")
    await disp.dispatch(cb_update(nid(), chat, tok))
    _, preset_kb = last_keyboard(fake)
    assert any("Conversa" in t for t, _ in preset_kb)

    # preset -> ENTER_OBJECTIVE
    tok = await click(disp, fake, chat, "Conversa")
    await disp.dispatch(cb_update(nid(), chat, tok))
    text_obj, _ = last_keyboard(fake)
    assert "Objectiu" in text_obj

    # message objective -> SELECT_ROLE_A (real consumption)
    await disp.dispatch(msg_update(nid(), chat, "objectiu de prova"))
    sid = bridge.active_session_id(chat)
    sess = wizard.get_session(sid, chat)
    assert sess.state == "SELECT_ROLE_A"
    assert sess.data["user_objective"] == "objectiu de prova"
    _, roleA_kb = last_keyboard(fake)
    assert any("proposer" in t for t, _ in roleA_kb)

    # role A -> SELECT_ROLE_B ; role B -> SELECT_ROUNDS
    tok = await click(disp, fake, chat, "proposer")
    await disp.dispatch(cb_update(nid(), chat, tok))
    _, roleB_kb = last_keyboard(fake)
    assert any("reviewer" in t for t, _ in roleB_kb)
    tok = await click(disp, fake, chat, "reviewer")
    await disp.dispatch(cb_update(nid(), chat, tok))
    _, rounds_kb = last_keyboard(fake)

    # rounds -> SELECT_OUTPUT_MODE ; output -> CONFIRM
    tok = await click(disp, fake, chat, "5")
    await disp.dispatch(cb_update(nid(), chat, tok))
    _, output_kb = last_keyboard(fake)
    assert any("summary" in t for t, _ in output_kb)
    tok = await click(disp, fake, chat, "summary")
    await disp.dispatch(cb_update(nid(), chat, tok))
    text_confirm, confirm_kb = last_keyboard(fake)
    assert "INICIAR" in [t for t, _ in confirm_kb]

    # START -> conversation created EXACTLY ONCE
    start_tok = [c for t, c in confirm_kb if t == "INICIAR"][0]
    start_update = cb_update(nid(), chat, start_tok, callback_query_id="cq_start")
    r = await disp.dispatch(start_update)
    assert r.handled
    assert fake_cmd.create_count == 1
    assert wizard.get_session(sid, chat).state == "STARTED"

    # new /xerrameca -> new ROOT
    await disp.dispatch(msg_update(nid(), chat, "/xerrameca"))
    _, root2_kb = last_keyboard(fake)
    assert any("Nova conversa" in t for t, _ in root2_kb)

    # Converses -> conversation visible -> detail -> refresh/mode/back
    tok = await click(disp, fake, chat, "Converses")
    await disp.dispatch(cb_update(nid(), chat, tok))
    _, conv_list_kb = last_keyboard(fake)
    assert any("RUNNING" in t for t, _ in conv_list_kb)  # created conv visible
    tok = await click(disp, fake, chat, "RUNNING")
    await disp.dispatch(cb_update(nid(), chat, tok))
    _, detail_kb = last_keyboard(fake)
    assert any("Actualitzar" in t for t, _ in detail_kb)
    tok = await click(disp, fake, chat, "Actualitzar")
    await disp.dispatch(cb_update(nid(), chat, tok))
    assert any("Actualitzar" in t for t, _ in last_keyboard(fake)[1])
    tok = await click(disp, fake, chat, "Mode")
    await disp.dispatch(cb_update(nid(), chat, tok))
    _, mode_kb = last_keyboard(fake)
    assert any("summary" in t.lower() for t, _ in mode_kb)
    tok = await click(disp, fake, chat, "Enrere")
    await disp.dispatch(cb_update(nid(), chat, tok))
    assert any("Actualitzar" in t for t, _ in last_keyboard(fake)[1])

    # zero pseudo-button lines anywhere
    for path, p in fake.requests:
        if "sendMessage" in path:
            assert " ::" not in p.get("text", "")
    # no secrets in any callback_data
    for path, p in fake.requests:
        if "sendMessage" in path:
            for r_ in p.get("reply_markup", {}).get("inline_keyboard", []):
                cb = r_[0]["callback_data"]
                assert "conversation_id" not in cb and "node_" not in cb
                assert "TEST" not in cb
                assert len(cb.encode("utf-8")) <= 64


# ===========================================================================
# Duplicate / concurrent START safety through dispatch()
# ===========================================================================
async def _drive_to_confirm(disp, fake, chat, *, start_uid=1000):
    """Drive dispatch() to the CONFIRM screen; return the START update."""
    uid = [start_uid]

    def nid():
        uid[0] += 1
        return uid[0]

    await disp.dispatch(msg_update(nid(), chat, "/xerrameca"))
    tok = await click(disp, fake, chat, "Nova conversa")
    await disp.dispatch(cb_update(nid(), chat, tok))
    tok = await click(disp, fake, chat, "PeerAgent")
    await disp.dispatch(cb_update(nid(), chat, tok))
    tok = await click(disp, fake, chat, "Conversa")
    await disp.dispatch(cb_update(nid(), chat, tok))
    await disp.dispatch(msg_update(nid(), chat, "objectiu de prova"))
    tok = await click(disp, fake, chat, "proposer")
    await disp.dispatch(cb_update(nid(), chat, tok))
    tok = await click(disp, fake, chat, "reviewer")
    await disp.dispatch(cb_update(nid(), chat, tok))
    tok = await click(disp, fake, chat, "5")
    await disp.dispatch(cb_update(nid(), chat, tok))
    tok = await click(disp, fake, chat, "summary")
    await disp.dispatch(cb_update(nid(), chat, tok))
    _, confirm_kb = last_keyboard(fake)
    start_tok = [c for t, c in confirm_kb if t == "INICIAR"][0]
    return cb_update(nid(), chat, start_tok, callback_query_id="cq_start")


def _started_ctx(monkeypatch, fake, *, allowed=None):
    """Build a dispatcher driving a real wizard to a trusted-peer peer list."""
    tr, _ = make_transport(fake)
    disp, adapter, bridge, wizard, callbacks, fake_cmd = _make_real_stack(tr)
    from xerrameca.integrations.telegram_updates import TelegramUpdateDispatcher as _D
    if allowed is not None:
        disp = _D(adapter, allowed_chat_ids=allowed)
    fake_cmd.agents.append(
        AgentChoice(node_id="peer1", display_name="PeerAgent",
                    endpoint="http://127.0.0.1:9", trusted=True)
    )
    _patch_command(monkeypatch, fake_cmd)
    return disp, adapter, bridge, wizard, callbacks, fake_cmd


@pytest.mark.asyncio
async def test_duplicate_start_single_conversation(monkeypatch):
    tr, fake = make_transport()
    disp, adapter, bridge, wizard, callbacks, fake_cmd = _started_ctx(monkeypatch, fake)
    chat = "100"
    start_update = await _drive_to_confirm(disp, fake, chat)
    r1 = await disp.dispatch(start_update)
    assert r1.kind == "handled"
    r2 = await disp.dispatch(start_update)
    assert r2.kind == "duplicate"
    assert fake_cmd.create_count == 1
    sid = bridge.active_session_id(chat)
    sess = wizard.get_session(sid, chat)
    assert sess.state == "STARTED"


@pytest.mark.asyncio
async def test_concurrent_start_single_conversation(monkeypatch):
    tr, fake = make_transport()
    disp, adapter, bridge, wizard, callbacks, fake_cmd = _started_ctx(monkeypatch, fake)
    chat = "100"
    start_update = await _drive_to_confirm(disp, fake, chat)
    results = await asyncio.gather(disp.dispatch(start_update), disp.dispatch(start_update))
    kinds = sorted([r.kind for r in results])
    assert kinds == ["duplicate", "handled"]
    assert fake_cmd.create_count == 1  # exactly one conversation
    sid = bridge.active_session_id(chat)
    assert wizard.get_session(sid, chat).state == "STARTED"


# ===========================================================================
# Allowlist E2E through dispatch()
# ===========================================================================
@pytest.mark.asyncio
async def test_allowlist_e2e(monkeypatch):
    tr, fake = make_transport()
    disp, adapter, bridge, wizard, callbacks, fake_cmd = _started_ctx(
        monkeypatch, fake, allowed={"100"}
    )
    # denied update from chat 999 must never act.
    r = await disp.dispatch(msg_update(1, "999", "/xerrameca"))
    assert r.kind == "rejected"
    assert ack_count(fake) == 0 and sendmsg_count(fake) == 0
    assert fake_cmd.create_count == 0
    # denied callback -> no wizard work, but safe ACK so no Telegram spinner.
    r = await disp.dispatch(cb_update(2, "999", "whatever", callback_query_id="cq_d9"))
    assert r.kind == "rejected"
    assert ack_count(fake) == 1
    assert fake_cmd.create_count == 0
    assert bridge.active_session_id("999") is None
    # allowed chat works normally
    r = await disp.dispatch(msg_update(3, "100", "/xerrameca"))
    assert r.kind == "handled"
    assert sendmsg_count(fake) == 1


# ===========================================================================
# Real-wizard objective + custom-role text consumption through dispatch()
# ===========================================================================
@pytest.mark.asyncio
async def test_message_objective_consumed_by_wizard(monkeypatch):
    tr, fake = make_transport()
    disp, adapter, bridge, wizard, callbacks, fake_cmd = _make_real_stack(tr)
    fake_cmd.agents.append(
        AgentChoice(node_id="peer1", display_name="PeerAgent",
                    endpoint="http://127.0.0.1:9", trusted=True)
    )
    _patch_command(monkeypatch, fake_cmd)
    chat = "100"
    await disp.dispatch(msg_update(1, chat, "/xerrameca"))
    tok = await click(disp, fake, chat, "Nova conversa")
    await disp.dispatch(cb_update(2, chat, tok))
    tok = await click(disp, fake, chat, "PeerAgent")
    await disp.dispatch(cb_update(3, chat, tok))
    tok = await click(disp, fake, chat, "Conversa")
    await disp.dispatch(cb_update(4, chat, tok))
    r = await disp.dispatch(msg_update(5, chat, "objectiu de prova"))
    assert r.handled
    sid = bridge.active_session_id(chat)
    sess = wizard.get_session(sid, chat)
    assert sess.state == "SELECT_ROLE_A"
    assert sess.data["user_objective"] == "objectiu de prova"


@pytest.mark.asyncio
async def test_custom_role_free_text_consumed_by_wizard(monkeypatch):
    tr, fake = make_transport()
    disp, adapter, bridge, wizard, callbacks, fake_cmd = _make_real_stack(tr)
    fake_cmd.agents.append(
        AgentChoice(node_id="peer1", display_name="PeerAgent",
                    endpoint="http://127.0.0.1:9", trusted=True)
    )
    _patch_command(monkeypatch, fake_cmd)
    chat = "100"
    await disp.dispatch(msg_update(1, chat, "/xerrameca"))
    tok = await click(disp, fake, chat, "Nova conversa")
    await disp.dispatch(cb_update(2, chat, tok))
    tok = await click(disp, fake, chat, "PeerAgent")
    await disp.dispatch(cb_update(3, chat, tok))
    tok = await click(disp, fake, chat, "Conversa")
    await disp.dispatch(cb_update(4, chat, tok))
    await disp.dispatch(msg_update(5, chat, "objectiu de prova"))
    # choose custom on SELECT_ROLE_A -> stays on SELECT_ROLE_A expecting text
    tok = await click(disp, fake, chat, "custom")
    await disp.dispatch(cb_update(6, chat, tok))
    sid = bridge.active_session_id(chat)
    sess = wizard.get_session(sid, chat)
    assert sess.state == "SELECT_ROLE_A"
    assert sess.data.get("role_a_expects_custom") is True
    # free text becomes role_a verbatim -> SELECT_ROLE_B
    r = await disp.dispatch(msg_update(7, chat, "El meu rol personalitzat"))
    assert r.handled
    sess = wizard.get_session(sid, chat)
    assert sess.state == "SELECT_ROLE_B"
    assert sess.data["role_a"] == "El meu rol personalitzat"


# ===========================================================================
# Pseudo-buttons NONE on the button-capable transport (real InlineKeyboard)
# ===========================================================================
@pytest.mark.asyncio
async def test_no_pseudo_buttons_anywhere(monkeypatch):
    tr, fake = make_transport()
    disp, adapter, bridge, wizard, callbacks, fake_cmd = _make_real_stack(tr)
    fake_cmd.agents.append(
        AgentChoice(node_id="peer1", display_name="PeerAgent",
                    endpoint="http://127.0.0.1:9", trusted=True)
    )
    _patch_command(monkeypatch, fake_cmd)
    chat = "100"
    uid = [0]

    def nid():
        uid[0] += 1
        return uid[0]

    # walk the whole creation flow with unique update_ids
    await disp.dispatch(msg_update(nid(), chat, "/xerrameca"))
    tok = await click(disp, fake, chat, "Nova conversa")
    await disp.dispatch(cb_update(nid(), chat, tok))
    tok = await click(disp, fake, chat, "PeerAgent")
    await disp.dispatch(cb_update(nid(), chat, tok))
    tok = await click(disp, fake, chat, "Conversa")
    await disp.dispatch(cb_update(nid(), chat, tok))
    await disp.dispatch(msg_update(nid(), chat, "objectiu"))
    tok = await click(disp, fake, chat, "proposer")
    await disp.dispatch(cb_update(nid(), chat, tok))
    tok = await click(disp, fake, chat, "reviewer")
    await disp.dispatch(cb_update(nid(), chat, tok))
    for lbl in ("5", "summary"):
        tok = await click(disp, fake, chat, lbl)
        await disp.dispatch(cb_update(nid(), chat, tok, callback_query_id="cqnp"))
    for path, p in fake.requests:
        if "sendMessage" in path:
            assert " ::" not in p.get("text", "")
            for r_ in p.get("reply_markup", {}).get("inline_keyboard", []):
                assert " ::" not in r_[0]["text"]
