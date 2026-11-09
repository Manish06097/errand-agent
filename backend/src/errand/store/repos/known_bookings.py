"""known_bookings: the agent's mirror of bookings it verified (architecture 4.1).

It knows only what we booked and verified; the world has bookings we never made (the
seeded history, other channels). So duplicate and nearby checks read a fresh
`list_bookings`, never this table. It is what the planner's last-outcome line and the
claim guard read: a booking is here only after `get_booking_status` confirmed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

from errand.memory.briefjson import scrub_brief_json

_COLUMNS = (
    "booking_id, user_id, provider_id, service, slot_start, slot_end, status, last_seen_at, "
    "origin_conversation_id, origin_brief_json, source_intent_id"
)


@dataclass(frozen=True)
class KnownBooking:
    booking_id: str
    user_id: str
    provider_id: str
    service: str
    slot_start: int
    slot_end: int
    status: str
    last_seen_at: int
    origin_conversation_id: str | None
    origin_brief_json: str | None
    source_intent_id: str | None


def _row(row: Any) -> KnownBooking:
    return KnownBooking(*(row[i] for i in range(11)))


class KnownBookingsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def upsert(self, b: KnownBooking) -> None:
        # A stale checkpoint must never write a forgotten value back into agent.sqlite
        # (gate 2 decision 10): the brief goes in without fields whose memory is not
        # eligible. Done here, at the repo, so verify.py is untouched.
        origin = b.origin_brief_json
        if origin is not None:
            async with self._conn.execute(
                "SELECT memory_id FROM memories_eligible WHERE user_id = ?", (b.user_id,)
            ) as cur:
                eligible = {r[0] for r in await cur.fetchall()}
            origin, _ = scrub_brief_json(origin, eligible)
        await self._conn.execute(
            f"INSERT INTO known_bookings ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(booking_id) DO UPDATE SET status = excluded.status, "
            "slot_start = excluded.slot_start, slot_end = excluded.slot_end, "
            "last_seen_at = excluded.last_seen_at",
            (
                b.booking_id,
                b.user_id,
                b.provider_id,
                b.service,
                b.slot_start,
                b.slot_end,
                b.status,
                b.last_seen_at,
                b.origin_conversation_id,
                origin,
                b.source_intent_id,
            ),
        )

    async def for_user(self, user_id: str) -> list[KnownBooking]:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM known_bookings WHERE user_id = ? ORDER BY last_seen_at, rowid",
            (user_id,),
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def latest_for_conversation(self, conversation_id: str) -> KnownBooking | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM known_bookings WHERE origin_conversation_id = ? "
            "ORDER BY last_seen_at DESC, rowid DESC LIMIT 1",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None
