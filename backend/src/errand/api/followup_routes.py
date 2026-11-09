"""Notifications and the user's event stream (architecture 14.2, 17.3).

- `GET /api/notifications` lists the bell, newest last, with the unread count;
- `POST /api/notifications/{id}/read` marks one read;
- `GET /api/users/me/events` is the user SSE stream: `notification` for a new bell row and
  `message` for a proactive line written into one of the user's conversations. A
  notification's event id is its sequence number, so `Last-Event-ID` replays the ones a
  reconnecting client missed; a proactive message has no replay (the client refetches the
  conversation it opens, which is the source of truth).

The stream reads the database on a short interval rather than a second in-process channel:
the follow-up runs write the rows in their own transaction, so what the stream sends is
exactly what was committed, whichever task wrote it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from errand.api import reconciler as reconciler_module
from errand.api.reconciler import Reconciler
from errand.api.runs import RunManager
from errand.clock import to_ms

router = APIRouter()
POLL_S = 0.25


class NotificationOut(BaseModel):
    notification_id: str
    conversation_id: str | None = None
    kind: str
    urgency: str
    title: str
    booking_id: str | None = None
    ready: bool | None = None
    quiet: bool = False
    created_at: int
    read: bool
    seq: int


class NotificationsOut(BaseModel):
    notifications: list[NotificationOut]
    unread: int


def _manager(request: Request) -> RunManager:
    manager: RunManager = request.app.state.manager
    return manager


def _out(n: Any) -> NotificationOut:
    return NotificationOut(
        notification_id=n.notification_id,
        conversation_id=n.conversation_id,
        kind=n.kind,
        urgency=n.urgency,
        title=str(n.body.get("title", "")),
        booking_id=n.body.get("booking_id"),
        ready=n.body.get("ready"),
        quiet=bool(n.body.get("quiet", False)),
        created_at=n.created_at,
        read=n.read_at is not None,
        seq=n.seq,
    )


@router.get("/api/notifications", response_model=NotificationsOut)
async def list_notifications(request: Request) -> NotificationsOut:
    manager = _manager(request)
    user_id = request.app.state.user_id
    async with manager.db.session() as repos:
        rows = await repos.notifications.list_for_user(user_id)
        unread = await repos.notifications.unread_count(user_id)
    return NotificationsOut(notifications=[_out(n) for n in rows], unread=unread)


@router.post("/api/notifications/{notification_id}/read", response_model=None)
async def read_notification(notification_id: str, request: Request) -> Any:
    manager = _manager(request)
    async with manager.db.session() as repos:
        found = await repos.notifications.get(notification_id)
        if found is None or found.user_id != request.app.state.user_id:
            return JSONResponse({"error": "not_found"}, status_code=404)
        await repos.notifications.mark_read(notification_id, to_ms(manager.clock.now()))
        unread = await repos.notifications.unread_count(found.user_id)
    return {"notification_id": notification_id, "unread": unread}


@router.get("/api/users/me/events", response_class=EventSourceResponse, response_model=None)
async def user_events(
    request: Request, last_event_id: str | None = Header(default=None), once: bool = False
) -> AsyncIterator[ServerSentEvent]:
    manager = _manager(request)
    user_id = request.app.state.user_id
    try:
        seen = max(0, int((last_event_id or "0").strip()))
    except ValueError:
        seen = 0
    # Proactive messages are announced from now on; the client refetches history itself.
    async with (
        manager.db.session() as repos,
        repos.conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM messages WHERE kind = 'proactive'"
        ) as cur,
    ):
        row = await cur.fetchone()
    proactive_seen = int(row[0]) if row else 0
    while True:
        async with manager.db.session() as repos:
            rows = await repos.notifications.list_for_user(user_id, after_seq=seen)
            async with repos.conn.execute(
                "SELECT m.rowid, m.message_id, m.conversation_id FROM messages m "
                "JOIN conversations c ON c.conversation_id = m.conversation_id "
                "WHERE m.kind = 'proactive' AND c.user_id = ? AND m.rowid > ? ORDER BY m.rowid",
                (user_id, proactive_seen),
            ) as cur:
                proactive = await cur.fetchall()
        for n in rows:
            seen = n.seq
            yield ServerSentEvent(data=_out(n).model_dump(), event="notification", id=str(n.seq))
        for r in proactive:
            proactive_seen = int(r[0])
            yield ServerSentEvent(
                data={"message_id": r[1], "conversation_id": r[2]}, event="message"
            )
        if once or await request.is_disconnected():
            return
        await asyncio.sleep(POLL_S)


async def followup_pass(reconciler: Reconciler, startup: bool) -> list[str]:
    """The reconciler's follow-up pass (9.7): resume every job a crash left `detected`
    (delivery through the graph is idempotent) and lapse the ones whose window has passed.
    It reads no provider: only the watcher's own tick lists bookings."""
    watcher = getattr(reconciler.manager, "followup_watcher", None)
    if watcher is None:
        return []
    result = await watcher.tick(poll=False)
    return [*result.delivered, *result.lapsed]


# Registered once, at import: it finds the watcher of whichever manager it is handed.
if followup_pass not in reconciler_module.PASSES:
    reconciler_module.PASSES.append(followup_pass)


def register(app: FastAPI) -> None:
    app.include_router(router)
