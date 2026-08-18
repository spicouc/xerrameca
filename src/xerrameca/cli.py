from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

import uvicorn

from .config import settings
from .node.app import create_node_app
from .node.identity import initialize_node, load_node_state


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

    info = sub.add_parser("node-info", help="show public durable node identity")
    info.add_argument("--state-dir", required=True)

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

    if args.command == "node-info":
        print(json.dumps(load_node_state(args.state_dir).public_dict(), sort_keys=True))
        return 0

    raise AssertionError(f"unhandled command: {args.command}")
