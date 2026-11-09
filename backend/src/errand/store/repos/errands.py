"""errands: archived errands for the ledger (architecture 4.1).

The brief goes in through the memory scrub (gate 2 decision 10): a field whose memory is
no longer eligible is dropped, so a stale checkpoint can never write a forgotten value
back into agent.sqlite when an errand is archived.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from errand.memory.briefjson import scrub_brief_json


@dataclass(frozen=True)
class ArchivedErrand:
    errand_id: str
    conversation_id: str
    kind: str
    phase: str
    brief_json: str
    replaces_booking_id: str | None
    outcome: str | None
    created_at: int
    closed_at: int | None


class ErrandsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def archive(self, e: ArchivedErrand, user_id: str) -> None:
        async with self._conn.execute(
            "SELECT memory_id FROM memories_eligible WHERE user_id = ?", (user_id,)
        ) as cur:
            eligible = {r[0] for r in await cur.fetchall()}
        brief, _ = scrub_brief_json(e.brief_json, eligible)
        await self._conn.execute(
            "INSERT INTO errands (errand_id, conversation_id, kind, phase, brief_json, "
            "replaces_booking_id, outcome, created_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(errand_id) DO UPDATE SET phase = excluded.phase, "
            "brief_json = excluded.brief_json, outcome = excluded.outcome, "
            "closed_at = excluded.closed_at",
            (
                e.errand_id,
                e.conversation_id,
                e.kind,
                e.phase,
                brief,
                e.replaces_booking_id,
                e.outcome,
                e.created_at,
                e.closed_at,
            ),
        )

    async def for_user(self, user_id: str) -> list[ArchivedErrand]:
        async with self._conn.execute(
            "SELECT e.errand_id, e.conversation_id, e.kind, e.phase, e.brief_json, "
            "e.replaces_booking_id, e.outcome, e.created_at, e.closed_at FROM errands e "
            "JOIN conversations c ON c.conversation_id = e.conversation_id "
            "WHERE c.user_id = ? ORDER BY e.created_at, e.errand_id",
            (user_id,),
        ) as cur:
            return [ArchivedErrand(*(r[i] for i in range(9))) for r in await cur.fetchall()]
