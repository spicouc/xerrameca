from __future__ import annotations

from fastapi import FastAPI

from ..adapters.local_identity import LocalIdentityAdapter
from ..app import create_app
from .identity import NodeState, load_node_state


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

    return app
