from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

import uvicorn

from .config import settings
from .dashboard import create_dashboard_app
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

    raise AssertionError(f"unhandled command: {args.command}")
