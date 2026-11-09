"""followup_jobs: one row per (booking, event kind), ever (architecture 14, 4.1).

M5 writes the first row a provider cancellation can leave, in the same transaction that
records the booking it found cancelled (`provider_cancel`, state `detected`). The unique
(booking_id, event_kind) index is what keeps M14's watcher from handling the same event
twice. N3 adds the reads and moves the watcher and the delivery need; the job id of a
provider cancel is `fj_{booking_id}_provider_cancel` everywhere, and `conversation_id` means
the follow-up conversation (null until delivery).
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

STATES = ("detected", "notified", "ignored", "done")


@dataclass(frozen=True)
class Job:
    job_id: str
    booking_id: str
    event_kind: str
    state: str
    conversation_id: str | None
    updated_at: int


def job_id_for(booking_id: str) -> str:
    return f"fj_{booking_id}_provider_cancel"


def conversation_id_for(job_id: str) -> str:
    """The follow-up conversation of a job, deterministic so delivery is idempotent."""
    return f"cf_{job_id}"


class FollowupsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def detect(
        self,
        *,
        job_id: str,
        booking_id: str,
        event_kind: str,
        conversation_id: str | None,
        now_ms: int,
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

    async def get(self, job_id: str) -> Job | None:
        async with self._conn.execute(
            "SELECT job_id, booking_id, event_kind, state, conversation_id, updated_at "
            "FROM followup_jobs WHERE job_id = ?",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
        return Job(*(row[i] for i in range(6))) if row else None

    async def in_state(self, *states: str) -> list[Job]:
        marks = ", ".join("?" * len(states))
        async with self._conn.execute(
            "SELECT job_id, booking_id, event_kind, state, conversation_id, updated_at "
            f"FROM followup_jobs WHERE state IN ({marks}) ORDER BY updated_at, rowid",
            states,
        ) as cur:
            return [Job(*(r[i] for i in range(6))) for r in await cur.fetchall()]

    async def move(
        self,
        job_id: str,
        *,
        frm: tuple[str, ...],
        to: str,
        now_ms: int,
        conversation_id: str | None = None,
    ) -> bool:
        """Conditional on the state it leaves, so two movers cannot both win."""
        marks = ", ".join("?" * len(frm))
        cur = await self._conn.execute(
            "UPDATE followup_jobs SET state = ?, updated_at = ?, "
            "conversation_id = COALESCE(?, conversation_id) "
            f"WHERE job_id = ? AND state IN ({marks})",
            (to, now_ms, conversation_id, job_id, *frm),
        )
        return cur.rowcount == 1
