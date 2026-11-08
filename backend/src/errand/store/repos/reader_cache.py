"""reader_cache: validated quarantined-reader output, keyed so a stale entry never hits.

The key is (provider_id, sha256 of the canonical raw reviews, reader_model). Raw, not
cleaned: two payloads that clean to the same text are still different reviews, and an
edited review must miss. `reader_model` carries the prompt and schema version, so a
prompt or enum change misses too. Only output that passed validation is written; a
failure is never cached.
"""

from __future__ import annotations

import aiosqlite


class ReaderCacheRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def get(self, provider_id: str, reviews_sha256: str, reader_model: str) -> str | None:
        async with self._conn.execute(
            "SELECT output_json FROM reader_cache "
            "WHERE provider_id = ? AND reviews_sha256 = ? AND reader_model = ?",
            (provider_id, reviews_sha256, reader_model),
        ) as cur:
            row = await cur.fetchone()
        return str(row["output_json"]) if row else None

    async def put(
        self, provider_id: str, reviews_sha256: str, reader_model: str, output_json: str
    ) -> None:
        # Same key, same reviews, same prompt: the first validated answer stands.
        await self._conn.execute(
            "INSERT OR IGNORE INTO reader_cache "
            "(provider_id, reviews_sha256, output_json, reader_model) VALUES (?, ?, ?, ?)",
            (provider_id, reviews_sha256, output_json, reader_model),
        )

    async def count(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM reader_cache") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0
