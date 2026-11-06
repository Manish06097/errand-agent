"""errand-world: HTTP wrapper around the mock provider world. For now it only reports health."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE = "errand-world"


class ServeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_WORLD_")

    host: str = "127.0.0.1"
    port: int = 8765


def create_app() -> FastAPI:
    app = FastAPI(title=SERVICE)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE}

    return app


app = create_app()


def main() -> None:
    settings = ServeSettings()
    uvicorn.run(app, host=settings.host, port=settings.port)
