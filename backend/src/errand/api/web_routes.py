"""Routes the web app (and `errand chat`) needs beyond the M3/M4 surface (17.3).

- `GET /api/meta` says whether dev mode is on and which model mode runs, so the client
  decides on the dev drawer without probing a route that must not exist;
- `GET /api/conversations` lists the user's conversations, newest activity first;
- `GET /api/runs/{id}/trace` and `/trace.jsonl` serve a run's trace. Both read it only
  through `errand.trace.reader` (Gate 2 decision 14: M11's forget mask lives there), so
  nothing here opens the file.

New routes live here, not in `app.py`, which only includes the router.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from errand.api import events
from errand.api.dev_routes import dev_enabled
from errand.api.runs import RunManager
from errand.trace import reader
from errand.trace.masked import MaskUnavailable, masker_for

router = APIRouter()


class MetaOut(BaseModel):
    dev: bool
    model_mode: Literal["scripted", "replay", "record", "live"]


class ConversationSummary(BaseModel):
    conversation_id: str
    title: str | None = None
    # The user's first message, for the list; their own words, shown as text.
    preview: str | None = None
    created_at: int
    last_activity_at: int
    active_run_id: str | None = None


class TraceOut(BaseModel):
    run_id: str
    conversation_id: str
    status: str
    outcome: str | None = None
    records: list[events.TraceRecordData]
    # The writer's last line was cut off (a run still writing, or a crash).
    truncated: bool = False


def _manager(request: Request) -> RunManager:
    manager: RunManager = request.app.state.manager
    return manager


@router.get("/api/meta", response_model=MetaOut)
async def meta(request: Request) -> MetaOut:
    return MetaOut(dev=dev_enabled(), model_mode=_manager(request).model_settings.model_mode)


@router.get("/api/conversations", response_model=list[ConversationSummary])
async def list_conversations(request: Request) -> list[ConversationSummary]:
    manager = _manager(request)
    async with manager.db.session() as repos:
        rows = await repos.conversations.list_with_previews(request.app.state.user_id)
    return [
        ConversationSummary(
            conversation_id=c.conversation_id,
            title=c.title,
            preview=text[:140] if isinstance(text, str) else None,
            created_at=c.created_at,
            last_activity_at=c.last_activity_at,
            active_run_id=manager.active_run(c.conversation_id),
        )
        for c, text in rows
    ]


async def _run_trace(request: Request, run_id: str) -> tuple[Any, reader.TraceFile | None]:
    manager = _manager(request)
    async with manager.db.session() as repos:
        run = await repos.runs.get(run_id)
    if run is None or run.trace_path is None:
        return None, None
    path = manager.traces_dir / run.trace_path
    if not path.is_file():
        return run, None
    # every user-facing read masks forgotten values (gate 2 decision 14)
    return run, reader.read_trace_masked(path, masker_for(manager.traces_dir.parent))


@router.get(
    "/api/runs/{run_id}/trace",
    response_model=TraceOut,
    responses={404: {"description": "unknown run"}},
)
async def run_trace(run_id: str, request: Request) -> Any:
    try:
        run, trace = await _run_trace(request, run_id)
    except MaskUnavailable:
        return JSONResponse({"error": "mask_unavailable"}, status_code=503)
    if run is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    records = [] if trace is None else trace.records
    return {
        "run_id": run.run_id,
        "conversation_id": run.conversation_id,
        "status": run.status,
        "outcome": run.outcome,
        "records": records,
        "truncated": trace is not None and trace.truncated_tail is not None,
    }


@router.get("/api/runs/{run_id}/trace.jsonl", response_model=None)
async def run_trace_jsonl(run_id: str, request: Request) -> Response:
    try:
        run, trace = await _run_trace(request, run_id)
    except MaskUnavailable:
        return JSONResponse({"error": "mask_unavailable"}, status_code=503)
    if run is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    body = b"" if trace is None else reader.to_jsonl(trace)
    return Response(
        body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.jsonl"'},
    )


def register(app: FastAPI) -> None:
    """One call from `create_app`: the routes, and the SSE event models in the OpenAPI
    document (they are not response models of any route, so they are merged in)."""
    app.include_router(router)
    build = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            events.add_components(build())
        return app.openapi_schema or build()

    app.openapi = openapi  # type: ignore[method-assign]
