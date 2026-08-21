from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request

from ..adapters.local_identity import LocalIdentityAdapter
from ..app import create_app
from ..domain.errors import ForbiddenError, ProviderUnavailableError, ValidationError
from .dialogue_transport import RemoteDialogueClient
from .failover import DEFAULT_LEASE_SECONDS, LeasedDialogueService
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
    dialogue = LeasedDialogueService(state.state_dir)
    failover = dialogue.failover
    remote_dialogue = RemoteDialogueClient(state.state_dir)
    app.state.node_state = state
    app.state.replication_service = replication
    app.state.federated_dialogue = dialogue
    app.state.failover_manager = failover

    # Add the X12 local failover loop without replacing the base application's
    # database/summary-dispatcher lifespan.
    base_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def node_lifespan(node_app: FastAPI):
        async with base_lifespan(node_app):
            failover_task = asyncio.create_task(failover.loop(replication))
            try:
                yield
            finally:
                failover_task.cancel()
                await asyncio.gather(failover_task, return_exceptions=True)

    app.router.lifespan_context = node_lifespan

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

    async def authenticate_local_agent(request: Request) -> None:
        api_key = request.headers.get("X-API-Key") or ""
        hint = request.headers.get("X-Agent-ID")
        caller = await identity.authenticate(api_key, agent_id_hint=hint)
        if caller.id != state.agent_id:
            raise ForbiddenError("credential does not belong to this node agent")

    async def best_effort_push(conversation: dict[str, Any]) -> str:
        if conversation["coordinator_id"] != state.node_id:
            return "not-coordinator"
        peers = [
            participant["node_id"]
            for participant in conversation["participants"]
            if participant["node_id"] != state.node_id
        ]
        if len(peers) != 1:
            return "invalid-participants"
        try:
            await replication.push_missing(
                peers[0],
                conversation["id"],
                epoch=int(conversation["coordinator_epoch"]),
            )
            return "synced"
        except ProviderUnavailableError:
            return "pending"

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

    # ------------------------------------------------------------------
    # Local-agent federated dialogue surface.
    # ------------------------------------------------------------------

    @app.post("/v1/node/federation/conversations", tags=["federated-dialogue"])
    async def create_federated_conversation(
        request: Request, body: dict[str, Any]
    ) -> dict[str, Any]:
        await authenticate_local_agent(request)
        try:
            peer_node_id = str(body["peer_node_id"])
            objective = str(body["objective"])
        except (KeyError, TypeError) as exc:
            raise ValidationError("peer_node_id and objective are required") from exc
        view = dialogue.create(
            peer_node_id,
            objective=objective,
            name=str(body.get("name") or "Xerrameca"),
            max_rounds=int(body.get("max_rounds", 5)),
            turn_timeout_seconds=int(body.get("turn_timeout_seconds", 300)),
            delay_seconds=int(body.get("delay_seconds", 0)),
        )
        lease_seconds = int(body.get("coordinator_lease_seconds", DEFAULT_LEASE_SECONDS))
        if lease_seconds > 0:
            failover.grant_initial_lease(view.id, lease_seconds=lease_seconds)
            view = dialogue.get(view.id)
        payload = view.to_dict()
        payload["replication_status"] = await best_effort_push(payload)
        if lease_seconds > 0:
            payload["coordinator_lease"] = failover.status(view.id).to_dict()
        return payload

    @app.get(
        "/v1/node/federation/conversations/{conversation_id}",
        tags=["federated-dialogue"],
    )
    async def get_federated_conversation(
        conversation_id: str, request: Request
    ) -> dict[str, Any]:
        await authenticate_local_agent(request)
        payload = dialogue.get(conversation_id).to_dict()
        lease = failover.status(conversation_id)
        if lease.enabled:
            payload["coordinator_lease"] = lease.to_dict()
        return payload

    @app.get(
        "/v1/node/federation/conversations",
        tags=["federated-dialogue"],
    )
    async def list_federated_conversations(
        request: Request,
        status: str | None = None,
        peer_node_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        await authenticate_local_agent(request)
        cids = dialogue.list_conversations()
        summaries: list[dict[str, Any]] = []
        for cid in cids:
            view = dialogue.get(cid)
            if status is not None and view.status != status:
                continue
            if peer_node_id is not None and peer_node_id not in view.participant_node_ids:
                continue
            summaries.append(
                {
                    "id": view.id,
                    "objective": view.objective,
                    "status": view.status,
                    "coordinator_id": view.coordinator_id,
                    "coordinator_epoch": view.coordinator_epoch,
                    "current_round": view.current_round,
                    "max_rounds": view.max_rounds,
                    "participants": view.participant_node_ids,
                }
            )
        if limit is not None:
            summaries = summaries[:limit]
        return {"conversations": summaries, "count": len(summaries)}

    @app.post(
        "/v1/node/federation/conversations/{conversation_id}/claim",
        tags=["federated-dialogue"],
    )
    async def claim_federated_turn(
        conversation_id: str, request: Request, body: dict[str, Any]
    ) -> dict[str, Any]:
        await authenticate_local_agent(request)
        view = dialogue.get(conversation_id)
        expected_epoch = int(body.get("expected_epoch", view.coordinator_epoch))
        if view.coordinator_id == state.node_id:
            claimed = dialogue.claim(
                conversation_id,
                claimant_node_id=state.node_id,
                expected_epoch=expected_epoch,
            )
            payload = claimed.to_dict()
            payload["replication_status"] = await best_effort_push(payload)
            return payload
        return (
            await remote_dialogue.claim(
                view.coordinator_id,
                conversation_id,
                expected_epoch=expected_epoch,
            )
        ).to_dict()

    @app.post(
        "/v1/node/federation/conversations/{conversation_id}/reply",
        tags=["federated-dialogue"],
    )
    async def reply_federated_turn(
        conversation_id: str, request: Request, body: dict[str, Any]
    ) -> dict[str, Any]:
        await authenticate_local_agent(request)
        try:
            content = str(body["content"])
            result = str(body["result"])
        except (KeyError, TypeError) as exc:
            raise ValidationError("content and result are required") from exc
        view = dialogue.get(conversation_id)
        expected_epoch = int(body.get("expected_epoch", view.coordinator_epoch))
        if view.coordinator_id == state.node_id:
            replied = dialogue.reply(
                conversation_id,
                author_node_id=state.node_id,
                content=content,
                result=result,
                expected_epoch=expected_epoch,
            )
            payload = replied.to_dict()
            payload["replication_status"] = await best_effort_push(payload)
            return payload
        return (
            await remote_dialogue.reply(
                view.coordinator_id,
                conversation_id,
                expected_epoch=expected_epoch,
                content=content,
                result=result,
            )
        ).to_dict()

    @app.post(
        "/v1/node/federation/conversations/{conversation_id}/sync",
        tags=["federated-dialogue"],
    )
    async def sync_federated_conversation(
        conversation_id: str, request: Request
    ) -> dict[str, Any]:
        await authenticate_local_agent(request)
        view = dialogue.get(conversation_id)
        if view.coordinator_id == state.node_id:
            payload = view.to_dict()
            payload["replication_status"] = await best_effort_push(payload)
            return payload

        head = dialogue.store.get_head(conversation_id)
        await replication.fetch_range(
            view.coordinator_id,
            conversation_id,
            epoch=head.coordinator_epoch,
            from_sequence=head.last_sequence + 1,
        )
        return dialogue.get(conversation_id).to_dict()

    # ------------------------------------------------------------------
    # Signed participant -> coordinator actions.
    # ------------------------------------------------------------------

    @app.post("/v1/node/federation/dialogue/claim", tags=["federated-dialogue"])
    async def peer_dialogue_claim(request: Request) -> dict[str, Any]:
        body = await request.body()
        peer = await authenticate_peer(request, body)
        try:
            raw = json.loads(body)
            conversation_id = str(raw["conversation_id"])
            expected_epoch = int(raw["expected_epoch"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid federated claim body") from exc
        before = dialogue.store.get_head(conversation_id)
        view = dialogue.claim(
            conversation_id,
            claimant_node_id=peer.node_id,
            expected_epoch=expected_epoch,
        )
        after = dialogue.store.get_head(conversation_id)
        events = (
            dialogue.store.list_events(
                conversation_id,
                epoch=after.coordinator_epoch,
                from_sequence=before.last_sequence + 1,
            )
            if after.coordinator_epoch == before.coordinator_epoch
            else dialogue.store.list_events(
                conversation_id,
                epoch=after.coordinator_epoch,
                from_sequence=1,
            )
        )
        return {
            "conversation": view.to_dict(),
            "events": [event.to_dict() for event in events],
        }

    @app.post("/v1/node/federation/dialogue/reply", tags=["federated-dialogue"])
    async def peer_dialogue_reply(request: Request) -> dict[str, Any]:
        body = await request.body()
        peer = await authenticate_peer(request, body)
        try:
            raw = json.loads(body)
            conversation_id = str(raw["conversation_id"])
            expected_epoch = int(raw["expected_epoch"])
            content = str(raw["content"])
            result = str(raw["result"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid federated reply body") from exc
        before = dialogue.store.get_head(conversation_id)
        view = dialogue.reply(
            conversation_id,
            author_node_id=peer.node_id,
            content=content,
            result=result,
            expected_epoch=expected_epoch,
        )
        after = dialogue.store.get_head(conversation_id)
        events = (
            dialogue.store.list_events(
                conversation_id,
                epoch=after.coordinator_epoch,
                from_sequence=before.last_sequence + 1,
            )
            if after.coordinator_epoch == before.coordinator_epoch
            else dialogue.store.list_events(
                conversation_id,
                epoch=after.coordinator_epoch,
                from_sequence=1,
            )
        )
        return {
            "conversation": view.to_dict(),
            "events": [event.to_dict() for event in events],
        }

    # ------------------------------------------------------------------
    # X12 coordinator lease/failover control and observability.
    # ------------------------------------------------------------------

    @app.post("/v1/node/failover/tick", tags=["failover"])
    async def failover_tick(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        await authenticate_local_agent(request)
        actions = await failover.tick(
            replication,
            lease_seconds=int(body.get("lease_seconds", DEFAULT_LEASE_SECONDS)),
            renew_before_seconds=int(body.get("renew_before_seconds", 40)),
            grace_seconds=int(body.get("grace_seconds", 5)),
        )
        return {"actions": actions}

    @app.get("/v1/node/failover/{conversation_id}", tags=["failover"])
    async def failover_status(conversation_id: str, request: Request) -> dict[str, Any]:
        await authenticate_local_agent(request)
        return failover.status(conversation_id).to_dict()

    @app.post("/v1/node/failover/{conversation_id}/renew", tags=["failover"])
    async def failover_renew(
        conversation_id: str, request: Request, body: dict[str, Any]
    ) -> dict[str, Any]:
        await authenticate_local_agent(request)
        failover.renew_lease(
            conversation_id,
            lease_seconds=int(body.get("lease_seconds", DEFAULT_LEASE_SECONDS)),
        )
        view = dialogue.get(conversation_id).to_dict()
        replication_status = await best_effort_push(view)
        return {
            "lease": failover.status(conversation_id).to_dict(),
            "replication_status": replication_status,
        }

    @app.post("/v1/node/failover/{conversation_id}/takeover", tags=["failover"])
    async def failover_takeover(
        conversation_id: str, request: Request, body: dict[str, Any]
    ) -> dict[str, Any]:
        await authenticate_local_agent(request)
        takeover = failover.takeover(
            conversation_id,
            lease_seconds=int(body.get("lease_seconds", DEFAULT_LEASE_SECONDS)),
            grace_seconds=int(body.get("grace_seconds", 5)),
        )
        view = dialogue.get(conversation_id).to_dict()
        replication_status = await best_effort_push(view)
        return {
            "lease": takeover.to_dict(),
            "conversation": view,
            "replication_status": replication_status,
        }

    return app
