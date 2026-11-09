"""memories, memory_events, memory_blocklist, memory_surface_hashes and message_redactions.

Plain SQL, no policy: `errand.memory.service` decides when each statement runs and always
inside a `BEGIN IMMEDIATE` transaction. Every status change is a conditional UPDATE that
names the states it may leave, so a row never moves out of a state the caller did not
expect (a double click, a click on a row that changed meanwhile).

Timestamps are integer epoch ms on the agent clock.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

MemoryStatus = Literal["active", "pending_confirmation", "superseded", "forgotten", "rejected"]
MemoryKind = Literal["explicit", "inferred"]
_COLUMNS = (
    "memory_id, user_id, key, scope, value_json, value_hash, kind, status, evidence_json, "
    "supersedes, superseded_by, created_at, confirmed_at, forgotten_at, last_used_at"
)


@dataclass(frozen=True)
class MemoryRow:
    memory_id: str
    user_id: str
    key: str
    scope: str
    value: Any  # None once forgotten
    value_hash: str
    kind: MemoryKind
    status: MemoryStatus
    evidence: dict[str, Any] | None
    supersedes: str | None
    superseded_by: str | None
    created_at: int
    confirmed_at: int | None
    forgotten_at: int | None
    last_used_at: int | None


@dataclass(frozen=True)
class MemoryEvent:
    event_id: str
    memory_id: str
    type: str
    actor: str
    run_id: str | None
    at: int
    details: dict[str, Any] | None


@dataclass(frozen=True)
class Redaction:
    message_id: str
    start: int
    end: int
    memory_id: str
    kind: Literal["forgotten", "superseded"]


def _loads(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)


def _row(r: Any) -> MemoryRow:
    return MemoryRow(
        r["memory_id"],
        r["user_id"],
        r["key"],
        r["scope"],
        _loads(r["value_json"]),
        r["value_hash"],
        r["kind"],
        r["status"],
        _loads(r["evidence_json"]),
        r["supersedes"],
        r["superseded_by"],
        r["created_at"],
        r["confirmed_at"],
        r["forgotten_at"],
        r["last_used_at"],
    )


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class MemoriesRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------------ memories

    async def next_id(self) -> str:
        """m-01, m-02, ...: the next number after the highest one ever used."""
        async with self._conn.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(memory_id, 3) AS INTEGER)), 0) FROM memories "
            "WHERE memory_id GLOB 'm-[0-9]*'"
        ) as cur:
            row = await cur.fetchone()
        n = int(row[0]) + 1 if row else 1
        return f"m-{n:02d}"

    async def insert(self, m: MemoryRow, value_json: str, evidence_json: str | None) -> None:
        await self._conn.execute(
            f"INSERT INTO memories ({_COLUMNS}) VALUES ({','.join('?' * 15)})",
            (
                m.memory_id,
                m.user_id,
                m.key,
                m.scope,
                value_json,
                m.value_hash,
                m.kind,
                m.status,
                evidence_json,
                m.supersedes,
                m.superseded_by,
                m.created_at,
                m.confirmed_at,
                m.forgotten_at,
                m.last_used_at,
            ),
        )

    async def get(self, memory_id: str) -> MemoryRow | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM memories WHERE memory_id = ?", (memory_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def for_user(self, user_id: str) -> list[MemoryRow]:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM memories WHERE user_id = ? ORDER BY rowid", (user_id,)
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def eligible_for_user(self, user_id: str) -> list[MemoryRow]:
        """The builder's only door: rows of the `memories_eligible` view."""
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM memories_eligible WHERE user_id = ? ORDER BY rowid",
            (user_id,),
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def eligible_ids(self, user_id: str) -> frozenset[str]:
        async with self._conn.execute(
            "SELECT memory_id FROM memories_eligible WHERE user_id = ?", (user_id,)
        ) as cur:
            return frozenset(r[0] for r in await cur.fetchall())

    async def find(
        self, user_id: str, key: str, scope: str, statuses: Sequence[str]
    ) -> list[MemoryRow]:
        marks = ",".join("?" for _ in statuses)
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM memories WHERE user_id = ? AND key = ? AND scope = ? "
            f"AND status IN ({marks}) ORDER BY rowid",
            (user_id, key, scope, *statuses),
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def transition(
        self,
        memory_id: str,
        *,
        from_statuses: Sequence[str],
        to: MemoryStatus,
        confirmed_at: int | None = None,
        forgotten_at: int | None = None,
        superseded_by: str | None = None,
    ) -> bool:
        """Conditional status change; False when the row was not in one of `from_statuses`."""
        marks = ",".join("?" for _ in from_statuses)
        cur = await self._conn.execute(
            "UPDATE memories SET status = ?, "
            "confirmed_at = COALESCE(?, confirmed_at), forgotten_at = COALESCE(?, forgotten_at), "
            "superseded_by = COALESCE(?, superseded_by) "
            f"WHERE memory_id = ? AND status IN ({marks})",
            (to, confirmed_at, forgotten_at, superseded_by, memory_id, *from_statuses),
        )
        return cur.rowcount == 1

    async def reactivate(self, memory_id: str) -> bool:
        """Undo of a correction: the older row it replaced becomes the active one again."""
        cur = await self._conn.execute(
            "UPDATE memories SET status = 'active', superseded_by = NULL "
            "WHERE memory_id = ? AND status = 'superseded'",
            (memory_id,),
        )
        return cur.rowcount == 1

    async def link_supersedes(self, memory_id: str, older: str) -> None:
        await self._conn.execute(
            "UPDATE memories SET supersedes = ? WHERE memory_id = ?", (older, memory_id)
        )

    async def null_value(self, memory_id: str, evidence_json: str | None) -> None:
        """Last step of a forget: the value and the quote leave the row."""
        await self._conn.execute(
            "UPDATE memories SET value_json = NULL, evidence_json = ? WHERE memory_id = ?",
            (evidence_json, memory_id),
        )

    async def touch_used(self, memory_ids: Sequence[str], now_ms: int) -> None:
        for memory_id in memory_ids:
            await self._conn.execute(
                "UPDATE memories SET last_used_at = ? WHERE memory_id = ? AND status = 'active'",
                (now_ms, memory_id),
            )

    # -------------------------------------------------------------------------- events

    async def add_event(
        self,
        memory_id: str,
        type_: str,
        actor: str,
        at: int,
        *,
        run_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM memory_events WHERE memory_id = ?", (memory_id,)
        ) as cur:
            row = await cur.fetchone()
        event_id = f"{memory_id}-e{(int(row[0]) if row else 0) + 1}"
        await self._conn.execute(
            "INSERT INTO memory_events "
            "(event_id, memory_id, type, actor, run_id, at, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                memory_id,
                type_,
                actor,
                run_id,
                at,
                None if details is None else dumps(details),
            ),
        )
        return event_id

    async def events(self, memory_id: str) -> list[MemoryEvent]:
        async with self._conn.execute(
            "SELECT event_id, memory_id, type, actor, run_id, at, details_json FROM memory_events "
            "WHERE memory_id = ? ORDER BY rowid",
            (memory_id,),
        ) as cur:
            return [
                MemoryEvent(r[0], r[1], r[2], r[3], r[4], r[5], _loads(r[6]))
                for r in await cur.fetchall()
            ]

    # ----------------------------------------------------------------------- blocklist

    async def block(self, user_id: str, key: str, scope: str, value_hash: str, reason: str) -> None:
        await self._conn.execute(
            "INSERT INTO memory_blocklist (user_id, key, scope, value_hash, reason) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (user_id, key, scope, value_hash, reason),
        )

    async def is_blocked(self, user_id: str, key: str, scope: str, value_hash: str) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM memory_blocklist WHERE user_id = ? AND key = ? AND scope = ? "
            "AND value_hash = ?",
            (user_id, key, scope, value_hash),
        ) as cur:
            return await cur.fetchone() is not None

    async def blocklist(self, user_id: str) -> list[tuple[str, str, str, str]]:
        async with self._conn.execute(
            "SELECT key, scope, value_hash, reason FROM memory_blocklist WHERE user_id = ? "
            "ORDER BY key, scope, value_hash",
            (user_id,),
        ) as cur:
            return [(r[0], r[1], r[2], r[3]) for r in await cur.fetchall()]

    # ------------------------------------------------------------------------- hashes

    async def add_hashes(self, memory_id: str, kind: str, hmacs: Sequence[str]) -> None:
        for digest in hmacs:
            await self._conn.execute(
                "INSERT INTO memory_surface_hashes (memory_id, kind, hmac) VALUES (?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (memory_id, kind, digest),
            )

    async def hashes_for_user(self, user_id: str) -> dict[str, str]:
        """hmac -> memory_id for every forgotten value and redacted span of the user."""
        async with self._conn.execute(
            "SELECT h.hmac, h.memory_id FROM memory_surface_hashes h "
            "JOIN memories m ON m.memory_id = h.memory_id WHERE m.user_id = ? "
            "ORDER BY h.memory_id, h.kind, h.hmac",
            (user_id,),
        ) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}

    # --------------------------------------------------------------------- redactions

    async def add_redaction(self, r: Redaction) -> None:
        await self._conn.execute(
            'INSERT INTO message_redactions (message_id, "start", "end", memory_id, kind) '
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (r.message_id, r.start, r.end, r.memory_id, r.kind),
        )

    async def redactions_for_message(self, message_id: str) -> list[Redaction]:
        async with self._conn.execute(
            'SELECT message_id, "start", "end", memory_id, kind FROM message_redactions '
            'WHERE message_id = ? ORDER BY "start", "end", memory_id',
            (message_id,),
        ) as cur:
            return [Redaction(r[0], r[1], r[2], r[3], r[4]) for r in await cur.fetchall()]

    async def redactions_for_messages(self, message_ids: Sequence[str]) -> list[Redaction]:
        out: list[Redaction] = []
        for message_id in message_ids:
            out.extend(await self.redactions_for_message(message_id))
        return out

    async def remove_superseded_redactions(self, memory_id: str) -> int:
        """Drop the model-view redactions that hide `memory_id`'s span (it is active again)."""
        cur = await self._conn.execute(
            "DELETE FROM message_redactions WHERE memory_id = ? AND kind = 'superseded'",
            (memory_id,),
        )
        return cur.rowcount

    async def replace_redactions(self, message_id: str, rows: Sequence[Redaction]) -> None:
        await self._conn.execute(
            "DELETE FROM message_redactions WHERE message_id = ?", (message_id,)
        )
        for row in rows:
            await self.add_redaction(row)

    # ---------------------------------------------------------- messages and archives

    async def user_messages(self, user_id: str) -> list[tuple[str, str, str, int]]:
        """(message_id, role, body_json, created_at) of every message in the user's chats."""
        async with self._conn.execute(
            "SELECT m.message_id, m.role, m.body_json, m.created_at FROM messages m "
            "JOIN conversations c ON c.conversation_id = m.conversation_id "
            "WHERE c.user_id = ? ORDER BY m.created_at, m.rowid",
            (user_id,),
        ) as cur:
            return [(r[0], r[1], r[2], r[3]) for r in await cur.fetchall()]

    async def set_message_body(self, message_id: str, body_json: str) -> None:
        await self._conn.execute(
            "UPDATE messages SET body_json = ? WHERE message_id = ?", (body_json, message_id)
        )

    async def archived_briefs(self, user_id: str) -> list[tuple[str, str, str]]:
        """('errand'|'booking', id, brief_json) for every stored brief of the user."""
        out: list[tuple[str, str, str]] = []
        async with self._conn.execute(
            "SELECT e.errand_id, e.brief_json FROM errands e "
            "JOIN conversations c ON c.conversation_id = e.conversation_id WHERE c.user_id = ?",
            (user_id,),
        ) as cur:
            out += [("errand", r[0], r[1]) for r in await cur.fetchall()]
        async with self._conn.execute(
            "SELECT booking_id, origin_brief_json FROM known_bookings "
            "WHERE user_id = ? AND origin_brief_json IS NOT NULL",
            (user_id,),
        ) as cur:
            out += [("booking", r[0], r[1]) for r in await cur.fetchall()]
        return out

    async def set_archived_brief(self, kind: str, ident: str, brief_json: str) -> None:
        if kind == "errand":
            await self._conn.execute(
                "UPDATE errands SET brief_json = ? WHERE errand_id = ?", (brief_json, ident)
            )
        else:
            await self._conn.execute(
                "UPDATE known_bookings SET origin_brief_json = ? WHERE booking_id = ?",
                (brief_json, ident),
            )

    async def blank_client_responses(self, memory_id: str) -> int:
        """A stored answer to an earlier mutation can hold the value; blank it on forget."""
        cur = await self._conn.execute(
            "UPDATE client_requests SET response_json = '{\"memory_forgotten\":true}' "
            "WHERE response_json LIKE ?",
            (f'%"memory_id":"{memory_id}"%',),
        )
        return cur.rowcount

    # ---------------------------------------------------------------------------- epoch

    async def bump_epoch(self, user_id: str) -> int:
        await self._conn.execute(
            "UPDATE users SET memory_epoch = memory_epoch + 1 WHERE user_id = ?", (user_id,)
        )
        return await self.epoch(user_id)

    async def epoch(self, user_id: str) -> int:
        async with self._conn.execute(
            "SELECT memory_epoch FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0
