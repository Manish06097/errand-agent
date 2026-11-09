from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

Role = Literal["user", "assistant"]
MessageKind = Literal["text", "card", "notice", "proactive"]
_COLUMNS = (
    "message_id, conversation_id, client_msg_id, role, kind, body_json, run_id, turn_no, "
    "approval_id, typed_grant_ok, rendered_at, created_at"
)


@dataclass(frozen=True)
class Message:
    message_id: str
    conversation_id: str
    client_msg_id: str | None
    role: Role
    kind: MessageKind
    body: dict[str, Any]
    run_id: str | None
    turn_no: int | None
    approval_id: str | None
    typed_grant_ok: bool | None
    rendered_at: int | None
    created_at: int


def _row(row: Any) -> Message:
    ok = row["typed_grant_ok"]
    return Message(
        row["message_id"],
        row["conversation_id"],
        row["client_msg_id"],
        row["role"],
        row["kind"],
        json.loads(row["body_json"]),
        row["run_id"],
        row["turn_no"],
        row["approval_id"],
        None if ok is None else bool(ok),
        row["rendered_at"],
        row["created_at"],
    )


class MessagesRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, message: Message) -> tuple[Message, bool]:
        """Insert, or return the existing row for the same client_msg_id.

        Returns (message, created). A retried POST gets the original message back and
        created=False, so the API can re-attach it to the run it already started.
        """
        ok = message.typed_grant_ok
        cur = await self._conn.execute(
            f"INSERT INTO messages ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(client_msg_id) DO NOTHING",
            (
                message.message_id,
                message.conversation_id,
                message.client_msg_id,
                message.role,
                message.kind,
                json.dumps(message.body, ensure_ascii=False, separators=(",", ":")),
                message.run_id,
                message.turn_no,
                message.approval_id,
                None if ok is None else int(ok),
                message.rendered_at,
                message.created_at,
            ),
        )
        if cur.rowcount == 1:
            return message, True
        assert message.client_msg_id is not None  # only that UNIQUE can skip the insert
        existing = await self.get_by_client_msg_id(message.client_msg_id)
        if existing is None:  # pragma: no cover - the conflict row cannot vanish in a transaction
            raise RuntimeError("client_msg_id conflict but no existing row")
        return existing, False

    async def upsert_assistant(self, message: Message) -> Message:
        """Write the assistant message for (run_id, kind), or overwrite it.

        `respond` may run again after a crash; the second run lands on the same row
        (migration 0002), so the transcript never shows a message twice.
        """
        if message.role != "assistant" or message.run_id is None:
            raise ValueError("upsert_assistant needs an assistant message with a run_id")
        ok = message.typed_grant_ok
        await self._conn.execute(
            f"INSERT INTO messages ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, kind) WHERE role = 'assistant' AND run_id IS NOT NULL "
            "DO UPDATE SET body_json = excluded.body_json, turn_no = excluded.turn_no, "
            "approval_id = excluded.approval_id, typed_grant_ok = excluded.typed_grant_ok",
            (
                message.message_id,
                message.conversation_id,
                message.client_msg_id,
                message.role,
                message.kind,
                json.dumps(message.body, ensure_ascii=False, separators=(",", ":")),
                message.run_id,
                message.turn_no,
                message.approval_id,
                None if ok is None else int(ok),
                message.rendered_at,
                message.created_at,
            ),
        )
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE run_id = ? AND kind = ? AND role = 'assistant'",
            (message.run_id, message.kind),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return _row(row)

    async def list_for_run(self, run_id: str) -> list[Message]:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE run_id = ? ORDER BY created_at, rowid",
            (run_id,),
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def count_user_messages(self, conversation_id: str) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND role = 'user'",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get(self, message_id: str) -> Message | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE message_id = ?", (message_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def get_by_client_msg_id(self, client_msg_id: str) -> Message | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE client_msg_id = ?", (client_msg_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def list_for_conversation(self, conversation_id: str) -> list[Message]:
        # rowid breaks ties between messages written in the same millisecond.
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid",
            (conversation_id,),
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def latest_assistant(self, conversation_id: str) -> Message | None:
        """The latest assistant message the typed-grant binding looks at. A `proactive` line
        (written by a run that does not own the conversation) never carries an approval or a
        question, so it is looked past (14, INV-15)."""
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE conversation_id = ? AND role = 'assistant' "
            "AND kind != 'proactive' ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def mark_rendered(self, message_id: str, now_ms: int) -> bool:
        # The first ack wins; the typed-grant check compares against it (8.3).
        cur = await self._conn.execute(
            "UPDATE messages SET rendered_at = ? WHERE message_id = ? AND rendered_at IS NULL",
            (now_ms, message_id),
        )
        return cur.rowcount == 1
