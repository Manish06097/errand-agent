from __future__ import annotations

from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True)
class User:
    user_id: str
    tz: str
    display_name: str


class UsersRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def upsert(self, user: User) -> None:
        await self._conn.execute(
            "INSERT INTO users (user_id, tz, display_name) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "tz = excluded.tz, display_name = excluded.display_name",
            (user.user_id, user.tz, user.display_name),
        )

    async def get(self, user_id: str) -> User | None:
        async with self._conn.execute(
            "SELECT user_id, tz, display_name FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return User(row["user_id"], row["tz"], row["display_name"]) if row else None
