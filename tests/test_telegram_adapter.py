from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from xerrameca.integrations.telegram import TelegramMode, TelegramUXAdapter
from xerrameca.node.app import create_node_app
from xerrameca.node.identity import LOCAL_API_KEY_FILENAME, initialize_node
from xerrameca.node.trust import (
    accept_incoming,
    build_acceptance,
    complete_acceptance,
    create_invite,
)


class FakeTelegramTransport:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.messages.append((chat_id, text))


def _trusted_nodes(tmp_path: Path):
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a = initialize_node(
        a_dir,
        agent_id="agent-a",
        display_name="Agent A",
        endpoint="http://node-a.invalid:8791",
    )
    b = initialize_node(
        b_dir,
        agent_id="agent-b",
        display_name="Agent B",
        endpoint="http://node-b.invalid:8791",
    )
    token = create_invite(a_dir, ttl_seconds=600)
    acceptance, _ = build_acceptance(b_dir, token)
    confirmation = accept_incoming(a_dir, acceptance)
    complete_acceptance(b_dir, token, confirmation)
    return a_dir, b_dir, a, b


@pytest.mark.asyncio
async def test_telegram_adapter_starts_and_follows_real_node_conversation(tmp_path: Path) -> None:
    a_dir, _, _, b = _trusted_nodes(tmp_path)
    app = create_node_app(str(a_dir))
    api_key = (a_dir / LOCAL_API_KEY_FILENAME).read_text(encoding="utf-8").strip()
    transport = FakeTelegramTransport()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = TelegramUXAdapter(
            node_base_url="http://testserver",
            api_key=api_key,
            transport=transport,
            client=client,
        )
        started = await adapter.handle_text(
            "chat-1", f"/xerrameca start {b.node_id} Decide a simple protocol"
        )
        assert started is not None
        cid = str(started["id"])
        assert started["status"] == "active"
        assert transport.messages[-1][0] == "chat-1"
        assert cid in transport.messages[-1][1]
        assert api_key not in transport.messages[-1][1]

        await adapter.handle_text("chat-1", f"/xerrameca mode {cid} live")
        assert adapter.mode_for(cid) is TelegramMode.LIVE

        status = await adapter.handle_text("chat-1", f"/xerrameca status {cid}")
        assert status is not None
        assert status["id"] == cid
        assert api_key not in "\n".join(text for _, text in transport.messages)


@pytest.mark.asyncio
async def test_silent_summary_live_modes_do_not_change_runtime_state(tmp_path: Path) -> None:
    a_dir, _, _, b = _trusted_nodes(tmp_path)
    app = create_node_app(str(a_dir))
    api_key = (a_dir / LOCAL_API_KEY_FILENAME).read_text(encoding="utf-8").strip()
    transport = FakeTelegramTransport()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = TelegramUXAdapter(
            node_base_url="http://testserver",
            api_key=api_key,
            transport=transport,
            client=client,
        )
        conversation = await adapter.start(
            peer_node_id=b.node_id, objective="Observe without coupling"
        )
        cid = str(conversation["id"])
        before = await adapter.status(cid)

        silent = adapter.render(before, mode=TelegramMode.SILENT)
        summary = adapter.render(before, mode=TelegramMode.SUMMARY)
        live = adapter.render(before, mode=TelegramMode.LIVE)
        after = await adapter.status(cid)

        assert "ACTIVE" in silent
        assert "Objectiu:" in summary
        assert "Objectiu:" in live
        assert before == after
        assert api_key not in silent + summary + live
