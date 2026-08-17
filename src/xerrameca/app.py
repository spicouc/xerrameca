from __future__ import annotations

from fastapi import FastAPI

from . import __version__


def create_app() -> FastAPI:
    app = FastAPI(
        title="Xerrameca",
        version=__version__,
        description="Independent agent-to-agent orchestration service",
    )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "xerrameca",
            "version": __version__,
        }

    return app


app = create_app()
