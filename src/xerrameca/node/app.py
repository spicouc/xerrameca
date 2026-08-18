from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request

from ..adapters.local_identity import LocalIdentityAdapter
from ..app import create_app
from ..domain.errors import ForbiddenError
from .identity import NodeState, load_node_state
from .trust import accept_incoming, verify_peer_request


def create_node_app(state_dir: str) -> FastAPI:
    """Create a per-agent node app from durable local state."""

    state: NodeState = load_node_state(state_dir)
    identity = LocalIdentityAdapter(state.local_identity_path)
    app = create_app(
        identity=identity,
        db_path=state.db_path,
        identity_provider_name="local-node",
    )
    app.state.node_state = state

    @app.get("/v1/node/identity", tags=["node"])
    async def node_identity() -> dict[str, str]:
        return state.public_dict()

    @app.post("/v1/node/invites/accept", tags=["node"])
    async def invite_acceptance(acceptance: dict[str, Any]) -> dict[str, Any]:
        return accept_incoming(state.state_dir, acceptance)

    @app.post("/v1/node/peer/ping", tags=["node"])
    async def peer_ping(request: Request) -> dict[str, str]:
        node_id = request.headers.get("X-Xerrameca-Node")
        timestamp = request.headers.get("X-Xerrameca-Timestamp")
        signature = request.headers.get("X-Xerrameca-Signature")
        if not node_id or not timestamp or not signature:
            raise ForbiddenError("missing peer authentication headers")
        body = await request.body()
        peer = verify_peer_request(
            state.state_dir,
            method=request.method,
            path=request.url.path,
            body=body,
            node_id=node_id,
            timestamp=timestamp,
            signature=signature,
        )
        return {
            "status": "ok",
            "node_id": state.node_id,
            "peer_node_id": peer.node_id,
        }

    return app
