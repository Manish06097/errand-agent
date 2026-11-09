"""notifications: the bell (architecture 14.2). One row per cancel, never a reminder.

`event_id` is the follow-up job id and is UNIQUE, so a second delivery of the same job can
never add a second notification, whatever else goes wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite


@dataclass(frozen=True)
class Notification:
    notification_id: str
    user_id: str
    conversation_id: str | None
    kind: str
    urgency: str
    body: dict[str, Any]
    event_id: str | None
    created_at: int
    read_at: int | None
    seq: int = 0


_COLUMNS = (
    "notification_id, user_id, conversation_id, kind, urgency, body_json, event_id, "
    "created_at, read_at"
)


def _row(row: Any) -> Notification:
    return Notification(
        row["notification_id"],
        row["user_id"],
        row["conversation_id"],
        row["kind"],
        row["urgency"],
        json.loads(row["body_json"]),
        row["event_id"],
        row["created_at"],
        row["read_at"],
        row["seq"],
    )


class NotificationsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, n: Notification) -> bool:
        cur = await self._conn.execute(
            f"INSERT INTO notifications ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(event_id) DO NOTHING",
            (
                n.notification_id,
                n.user_id,
                n.conversation_id,
                n.kind,
                n.urgency,
                json.dumps(n.body, ensure_ascii=False, separators=(",", ":")),
                n.event_id,
                n.created_at,
                n.read_at,
            ),
        )
        return cur.rowcount == 1

    async def list_for_user(self, user_id: str, *, after_seq: int = 0) -> list[Notification]:
        async with self._conn.execute(
            f"SELECT rowid AS seq, {_COLUMNS} FROM notifications WHERE user_id = ? AND rowid > ? "
            "ORDER BY rowid",
            (user_id, after_seq),
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def unread_count(self, user_id: str) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get(self, notification_id: str) -> Notification | None:
        async with self._conn.execute(
            f"SELECT rowid AS seq, {_COLUMNS} FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def mark_read(self, notification_id: str, now_ms: int) -> bool:
        cur = await self._conn.execute(
            "UPDATE notifications SET read_at = ? WHERE notification_id = ? AND read_at IS NULL",
            (now_ms, notification_id),
        )
        return cur.rowcount == 1

    async def count(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM notifications") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0
