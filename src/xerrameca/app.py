from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .adapters.unavailable_identity import UnavailableIdentityAdapter
from .api.rest import router as xerrameca_router
from .config import settings
from .domain.errors import XerramecaError
from .ports.identity import IdentityPort
from .services.engine import ConversationEngine
from .services.gateway import XerramecaGateway


def create_app(
    *,
    identity: IdentityPort | None = None,
    db_path: str | None = None,
) -> FastAPI:
    engine = ConversationEngine(db_path or settings.XERRAMECA_DB_PATH)
    gateway = XerramecaGateway(engine, identity or UnavailableIdentityAdapter())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await engine.bootstrap()
        yield

    app = FastAPI(
        title="Xerrameca",
        version=__version__,
        description="Independent agent-to-agent orchestration service",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.gateway = gateway

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
        }

    app.include_router(xerrameca_router)
    return app


app = create_app()
