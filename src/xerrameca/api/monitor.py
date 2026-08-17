from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request

from ..domain.errors import ForbiddenError


router = APIRouter(prefix="/v1/xerrameca/monitor", tags=["xerrameca-monitor"])


def _require_admin(caller: Any) -> None:
    if not bool(caller.permissions.get("admin", False)):
        raise ForbiddenError("Xerrameca Monitor requereix permís admin")


@router.get("/snapshot")
async def monitor_snapshot(
    request: Request,
    api_key: Annotated[str, Header(alias="X-API-Key")],
    agent_id_hint: Annotated[str | None, Header(alias="X-Agent-ID")] = None,
    stalled_after_seconds: int = Query(default=300, ge=30, le=86_400),
    near_rounds_threshold: int = Query(default=1, ge=1, le=20),
    loop_window: int = Query(default=4, ge=4, le=12),
) -> dict[str, Any]:
    caller = await request.app.state.gateway.authenticate(
        api_key, agent_id_hint=agent_id_hint
    )
    _require_admin(caller)
    return await request.app.state.monitor.snapshot(
        stalled_after_seconds=stalled_after_seconds,
        near_rounds_threshold=near_rounds_threshold,
        loop_window=loop_window,
    )
