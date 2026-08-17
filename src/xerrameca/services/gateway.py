from __future__ import annotations

import shlex
from typing import Any

from ..domain.errors import NotFoundError, ValidationError
from ..domain.models import ConversationCreateRequest, ReplyRequest
from ..ports.identity import AgentIdentity, IdentityPort
from .control import cancel_conversation
from .engine import ConversationEngine


HELP_TEXT = """XERRAMECA — Converses entre agents

Ús:
  /xerrameca <agent> <objectiu> [opcions]
  /xerrameca agents
  /xerrameca agents available
  /xerrameca status
  /xerrameca <conversation_id>
  /xerrameca stop <conversation_id>
  /xerrameca help

Opcions:
  --rounds N
  --timeout SECONDS
  --delay SECONDS
  --supervisor
"""


class XerramecaGateway:
    def __init__(self, engine: ConversationEngine, identity: IdentityPort):
        self.engine = engine
        self.identity = identity

    async def authenticate(self, agent_id: str, api_key: str) -> AgentIdentity:
        return await self.identity.authenticate(agent_id, api_key)

    async def available_agents(self, caller: AgentIdentity, scope: str = "shared") -> list[dict[str, Any]]:
        agents = await self.identity.list_available_agents(requester=caller, scope=scope)
        return [
            {
                "id": agent.id,
                "name": agent.name,
                "capabilities": agent.capabilities,
                "is_active": agent.is_active,
            }
            for agent in agents
            if agent.id != caller.id and agent.is_active
        ]

    async def _resolve_target(self, caller: AgentIdentity, token: str) -> AgentIdentity:
        available = await self.identity.list_available_agents(requester=caller, scope="shared")
        candidates = [agent for agent in available if agent.id != caller.id and agent.is_active]
        by_id = [agent for agent in candidates if agent.id == token]
        if len(by_id) == 1:
            return by_id[0]
        by_name = [agent for agent in candidates if agent.name.casefold() == token.casefold()]
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise ValidationError("nom d'agent ambigu; utilitza agent_id")
        raise NotFoundError(f"agent '{token}' no disponible")

    @staticmethod
    def _parse_start(tokens: list[str]) -> tuple[str, str, dict[str, Any]]:
        if len(tokens) < 2:
            raise ValidationError("cal indicar agent i objectiu")
        target = tokens[0]
        objective_parts: list[str] = []
        options: dict[str, Any] = {
            "max_rounds": 5,
            "turn_timeout_seconds": 300,
            "delay_seconds": 2,
            "supervisor": False,
        }
        i = 1
        while i < len(tokens):
            token = tokens[i]
            if token == "--supervisor":
                options["supervisor"] = True
                i += 1
                continue
            matched = False
            for flag, key in (
                ("--rounds", "max_rounds"),
                ("--timeout", "turn_timeout_seconds"),
                ("--delay", "delay_seconds"),
            ):
                if token == flag:
                    if i + 1 >= len(tokens):
                        raise ValidationError(f"falta valor per {flag}")
                    try:
                        options[key] = int(tokens[i + 1])
                    except ValueError as exc:
                        raise ValidationError(f"valor invàlid per {flag}") from exc
                    i += 2
                    matched = True
                    break
                prefix = flag + "="
                if token.startswith(prefix):
                    try:
                        options[key] = int(token[len(prefix):])
                    except ValueError as exc:
                        raise ValidationError(f"valor invàlid per {flag}") from exc
                    i += 1
                    matched = True
                    break
            if matched:
                continue
            if token.startswith("--"):
                raise ValidationError(f"opció desconeguda: {token}")
            objective_parts.append(token)
            i += 1
        objective = " ".join(objective_parts).strip()
        if not objective:
            raise ValidationError("objectiu buit")
        return target, objective, options

    async def command(self, caller: AgentIdentity, raw_command: str) -> dict[str, Any]:
        raw_command = (raw_command or "").strip()
        try:
            tokens = shlex.split(raw_command)
        except ValueError as exc:
            raise ValidationError(f"comanda invàlida: {exc}") from exc
        if tokens and tokens[0].casefold() == "/xerrameca":
            tokens = tokens[1:]
        if not tokens or tokens[0].casefold() in {"help", "-h", "--help"}:
            return {"action": "help", "text": HELP_TEXT}

        verb = tokens[0].casefold()
        if verb == "agents":
            if len(tokens) > 2 or (len(tokens) == 2 and tokens[1].casefold() != "available"):
                raise ValidationError("ús: /xerrameca agents [available]")
            return {"action": "agents", "agents": await self.available_agents(caller)}
        if verb == "status":
            if len(tokens) != 1:
                raise ValidationError("ús: /xerrameca status")
            return {"action": "status", "conversations": await self.engine.list_conversations(caller)}
        if verb == "stop":
            if len(tokens) != 2:
                raise ValidationError("ús: /xerrameca stop <conversation_id>")
            conversation = await cancel_conversation(self.engine.db_path, caller, tokens[1])
            return {"action": "stopped", "conversation": conversation}
        if len(tokens) == 1:
            return {
                "action": "show",
                "conversation": await self.engine.get_conversation(caller, tokens[0]),
            }

        target_token, objective, options = self._parse_start(tokens)
        target = await self._resolve_target(caller, target_token)
        supervisor = caller.id if options.pop("supervisor") else None
        body = ConversationCreateRequest(
            name=f"{caller.name} ↔ {target.name}",
            objective=objective,
            scope="shared",
            participant_agent_ids=[caller.id, target.id],
            turn_policy="supervisor" if supervisor else "alternating",
            supervisor_agent_id=supervisor,
            first_agent_id=caller.id,
            **options,
        )
        created = await self.engine.create_conversation(
            caller,
            [caller, target],
            body,
            provider="identity-port",
        )
        started = await self.engine.start_conversation(caller, created["id"])
        return {"action": "started", "conversation": started}

    async def inbox(self, caller: AgentIdentity) -> dict[str, Any]:
        return await self.engine.inbox(caller)

    async def claim(self, caller: AgentIdentity, turn_id: str) -> dict[str, Any]:
        return await self.engine.claim_turn(caller, turn_id)

    async def reply(self, caller: AgentIdentity, turn_id: str, body: ReplyRequest) -> dict[str, Any]:
        return await self.engine.reply_turn(caller, turn_id, body)
