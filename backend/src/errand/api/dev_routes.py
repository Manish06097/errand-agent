"""`/api/dev/*`: the dev drawer's controls (architecture 17.3).

These routes exist only when the API process runs with ERRAND_DEV=1. With it off they
are not registered at all, so a request is a 404, not a 403 from a route that is there.
They proxy to the world's own `/dev` surface through the `WorldInspector`; the agent's
planning code never gets one. Faults stay named and test-only (5.5); nothing here
changes the default world.

- `GET /api/dev/scenarios` the scenarios the picker offers (the eval catalogue);
- `POST /api/dev/scenario` loads one into a fresh world store (or resets the current);
- `POST /api/dev/clock/advance` moves the world clock, and with it the agent clock;
- `POST /api/dev/next-book` forces the next `book` outcome ("times out after success");
- `GET /api/dev/bookings` and `POST /api/dev/provider-cancel` ("provider cancels booking").
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from errand.api.runs import RunManager
from errand.clock import to_ms

MAX_ADVANCE_S = 7 * 24 * 3600


class _DevFlag(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_", extra="ignore")

    dev: bool = False


def dev_enabled() -> bool:
    return _DevFlag().dev


class ScenarioInfo(BaseModel):
    id: str
    title: str


class ScenariosOut(BaseModel):
    scenarios: list[ScenarioInfo]
    current: str | None = None


class LoadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None resets the world to the scenario it already has.
    scenario_id: str | None = None


class AdvanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: float = Field(gt=0, le=MAX_ADVANCE_S)


class NextBookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["success", "slot_taken", "timeout_booked", "timeout_not_booked"] = (
        "timeout_booked"
    )


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    booking_id: str


class DevBooking(BaseModel):
    booking_id: str
    provider_id: str
    service: str
    status: str
    slot_start: int


router = APIRouter(prefix="/api/dev")


def _inspector(request: Request) -> Any:
    """The world's WorldInspector (typed in errand.tools, not imported here)."""
    manager: RunManager = request.app.state.manager
    return manager.world.inspector


def _no_inspector() -> JSONResponse:
    return JSONResponse({"error": "no_inspector"}, status_code=501)


def scenario_dir() -> Path:
    import evals

    return Path(evals.__file__).resolve().parent / "scenarios"


def _catalogue() -> dict[str, Any]:
    from evals.dsl import find_scenarios, load_scenario

    loaded = (load_scenario(p) for p in find_scenarios(scenario_dir()))
    return {s.id: s for s in loaded}


@router.get("/scenarios", response_model=ScenariosOut)
async def scenarios(request: Request) -> ScenariosOut:
    catalogue = _catalogue()
    return ScenariosOut(
        scenarios=[ScenarioInfo(id=s.id, title=s.title) for s in catalogue.values()],
        current=getattr(request.app.state, "dev_scenario", None),
    )


@router.post("/scenario", response_model=None)
async def load_scenario(body: LoadIn, request: Request) -> Any:
    inspector = _inspector(request)
    if inspector is None:
        return _no_inspector()
    manager: RunManager = request.app.state.manager
    mode = manager.clock.mode if hasattr(manager.clock, "mode") else "sim"
    if body.scenario_id is None:
        await inspector.reset()
    else:
        scenario = _catalogue().get(body.scenario_id)
        if scenario is None:
            return JSONResponse({"error": "unknown_scenario"}, status_code=404)
        await inspector.load_scenario(scenario.world_config(), mode)
        request.app.state.dev_scenario = scenario.id
    return {"scenario_id": getattr(request.app.state, "dev_scenario", None)}


@router.post("/clock/advance", response_model=None)
async def advance(body: AdvanceIn, request: Request) -> Any:
    inspector = _inspector(request)
    if inspector is None:
        return _no_inspector()
    now = await inspector.advance(body.seconds)
    return {"now_ms": to_ms(now)}


@router.post("/next-book", response_model=None)
async def next_book(body: NextBookIn, request: Request) -> Any:
    inspector = _inspector(request)
    if inspector is None:
        return _no_inspector()
    await inspector.force_next_book(body.outcome)
    return {"next_book": body.outcome}


@router.get("/bookings", response_model=list[DevBooking])
async def bookings(request: Request) -> Any:
    inspector = _inspector(request)
    if inspector is None:
        return _no_inspector()
    return [
        DevBooking(
            booking_id=str(row["booking_id"]),
            provider_id=str(row["provider_id"]),
            service=str(row["service"]),
            status=str(row["status"]),
            slot_start=int(row["slot_start"]),
        )
        for row in (dataclasses.asdict(b) for b in await inspector.bookings())
    ]


@router.post("/provider-cancel", response_model=None)
async def provider_cancel(body: CancelIn, request: Request) -> Any:
    inspector = _inspector(request)
    if inspector is None:
        return _no_inspector()
    if body.booking_id not in {b.booking_id for b in await inspector.bookings()}:
        return JSONResponse({"error": "unknown_booking"}, status_code=404)
    event = await inspector.fire_provider_cancel(body.booking_id)
    return {"event_id": str(getattr(event, "event_id", ""))}


def register(app: FastAPI) -> None:
    """One call from `create_app`. Adds the routes only when ERRAND_DEV is on."""
    if dev_enabled():
        app.include_router(router)
