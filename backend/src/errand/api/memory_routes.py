"""The memory API (architecture 17.3, gate 2 decision 7).

`GET /api/memory` groups the user's rows; `PATCH` corrects (a new version), `DELETE`
forgets, `confirm`, `dismiss` and `undo` move a row out of `pending_confirmation` or
undo a save. Every mutation carries a `client_action_id` and is idempotent through
`client_requests`; a row that is not in the state the action needs answers 409 with its
current view. `DELETE` takes the id as a query parameter so the browser's keepalive
fetch on `pagehide` can send it without a body.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from errand.memory.evidence import value_from_span
from errand.memory.service import (
    MemoryConflict,
    MemoryNotFound,
    MemoryRefused,
    MemoryService,
    RouteMismatch,
)

router = APIRouter()


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryActionIn(_In):
    client_action_id: str = Field(min_length=1, max_length=100)


class AddIn(MemoryActionIn):
    key: str
    scope: str
    value: Any


class CorrectIn(MemoryActionIn):
    value: Any


def _service(request: Request) -> MemoryService:
    service: MemoryService = request.app.state.memory
    return service


def _user(request: Request) -> str:
    user: str = request.app.state.user_id
    return user


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, MemoryNotFound):
        return JSONResponse({"error": "not_found"}, status_code=404)
    if isinstance(exc, MemoryConflict):
        return JSONResponse(
            {"error": "conflict", "detail": str(exc), "current": exc.current}, status_code=409
        )
    if isinstance(exc, MemoryRefused):
        return JSONResponse(
            {"error": "refused", "reason": exc.reason, "detail": exc.detail}, status_code=422
        )
    if isinstance(exc, RouteMismatch):
        return JSONResponse(
            {"error": "client_action_id_reused", "detail": str(exc)}, status_code=422
        )
    raise exc


@router.get("/api/memory", response_model=None)
async def list_memory(request: Request) -> Any:
    return await _service(request).list_grouped(_user(request))


@router.get("/api/memory/{memory_id}", response_model=None)
async def get_memory(memory_id: str, request: Request) -> Any:
    try:
        return await _service(request).detail(_user(request), memory_id)
    except MemoryNotFound as exc:
        return _error(exc)


@router.post("/api/memory", status_code=201, response_model=None)
async def add_memory(body: AddIn, request: Request) -> Any:
    try:
        out = await _service(request).remember_from_ui(
            user_id=_user(request),
            key=body.key,
            scope=body.scope,
            value=body.value,
            client_action_id=body.client_action_id,
            action="add",
        )
    except (MemoryRefused, RouteMismatch, MemoryConflict) as exc:
        return _error(exc)
    return JSONResponse(out, status_code=201)


@router.patch("/api/memory/{memory_id}", response_model=None)
async def correct_memory(memory_id: str, body: CorrectIn, request: Request) -> Any:
    try:
        return await _service(request).correct(
            user_id=_user(request),
            memory_id=memory_id,
            value=body.value,
            client_action_id=body.client_action_id,
        )
    except (MemoryNotFound, MemoryConflict, MemoryRefused, RouteMismatch) as exc:
        return _error(exc)


@router.delete("/api/memory/{memory_id}", response_model=None)
async def forget_memory(
    memory_id: str, request: Request, client_action_id: str = Query(min_length=1, max_length=100)
) -> Any:
    try:
        return await _service(request).forget(
            user_id=_user(request), memory_id=memory_id, client_action_id=client_action_id
        )
    except (MemoryNotFound, MemoryConflict, RouteMismatch) as exc:
        return _error(exc)


@router.post("/api/memory/{memory_id}/confirm", response_model=None)
async def confirm_memory(memory_id: str, body: MemoryActionIn, request: Request) -> Any:
    try:
        return await _service(request).keep(
            user_id=_user(request), memory_id=memory_id, client_action_id=body.client_action_id
        )
    except (MemoryNotFound, MemoryConflict, RouteMismatch) as exc:
        return _error(exc)


@router.post("/api/memory/{memory_id}/dismiss", response_model=None)
async def dismiss_memory(memory_id: str, body: MemoryActionIn, request: Request) -> Any:
    try:
        return await _service(request).dismiss(
            user_id=_user(request), memory_id=memory_id, client_action_id=body.client_action_id
        )
    except (MemoryNotFound, MemoryConflict, RouteMismatch) as exc:
        return _error(exc)


@router.post("/api/memory/{memory_id}/undo", response_model=None)
async def undo_memory(memory_id: str, body: MemoryActionIn, request: Request) -> Any:
    try:
        return await _service(request).undo(
            user_id=_user(request), memory_id=memory_id, client_action_id=body.client_action_id
        )
    except (MemoryNotFound, MemoryConflict, RouteMismatch) as exc:
        return _error(exc)


def mount(app: FastAPI) -> None:
    """Register the memory routes on `app` itself, so they are plain APIRoutes like the rest."""
    for route in router.routes:
        if isinstance(route, APIRoute):
            app.add_api_route(
                route.path,
                route.endpoint,
                methods=sorted(route.methods or []),
                status_code=route.status_code,
                response_model=None,
            )


# ----------------------------------------------------------------- chat actions (17.3)

MEMORY_ACTIONS = frozenset({"keep_memory", "confirm_forget", "update_memory", "save_memory"})


async def memory_chat_action(
    request: Request,
    conversation_id: str,
    kind: str,
    client_action_id: str,
    payload: dict[str, str],
) -> JSONResponse:
    """The chat actions on memory chips. Each is idempotent on `client_action_id` (a repeat
    replays the first answer) and answers 409 with the current row when the row is not in
    the state the action needs. Dismiss and Undo use the memory routes."""
    service, user = _service(request), _user(request)
    db = request.app.state.manager.db
    memory_id = payload.get("memory_id", "")
    try:
        if kind == "keep_memory":
            out = await service.keep(
                user_id=user,
                memory_id=memory_id,
                client_action_id=client_action_id,
            )
        elif kind == "confirm_forget":
            out = await service.forget(
                user_id=user,
                memory_id=memory_id,
                client_action_id=client_action_id,
            )
        else:
            chip = await _chip_body(db, conversation_id, payload.get("chip_message_id", ""))
            if chip is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            value = await _value_of_chip(db, chip)
            if value is None:
                return JSONResponse(
                    {"error": "conflict", "detail": "nothing to save: those words are gone"},
                    status_code=409,
                )
            if kind == "save_memory":
                out = await service.remember_from_ui(
                    user_id=user,
                    key=chip["key"],
                    scope=chip["scope"],
                    value=value,
                    client_action_id=client_action_id,
                    action="save",
                )
            elif payload.get("choice") == "just_this_time":
                return JSONResponse({"ok": True, "changed": False})
            else:
                out = await service.correct(
                    user_id=user,
                    memory_id=chip["memory_id"],
                    value=value,
                    client_action_id=client_action_id,
                )
    except (MemoryNotFound, MemoryConflict, MemoryRefused, RouteMismatch) as exc:
        return _error(exc)
    return JSONResponse(out)


async def _chip_body(db: Any, conversation_id: str, message_id: str) -> dict[str, Any] | None:
    async with db.session() as repos:
        message = await repos.messages.get(message_id)
    if message is None or message.conversation_id != conversation_id:
        return None
    body = message.body
    return body if body.get("type") in ("memory_offer", "memory_update") else None


async def _value_of_chip(db: Any, chip: dict[str, Any]) -> Any | None:
    """Re-read the user's span and parse it; None if a forget redacted it meanwhile."""
    async with db.session() as repos:
        source = await repos.messages.get(chip["source_message_id"])
        redactions = await repos.memories.redactions_for_message(chip["source_message_id"])
    text = source.body.get("text") if source is not None else None
    if not isinstance(text, str) or chip["end"] > len(text):
        return None
    # A forget that overwrote an earlier span moved the words, so the offsets read other text.
    if any(r.kind == "forgotten" and r.start < chip["end"] for r in redactions):
        return None
    return value_from_span(chip["key"], text[chip["start"] : chip["end"]])
