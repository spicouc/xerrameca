from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from ..domain.models import ReplyRequest
from ..ports.identity import AgentIdentity


router = APIRouter(prefix="/v1/xerrameca", tags=["xerrameca"])


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=100_000)


async def _caller(
    request: Request,
    api_key: str,
    agent_id_hint: str | None,
) -> AgentIdentity:
    return await request.app.state.gateway.authenticate(
        api_key, agent_id_hint=agent_id_hint
    )


@router.post("/command")
async def command(
    body: CommandRequest,
    request: Request,
    api_key: Annotated[str, Header(alias="X-API-Key")],
    agent_id_hint: Annotated[str | None, Header(alias="X-Agent-ID")] = None,
) -> dict[str, Any]:
    caller = await _caller(request, api_key, agent_id_hint)
    return await request.app.state.gateway.command(
        caller, body.command, credential=api_key
    )


@router.get("/inbox")
async def inbox(
    request: Request,
    api_key: Annotated[str, Header(alias="X-API-Key")],
    agent_id_hint: Annotated[str | None, Header(alias="X-Agent-ID")] = None,
) -> dict[str, Any]:
    caller = await _caller(request, api_key, agent_id_hint)
    return await request.app.state.gateway.inbox(caller)


@router.post("/turns/{turn_id}/claim")
async def claim(
    turn_id: str,
    request: Request,
    api_key: Annotated[str, Header(alias="X-API-Key")],
    agent_id_hint: Annotated[str | None, Header(alias="X-Agent-ID")] = None,
) -> dict[str, Any]:
    caller = await _caller(request, api_key, agent_id_hint)
    return await request.app.state.gateway.claim(caller, turn_id)


@router.post("/turns/{turn_id}/reply")
async def reply(
    turn_id: str,
    body: ReplyRequest,
    request: Request,
    api_key: Annotated[str, Header(alias="X-API-Key")],
    agent_id_hint: Annotated[str | None, Header(alias="X-Agent-ID")] = None,
) -> dict[str, Any]:
    caller = await _caller(request, api_key, agent_id_hint)
    return await request.app.state.gateway.reply(caller, turn_id, body)


@router.get("/conversations")
async def conversations(
    request: Request,
    api_key: Annotated[str, Header(alias="X-API-Key")],
    agent_id_hint: Annotated[str | None, Header(alias="X-Agent-ID")] = None,
) -> list[dict[str, Any]]:
    caller = await _caller(request, api_key, agent_id_hint)
    return await request.app.state.gateway.engine.list_conversations(caller)


@router.get("/conversations/{conversation_id}")
async def conversation(
    conversation_id: str,
    request: Request,
    api_key: Annotated[str, Header(alias="X-API-Key")],
    agent_id_hint: Annotated[str | None, Header(alias="X-Agent-ID")] = None,
) -> dict[str, Any]:
    caller = await _caller(request, api_key, agent_id_hint)
    return await request.app.state.gateway.engine.get_conversation(
        caller, conversation_id
    )


@router.get("/conversations/{conversation_id}/messages")
async def messages(
    conversation_id: str,
    request: Request,
    api_key: Annotated[str, Header(alias="X-API-Key")],
    agent_id_hint: Annotated[str | None, Header(alias="X-Agent-ID")] = None,
) -> list[dict[str, Any]]:
    caller = await _caller(request, api_key, agent_id_hint)
    return await request.app.state.gateway.engine.list_messages(
        caller, conversation_id
    )
