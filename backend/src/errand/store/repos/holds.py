"""Holds the agent placed, keyed so a re-run of `prepare_proposal` reuses its hold (6.5).

`UNIQUE(errand_id, provider_id, slot_start, generation)`: a new generation is a new
hold for the same slot (a refresh, or the old one lapsed). `expires_at` is the world's
expiry, in epoch ms on the world clock; it is only ever compared with the world clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

_COLUMNS = (
    "hold_record_id, errand_id, provider_id, slot_start, generation, hold_id, "
    "quoted_price_inr, expires_at"
)


@dataclass(frozen=True)
class HoldRow:
    hold_record_id: str
    errand_id: str
    provider_id: str
    slot_start: int
    generation: int
    hold_id: str
    quoted_price_inr: int
    expires_at: int


def _row(row: Any) -> HoldRow:
    return HoldRow(*(row[i] for i in range(8)))


def hold_record_id_for(errand_id: str, provider_id: str, slot_start: int, generation: int) -> str:
    # Derived, never minted: a node re-run computes the same id for the same hold.
    return f"hr_{errand_id}_{provider_id}_{slot_start}_{generation}"


class HoldsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def latest(self, errand_id: str, provider_id: str, slot_start: int) -> HoldRow | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM holds WHERE errand_id = ? AND provider_id = ? "
            "AND slot_start = ? ORDER BY generation DESC LIMIT 1",
            (errand_id, provider_id, slot_start),
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def get_by_hold_id(self, hold_id: str) -> HoldRow | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM holds WHERE hold_id = ?", (hold_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def insert(self, row: HoldRow) -> HoldRow:
        """Insert, or return the row already there for the same key (a re-run)."""
        await self._conn.execute(
            f"INSERT INTO holds ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(errand_id, provider_id, slot_start, generation) DO NOTHING",
            (
                row.hold_record_id,
                row.errand_id,
                row.provider_id,
                row.slot_start,
                row.generation,
                row.hold_id,
                row.quoted_price_inr,
                row.expires_at,
            ),
        )
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM holds WHERE errand_id = ? AND provider_id = ? "
            "AND slot_start = ? AND generation = ?",
            (row.errand_id, row.provider_id, row.slot_start, row.generation),
        ) as cur:
            found = await cur.fetchone()
        assert found is not None
        return _row(found)
