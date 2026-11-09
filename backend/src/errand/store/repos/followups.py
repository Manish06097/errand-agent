"""followup_jobs: one row per (booking, event kind), ever (architecture 14, 4.1).

M5 writes the first row a provider cancellation can leave, in the same transaction that
records the booking it found cancelled (`provider_cancel`, state `detected`). The unique
(booking_id, event_kind) index is what keeps M14's watcher from handling the same event
twice; the notify policy and the move to `notified` are M14's.
"""

from __future__ import annotations

import aiosqlite


class FollowupsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def detect(
        self, *, job_id: str, booking_id: str, event_kind: str, conversation_id: str, now_ms: int
    ) -> bool:
        """Insert a `detected` job unless one exists for this booking and kind."""
        cur = await self._conn.execute(
            "INSERT INTO followup_jobs (job_id, booking_id, event_kind, state, conversation_id, "
            "updated_at) VALUES (?, ?, ?, 'detected', ?, ?) "
            "ON CONFLICT(booking_id, event_kind) DO NOTHING",
            (job_id, booking_id, event_kind, conversation_id, now_ms),
        )
        return cur.rowcount == 1

    async def for_booking(self, booking_id: str) -> list[tuple[str, str, str]]:
        """(job_id, event_kind, state) rows of one booking."""
        async with self._conn.execute(
            "SELECT job_id, event_kind, state FROM followup_jobs WHERE booking_id = ? "
            "ORDER BY rowid",
            (booking_id,),
        ) as cur:
            return [(str(r[0]), str(r[1]), str(r[2])) for r in await cur.fetchall()]
