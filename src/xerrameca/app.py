from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .adapters.local_identity import LocalIdentityAdapter
from .adapters.pluribus_identity import PluribusIdentityAdapter
from .adapters.pluribus_memory import PluribusMemoryAdapter
from .adapters.unavailable_identity import UnavailableIdentityAdapter
from .api.mcp import router as mcp_router
from .api.monitor import router as monitor_router
from .api.rest import router as xerrameca_router
from .config import settings
from .domain.errors import XerramecaError
from .ports.identity import IdentityPort
from .ports.memory import MemoryPort
from .services.engine import ConversationEngine
from .services.gateway import XerramecaGateway
from .services.monitor import PassiveMonitor
from .services.summary_dispatcher import SummaryDispatcher


def _configured_identity_provider() -> IdentityPort:
    if settings.XERRAMECA_IDENTITY_PROVIDER == "pluribus":
        return PluribusIdentityAdapter(
            settings.PLURIBUS_BASE_URL,
            timeout_seconds=settings.PLURIBUS_TIMEOUT_SECONDS,
        )
    if settings.XERRAMECA_IDENTITY_PROVIDER == "local":
        return LocalIdentityAdapter(settings.XERRAMECA_LOCAL_IDENTITY_PATH)
    return UnavailableIdentityAdapter()


def _configured_memory_provider() -> MemoryPort | None:
    if settings.PLURIBUS_SERVICE_API_KEY is None:
        return None
    return PluribusMemoryAdapter(
        settings.PLURIBUS_BASE_URL,
        settings.PLURIBUS_SERVICE_API_KEY.get_secret_value(),
        timeout_seconds=settings.PLURIBUS_TIMEOUT_SECONDS,
    )


def create_app(
    *,
    identity: IdentityPort | None = None,
    memory: MemoryPort | None = None,
    db_path: str | None = None,
) -> FastAPI:
    engine = ConversationEngine(db_path or settings.XERRAMECA_DB_PATH)
    gateway = XerramecaGateway(engine, identity or _configured_identity_provider())
    monitor = PassiveMonitor(engine.db_path)
    memory_provider = memory or _configured_memory_provider()
    dispatcher = (
        SummaryDispatcher(
            engine.db_path,
            memory_provider,
            max_attempts=settings.XERRAMECA_SUMMARY_MAX_ATTEMPTS,
        )
        if memory_provider is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await engine.bootstrap()
        dispatcher_task: asyncio.Task[None] | None = None
        if dispatcher is not None:
            dispatcher_task = asyncio.create_task(
                dispatcher.loop(
                    interval_seconds=settings.XERRAMECA_SUMMARY_DISPATCH_SECONDS
                )
            )
        try:
            yield
        finally:
            if dispatcher_task is not None:
                dispatcher_task.cancel()
                await asyncio.gather(dispatcher_task, return_exceptions=True)

    app = FastAPI(
        title="Xerrameca",
        version=__version__,
        description="Independent agent-to-agent orchestration service",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.gateway = gateway
    app.state.monitor = monitor
    app.state.summary_dispatcher = dispatcher

    @app.exception_handler(XerramecaError)
    async def xerrameca_error_handler(
        request: Request, exc: XerramecaError
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "xerrameca",
            "version": __version__,
            "identity_provider": settings.XERRAMECA_IDENTITY_PROVIDER,
            "summary_dispatcher": dispatcher is not None,
            "passive_monitor": True,
        }

    app.include_router(mcp_router)
    app.include_router(monitor_router)
    app.include_router(xerrameca_router)
    return app


app = create_app()
