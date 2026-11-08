"""reader_refusals: prompts the reader's model refused, so they are not sent again.

Same key as reader_cache (provider_id, sha256 of the raw reviews, reader_model) but a
separate table: a marker carries no output and is never a source of labels. Timeouts
and transport errors are not marked; they may be tried again in a later run.
"""

from __future__ import annotations

import aiosqlite


class ReaderRefusalsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def has(self, provider_id: str, reviews_sha256: str, reader_model: str) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM reader_refusals "
            "WHERE provider_id = ? AND reviews_sha256 = ? AND reader_model = ?",
            (provider_id, reviews_sha256, reader_model),
        ) as cur:
            return await cur.fetchone() is not None

    async def mark(self, provider_id: str, reviews_sha256: str, reader_model: str) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO reader_refusals "
            "(provider_id, reviews_sha256, reader_model) VALUES (?, ?, ?)",
            (provider_id, reviews_sha256, reader_model),
        )

    async def count(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM reader_refusals") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0
