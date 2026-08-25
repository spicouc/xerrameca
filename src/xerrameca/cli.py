from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn

from .config import settings
from .dashboard import create_dashboard_app
from .integrations.telegram_polling import TelegramPollingError
from .node.app import create_node_app
from .node.identity import initialize_node, load_node_state
from .node.supervisor import LocalSupervisor
from .node.trust import (
    accept_invite_over_http,
    create_invite,
    list_peers,
    revoke_peer,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xerrameca")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the existing standalone service")
    serve.add_argument("--host", default=settings.XERRAMECA_HOST)
    serve.add_argument("--port", type=int, default=settings.XERRAMECA_PORT)

    init = sub.add_parser("init", help="initialize durable per-agent node state")
    init.add_argument("--state-dir", required=True)
    init.add_argument("--agent-id", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--endpoint", default="http://127.0.0.1:8791")

    node = sub.add_parser("node", help="run a per-agent Xerrameca node")
    node.add_argument("--state-dir", required=True)
    node.add_argument("--host", default=settings.XERRAMECA_HOST)
    node.add_argument("--port", type=int, default=settings.XERRAMECA_PORT)

    dashboard = sub.add_parser("dashboard", help="run optional read-only dashboard")
    dashboard.add_argument("--state-dir", required=True)
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8792)

    info = sub.add_parser("node-info", help="show public durable node identity")
    info.add_argument("--state-dir", required=True)

    invite = sub.add_parser("invite", help="explicit peer trust bootstrap")
    invite_sub = invite.add_subparsers(dest="invite_command", required=True)
    invite_create = invite_sub.add_parser("create", help="create one bounded invite")
    invite_create.add_argument("--state-dir", required=True)
    invite_create.add_argument("--ttl", type=int, default=600)
    invite_accept = invite_sub.add_parser("accept", help="accept a peer invite")
    invite_accept.add_argument("--state-dir", required=True)
    invite_accept.add_argument("token")
    invite_accept.add_argument("--timeout", type=float, default=10.0)

    peer = sub.add_parser("peer", help="inspect/revoke trusted peers")
    peer_sub = peer.add_subparsers(dest="peer_command", required=True)
    peer_list = peer_sub.add_parser("list", help="list trusted peers")
    peer_list.add_argument("--state-dir", required=True)
    peer_revoke = peer_sub.add_parser("revoke", help="revoke one peer")
    peer_revoke.add_argument("--state-dir", required=True)
    peer_revoke.add_argument("node_id")

    supervisor = sub.add_parser("supervisor", help="inspect/recover local conversations")
    supervisor_sub = supervisor.add_subparsers(dest="supervisor_command", required=True)
    supervisor_inspect = supervisor_sub.add_parser("inspect", help="show passive findings/metrics")
    supervisor_inspect.add_argument("--state-dir", required=True)
    supervisor_inspect.add_argument("--conversation")
    supervisor_inspect.add_argument("--now", type=int)
    supervisor_recover = supervisor_sub.add_parser(
        "recover", help="explicitly reopen one expired claimed turn"
    )
    supervisor_recover.add_argument("--state-dir", required=True)
    supervisor_recover.add_argument("conversation_id")
    supervisor_recover.add_argument("--epoch", type=int, required=True)
    supervisor_recover.add_argument("--now", type=int)
    supervisor_recover.add_argument("--max-retries", type=int, default=3)

    conversation = sub.add_parser("conversation", help="federated conversation control")
    conv_sub = conversation.add_subparsers(dest="conversation_command", required=True)
    conv_create = conv_sub.add_parser("create", help="create a federated conversation")
    conv_create.add_argument("--state-dir", required=True)
    conv_create.add_argument("--peer", required=True)
    conv_create.add_argument("--objective", required=True)
    conv_create.add_argument("--rounds", type=int, default=5)
    conv_create.add_argument("--delay", type=int, default=0)
    conv_create.add_argument("--json", dest="as_json", action="store_true")
    conv_status = conv_sub.add_parser("status", help="show conversation status")
    conv_status.add_argument("--state-dir", required=True)
    conv_status.add_argument("conversation_id")
    conv_status.add_argument("--json", dest="as_json", action="store_true")
    conv_list = conv_sub.add_parser("list", help="list federated conversations")
    conv_list.add_argument("--state-dir", required=True)
    conv_list.add_argument("--status", default=None)
    conv_list.add_argument("--peer", default=None)
    conv_list.add_argument("--limit", type=int, default=None)
    conv_list.add_argument("--json", dest="as_json", action="store_true")
    conv_sync = conv_sub.add_parser("sync", help="sync a conversation from coordinator")
    conv_sync.add_argument("--state-dir", required=True)
    conv_sync.add_argument("conversation_id")
    conv_sync.add_argument("--json", dest="as_json", action="store_true")

    telegram = sub.add_parser(
        "telegram", help="run the Telegram long-polling runner (getUpdates)"
    )
    telegram.add_argument("--state-dir", required=True, help="durable node state dir")
    telegram.add_argument(
        "--node-base-url",
        required=True,
        help="explicit local node base URL, e.g. http://127.0.0.1:8791 "
        "(no implicit default; required by design)",
    )
    telegram.add_argument(
        "--token-file",
        required=True,
        help="path to the Telegram bot token file (NOT a --token argument; "
        "the credential is never accepted or echo'd on the command line)",
    )
    telegram.add_argument(
        "--allowed-chat-id",
        action="append",
        default=None,
        metavar="ID",
        help="telegram chat id allowed to act (repeatable). When omitted, every "
        "chat may act (UX-4.3 allowlist policy is authoritative).",
    )
    telegram.add_argument(
        "--poll-timeout",
        type=int,
        default=30,
        help="positive long-poll timeout in seconds (default 30)",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "serve":
        uvicorn.run("xerrameca.app:app", host=args.host, port=args.port)
        return 0

    if args.command == "init":
        state = initialize_node(
            args.state_dir,
            agent_id=args.agent_id,
            display_name=args.name,
            endpoint=args.endpoint,
        )
        print(
            json.dumps(
                {
                    **state.public_dict(),
                    "state_dir": state.state_dir,
                    "local_api_key_path": state.local_api_key_path,
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "node":
        app = create_node_app(args.state_dir)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    if args.command == "dashboard":
        app = create_dashboard_app(args.state_dir)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    if args.command == "node-info":
        print(json.dumps(load_node_state(args.state_dir).public_dict(), sort_keys=True))
        return 0

    if args.command == "invite" and args.invite_command == "create":
        print(create_invite(args.state_dir, ttl_seconds=args.ttl))
        return 0

    if args.command == "invite" and args.invite_command == "accept":
        peer = asyncio.run(
            accept_invite_over_http(
                args.state_dir,
                args.token,
                timeout_seconds=args.timeout,
            )
        )
        print(json.dumps(peer.public_dict(), sort_keys=True))
        return 0

    if args.command == "peer" and args.peer_command == "list":
        print(
            json.dumps(
                [peer.public_dict() for peer in list_peers(args.state_dir)],
                sort_keys=True,
            )
        )
        return 0

    if args.command == "peer" and args.peer_command == "revoke":
        peer = revoke_peer(args.state_dir, args.node_id)
        print(json.dumps(peer.public_dict(), sort_keys=True))
        return 0

    if args.command == "supervisor" and args.supervisor_command == "inspect":
        supervisor = LocalSupervisor(args.state_dir)
        payload = (
            supervisor.inspect(args.conversation, now=args.now)
            if args.conversation
            else supervisor.inspect_all(now=args.now)
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "supervisor" and args.supervisor_command == "recover":
        supervisor = LocalSupervisor(
            args.state_dir, max_lease_retries=args.max_retries
        )
        payload = supervisor.recover_expired_lease(
            args.conversation_id,
            expected_epoch=args.epoch,
            now=args.now,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "conversation" and args.conversation_command == "create":
        from .command.service import XerramecaCommandService

        svc = XerramecaCommandService(args.state_dir)
        result = svc.create_conversation(
            peer_node_id=args.peer,
            objective=args.objective,
            max_rounds=args.rounds,
            delay_seconds=args.delay,
        )
        if args.as_json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(f"Creat: {result.get('id')}")
            print(f"Peer: {args.peer}")
            print(f"Objectiu: {args.objective}")
            print(f"Rondes: {args.rounds}")
            print(f"Status: {result.get('status', 'active')}")
        return 0

    if args.command == "conversation" and args.conversation_command == "status":
        from .command.service import XerramecaCommandService

        svc = XerramecaCommandService(args.state_dir)
        result = svc.get_conversation(args.conversation_id)
        if args.as_json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(f"Xerrameca #{result.get('id')} — {str(result.get('status','?')).upper()}")
            print(f"Ronda {result.get('current_round','?')}/{result.get('max_rounds','?')}")
            print(f"Objectiu: {result.get('objective','')}")
            msgs = result.get('messages') or []
            if msgs:
                m = msgs[-1]
                print(f"Últim: {m.get('author_node_id')}: {m.get('content')}")
        return 0

    if args.command == "conversation" and args.conversation_command == "list":
        from .command.service import XerramecaCommandService

        svc = XerramecaCommandService(args.state_dir)
        items = svc.list_conversations(status=args.status, peer_node_id=args.peer, limit=args.limit)
        if args.as_json:
            print(json.dumps([i.to_dict() for i in items], sort_keys=True))
        else:
            if not items:
                print("Cap conversa federada.")
            for item in items:
                print(f"{item.id}  [{item.status}]  ronda {item.current_round}/{item.max_rounds}  {item.objective}")
        return 0

    if args.command == "conversation" and args.conversation_command == "sync":
        from .command.service import XerramecaCommandService

        svc = XerramecaCommandService(args.state_dir)
        result = svc.sync_conversation(args.conversation_id)
        if args.as_json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(f"Sincronitzat: {result.get('id')}")
            print(f"Status: {result.get('status','?')}")
            print(f"Replication: {result.get('replication_status','n/a')}")
        return 0

    if args.command == "telegram":
        asyncio.run(run_telegram_forever(args))
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


# ---------------------------------------------------------------------------
# `xerrameca telegram` runtime helper (UX-4.4A)
# ---------------------------------------------------------------------------
_WRITE_BITS = stat.S_IWGRP | stat.S_IWOTH


def _read_telegram_token(token_file: str) -> str:
    """Read, strip and validate the bot token from ``--token-file``.

    - Token CLI argument is PROHIBITED: credentials come only from a file.
    - Insecure permissions (group/world writable) FAIL FAST with the sanitised
      message ``"Telegram credential unavailable"`` (no path, no token).
    - Empty / blank tokens are rejected. The token is kept only in memory and
      is never persisted, logged, printed or stored.
    """
    path = Path(token_file)
    try:
        st = path.stat()
    except OSError:
        raise TelegramPollingError(
            "Telegram credential unavailable", fatal=True
        ) from None
    if st.st_mode & _WRITE_BITS:
        raise TelegramPollingError(
            "Telegram credential unavailable", fatal=True
        ) from None
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise TelegramPollingError(
            "Telegram credential unavailable", fatal=True
        ) from None
    if not token:
        raise TelegramPollingError(
            "Telegram credential unavailable", fatal=True
        ) from None
    return token


def _read_local_api_key(state_dir: str) -> str:
    """Read the node-local API key from state-dir (runtime only).

    NEVER generated, copied, printed or stored in any Telegram config; it is
    read from the existing durable ``local-agent-api-key`` material so the
    Telegram runtime can talk to the local node as an authorised agent.
    """
    from .node.identity import LOCAL_API_KEY_FILENAME

    key_path = Path(state_dir) / LOCAL_API_KEY_FILENAME
    try:
        token = key_path.read_text(encoding="utf-8").strip()
    except OSError:
        raise TelegramPollingError(
            "Node credential unavailable", fatal=True
        ) from None
    if not token:
        raise TelegramPollingError(
            "Node credential unavailable", fatal=True
        ) from None
    return token


def _node_port_from_url(node_base_url: str) -> int:
    """Derive the node HTTP port from the --node-base-url.

    The Telegram runtime must replay CommandService operations to the SAME
    staging node it polls (--node-base-url). We require an explicit, valid
    port; any missing/invalid/malformed port FAILS FAST rather than silently
    falling back to the default 8891 (which would misroute staging writes at
    production). Error stays sanitized (no URL, no token, no API key).
    """
    parts = urlsplit(node_base_url or "")
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        # accessing parts.port raises for out-of-range/invalid ports
        raise ValueError("el port del node base URL no és vàlid")
    if hostname is None or port is None:
        raise ValueError("no es pot determinar el port del node base URL")
    if not (0 < port <= 65535):
        raise ValueError("el port del node base URL no és vàlid")
    return int(port)


def _build_telegram_stack(args):
    """Build the full Telegram polling runtime from CLI args.

    Returns (runner, dispatcher, client, offset_store). Wiring:
    Token -> TelegramGetUpdatesClient; OffsetStore + Runner -> the rest.
    """
    from .command.wizard import XerramecaWizardService
    from .integrations.telegram import TelegramUXAdapter
    from .integrations.telegram_bot_api import TelegramBotAPITransport
    from .integrations.telegram_polling import (
        TelegramGetUpdatesClient,
        TelegramOffsetStore,
        TelegramPollingRunner,
    )
    from .integrations.telegram_updates import TelegramUpdateDispatcher
    from .ui import CallbackStore, TelegramWizardBridge

    token = _read_telegram_token(args.token_file)
    api_key = _read_local_api_key(args.state_dir)

    state_dir = args.state_dir
    callbacks = CallbackStore()
    node_port = _node_port_from_url(args.node_base_url)
    wizard = XerramecaWizardService(state_dir, node_port=node_port)
    bridge = TelegramWizardBridge(wizard, callbacks)
    transport = TelegramBotAPITransport(token=token)
    adapter = TelegramUXAdapter(
        node_base_url=args.node_base_url,
        api_key=api_key,
        transport=transport,
        wizard=bridge,
    )
    allowed = set(args.allowed_chat_id) if args.allowed_chat_id else None
    dispatcher = TelegramUpdateDispatcher(adapter, allowed_chat_ids=allowed)
    client = TelegramGetUpdatesClient(
        token=token,
        poll_timeout=args.poll_timeout,
    )
    offset_store = TelegramOffsetStore(state_dir)
    runner = TelegramPollingRunner(
        client=client,
        dispatcher=dispatcher,
        offset_store=offset_store,
        state_dir=state_dir,
    )
    return runner, dispatcher, client, offset_store


async def run_telegram_forever(args) -> int:
    """Acquire the runner lock and poll until cancelled / fatal error.

    Ctrl+C / asyncio cancel propagates cleanly; the exclusive lock is always
    released via the runner's lifecycle (finally).
    """
    runner, dispatcher, client, offset_store = _build_telegram_stack(args)
    runner.acquire_lock()
    try:
        await runner.run_forever()
    finally:
        runner.release_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
