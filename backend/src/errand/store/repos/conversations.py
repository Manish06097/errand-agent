from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

ConversationKind = Literal["chat", "followup"]
_COLUMNS = "conversation_id, user_id, kind, title, origin_booking_id, created_at, last_activity_at"


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    user_id: str
    kind: ConversationKind
    title: str | None
    origin_booking_id: str | None
    created_at: int
    last_activity_at: int


def _row(row: Any) -> Conversation:
    return Conversation(
        row["conversation_id"],
        row["user_id"],
        row["kind"],
        row["title"],
        row["origin_booking_id"],
        row["created_at"],
        row["last_activity_at"],
    )


class ConversationsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def create(
        self,
        *,
        conversation_id: str,
        user_id: str,
        now_ms: int,
        kind: ConversationKind = "chat",
        title: str | None = None,
        origin_booking_id: str | None = None,
    ) -> Conversation:
        conversation = Conversation(
            conversation_id, user_id, kind, title, origin_booking_id, now_ms, now_ms
        )
        await self._conn.execute(
            f"INSERT INTO conversations ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                conversation.conversation_id,
                conversation.user_id,
                conversation.kind,
                conversation.title,
                conversation.origin_booking_id,
                conversation.created_at,
                conversation.last_activity_at,
            ),
        )
        return conversation

    async def get(self, conversation_id: str) -> Conversation | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM conversations WHERE conversation_id = ?", (conversation_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def touch(self, conversation_id: str, now_ms: int) -> None:
        # Never moves backwards, so a late writer cannot reorder the conversation list.
        await self._conn.execute(
            "UPDATE conversations SET last_activity_at = MAX(last_activity_at, ?) "
            "WHERE conversation_id = ?",
            (now_ms, conversation_id),
        )

    async def list_for_user(self, user_id: str) -> list[Conversation]:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM conversations WHERE user_id = ? "
            "ORDER BY last_activity_at DESC, conversation_id",
            (user_id,),
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]
