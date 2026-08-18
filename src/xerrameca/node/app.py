from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request

from ..adapters.local_identity import LocalIdentityAdapter
from ..app import create_app
from ..domain.errors import ForbiddenError, ValidationError
from .identity import NodeState, load_node_state
from .replication import ReplicationService
from .trust import PeerRecord, accept_incoming, verify_peer_request


def create_node_app(state_dir: str) -> FastAPI:
    """Create a per-agent node app from durable local state."""

    state: NodeState = load_node_state(state_dir)
    identity = LocalIdentityAdapter(state.local_identity_path)
    app = create_app(
        identity=identity,
        db_path=state.db_path,
        identity_provider_name="local-node",
    )
    replication = ReplicationService(state.state_dir)
    app.state.node_state = state
    app.state.replication_service = replication

    async def authenticate_peer(request: Request, body: bytes) -> PeerRecord:
        node_id = request.headers.get("X-Xerrameca-Node")
        timestamp = request.headers.get("X-Xerrameca-Timestamp")
        signature = request.headers.get("X-Xerrameca-Signature")
        if not node_id or not timestamp or not signature:
            raise ForbiddenError("missing peer authentication headers")
        return verify_peer_request(
            state.state_dir,
            method=request.method,
            path=request.url.path,
            body=body,
            node_id=node_id,
            timestamp=timestamp,
            signature=signature,
        )

    @app.get("/v1/node/identity", tags=["node"])
    async def node_identity() -> dict[str, str]:
        return state.public_dict()

    @app.post("/v1/node/invites/accept", tags=["node"])
    async def invite_acceptance(acceptance: dict[str, Any]) -> dict[str, Any]:
        return accept_incoming(state.state_dir, acceptance)

    @app.post("/v1/node/peer/ping", tags=["node"])
    async def peer_ping(request: Request) -> dict[str, str]:
        body = await request.body()
        peer = await authenticate_peer(request, body)
        return {
            "status": "ok",
            "node_id": state.node_id,
            "peer_node_id": peer.node_id,
        }

    @app.post("/v1/node/federation/events", tags=["federation"])
    async def receive_federated_events(request: Request) -> dict[str, int | str]:
        body = await request.body()
        peer = await authenticate_peer(request, body)
        try:
            raw = json.loads(body)
            rows = raw["events"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValidationError("invalid replication request body") from exc
        if not isinstance(rows, list):
            raise ValidationError("replication events must be a list")
        ack = replication.receive_events(sender_node_id=peer.node_id, raw_events=rows)
        return ack.to_dict()

    @app.get(
        "/v1/node/federation/conversations/{conversation_id}/events",
        tags=["federation"],
    )
    async def federated_event_range(
        conversation_id: str,
        request: Request,
        epoch: int,
        from_sequence: int = 1,
        to_sequence: int | None = None,
    ) -> dict[str, object]:
        await authenticate_peer(request, b"")
        events = replication.event_range(
            conversation_id,
            epoch=epoch,
            from_sequence=from_sequence,
            to_sequence=to_sequence,
        )
        return {"events": [event.to_dict() for event in events]}

    @app.post("/v1/node/federation/ack", tags=["federation"])
    async def receive_federated_ack(request: Request) -> dict[str, int | str]:
        body = await request.body()
        peer = await authenticate_peer(request, body)
        try:
            raw = json.loads(body)
            conversation_id = str(raw["conversation_id"])
            epoch = int(raw["coordinator_epoch"])
            acked_sequence = int(raw["acked_sequence"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid replication ACK body") from exc
        ack = replication.receive_ack(
            sender_node_id=peer.node_id,
            conversation_id=conversation_id,
            coordinator_epoch=epoch,
            acked_sequence=acked_sequence,
        )
        return ack.to_dict()

    return app
