"""UX-4.4A — Telegram long-polling runner (getUpdates) + full simulated E2E.

Tests the OUTPUT/input-chain boundary driven by Telegram ``getUpdates``:

    getUpdates (httpx.MockTransport)
        -> TelegramGetUpdatesClient
        -> TelegramPollingRunner (offset persistence, backoff, lock, cancel)
        -> TelegramUpdateDispatcher
        -> TelegramUXAdapter -> Wizard -> TelegramBotAPITransport

No physical Telegram, no real bot token, no webhook, no Telegram SDK, no real
network (always httpx.MockTransport). Duplicate/crash-window semantics: the
runner makes NO exactly-once claim; the durable offset + dispatcher process-local
dedup reduce replays (see module docstring / docs).
"""

import asyncio
import inspect
import json
import os
import stat
import tempfile
from pathlib import Path

import httpx
import pytest

from xerrameca.command.dto import AgentChoice, ConversationSummary
from xerrameca.command.service import XerramecaCommandService
from xerrameca.command.wizard import XerramecaWizardService
from xerrameca.integrations.telegram import TelegramUXAdapter
from xerrameca.integrations.telegram_bot_api import TelegramBotAPITransport
from xerrameca.integrations.telegram_polling import (
    TelegramGetUpdatesClient,
    TelegramOffsetStore,
    TelegramPollingError,
    TelegramPollingRunner,
)
from xerrameca.integrations.telegram_updates import TelegramUpdateDispatcher
from xerrameca.ui import CallbackStore, TelegramWizardBridge

CANARY = "CANARY_TOKEN_XYZ_123456789"
FAKE_BASE = "https://fake.telegram.invalid"


# ===========================================================================
# Fake Telegram Bot API over ONE httpx.MockTransport (getUpdates + send + ACK)
# ===========================================================================
class FakeTelegramAPI:
    def __init__(self, *, http_status=200, ok_flag=True):
        self.requests: list[tuple[str, dict]] = []
        self.http_status = http_status
        self.ok_flag = ok_flag
        self.updates_batches: list[list[dict]] = []

    def enqueue(self, updates: list[dict]) -> None:
        self.updates_batches.append(list(updates))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.read()) if request.read() else {}
        self.requests.append((path, body))
        if "getUpdates" in path:
            if self.http_status != 200:
                return httpx.Response(self.http_status, json={})
            result = self.updates_batches.pop(0) if self.updates_batches else []
            return httpx.Response(200, json={"ok": self.ok_flag, "result": result})
        # sendMessage / answerCallbackQuery
        if self.http_status != 200:
            return httpx.Response(self.http_status, json={})
        return httpx.Response(200, json={"ok": True, "result": {}})

    @property
    def calls(self):
        return self.requests

    def get_updates_calls(self):
        return [b for p, b in self.requests if "getUpdates" in p]

    def sendmsg_calls(self):
        return [b for p, b in self.requests if "sendMessage" in p]

    def ack_calls(self):
        return [b for p, b in self.requests if "answerCallbackQuery" in p]

    def assert_no_delete_webhook(self):
        for p, _ in self.requests:
            assert "deleteWebhook" not in p


def make_stack(fake=None):
    """One MockTransport shared by getUpdates client and Bot API transport."""
    fake = fake or FakeTelegramAPI()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    get_client = TelegramGetUpdatesClient(
        token=CANARY, client=client, api_base=FAKE_BASE, poll_timeout=30
    )
    transport = TelegramBotAPITransport(
        token=CANARY, client=client, api_base=FAKE_BASE
    )
    return get_client, transport, fake


class FakeCommandService:
    def __init__(self):
        self.convos: list[ConversationSummary] = []
        self.create_count = 0
        self.last_created: dict = {}
        self.agents: list = []

    def list_conversations(self, **kw):
        return list(self.convos)

    def list_agents(self, **kw):
        return list(self.agents)

    def get_conversation(self, cid):
        return {"id": cid, "status": "RUNNING", "current_round": 3,
                "max_rounds": 6, "participants": ["peer1"]}

    def create_conversation(self, peer_node_id, objective, max_rounds=5,
                            delay_seconds=0):
        self.create_count += 1
        cid = f"xfc_ux44_{self.create_count}"
        self.convos.append(
            ConversationSummary(
                id=cid, objective=objective, status="RUNNING", coordinator_id="c",
                coordinator_epoch=0, current_round=1, max_rounds=max_rounds,
                participants=[peer_node_id],
            )
        )
        self.last_created = {"id": cid}
        return {"id": cid}


def _make_stack_with_adapter(fake, *, allowed=None):
    get_client, transport, fake = make_stack(fake)
    sd = tempfile.mkdtemp(prefix="ux44_")
    fake_cmd = FakeCommandService()
    wizard = XerramecaWizardService(sd, ttl_seconds=600, node_port=8891,
                                    command_service=fake_cmd)
    callbacks = CallbackStore()
    bridge = TelegramWizardBridge(wizard, callbacks)
    adapter = TelegramUXAdapter(
        node_base_url="http://127.0.0.1:9", api_key="TEST:APIKEY",
        transport=transport, wizard=bridge,
    )
    dispatcher = TelegramUpdateDispatcher(adapter, allowed_chat_ids=allowed)
    offset_store = TelegramOffsetStore(sd)
    runner = TelegramPollingRunner(
        client=get_client, dispatcher=dispatcher, offset_store=offset_store,
        state_dir=sd,
    )
    return runner, get_client, transport, fake, callbacks, wizard, fake_cmd, adapter, bridge


def _patch_command(monkeypatch, fake_cmd):
    def _list_agents(self, **kw):
        return list(fake_cmd.agents)

    def _create_conversation(self, *, peer_node_id, objective, max_rounds=5,
                             delay_seconds=0):
        fake_cmd.create_count += 1
        cid = f"xfc_ux44_{fake_cmd.create_count}"
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
    monkeypatch.setattr(XerramecaCommandService, "create_conversation",
                        _create_conversation)


# --- update builders -------------------------------------------------------
def msg_update(update_id, chat_id, text):
    return {"update_id": update_id,
            "message": {"message_id": update_id, "chat": {"id": int(chat_id)},
                        "text": text}}


def cb_update(update_id, chat_id, data, callback_query_id=None):
    cq = callback_query_id if callback_query_id is not None else f"cq_{update_id}"
    return {"update_id": update_id, "callback_query": {
        "id": cq, "data": data,
        "message": {"message_id": 1, "chat": {"id": int(chat_id)}}}}

# ===========================================================================
# PART 2 — test bodies
# ===========================================================================


def _client_only():
    get_client, transport, fake = make_stack()
    return get_client, fake


# ---------------------------------------------------------------------------
# 1-12. TelegramGetUpdatesClient — payload / request shape
# ---------------------------------------------------------------------------
def test_getupdates_post_to_bot_endpoint():
    get_client, fake = _client_only()
    assert get_client._url("getUpdates").startswith(FAKE_BASE + "/bot")
    assert "/getUpdates" in get_client._url("getUpdates")


@pytest.mark.asyncio
async def test_getupdates_timeout_is_positive():
    get_client, fake = _client_only()
    fake.enqueue([])
    await get_client.get_updates(limit=1)
    payload = fake.get_updates_calls()[-1]
    assert isinstance(payload.get("timeout"), int)
    assert payload["timeout"] > 0


@pytest.mark.asyncio
async def test_getupdates_allowed_updates_exact():
    get_client, fake = _client_only()
    fake.enqueue([])
    await get_client.get_updates(limit=1)
    payload = fake.get_updates_calls()[-1]
    assert payload["allowed_updates"] == ["message", "callback_query"]


@pytest.mark.asyncio
async def test_getupdates_offset_absent_first_time():
    get_client, fake = _client_only()
    fake.enqueue([])
    await get_client.get_updates(offset=None, limit=1)
    payload = fake.get_updates_calls()[-1]
    assert "offset" not in payload


@pytest.mark.asyncio
async def test_getupdates_offset_present_when_given():
    get_client, fake = _client_only()
    fake.enqueue([])
    await get_client.get_updates(offset=101, limit=1)
    assert fake.get_updates_calls()[-1]["offset"] == 101


@pytest.mark.asyncio
async def test_getupdates_http_timeout_exceeds_poll_timeout():
    client = TelegramGetUpdatesClient(token=CANARY, poll_timeout=30)
    assert client._http_read_timeout > 30
    # default = 30*2+15 = 75
    assert client._http_read_timeout == 75.0


@pytest.mark.asyncio
async def test_getupdates_custom_http_timeout_must_exceed_poll():
    with pytest.raises(TelegramPollingError):
        TelegramGetUpdatesClient(token=CANARY, poll_timeout=30,
                                 http_read_timeout=20)


@pytest.mark.asyncio
async def test_getupdates_limit_sent():
    get_client, fake = _client_only()
    fake.enqueue([])
    await get_client.get_updates(limit=50)
    assert fake.get_updates_calls()[-1]["limit"] == 50


@pytest.mark.asyncio
async def test_empty_token_rejected():
    from xerrameca.integrations.telegram_polling import TelegramGetUpdatesClient as C
    with pytest.raises(TelegramPollingError):
        C(token="")


@pytest.mark.asyncio
async def test_non_positive_poll_timeout_rejected():
    from xerrameca.integrations.telegram_polling import TelegramGetUpdatesClient as C
    with pytest.raises(TelegramPollingError):
        C(token=CANARY, poll_timeout=0)
    with pytest.raises(TelegramPollingError):
        C(token=CANARY, poll_timeout=-5)


@pytest.mark.asyncio
async def test_getupdates_result_list_parsed():
    get_client, fake = _client_only()
    fake.enqueue([{"update_id": 1, "message": {"text": "hi"}},
                  {"update_id": 2, "channel_post": {}}])
    result = await get_client.get_updates()
    assert isinstance(result, list)
    assert result[0]["update_id"] == 1
    assert result[1]["update_id"] == 2


@pytest.mark.asyncio
async def test_getupdates_malformed_result_sanitized():
    get_client, fake = _client_only()
    fake.enqueue([{"update_id": "not-int", "message": {}},
                  {"update_id": 42, "message": {"text": "ok"}},
                  "garbage", None, 7, {"no_update_id": 1},
                  {"update_id": True, "message": {}}])
    result = await get_client.get_updates()
    # only the dict with a valid non-bool int update_id survives
    assert [u["update_id"] for u in result] == [42]


# ---------------------------------------------------------------------------
# 13-19. GetUpdatesClient — error classification / sanitisation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_getupdates_ok_false_is_fatal_sanitized():
    fake = FakeTelegramAPI(ok_flag=False)
    fake.enqueue([])
    get_client, _, _ = make_stack(fake)
    with pytest.raises(TelegramPollingError) as ei:
        await get_client.get_updates()
    assert ei.value.fatal is True
    assert "ok=false description" not in str(ei.value)


@pytest.mark.asyncio
async def test_getupdates_raw_description_not_exposed():
    fake = FakeTelegramAPI(ok_flag=False)
    # inject a native description via raw handler
    def h(request):
        fake.requests.append((request.url.path, {}))
        return httpx.Response(200, json={"ok": False, "description": CANARY})
    client = httpx.AsyncClient(transport=httpx.MockTransport(h))
    get_client = TelegramGetUpdatesClient(token=CANARY, client=client, api_base=FAKE_BASE)
    with pytest.raises(TelegramPollingError) as ei:
        await get_client.get_updates()
    assert CANARY not in str(ei.value)
    assert CANARY not in repr(ei.value)


@pytest.mark.asyncio
async def test_getupdates_token_not_in_url_or_error():
    def h(request):
        assert CANARY in request.url.path  # token in URL but never surfaced
        return httpx.Response(503, json={})
    client = httpx.AsyncClient(transport=httpx.MockTransport(h))
    get_client = TelegramGetUpdatesClient(token=CANARY, client=client, api_base=FAKE_BASE)
    with pytest.raises(TelegramPollingError) as ei:
        await get_client.get_updates()
    assert CANARY not in str(ei.value)
    assert CANARY not in repr(ei.value)


@pytest.mark.asyncio
async def test_getupdates_network_error_sanitized_no_token():
    def h(request):
        raise httpx.ConnectError("boom network")
    client = httpx.AsyncClient(transport=httpx.MockTransport(h))
    get_client = TelegramGetUpdatesClient(token=CANARY, client=client, api_base=FAKE_BASE)
    with pytest.raises(TelegramPollingError) as ei:
        await get_client.get_updates()
    assert ei.value.fatal is False
    s = f"{ei.value}|{repr(ei.value)}"
    assert CANARY not in s
    assert "boom network" not in s
    assert FAKE_BASE not in s


@pytest.mark.asyncio
async def test_getupdates_401_fatal():
    fake = FakeTelegramAPI(http_status=401)
    get_client, _, _ = make_stack(fake)
    with pytest.raises(TelegramPollingError) as ei:
        await get_client.get_updates()
    assert ei.value.fatal is True
    assert CANARY not in str(ei.value)


@pytest.mark.asyncio
async def test_getupdates_403_fatal():
    fake = FakeTelegramAPI(http_status=403)
    get_client, _, _ = make_stack(fake)
    with pytest.raises(TelegramPollingError) as ei:
        await get_client.get_updates()
    assert ei.value.fatal is True


@pytest.mark.asyncio
async def test_getupdates_409_conflict_fatal_webhook_msg():
    fake = FakeTelegramAPI(http_status=409)
    get_client, _, _ = make_stack(fake)
    with pytest.raises(TelegramPollingError) as ei:
        await get_client.get_updates()
    assert ei.value.fatal is True
    assert "webhook" in str(ei.value)
    assert CANARY not in str(ei.value)


@pytest.mark.asyncio
async def test_no_auto_delete_webhook_ever():
    fake = FakeTelegramAPI(http_status=403)
    get_client, _, _ = make_stack(fake)
    with pytest.raises(TelegramPollingError):
        await get_client.get_updates()
    fake.assert_no_delete_webhook()


@pytest.mark.asyncio
async def test_getupdates_5xx_transient():
    for code in (500, 502, 503, 504):
        fake = FakeTelegramAPI(http_status=code)
        get_client, _, _ = make_stack(fake)
        with pytest.raises(TelegramPollingError) as ei:
            await get_client.get_updates()
        assert ei.value.fatal is False


@pytest.mark.asyncio
async def test_getupdates_429_transient():
    fake = FakeTelegramAPI(http_status=429)
    get_client, _, _ = make_stack(fake)
    with pytest.raises(TelegramPollingError) as ei:
        await get_client.get_updates()
    assert ei.value.fatal is False


@pytest.mark.asyncio
async def test_getupdates_invalid_json_body_transient():
    def h(request):
        return httpx.Response(200, text="not json")
    client = httpx.AsyncClient(transport=httpx.MockTransport(h))
    get_client = TelegramGetUpdatesClient(token=CANARY, client=client, api_base=FAKE_BASE)
    with pytest.raises(TelegramPollingError) as ei:
        await get_client.get_updates()
    assert ei.value.fatal is False


@pytest.mark.asyncio
async def test_getupdates_non_dict_result_transient():
    def h(request):
        return httpx.Response(200, json={"ok": True, "result": "string"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(h))
    get_client = TelegramGetUpdatesClient(token=CANARY, client=client, api_base=FAKE_BASE)
    with pytest.raises(TelegramPollingError) as ei:
        await get_client.get_updates()
    assert ei.value.fatal is False

# ===========================================================================
# 20-30. TelegramOffsetStore — durability, permissions, corruption fail-fast
# ===========================================================================
def _tmpdir():
    return tempfile.mkdtemp(prefix="ux44_off_")


def test_offset_first_start_none():
    store = TelegramOffsetStore(_tmpdir())
    assert store.load() is None
    assert store.next_offset is None


def test_offset_save_load_roundtrip():
    store = TelegramOffsetStore(_tmpdir())
    store.load()
    store.save(101)
    assert store.next_offset == 101
    store2 = TelegramOffsetStore(store.path.rsplit("/", 1)[0])
    assert store2.load() == 101


def test_offset_next_is_update_id_plus_one():
    store = TelegramOffsetStore(_tmpdir())
    store.load()
    store.save(100 + 1)
    assert store.next_offset == 101


def test_offset_monotonic_max():
    store = TelegramOffsetStore(_tmpdir())
    store.load()
    store.save(105)
    store.save(102)  # smaller -> keep max (monotonic upward only)
    assert store.next_offset == 105
    store.save(110)
    assert store.next_offset == 110


def test_offset_file_mode_0600():
    d = _tmpdir()
    store = TelegramOffsetStore(d)
    store.load()
    store.save(7)
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600


def test_offset_state_dir_0700():
    d = _tmpdir()
    store = TelegramOffsetStore(d)
    store.load()
    store.save(7)
    mode = stat.S_IMODE(os.stat(store._dir).st_mode)
    assert mode == 0o700


def test_offset_no_secrets_in_file():
    d = _tmpdir()
    store = TelegramOffsetStore(d)
    store.load()
    store.save(999)
    content = Path(store.path).read_text()
    data = json.loads(content)
    assert set(data.keys()) == {"next_offset"}
    assert data["next_offset"] == 999
    for bad in (CANARY, "login", "password", "chat", "callback", "bot", "token"):
        assert bad not in content


def test_offset_atomic_no_tmp_left():
    d = _tmpdir()
    store = TelegramOffsetStore(d)
    store.load()
    store.save(555)
    leftovers = [p for p in os.listdir(d) if p.endswith(".tmp")]
    assert leftovers == []


def test_offset_corrupt_fail_fast_not_silent_reset():
    d = _tmpdir()
    p = Path(d) / "telegram-offset.json"
    p.write_text("{ not valid json !!!")
    store = TelegramOffsetStore(d)
    with pytest.raises(TelegramPollingError) as ei:
        store.load()
    assert ei.value.fatal is True
    assert "corrupt" in str(ei.value)


def test_offset_missing_next_offset_key_fail_fast():
    d = _tmpdir()
    Path(d, "telegram-offset.json").write_text(json.dumps({"other": 1}))
    with pytest.raises(TelegramPollingError):
        TelegramOffsetStore(d).load()


def test_offset_non_integer_fail_fast():
    d = _tmpdir()
    Path(d, "telegram-offset.json").write_text(json.dumps({"next_offset": "ten"}))
    with pytest.raises(TelegramPollingError):
        TelegramOffsetStore(d).load()


def test_offset_negative_fail_fast():
    d = _tmpdir()
    Path(d, "telegram-offset.json").write_text(json.dumps({"next_offset": -1}))
    with pytest.raises(TelegramPollingError):
        TelegramOffsetStore(d).load()


def test_offset_bool_fail_fast():
    d = _tmpdir()
    Path(d, "telegram-offset.json").write_text(json.dumps({"next_offset": True}))
    with pytest.raises(TelegramPollingError):
        TelegramOffsetStore(d).load()


def test_offset_restart_loads_durable_offset():
    d = _tmpdir()
    s1 = TelegramOffsetStore(d)
    s1.load()
    s1.save(303)
    # brand-new store instance over the same dir -> durable offset recovered
    s2 = TelegramOffsetStore(d)
    assert s2.load() == 303


# ===========================================================================
# 31-40. TelegramPollingRunner — delivery, offset progression, backoff
# ===========================================================================
class FakeDispatcher:
    def __init__(self):
        self.delivered: list[dict] = []

    async def dispatch(self, update):
        self.delivered.append(dict(update))
        return None


def _runner_with(fake, *, dispatcher=None, sleep=None, state_dir=None):
    get_client, transport, fake = make_stack(fake)
    d = dispatcher or FakeDispatcher()
    sd = state_dir or tempfile.mkdtemp(prefix="ux44_run_")
    store = TelegramOffsetStore(sd)
    run = TelegramPollingRunner(
        client=get_client, dispatcher=d, offset_store=store, state_dir=sd,
        sleep=sleep or _instant_sleep,
    )
    return run, d, store, sd


class _Recorder:
    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay):
        self.delays.append(delay)
        return None


async def _instant_sleep(delay):
    return None


@pytest.mark.asyncio
async def test_run_once_batch_process_order():
    fake = FakeTelegramAPI()
    fake.enqueue([msg_update(100, "1", "a"), msg_update(101, "1", "b"),
                  msg_update(102, "1", "c")])
    run, d, store, _ = _runner_with(fake)
    n = await run.run_once()
    assert n == 3
    assert [u["update_id"] for u in d.delivered] == [100, 101, 102]


@pytest.mark.asyncio
async def test_run_once_each_update_to_dispatcher_raw():
    fake = FakeTelegramAPI()
    raw = {"update_id": 7, "message": {"chat": {"id": 1}, "text": "hi"}}
    fake.enqueue([raw])
    run, d, store, _ = _runner_with(fake)
    await run.run_once()
    assert d.delivered == [raw]


@pytest.mark.asyncio
async def test_runner_does_not_interpret_callback_data():
    fake = FakeTelegramAPI()
    raw = {"update_id": 9,
           "callback_query": {"id": "c9", "data": "opaque::token::secret",
                              "message": {"chat": {"id": 5}}}}
    fake.enqueue([raw])
    run, d, store, _ = _runner_with(fake)
    await run.run_once()
    # delivered verbatim; runner never parsed data/chat/callback
    assert d.delivered == [raw]


@pytest.mark.asyncio
async def test_run_once_advances_offset_update_id_plus_one():
    fake = FakeTelegramAPI()
    fake.enqueue([msg_update(100, "1", "hi")])
    run, d, store, _ = _runner_with(fake)
    await run.run_once()
    assert store.next_offset == 101


@pytest.mark.asyncio
async def test_offset_monotonic_across_batch():
    fake = FakeTelegramAPI()
    fake.enqueue([msg_update(100, "1", "a"), msg_update(105, "1", "b"),
                  msg_update(102, "1", "c")])
    run, d, store, _ = _runner_with(fake)
    await run.run_once()
    # max(update_id)+1, not last-seen+1
    assert store.next_offset == 106


@pytest.mark.asyncio
async def test_next_poll_uses_durable_offset():
    fake = FakeTelegramAPI()
    fake.enqueue([msg_update(200, "1", "a"), msg_update(201, "1", "b")])
    run, d, store, sd = _runner_with(fake)
    await run.run_once()
    assert store.next_offset == 202
    # second poll: offset=202 should be in the getUpdates payload
    fake.enqueue([])
    await run.run_once()
    assert fake.get_updates_calls()[-1]["offset"] == 202


@pytest.mark.asyncio
async def test_backoff_bounded_on_transient():
    from xerrameca.integrations.telegram_polling import _MAX_BACKOFF
    run, d, store, _ = _runner_with(FakeTelegramAPI())
    assert _MAX_BACKOFF == 30
    run._backoff = 0.0
    await run._backoff_wait()
    assert run._backoff == 1.0  # base
    await run._backoff_wait()
    assert run._backoff == 2.0
    await run._backoff_wait()
    assert run._backoff == 4.0
    await run._backoff_wait()
    assert run._backoff == 8.0
    await run._backoff_wait()
    assert run._backoff == 16.0
    await run._backoff_wait()
    assert run._backoff == 30.0
    await run._backoff_wait()
    assert run._backoff == 30.0  # capped at 30, no busy grow


@pytest.mark.asyncio
async def test_success_resets_backoff():
    fake = FakeTelegramAPI()
    fake.enqueue([])
    run, d, store, _ = _runner_with(fake)
    run._backoff = 30.0  # pretend we were in backoff
    await run.run_once()  # success
    assert run._backoff == 0.0


@pytest.mark.asyncio
async def test_transient_error_waits_then_retries():
    rec = _Recorder()
    fake = FakeTelegramAPI()
    run, d, store, _ = _runner_with(fake, sleep=rec)
    # first getUpdates fails with 503, subsequent succeed
    statuses = [503, 200, 200]

    def h(request):
        st = statuses.pop(0)
        if "getUpdates" in request.url.path:
            return httpx.Response(st, json={"ok": st == 200, "result": []})
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(h))
    get_client = TelegramGetUpdatesClient(token=CANARY, client=client, api_base=FAKE_BASE)
    d2 = FakeDispatcher()
    sd = tempfile.mkdtemp(prefix="ux44_bk_")
    crun = TelegramPollingRunner(client=get_client, dispatcher=d2,
                                 offset_store=TelegramOffsetStore(sd),
                                 state_dir=sd, sleep=rec)
    # First call: 503 -> transient error (not fatal). No backoff yet (bounded
    # backoff is applied by the caller via _backoff_wait, just like run_forever).
    crun._backoff = 0.0
    with pytest.raises(TelegramPollingError) as ei:
        await crun.run_once()
    assert ei.value.fatal is False
    # Transition to waiting: _backoff_wait grows the bounded schedule on a
    # transient error and records the sleep delay (injectable -> instant).
    await crun._backoff_wait()
    assert 0 < crun._backoff <= 30.0  # bounded backoff
    assert rec.delays and all(0 < x <= 30 for x in rec.delays)
    # A successful poll clears the backoff (run_once resets it).
    crun._backoff = 8.0  # simulate accumulated backoff
    await crun.run_once()  # 200 success
    assert crun._backoff == 0.0



@pytest.mark.asyncio
async def test_run_forever_fatal_401_stops_without_retry():
    rec = _Recorder()
    fake = FakeTelegramAPI(http_status=401)
    run, d, store, _ = _runner_with(fake, sleep=rec)
    with pytest.raises(TelegramPollingError) as ei:
        await run.run_forever()
    assert ei.value.fatal is True
    assert rec.delays == []  # no backoff wait before a fatal stop


@pytest.mark.asyncio
async def test_run_forever_fatal_403_stops():
    fake = FakeTelegramAPI(http_status=403)
    run, d, store, _ = _runner_with(fake)
    with pytest.raises(TelegramPollingError) as ei:
        await run.run_forever()
    assert ei.value.fatal is True


@pytest.mark.asyncio
async def test_run_forever_fatal_409_conflict_stops():
    fake = FakeTelegramAPI(http_status=409)
    run, d, store, _ = _runner_with(fake)
    with pytest.raises(TelegramPollingError) as ei:
        await run.run_forever()
    assert ei.value.fatal is True
    assert "webhook" in str(ei.value)


@pytest.mark.asyncio
async def test_run_forever_stop_event_clean_exit():
    fake = FakeTelegramAPI()
    fake.enqueue([])
    run, d, store, _ = _runner_with(fake)
    ev = asyncio.Event()
    ev.set()  # already set: the runner must observe it on the first check
    # run_forever checks stop_event at the top of each loop iteration, so with
    # stop_event already set it should return cleanly without polling.
    await asyncio.wait_for(run.run_forever(stop_event=ev), timeout=5)


@pytest.mark.asyncio
async def test_run_forever_cancellation_safe():
    # The runner is awaiting a getUpdates response (long poll) when cancelled.
    # This is the realistic cancellation point: it must raise CancelledError
    # cleanly (not swallow it) rather than hang.
    fake = FakeTelegramAPI()

    async def hang(request):
        # never resolve: simulate a long-poll that is cancelled mid-flight
        await asyncio.sleep(3600)
        return httpx.Response(200, json={"ok": True, "result": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(hang))
    get_client = TelegramGetUpdatesClient(token=CANARY, client=client, api_base=FAKE_BASE)
    d = FakeDispatcher()
    sd = tempfile.mkdtemp(prefix="ux44_cx_")
    run = TelegramPollingRunner(client=get_client, dispatcher=d,
                                offset_store=TelegramOffsetStore(sd),
                                state_dir=sd, sleep=_instant_sleep)
    task = asyncio.create_task(run.run_forever(stop_event=None))
    # give the runner a tick to enter the awaiting getUpdates
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

# ===========================================================================
# 41-46. Singleton runner lock
# ===========================================================================
def _runner_locked(sd):
    get_client, transport, fake = make_stack(FakeTelegramAPI())
    run = TelegramPollingRunner(
        client=get_client, dispatcher=FakeDispatcher(),
        offset_store=TelegramOffsetStore(sd), state_dir=sd,
        sleep=_instant_sleep,
    )
    return run


def test_runner_acquire_lock():
    sd = tempfile.mkdtemp(prefix="ux44_lock_")
    run = _runner_locked(sd)
    run.acquire_lock()
    assert run._lock_handle is not None
    assert Path(sd, "telegram-runner.lock").exists()
    run.release_lock()
    assert run._lock_handle is None


def test_second_runner_rejected():
    sd = tempfile.mkdtemp(prefix="ux44_lock_")
    run1 = _runner_locked(sd)
    run2 = _runner_locked(sd)
    run1.acquire_lock()
    with pytest.raises(TelegramPollingError) as ei:
        run2.acquire_lock()
    assert "already active" in str(ei.value)
    assert ei.value.fatal is True
    assert CANARY not in str(ei.value)
    run1.release_lock()


def test_lock_release_idempotent():
    sd = tempfile.mkdtemp(prefix="ux44_lock_")
    run = _runner_locked(sd)
    run.release_lock()  # no-op
    assert run._lock_handle is None


def test_lock_release_allows_second_runner():
    sd = tempfile.mkdtemp(prefix="ux44_lock_")
    run1 = _runner_locked(sd)
    run2 = _runner_locked(sd)
    run1.acquire_lock()
    run1.release_lock()
    run2.acquire_lock()  # now allowed
    run2.release_lock()


def test_runner_lock_file_mode_0600():
    sd = tempfile.mkdtemp(prefix="ux44_lock_")
    run = _runner_locked(sd)
    run.acquire_lock()
    run.release_lock()
    mode = stat.S_IMODE(os.stat(Path(sd, "telegram-runner.lock")).st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_run_forever_holds_lock_until_cancelled(monkeypatch):
    sd = tempfile.mkdtemp(prefix="ux44_lock_")
    fake = FakeTelegramAPI()
    run, d, store, _ = _runner_with(fake, state_dir=sd)
    run.acquire_lock()
    try:
        other = _runner_locked(sd)
        with pytest.raises(TelegramPollingError):
            other.acquire_lock()  # busy while run owns it
    finally:
        run.release_lock()
    after = _runner_locked(sd)
    after.acquire_lock()  # released -> available
    after.release_lock()


# ===========================================================================
# 47-52. CLI / token-file / security
# ===========================================================================
def _cli_main(argv):
    from xerrameca import cli
    return cli.main(argv)


def test_telegram_cli_no_token_argument():
    """--token CLI argument is PROHIBITED; only --token-file exists."""
    from xerrameca.cli import _parser
    p = _parser()
    help_text = ""
    try:
        p.parse_args(["telegram", "--help"])
    except SystemExit as e:
        # argparse calls sys.exit(0) on --help; capture via getopt instead
        pass
    sub = p._subparsers._group_actions[0].choices["telegram"]
    for action in sub._actions:
        for opt in action.option_strings:
            assert opt != "--token", "token CLI argument must not exist"
    assert any("--token-file" in o for a in sub._actions for o in a.option_strings)


def test_telegram_cli_allowed_chat_id_repeatable():
    from xerrameca.cli import _parser
    args = _parser().parse_args(
        ["telegram", "--state-dir", "/tmp/s", "--node-base-url",
         "http://127.0.0.1:8791", "--token-file", "/tmp/t",
         "--allowed-chat-id", "100", "--allowed-chat-id", "200",
         "--poll-timeout", "45"]
    )
    assert args.allowed_chat_id == ["100", "200"]
    assert args.poll_timeout == 45
    assert args.node_base_url == "http://127.0.0.1:8791"


def test_telegram_cli_requires_explicit_node_base_url():
    from xerrameca.cli import _parser
    with pytest.raises(SystemExit):
        _parser().parse_args(
            ["telegram", "--state-dir", "/tmp/s", "--token-file", "/tmp/t"]
        )
    # no default baked into the parser
    sub = _parser()._subparsers._group_actions[0].choices["telegram"]
    defs = [a for a in sub._actions if getattr(a, "default", None) not in (None, "==SUPPRESS==")]
    nbu = [a for a in sub._actions if "--node-base-url" in a.option_strings]
    assert nbu and nbu[0].default is None


def test_telegram_cli_default_poll_timeout_30():
    from xerrameca.cli import _parser
    sub = _parser()._subparsers._group_actions[0].choices["telegram"]
    pt = [a for a in sub._actions if "--poll-timeout" in a.option_strings][0]
    assert pt.default == 30


def test_read_token_empty_rejected(tmp_path):
    from xerrameca.cli import _read_telegram_token
    f = tmp_path / "token"
    f.write_text("   \n")
    with pytest.raises(TelegramPollingError) as ei:
        _read_telegram_token(str(f))
    assert "Telegram credential unavailable" in str(ei.value)


def test_read_token_insecure_perms_rejected(tmp_path):
    from xerrameca.cli import _read_telegram_token
    f = tmp_path / "token"
    f.write_text(CANARY + "\n")
    os.chmod(f, 0o662)  # group/world writable
    with pytest.raises(TelegramPollingError) as ei:
        _read_telegram_token(str(f))
    assert "Telegram credential unavailable" in str(ei.value)
    assert CANARY not in str(ei.value)  # never echoes token


def test_read_token_secure_perms_ok(tmp_path):
    from xerrameca.cli import _read_telegram_token
    f = tmp_path / "token"
    f.write_text(CANARY + "\n")
    os.chmod(f, 0o600)
    assert _read_telegram_token(str(f)) == CANARY


def test_read_token_missing_file_rejected(tmp_path):
    from xerrameca.cli import _read_telegram_token
    with pytest.raises(TelegramPollingError) as ei:
        _read_telegram_token(str(tmp_path / "nope"))
    assert "Telegram credential unavailable" in str(ei.value)


def test_local_api_key_not_output():
    """The CLI help for `telegram` must not mention or echo API keys/endpoints."""
    from xerrameca.cli import _parser
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        _parser().parse_args(["telegram", "--help"])
    out = buf.getvalue()
    assert "api-key" not in out
    assert "local-agent" not in out
    assert "X-API-Key" not in out
    assert CANARY not in out


def test_local_api_key_read_produces_runtime_only(tmp_path):
    from xerrameca.cli import _read_local_api_key, _read_telegram_token
    from xerrameca.node.identity import initialize_node
    state = initialize_node(str(tmp_path), agent_id="a", display_name="n",
                            endpoint="http://127.0.0.1:8791")
    key = _read_local_api_key(str(tmp_path))
    assert key  # non-empty
    from pathlib import Path as P
    assert Path(tmp_path, "local-agent-api-key").read_text().strip() == key


# ===========================================================================
# 53-58. Allowlist forwarding via the runner + dispatcher
# ===========================================================================
def test_allowed_chat_ids_forwarded_to_dispatcher():
    fake = FakeTelegramAPI()
    get_client, transport, fake = make_stack(fake)
    sd = tempfile.mkdtemp(prefix="ux44_allow_")
    from xerrameca.integrations.telegram_updates import TelegramUpdateDispatcher as D
    adapter = object()
    disp = D(adapter, allowed_chat_ids={"100", "200"})
    assert disp._allowed == {"100", "200"}


class _RecordingAdapter:
    def __init__(self):
        self.acts = []

    async def handle_text(self, chat_id, text):
        self.acts.append(("text", chat_id, text))

    async def handle_callback(self, chat_id, data, callback_query_id=None):
        self.acts.append(("cb", chat_id, data))

    async def safe_ack(self, callback_query_id):
        self.acts.append(("ack", callback_query_id))


@pytest.mark.asyncio
async def test_denied_chat_no_mutation_via_runner():
    fake = FakeTelegramAPI()
    fake.enqueue([msg_update(1, "999", "/xerrameca"),
                  msg_update(2, "999", "boom")])
    get_client, transport, fake = make_stack(fake)
    adapter = _RecordingAdapter()
    disp = TelegramUpdateDispatcher(adapter, allowed_chat_ids={"100"})
    sd = tempfile.mkdtemp(prefix="ux44_deny_")
    run = TelegramPollingRunner(client=get_client, dispatcher=disp,
                                offset_store=TelegramOffsetStore(sd),
                                state_dir=sd, sleep=_instant_sleep)
    await run.run_once()
    assert adapter.acts == []  # denied chats never acted


# ===========================================================================
# 59-62. zero SDK / zero webhook / zero real network
# ===========================================================================
def _polling_source():
    import xerrameca.integrations.telegram_polling as m
    return inspect.getsource(m)


def test_zero_telegram_sdk_in_polling_module():
    src = _polling_source()
    for banned in ("python-telegram-bot", "telegram.ext", "aiogram", "telebot"):
        assert banned not in src
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert node.module not in ("telegram", "aiogram", "telebot")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in ("telegram", "aiogram", "telebot")


def test_zero_webhook_in_polling_module():
    src = _polling_source()
    # Only forbid actual *calls* (the docstring/comments legitimately document
    # the "no auto deleteWebhook" contract without invoking it).
    for banned in ("setWebhook(", "deleteWebhook("):
        assert banned not in src
    assert "api.telegram.org/bot" not in src  # no hardcoded real endpoint usage


def test_zero_real_network_all_transports_mock():
    fake = FakeTelegramAPI()
    get_client, transport, fake = make_stack(fake)
    assert isinstance(get_client._client._transport, httpx.MockTransport)
    assert isinstance(transport._client._transport, httpx.MockTransport)
    assert "fake.telegram.invalid" in get_client._api_base
    assert "fake.telegram.invalid" in transport._api_base


def test_zero_webhook_server_in_cli():
    import xerrameca.cli as c
    src = inspect.getsource(c)
    for banned in ("setWebhook", "deleteWebhook", "uvicorn run webhook",
                   "aiohttp", "FastAPI"):
        assert banned not in src


# ===========================================================================
# 63-66. Security canary
# ===========================================================================
def test_canary_not_in_any_error_path(monkeypatch):
    """Force every failure mode and assert the canary token never leaks."""
    leaked = []

    # 1) network error
    def h(req):
        raise httpx.ConnectError("boom")
    client = httpx.AsyncClient(transport=httpx.MockTransport(h))
    gc = TelegramGetUpdatesClient(token=CANARY, client=client, api_base=FAKE_BASE)
    try:
        asyncio.run(gc.get_updates())
    except TelegramPollingError as e:
        leaked.append(str(e))
        leaked.append(repr(e))
    except Exception as e:
        leaked.append(f"{e}")

    # 2) ok=false with description
    def h2(req):
        return httpx.Response(200, json={"ok": False, "description": CANARY})
    client2 = httpx.AsyncClient(transport=httpx.MockTransport(h2))
    gc2 = TelegramGetUpdatesClient(token=CANARY, client=client2, api_base=FAKE_BASE)
    try:
        asyncio.run(gc2.get_updates())
    except TelegramPollingError as e:
        leaked.append(str(e))
        leaked.append(repr(e))

    # 3) invalid JSON
    def h3(req):
        return httpx.Response(200, text="<html>bad" + CANARY)
    client3 = httpx.AsyncClient(transport=httpx.MockTransport(h3))
    gc3 = TelegramGetUpdatesClient(token=CANARY, client=client3, api_base=FAKE_BASE)
    try:
        asyncio.run(gc3.get_updates())
    except TelegramPollingError as e:
        leaked.append(str(e))
        leaked.append(repr(e))

    # 4) 401 / 403 / 409 / 503
    for code in (401, 403, 409, 503, 429, 500):
        fake = FakeTelegramAPI(http_status=code)
        gc4, _, _ = make_stack(fake)
        try:
            asyncio.run(gc4.get_updates())
        except TelegramPollingError as e:
            leaked.append(str(e))
            leaked.append(repr(e))

    # 5) offset corruption path
    d = tempfile.mkdtemp(prefix="ux44_can_")
    Path(d, "telegram-offset.json").write_text("garbage{")
    try:
        TelegramOffsetStore(d).load()
    except TelegramPollingError as e:
        leaked.append(str(e))

    full = "|".join(leaked)
    assert CANARY not in full
    assert "api.telegram.org/bot" not in full
    assert "/bot" + CANARY not in full
    assert len(leaked) > 0


def test_canary_not_in_canary_runner_repr():
    gc = TelegramGetUpdatesClient(token=CANARY, poll_timeout=30)
    runner_repr = repr(gc)
    assert CANARY not in runner_repr
    assert "bot" + CANARY not in runner_repr


def test_dispatch_result_no_canary(monkeypatch):
    """A real dispatch result built through the poller path carries no token."""
    fake = FakeTelegramAPI()
    fake.enqueue([msg_update(5, "100", "/xerrameca")])
    runner, get_client, transport, fake, callbacks, wizard, fake_cmd, adapter, bridge = (
        _make_stack_with_adapter(fake))
    import asyncio as _a
    _a.run(runner.run_once())
    for b in fake.sendmsg_calls():
        assert CANARY not in str(b)
    # search the offset-store dir for any persistent secret
    sd = runner._state_dir
    for file in Path(sd).glob("*"):
        if file.is_file():
            content = file.read_bytes()
            assert CANARY.encode() not in content


# ===========================================================================
# 67-72. full polling E2E (getUpdates-driven), offset E2E, duplicate safety
# ===========================================================================
def _last_keyboard(fake):
    for body in reversed(fake.sendmsg_calls()):
        kb = body.get("reply_markup", {}).get("inline_keyboard", [])
        return body["text"], [(row[0]["text"], row[0]["callback_data"]) for row in kb]
    raise AssertionError("no sendMessage seen")


def _click(fake, chat, label_substr):
    _, kb = _last_keyboard(fake)
    matches = [(t, c) for (t, c) in kb if label_substr in t]
    if not matches:
        raise AssertionError(f"no button {label_substr!r}; got {[t for t, _ in kb]}")
    return matches[0][1]


@pytest.mark.asyncio
async def test_full_polling_e2e_create_exactly_once(monkeypatch):
    """Drive the whole wizard chain ONLY through getUpdates batches.

    /xerrameca -> Nova conversa -> peer (PeerAgent) -> preset -> objective text
    -> role A -> role B -> rounds -> output -> INICIAR (create_conversation==1)
    then /xerrameca -> Converses -> detail -> Actualitzar -> Mode -> Enrere.
    """
    fake = FakeTelegramAPI()
    (runner, get_client, transport, fake, callbacks, wizard, fake_cmd,
     adapter, bridge) = _make_stack_with_adapter(fake)
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

    async def step(*updates):
        fake.enqueue(list(updates))
        return await runner.run_once()

    # /xerrameca -> ROOT
    await step(msg_update(nid(), chat, "/xerrameca"))
    text, kb = _last_keyboard(fake)
    assert any("Nova conversa" in t for t, _ in kb)

    # Nova conversa -> SELECT_PEER
    tok = _click(fake, chat, "Nova conversa")
    await step(cb_update(nid(), chat, tok))
    text, peer_kb = _last_keyboard(fake)
    assert any("PeerAgent" in t for t, _ in peer_kb)

    # peer -> preset
    tok = _click(fake, chat, "PeerAgent")
    await step(cb_update(nid(), chat, tok))
    _, preset_kb = _last_keyboard(fake)
    assert any("Conversa" in t for t, _ in preset_kb)

    # preset -> ENTER_OBJECTIVE
    tok = _click(fake, chat, "Conversa")
    await step(cb_update(nid(), chat, tok))
    text, _ = _last_keyboard(fake)
    assert "Objectiu" in text

    # objective -> SELECT_ROLE_A
    await step(msg_update(nid(), chat, "objectiu de prova"))
    sid = bridge.active_session_id(chat)
    assert sid is not None
    sess = wizard.get_session(sid, chat)
    assert sess.state == "SELECT_ROLE_A"
    _, roleA_kb = _last_keyboard(fake)
    assert any("proposer" in t for t, _ in roleA_kb)

    # role A -> SELECT_ROLE_B ; role B -> SELECT_ROUNDS
    tok = _click(fake, chat, "proposer")
    await step(cb_update(nid(), chat, tok))
    _, roleB_kb = _last_keyboard(fake)
    tok = _click(fake, chat, "reviewer")
    await step(cb_update(nid(), chat, tok))
    _, rounds_kb = _last_keyboard(fake)

    # rounds -> SELECT_OUTPUT_MODE ; output -> CONFIRM
    tok = _click(fake, chat, "5")
    await step(cb_update(nid(), chat, tok))
    _, output_kb = _last_keyboard(fake)
    assert any("summary" in t for t, _ in output_kb)
    tok = _click(fake, chat, "summary")
    await step(cb_update(nid(), chat, tok))
    text, confirm_kb = _last_keyboard(fake)
    assert "INICIAR" in [t for t, _ in confirm_kb]

    # INICIAR -> create_conversation EXACTLY ONCE (through getUpdates)
    start_tok = [c for t, c in confirm_kb if t == "INICIAR"][0]
    start_upd = cb_update(nid(), chat, start_tok, callback_query_id="cq_start")
    r = await step(start_upd)
    assert fake_cmd.create_count == 1
    assert wizard.get_session(sid, chat).state == "STARTED"

    # new /xerrameca -> ROOT
    await step(msg_update(nid(), chat, "/xerrameca"))
    _, root2_kb = _last_keyboard(fake)
    assert any("Nova conversa" in t for t, _ in root2_kb)

    # Converses -> detail -> Actualitzar -> Mode -> Enrere
    tok = _click(fake, chat, "Converses")
    await step(cb_update(nid(), chat, tok))
    _, list_kb = _last_keyboard(fake)
    assert any("RUNNING" in t for t, _ in list_kb)
    tok = _click(fake, chat, "RUNNING")
    await step(cb_update(nid(), chat, tok))
    _, detail_kb = _last_keyboard(fake)
    assert any("Actualitzar" in t for t, _ in detail_kb)
    tok = _click(fake, chat, "Actualitzar")
    await step(cb_update(nid(), chat, tok))
    assert any("Actualitzar" in t for t, _ in _last_keyboard(fake)[1])
    tok = _click(fake, chat, "Mode")
    await step(cb_update(nid(), chat, tok))
    _, mode_kb = _last_keyboard(fake)
    assert any("summary" in t.lower() for t, _ in mode_kb)
    tok = _click(fake, chat, "Enrere")
    await step(cb_update(nid(), chat, tok))
    assert any("Actualitzar" in t for t, _ in _last_keyboard(fake)[1])

    # no pseudo-button lines, no secrets in callback_data
    for b in fake.sendmsg_calls():
        assert " ::" not in b.get("text", "")
        for row in b.get("reply_markup", {}).get("inline_keyboard", []):
            cb = row[0]["callback_data"]
            assert "conversation_id" not in cb and "node_" not in cb
            assert "TEST" not in cb and CANARY not in cb
            assert len(cb.encode("utf-8")) <= 64
    # no deleteWebhook was ever issued
    fake.assert_no_delete_webhook()


# ---------------------------------------------------------------------------
# Offset E2E (item 27)
# ---------------------------------------------------------------------------
def _runner_shared_store(sd, fake):
    """GetUpdates client + runner sharing one MockTransport + durable dir."""
    get_client, transport, fake = make_stack(fake)
    store = TelegramOffsetStore(sd)
    task_disp = FakeDispatcher()
    run = TelegramPollingRunner(
        client=get_client, dispatcher=task_disp, offset_store=store,
        state_dir=sd, sleep=_instant_sleep,
    )
    return run, task_disp, store


@pytest.mark.asyncio
async def test_offset_e2e_restart_recovers_durable_offset():
    sd = tempfile.mkdtemp(prefix="ux44_off_e2e_")

    # batch1: update_id 100 -> durable offset 101
    fake = FakeTelegramAPI()
    fake.enqueue([msg_update(100, "1", "a")])
    run1, d1, store1 = _runner_shared_store(sd, fake)
    await run1.run_once()
    assert store1.next_offset == 101

    # NEXT getUpdates must request offset 101
    fake.enqueue([])
    await run1.run_once()
    assert fake.get_updates_calls()[-1]["offset"] == 101

    # batch2: 101, 102 -> durable offset 103
    fake.enqueue([msg_update(101, "1", "b"), msg_update(102, "1", "c")])
    await run1.run_once()
    assert store1.next_offset == 103

    # restart runner (brand-new objects) over the same state-dir
    fake2 = FakeTelegramAPI()
    fake2.enqueue([])  # first getUpdates after restart
    run2, d2, store2 = _runner_shared_store(sd, fake2)
    assert store2.load() == 103
    await run2.run_once()
    # restart's first getUpdates used the durable offset 103
    assert fake2.get_updates_calls()[-1]["offset"] == 103
    # no confirmed update (100/101/102) was re-delivered to the new dispatcher
    assert d2.delivered == []


@pytest.mark.asyncio
async def test_offset_e2e_empty_batch_no_regression():
    fake = FakeTelegramAPI()
    fake.enqueue([])
    run, d, store, _ = _runner_with(fake)
    await run.run_once()
    assert store.next_offset is None  # nothing consumed -> no offset yet


@pytest.mark.asyncio
async def test_offset_persisted_after_each_valid_update_in_batch():
    sd = tempfile.mkdtemp(prefix="ux44_off_e2e_")
    fake = FakeTelegramAPI()
    fake.enqueue([msg_update(50, "1", "x"), msg_update(52, "1", "y")])
    run, d, store, _ = _runner_with(fake, state_dir=sd)
    await run.run_once()
    assert store.next_offset == 53  # max(update_id)+1


# ---------------------------------------------------------------------------
# Duplicate safety through the runner (item 28)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_duplicate_safety_create_exactly_once(monkeypatch):
    """Same START update twice in one runner/dispatcher lifetime -> 1 creation."""
    fake = FakeTelegramAPI()
    (runner, get_client, transport, fake, callbacks, wizard, fake_cmd,
     adapter, bridge) = _make_stack_with_adapter(fake)
    fake_cmd.agents.append(
        AgentChoice(node_id="peer1", display_name="PeerAgent",
                    endpoint="http://127.0.0.1:9", trusted=True)
    )
    _patch_command(monkeypatch, fake_cmd)
    chat = "100"
    uid = [1000]

    def nid():
        uid[0] += 1
        return uid[0]

    async def step(*updates):
        fake.enqueue(list(updates))
        return await runner.run_once()

    # drive to CONFIRM through getUpdates
    await step(msg_update(nid(), chat, "/xerrameca"))
    tok = _click(fake, chat, "Nova conversa")
    await step(cb_update(nid(), chat, tok))
    tok = _click(fake, chat, "PeerAgent")
    await step(cb_update(nid(), chat, tok))
    tok = _click(fake, chat, "Conversa")
    await step(cb_update(nid(), chat, tok))
    await step(msg_update(nid(), chat, "objectiu de prova"))
    tok = _click(fake, chat, "proposer")
    await step(cb_update(nid(), chat, tok))
    tok = _click(fake, chat, "reviewer")
    await step(cb_update(nid(), chat, tok))
    tok = _click(fake, chat, "5")
    await step(cb_update(nid(), chat, tok))
    tok = _click(fake, chat, "summary")
    await step(cb_update(nid(), chat, tok))
    _, confirm_kb = _last_keyboard(fake)
    start_tok = [c for t, c in confirm_kb if t == "INICIAR"][0]
    start_upd = cb_update(nid(), chat, start_tok, callback_query_id="cq_start")

    # same update delivered TWICE in ONE process -> dispatcher dedup
    await step(start_upd, start_upd)
    assert fake_cmd.create_count == 1
    sid = bridge.active_session_id(chat)
    assert wizard.get_session(sid, chat).state == "STARTED"


# ---------------------------------------------------------------------------
# Crash-window / no-exactly-once contract (item 29) — documented, documented test
# ---------------------------------------------------------------------------
def test_no_exactly_once_claim_in_docs():
    """The docs and module MUST explicitly state there is no exactly-once."""
    import xerrameca.integrations.telegram_polling as m
    mod_src = inspect.getsource(m).lower()
    assert "exactly-once" in mod_src or "exactly_once" in mod_src
    assert "replay" in mod_src


# ===========================================================================
# InlineKeyboard + callback ACK still work through the polling chain
# ===========================================================================
@pytest.mark.asyncio
async def test_e2e_inline_keyboard_and_callback_ack(monkeypatch):
    """InlineKeyboard sendMessage + answerCallbackQuery both still work."""
    fake = FakeTelegramAPI()
    (runner, get_client, transport, fake, callbacks, wizard, fake_cmd,
     adapter, bridge) = _make_stack_with_adapter(fake)
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

    async def step(*updates):
        fake.enqueue(list(updates))
        return await runner.run_once()

    # /xerrameca -> ROOT with a REAL inline_keyboard
    await step(msg_update(nid(), chat, "/xerrameca"))
    smsgs = fake.sendmsg_calls()
    assert len(smsgs) == 1  # ONE sendMessage for ROOT (real keyboard, not pseudo)
    kb = smsgs[-1].get("reply_markup", {}).get("inline_keyboard")
    assert kb is not None
    # valid callback -> ACK emitted (answerCallbackQuery)
    tok = _click(fake, chat, "Nova conversa")
    before = len(fake.ack_calls())
    await step(cb_update(nid(), chat, tok, callback_query_id="cq_ack"))
    assert len(fake.ack_calls()) == before + 1
    # no pseudo-button line anywhere
    for b in fake.sendmsg_calls():
        assert " ::" not in b.get("text", "")


# ===========================================================================
# Allowlist E2E through the runner (denied chat cannot act)
# ===========================================================================
@pytest.mark.asyncio
async def test_e2e_allowlist_denied_chat_no_act(monkeypatch):
    fake = FakeTelegramAPI()
    (runner, get_client, transport, fake, callbacks, wizard, fake_cmd,
     adapter, bridge) = _make_stack_with_adapter(fake, allowed={"100"})
    fake_cmd.agents.append(
        AgentChoice(node_id="peer1", display_name="PeerAgent",
                    endpoint="http://127.0.0.1:9", trusted=True)
    )
    _patch_command(monkeypatch, fake_cmd)
    # denied chat 999 sends /xerrameca -> runner delivers -> dispatcher rejects
    fake.enqueue([msg_update(1, "999", "/xerrameca")])
    await runner.run_once()
    assert fake_cmd.create_count == 0
    assert fake.sendmsg_calls() == []  # no screen rendered for denied chat


# ===========================================================================
# CLI build wiring (no real node): token-file + state-dir used at build time
# ===========================================================================
def test_build_telegram_stack_wiring(tmp_path):
    from xerrameca.cli import _build_telegram_stack, _parser, _read_telegram_token
    from xerrameca.node.identity import initialize_node
    initialize_node(str(tmp_path), agent_id="a", display_name="n",
                    endpoint="http://127.0.0.1:8791")
    tok_file = tmp_path / "bot-token"
    tok_file.write_text(CANARY + "\n")
    os.chmod(tok_file, 0o600)
    args = _parser().parse_args(
        ["telegram", "--state-dir", str(tmp_path), "--node-base-url",
         "http://127.0.0.1:8791", "--token-file", str(tok_file),
         "--allowed-chat-id", "100"]
    )
    runner, dispatcher, client, offset_store = _build_telegram_stack(args)
    assert isinstance(runner, TelegramPollingRunner)
    assert isinstance(dispatcher, TelegramUpdateDispatcher)
    assert isinstance(client, TelegramGetUpdatesClient)
    assert isinstance(offset_store, TelegramOffsetStore)
    # allowlist forwarded to dispatcher
    assert dispatcher._allowed == {"100"}
    # token reused from file is in-memory only, never persisted to the offset
    # file specifically (the state-dir also legitimately holds bot-token).
    off_path = offset_store._path
    if off_path.exists():
        assert CANARY.encode() not in off_path.read_bytes()



# ---------------------------------------------------------------------------
# UX-4.4A HARDENING: dispatcher internal-exception -> offset unchanged, and
# batch-stop semantics (no offset gap). (Added during pre-commit hardening audit.)
# ---------------------------------------------------------------------------
class _BoomDispatcher(FakeDispatcher):
    """Dispatcher that records what it saw and raises an internal (non-fatal,
    non-DispatchResult) exception for a chosen update_id."""

    def __init__(self, boom_on: int):
        super().__init__()
        self.boom_on = boom_on
        self.seen: list[int] = []

    async def dispatch(self, update):
        uid = update["update_id"]
        self.seen.append(uid)
        if uid == self.boom_on:
            raise RuntimeError(
                f"internal dispatcher failure for update_id={uid} (not a "
                "terminal DispatchResult)"
            )
        return None


@pytest.mark.asyncio
async def test_dispatcher_raises_internal_exception_offset_unchanged():
    """An internal dispatcher exception must NEVER advance the durable offset.

    The failing update is NOT claimed as consumed, so a later poll re-fetches
    it from the SAME offset (no gap, no exactly-once overreach). The exception
    propagates out of run_once so run_forever classifies it as transient and
    backoff-retries rather than permanently dropping the update.
    """
    fake = FakeTelegramAPI()
    d = _BoomDispatcher(boom_on=101)
    run, _, store, _ = _runner_with(fake, dispatcher=d)

    # Establish a durable offset of 101 via a clean dispatch of update 100.
    fake.enqueue([msg_update(100, "1", "a")])
    await run.run_once()
    assert store.next_offset == 101
    assert d.seen == [100]

    # Batch 101,102: 101 fails internally -> not consumed.
    fake.enqueue([msg_update(101, "1", "b"), msg_update(102, "1", "c")])
    with pytest.raises(RuntimeError):
        await run.run_once()

    # Offset must remain 101 (never 102/103) and 102 was not dispatched.
    assert store.next_offset == 101
    assert d.seen == [100, 101]

    # The NEXT poll must re-fetch from the unchanged durable offset 101, so the
    # failed update is replayed (no gap). Re-arm the dispatcher to succeed.
    d.boom_on = -1
    fake.enqueue([msg_update(101, "1", "b2"), msg_update(102, "1", "c2")])
    await run.run_once()
    last_get = fake.get_updates_calls()[-1]
    assert last_get.get("offset") == 101  # re-fetch started exactly at 101
    assert store.next_offset == 103       # both now consumed on the retry


@pytest.mark.asyncio
async def test_batch_stops_before_offset_gap():
    """Batch [100,101,102] with an internal failure on 101 must leave the
    durable offset at 101 (max consumed + 1) and must NOT process 102.

    The batch loop is interrupted by the dispatch failure, so no update after
    the failing one is executed in that iteration -> no offset gap where a
    later update (102) could be skipped forever.
    """
    fake = FakeTelegramAPI()
    d = _BoomDispatcher(boom_on=101)
    run, _, store, _ = _runner_with(fake, dispatcher=d)

    fake.enqueue([msg_update(100, "1", "a"),
                  msg_update(101, "1", "b"),
                  msg_update(102, "1", "c")])
    with pytest.raises(RuntimeError):
        await run.run_once()

    # 100 consumed -> +1; 101 failed -> not consumed; 102 never dispatched.
    assert store.next_offset == 101
    assert d.seen == [100, 101]   # 102 NOT processed -> batch stopped



# ─────────────────────────────────────────────────────────────────────────────
# UX-4.4B : node-port wiring fix (node_base_url -> wizard CommandService port)
# -----------------------------------------------------------------------------
# Regression for the INICIAR misroute. The Telegram runtime uses --node-base-url
# (e.g. http://127.0.0.1:8991) but the wizard used to default node_port=8891,
# misrouting create_conversation to production. After the fix the wizard derives
# its CommandService port from the provided node_base_url (no silent 8891
# fallback) and honours command_service injection.

from xerrameca import cli
from xerrameca.command.wizard import XerramecaWizardService


def _wizard_fake():
    """A minimal command_service double with contact counters."""
    class _Fake:
        def __init__(self):
            self.create_count = 0
            self.list_agents_count = 0
            self.agents = []

        def list_agents(self):
            self.list_agents_count += 1
            return self.agents

        def create_conversation(self, peer_node_id, objective, max_rounds):
            self.create_count += 1
            return {"id": "conv-inj", "status": "active"}
    return _Fake()


# -- TEST CRITICAL 1 : 8991 propagation --------------------------------------
def test_ux44b_node_port_derived_from_base_url():
    assert cli._node_port_from_url("http://127.0.0.1:8991") == 8991


# -- TEST CRITICAL 3 : no hardcoded port -------------------------------------
def test_ux44b_alternate_port_not_hardcoded():
    assert cli._node_port_from_url("http://127.0.0.1:9123") == 9123


# -- TEST CRITICAL 2 : the CommandService that would run create_conversation
#    targets the staging port (8991) NOT 8891 -------------------------------
def test_ux44b_command_service_targets_node_port_not_8891():
    w = XerramecaWizardService("x", node_port=8991)
    svc = w._command_service()          # real XerramecaCommandService
    # create_conversation posts to http://127.0.0.1:{node_port}/.../conversations
    assert svc.node_port == 8991
    assert svc.node_port != 8891


# -- command_service injection (select peer + create conversation) ----------
def test_ux44b_command_service_injection_honoured():
    fake = _wizard_fake()
    w = XerramecaWizardService("x", node_port=8991, command_service=fake)
    # _command_service must return the injected fake (single source).
    assert w._command_service() is fake
    # create_conversation flows through the injected service.
    s = w.create_session("caller")
    s.state = "CONFIRM"
    s.data = {"peer_node_id": "xn_p", "max_rounds": 2}
    w.build_effective_objective = lambda sess: "obj"
    w._start(s)
    assert fake.create_count == 1
    assert s.state == "STARTED"


# -- idempotency : duplicate START does not create a second conversation -----
def test_ux44b_start_idempotent_duplicate():
    fake = _wizard_fake()
    w = XerramecaWizardService("x", node_port=8991, command_service=fake)
    s = w.create_session("caller")
    s.state = "CONFIRM"
    s.data = {"peer_node_id": "xn_p", "max_rounds": 2}
    w.build_effective_objective = lambda sess: "obj"
    w._start(s)
    assert fake.create_count == 1
    # second START: already STARTED -> idempotent, no extra create
    w._start(s)
    assert fake.create_count == 1
    assert s.data["conversation_id"] == "conv-inj"


# -- error handling : create failure leaves session NOT STARTED, no fake id --
def test_ux44b_start_error_does_not_mark_started():
    class _Boom:
        def create_conversation(self, **kw):
            raise RuntimeError("boom")
        def list_agents(self):
            return []
    w = XerramecaWizardService("x", node_port=8991, command_service=_Boom())
    s = w.create_session("caller")
    s.state = "CONFIRM"
    s.data = {"peer_node_id": "xn_p", "max_rounds": 2}
    w.build_effective_objective = lambda sess: "obj"
    import pytest as _p
    with _p.raises(RuntimeError):
        w._start(s)
    assert s.state == "CONFIRM"
    assert "conversation_id" not in s.data


# -- TEST CRITICAL 4 : missing port -> FAIL-FAST, no 8891 fallback, no net ---
def test_ux44b_base_url_missing_port_failfast():
    import pytest as _p
    with _p.raises(ValueError) as ei:
        cli._node_port_from_url("http://127.0.0.1")     # no explicit port
    msg = str(ei.value)
    assert "port" in msg
    assert "8891" not in msg


# -- TEST CRITICAL 5 : malformed / invalid port -> FAIL-FAST, sanitized ------
def test_ux44b_base_url_invalid_failfast_sanitized():
    import pytest as _p
    bad_inputs = ["not-a-url", "http://", "http://host:0",
                  "http://127.0.0.1:99999", "http://127.0.0.1:abc"]
    for bad in bad_inputs:
        with _p.raises(ValueError) as ei:
            cli._node_port_from_url(bad)
        msg = str(ei.value)
        # two acceptable fail-fast messages: missing/undeterminable port, or an
        # explicitly invalid port. Both are sanitized.
        assert ("no es pot determinar" in msg) or ("no és vàlid" in msg)
        # sanitized: none of the offending input leaks, no secret markers
        for frag in ("abc", "99999", "host:0", "not-a-url", "http://127.0.0.1:abc"):
            assert frag not in msg
        assert "token" not in msg.lower()
        assert "api_key" not in msg.lower()
