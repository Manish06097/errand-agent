"""Each service exposes exactly its health route for now."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from errand.api.app import app as api_app
from errand.world.server import app as world_app


@pytest.mark.parametrize(
    ("app", "path", "service"),
    [(api_app, "/api/health", "errand-api"), (world_app, "/health", "errand-world")],
)
async def test_health(app: FastAPI, path: str, service: str) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": service}


@pytest.mark.parametrize(("app", "path"), [(api_app, "/api/health"), (world_app, "/health")])
def test_no_other_routes(app: FastAPI, path: str) -> None:
    assert [r.path for r in app.routes if isinstance(r, APIRoute)] == [path]
