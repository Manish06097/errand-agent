"""Each service exposes exactly the routes it should: health, and for the world its tools."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from errand.api.app import app as api_app
from errand.world.server import app as world_app
from errand.world.server import create_app as create_world_app

# The suite forces ERRAND_LANGFUSE=off, so the API reports its Langfuse sink disabled.
LANGFUSE_OFF = {"langfuse": {"state": "off", "reason": "disabled"}}


@pytest.mark.parametrize(
    ("app", "path", "service", "extra"),
    [
        (api_app, "/api/health", "errand-api", LANGFUSE_OFF),
        (world_app, "/health", "errand-world", {}),
    ],
)
async def test_health(app: FastAPI, path: str, service: str, extra: dict[str, object]) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": service, **extra}


def routes(app: FastAPI) -> set[str]:
    return {r.path for r in app.routes if isinstance(r, APIRoute)}


def test_api_has_exactly_the_m3_routes() -> None:
    assert routes(api_app) == {
        "/api/health",
        "/api/conversations",
        "/api/conversations/{conversation_id}",
        "/api/conversations/{conversation_id}/messages",
        "/api/runs/{run_id}/events",
    }


def test_world_has_health_and_tools_and_no_dev_routes_by_default() -> None:
    assert routes(create_world_app(dev=False)) == {"/health", "/tools/{tool}"}


def test_world_dev_routes_only_with_the_dev_flag() -> None:
    dev = {r for r in routes(create_world_app(dev=True)) if r.startswith("/dev")}
    assert dev == {
        "/dev/clock",
        "/dev/clock/advance",
        "/dev/world/events",
        "/dev/world/tick",
        "/dev/scenario",
        "/dev/op_log",
        "/dev/bookings",
        "/dev/operations",
    }
