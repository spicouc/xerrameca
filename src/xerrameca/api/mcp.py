from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..domain.errors import XerramecaError
from ..domain.models import ReplyRequest


router = APIRouter(prefix="/mcp", tags=["mcp"])

TOOLS = [
    {
        "name": "xerrameca_command",
        "description": "Executa la interfície uniforme `/xerrameca`: help, agents, status, consulta, stop o inici amb rounds/timeout/delay/supervisor.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "xerrameca_inbox",
        "description": "Llista els torns Xerrameca disponibles per a l'agent autenticat, respectant l'espera entre torns.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "xerrameca_claim",
        "description": "Reclama atòmicament un torn i obté lease + context Dialogue Protocol v1.",
        "input_schema": {
            "type": "object",
            "properties": {"turn_id": {"type": "string"}},
            "required": ["turn_id"],
        },
    },
    {
        "name": "xerrameca_reply",
        "description": "Respon un torn reclamat. En alternating, complete proposa tancament i l'altre agent l'ha de confirmar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "turn_id": {"type": "string"},
                "lease_token": {"type": "string"},
                "content": {"type": "string"},
                "result": {
                    "type": "string",
                    "enum": ["continue", "complete", "blocked", "needs_human", "error"],
                    "default": "continue",
                },
                "next_agent_id": {"type": "string"},
                "metadata": {"type": "object", "default": {}},
            },
            "required": ["turn_id", "lease_token", "content"],
        },
    },
    {
        "name": "xerrameca_list",
        "description": "Llista les Xerrameques visibles per a l'agent.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "xerrameca_get",
        "description": "Obté l'estat i protocol d'una Xerrameca on l'agent participa.",
        "input_schema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"}},
            "required": ["conversation_id"],
        },
    },
    {
        "name": "xerrameca_messages",
        "description": "Obté l'historial estructurat d'una Xerrameca visible.",
        "input_schema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"}},
            "required": ["conversation_id"],
        },
    },
]

TOOL_NAMES = {tool["name"] for tool in TOOLS}


def _success(result: Any, id_: Any = None) -> JSONResponse:
    return JSONResponse(content={"jsonrpc": "2.0", "result": result, "id": id_})


def _error(code: int, message: str, id_: Any = None) -> JSONResponse:
    http_code = 500 if code < 0 else code
    return JSONResponse(
        status_code=http_code,
        content={
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": id_,
        },
    )


def _required(arguments: dict[str, Any], key: str) -> Any:
    value = arguments.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} és obligatori")
    return value


async def _handle_tool(
    request: Request,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    api_key: str,
    agent_id_hint: str | None,
) -> Any:
    gateway = request.app.state.gateway
    caller = await gateway.authenticate(api_key, agent_id_hint=agent_id_hint)

    if tool_name == "xerrameca_command":
        return await gateway.command(
            caller,
            str(_required(arguments, "command")),
            credential=api_key,
        )
    if tool_name == "xerrameca_inbox":
        return await gateway.inbox(caller)
    if tool_name == "xerrameca_claim":
        return await gateway.claim(caller, str(_required(arguments, "turn_id")))
    if tool_name == "xerrameca_reply":
        body = ReplyRequest(
            content=_required(arguments, "content"),
            result=arguments.get("result", "continue"),
            lease_token=_required(arguments, "lease_token"),
            next_agent_id=arguments.get("next_agent_id"),
            metadata=arguments.get("metadata") or {},
        )
        return await gateway.reply(
            caller,
            str(_required(arguments, "turn_id")),
            body,
        )
    if tool_name == "xerrameca_list":
        return await gateway.engine.list_conversations(caller)
    if tool_name == "xerrameca_get":
        return await gateway.engine.get_conversation(
            caller, str(_required(arguments, "conversation_id"))
        )
    if tool_name == "xerrameca_messages":
        return await gateway.engine.list_messages(
            caller, str(_required(arguments, "conversation_id"))
        )
    raise ValueError("Eina Xerrameca desconeguda")


@router.get("/")
async def mcp_list_tools() -> JSONResponse:
    return _success(
        {
            "tools": TOOLS,
            "protocol": "model-context-protocol",
            "version": "1.0.0",
        }
    )


@router.post("/")
async def mcp_handle(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _error(-32700, "Parse error: invalid JSON")
    if not isinstance(body, dict):
        return _error(-32600, "Invalid Request")

    method = body.get("method", "")
    params = body.get("params", {})
    id_ = body.get("id")

    if method == "tools/list":
        return _success({"tools": TOOLS}, id_)
    if method != "tools/call":
        return _error(-32601, f"Method not found: {method}", id_)
    if not isinstance(params, dict):
        return _error(-32602, "params invàlid", id_)

    tool_name = params.get("name", "")
    arguments = params.get("arguments") or {}
    if tool_name not in TOOL_NAMES:
        return _error(-32602, f"Unknown tool: {tool_name}", id_)
    if not isinstance(arguments, dict):
        return _error(-32602, "arguments invàlid", id_)

    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return _error(401, "Falta la capçalera X-API-Key", id_)
    agent_id_hint = request.headers.get("X-Agent-ID")

    try:
        result = await _handle_tool(
            request,
            tool_name,
            arguments,
            api_key=api_key,
            agent_id_hint=agent_id_hint,
        )
    except XerramecaError as exc:
        return _error(exc.status_code, exc.detail, id_)
    except (ValueError, TypeError) as exc:
        return _error(-32602, str(exc), id_)

    return _success(result, id_)
